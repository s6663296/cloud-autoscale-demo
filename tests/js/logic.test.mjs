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
  roadPaths,
} from "../../web/static/logic.js";

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

test("reportBody 逾時記為 10000 毫秒，延遲取整數", () => {
  const body = reportBody("id-1", {
    fixed: { outcome: "timeout", latencyMs: 50.2, instanceId: null },
    auto: { outcome: "ok", latencyMs: 411.6, instanceId: "00bf" },
  });
  assert.deepEqual(body, {
    order_id: "id-1",
    results: [
      { target: "fixed", outcome: "timeout", latency_ms: 10000, instance_id: null },
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

// --- 購物車 ----------------------------------------------------------------

import { cartLines, cartSummary, decorations, etaText, polylineAt, progressFromTrack } from "../../web/static/logic.js";

const MENU = [
  { id: "r1-m1", name: "紅燒牛肉麵", price: 180 },
  { id: "r1-m2", name: "清燉牛肉麵", price: 190 },
  { id: "r1-m3", name: "燙青菜", price: 50 },
];

test("cartSummary 計算件數與總額", () => {
  assert.deepEqual(cartSummary(MENU, {}), { count: 0, total: 0 });
  assert.deepEqual(cartSummary(MENU, { "r1-m1": 1, "r1-m2": 2, "r1-m3": 0 }), { count: 3, total: 560 });
});

test("cartLines 依菜單順序列出數量大於 0 的品項", () => {
  assert.deepEqual(cartLines(MENU, { "r1-m3": 1, "r1-m1": 2 }), [
    { id: "r1-m1", name: "紅燒牛肉麵", qty: 2, subtotal: 360 },
    { id: "r1-m3", name: "燙青菜", qty: 1, subtotal: 50 },
  ]);
});

// --- 追蹤進度 --------------------------------------------------------------

test("progressFromTrack 依伺服器回傳的階段計算進度", () => {
  const sched = { arrive_restaurant_s: 100, pickup_s: 300, deliver_s: 700 };
  assert.deepEqual(progressFromTrack("to_restaurant", 50, 650, sched), { step: 0, frac: 0.5 });
  assert.deepEqual(progressFromTrack("to_restaurant", 400, 650, sched), { step: 0, frac: 0.95 });
  assert.deepEqual(progressFromTrack("waiting", 200, 500, sched), { step: 1, frac: 0.5 });
  assert.deepEqual(progressFromTrack("to_customer", 500, 200, sched), { step: 2, frac: 0.5 });
  assert.deepEqual(progressFromTrack("delivered", 700, 0, sched), { step: 3, frac: 1 });
});

test("progressFromTrack 不需等待備餐時，等待段直接視為完成", () => {
  const sched = { arrive_restaurant_s: 300, pickup_s: 300, deliver_s: 700 };
  assert.deepEqual(progressFromTrack("waiting", 300, 400, sched), { step: 1, frac: 1 });
});

test("polylineAt 依路徑長度內插位置", () => {
  const pts = [[0, 0], [1, 0], [1, 1]];
  assert.deepEqual(polylineAt(pts, 0), [0, 0]);
  assert.deepEqual(polylineAt(pts, 0.25), [0.5, 0]);
  assert.deepEqual(polylineAt(pts, 0.75), [1, 0.5]);
  assert.deepEqual(polylineAt(pts, 1), [1, 1]);
  assert.deepEqual(polylineAt(pts, 2), [1, 1]);
  assert.deepEqual(polylineAt([[3, 4]], 0.5), [3, 4]);
  assert.deepEqual(polylineAt([[2, 2], [2, 2]], 0.5), [2, 2]);
});

test("etaText 顯示剩餘時間", () => {
  assert.equal(etaText(1380, 0), "23 分鐘");
  assert.equal(etaText(1380, 1361), "少於 5 分鐘");
  assert.equal(etaText(1380, 1380), "已送達");
  assert.equal(etaText(1381, 0), "24 分鐘");
});

// --- 地圖裝飾 --------------------------------------------------------------

test("decorations 對同一張地圖結果固定，且都在地圖範圍內", () => {
  const map = { size: 60 };
  const a = decorations(map);
  assert.deepEqual(a, decorations(map));
  assert.ok(a.parks.length >= 3);
  for (const p of a.parks) {
    assert.ok(p.x >= 0 && p.y >= 0 && p.x + p.w <= 59 && p.y + p.h <= 59);
  }
  assert.notDeepEqual(decorations({ size: 30 }), a);
});

import { outskirtPaths, riverLine, trackReportBody } from "../../web/static/logic.js";

test("trackReportBody 逾時記為 10000 毫秒", () => {
  assert.deepEqual(trackReportBody("auto", { outcome: "ok", latencyMs: 17.6, instanceId: "00bf" }), {
    target: "auto", outcome: "ok", latency_ms: 18, instance_id: "00bf",
  });
  assert.deepEqual(trackReportBody("fixed", { outcome: "timeout", latencyMs: 15003, instanceId: null }), {
    target: "fixed", outcome: "timeout", latency_ms: 10000, instance_id: null,
  });
});

test("outskirtPaths 只畫城市外圍，主幹道沿原行列延伸", () => {
  const map = { size: 3, closed: [], arterials: { rows: [1], cols: [] } };
  const paths = outskirtPaths(map, 1);
  // 城市內第 1 列是主幹道：只延伸左右兩段外圍
  assert.ok(paths.arterials.includes("M-1 1H0") && paths.arterials.includes("M2 1H3"));
  // 城市外的列整條畫
  assert.ok(paths.roads.includes("M-1 -1H3") && paths.roads.includes("M-1 3H3"));
  // 不會畫穿過城市內部
  assert.ok(!paths.roads.includes("M0 1H") && !paths.arterials.includes("M-1 1H3"));
});

test("riverLine 畫在兩列之間並延伸到外圍", () => {
  assert.deepEqual(riverLine({ size: 3, river: [1, 1, 0] }, 2), [[-2, 1.5], [0, 1.5], [1, 1.5], [2, 0.5], [4, 0.5]]);
  assert.deepEqual(riverLine({ size: 3, river: [] }, 2), []);
});
