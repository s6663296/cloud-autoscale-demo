import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildOrderBody,
  chartPath,
  fitView,
  formatMs,
  formatPct,
  makeOrderId,
  outcomeFromStatus,
  reportBody,
  riderPosition,
  roadPaths,
  simSeconds,
} from "../../hub/static/logic.js";

// --- 外送員位置 ------------------------------------------------------------

const route = {
  to_restaurant: [[0, 0], [1, 0], [2, 0]],
  to_customer: [[2, 0], [2, 1], [2, 2], [2, 3], [2, 4]],
};
const schedule = { arrive_restaurant_s: 100, pickup_s: 300, deliver_s: 700 };

test("riderPosition 依序經過店家與顧客", () => {
  assert.deepEqual(riderPosition(route, schedule, 0), { x: 0, y: 0, phase: "to_restaurant" });
  assert.deepEqual(riderPosition(route, schedule, 50), { x: 1, y: 0, phase: "to_restaurant" });
  assert.deepEqual(riderPosition(route, schedule, 100), { x: 2, y: 0, phase: "waiting" });
  assert.deepEqual(riderPosition(route, schedule, 200), { x: 2, y: 0, phase: "waiting" });
  assert.deepEqual(riderPosition(route, schedule, 500), { x: 2, y: 2, phase: "to_customer" });
  assert.deepEqual(riderPosition(route, schedule, 700), { x: 2, y: 4, phase: "delivered" });
  assert.deepEqual(riderPosition(route, schedule, 9999), { x: 2, y: 4, phase: "delivered" });
});

test("riderPosition 在路段中間做線性內插", () => {
  const p = riderPosition(route, schedule, 25);
  assert.equal(p.x, 0.5);
  assert.equal(p.y, 0);
});

test("riderPosition 外送員就在店家、不需等待", () => {
  const r = { to_restaurant: [[2, 0]], to_customer: [[2, 0], [2, 1]] };
  const s = { arrive_restaurant_s: 0, pickup_s: 0, deliver_s: 60 };
  assert.deepEqual(riderPosition(r, s, 0), { x: 2, y: 0, phase: "to_customer" });
  assert.deepEqual(riderPosition(r, s, 30), { x: 2, y: 0.5, phase: "to_customer" });
});

test("simSeconds 把整趟壓縮成 30 秒播放", () => {
  assert.equal(simSeconds(0, 1200), 0);
  assert.equal(simSeconds(15000, 1200), 600);
  assert.equal(simSeconds(30000, 1200), 1200);
  assert.equal(simSeconds(45000, 1200), 1200);
});

// --- 地圖 ------------------------------------------------------------------

test("roadPaths 合併連續路段，略過封閉路段，主幹道另成一條", () => {
  const map = {
    size: 3,
    closed: [[[1, 0], [2, 0]], [[0, 1], [0, 2]]],
    arterials: { rows: [2], cols: [] },
  };
  const paths = roadPaths(map);
  assert.equal(paths.roads, "M0 0H1M0 1H2M0 0V1M1 0V2M2 0V2");
  assert.equal(paths.arterials, "M0 2H2");
  assert.equal(paths.closed, "M1 0H2M0 1V2");
});

test("fitView 涵蓋所有點並保留邊界與最小範圍", () => {
  assert.deepEqual(fitView([[10, 10], [12, 11]], 60, { minSpan: 16, pad: 2 }), [3, 2.5, 16, 16]);
  const [x, y, w, h] = fitView([[0, 0], [59, 59]], 60, { minSpan: 16, pad: 2 });
  assert.ok(x <= -1 && y <= -1 && x + w >= 60 && y + h >= 60);
  assert.equal(w, h);
});

// --- 下單與回報 ------------------------------------------------------------

test("buildOrderBody 只放數量大於 0 的品項並修剪顧客欄位", () => {
  const body = buildOrderBody({
    orderId: "id-1",
    restaurantId: "r1",
    menu: [{ id: "r1-m1" }, { id: "r1-m2" }, { id: "r1-m3" }],
    qty: { "r1-m3": 2, "r1-m1": 1, "r1-m2": 0 },
    customer: { name: " 小明 ", address: "台北市 " },
  });
  assert.deepEqual(body, {
    order_id: "id-1",
    restaurant_id: "r1",
    items: [{ item_id: "r1-m1", qty: 1 }, { item_id: "r1-m3", qty: 2 }],
    customer: { name: "小明", phone: "", address: "台北市", note: "" },
    seed: null,
  });
});

test("outcomeFromStatus 依 README 分類", () => {
  assert.equal(outcomeFromStatus(200), "ok");
  assert.equal(outcomeFromStatus(429), "busy");
  assert.equal(outcomeFromStatus(500), "error");
  assert.equal(outcomeFromStatus(422), "error");
});

test("reportBody 逾時記為 15000 毫秒，延遲取整數", () => {
  const body = reportBody("id-1", {
    fixed: { outcome: "timeout", latencyMs: 50.2, instanceId: null },
    auto: { outcome: "ok", latencyMs: 411.6, instanceId: "00bf" },
  });
  assert.deepEqual(body, {
    order_id: "id-1",
    results: [
      { target: "fixed", outcome: "timeout", latency_ms: 15000, instance_id: null },
      { target: "auto", outcome: "ok", latency_ms: 412, instance_id: "00bf" },
    ],
  });
});

test("makeOrderId 在非安全環境（無 randomUUID）仍產生 UUID v4", () => {
  const fake = { getRandomValues: (a) => a.fill(0xab) };
  const id = makeOrderId(fake);
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(makeOrderId({ randomUUID: () => "native" }), "native");
});

// --- 儀表板 ----------------------------------------------------------------

test("chartPath 遇到 null 斷開線段", () => {
  const values = [1, null, 0.5, 0.6];
  const d = chartPath(values, (i) => i * 10, (v) => 100 - v * 100);
  assert.equal(d, "M0 0M20 50L30 40");
});

test("formatPct 與 formatMs", () => {
  assert.equal(formatPct(null), "—");
  assert.equal(formatPct(0.134), "13%");
  assert.equal(formatPct(1), "100%");
  assert.equal(formatMs(null), "—");
  assert.equal(formatMs(412.4), "412 ms");
  assert.equal(formatMs(15000), "15.0 s");
});
