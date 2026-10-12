import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildOrderBody,
  chartPath,
  columnPositions,
  edgeWidth,
  graphPhones,
  loadtestDotCount,
  machineNodeState,
  sideUnresponsive,
  unservedText,
  countAxisMax,
  countTicks,
  instancesLabel,
  loadIdentity,
  normalizeNickname,
  saveNickname,
  withClient,
  stepPath,
  fitView,
  failureText,
  formatLatency,
  formatMs,
  latencyText,
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

test("stepPath 畫階梯線、遇到 null 斷開", () => {
  const d = stepPath([1, 1, 3, null, 2], (i) => i * 10, (v) => v);
  assert.equal(d, "M0 1H10V1H20V3M40 2");
  assert.equal(stepPath([null, null], (i) => i, (v) => v), "");
});

test("countAxisMax 取上限與資料最大值，至少為 1", () => {
  assert.equal(countAxisMax([1, 7], 10), 10);
  assert.equal(countAxisMax([12], 10), 12);
  assert.equal(countAxisMax([], 0), 1);
});

test("countTicks 只標整數且不重複", () => {
  assert.deepEqual(countTicks(10), [0, 5, 10]);
  assert.deepEqual(countTicks(7), [0, 4, 7]);
  assert.deepEqual(countTicks(1), [0, 1]);
});

test("instancesLabel 區分沒有資料與全部失敗", () => {
  assert.equal(instancesLabel({ instances: 7, success_rate: 1 }), "7 台");
  assert.equal(instancesLabel({ instances: 0, success_rate: null }), "0 台");
  assert.equal(instancesLabel({ instances: null, success_rate: 0 }), "全部失敗，拿不到機器編號");
  assert.equal(instancesLabel({ instances: null }), "—");
  assert.equal(instancesLabel({}), "—");
});

test("formatPct 與 formatMs", () => {
  assert.equal(formatPct(null), "—");
  assert.equal(formatPct(0.134), "13%");
  assert.equal(formatPct(1), "100%");
  assert.equal(formatMs(null), "—");
  assert.equal(formatMs(412.4), "412 ms");
  assert.equal(formatMs(15000), "15.0 s");
});

test("formatLatency 逾時上限顯示為至少 10 秒", () => {
  assert.equal(formatLatency(null), "—");
  assert.equal(formatLatency(412.4), "412 ms");
  assert.equal(formatLatency(9999), "10.0 s");
  assert.equal(formatLatency(10000), "≥10 s");
});

test("latencyText 分辨沒有流量與全部失敗", () => {
  assert.deepEqual(latencyText({ rps: 0, avg_ms: null }), ["—", false]);
  assert.deepEqual(latencyText({ rps: 8.1, avg_ms: null }), ["全部失敗", true]);
  assert.deepEqual(latencyText({ rps: 84, avg_ms: 159.4 }), ["159 ms", false]);
  assert.deepEqual(latencyText({ rps: 8, avg_ms: 10000 }), ["≥10 s", false]);
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

test("failureText 只列出有發生的失敗種類", () => {
  assert.equal(failureText({ timeout: 0, busy: 0, error: 0 }), "無");
  assert.equal(failureText({ timeout: 173, busy: 0, error: 0 }), "逾時 173");
  assert.equal(failureText({ timeout: 173, busy: 5, error: 2 }), "逾時 173 · 忙碌 5 · 錯誤 2");
});

test("normalizeNickname 去控制字元與空白、截 12 字、空的變訪客", () => {
  assert.equal(normalizeNickname("  小明 "), "小明");
  assert.equal(normalizeNickname(""), "訪客");
  assert.equal(normalizeNickname("a\u0000b​c"), "abc");
  assert.equal(normalizeNickname("一二三四五六七八九十甲乙丙"), "一二三四五六七八九十甲乙");
  assert.equal(normalizeNickname("😀😀😀😀😀😀😀😀😀😀😀😀😀"), "😀😀😀😀😀😀😀😀😀😀😀😀");
});

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, data };
}

test("loadIdentity 第一次產生編號並記住，之後沿用", () => {
  const storage = memoryStorage();
  const first = loadIdentity(storage, () => "id-1");
  assert.deepEqual(first, { clientId: "id-1", nickname: null });
  assert.equal(storage.data.client_id, "id-1");
  storage.setItem("nickname", "小明");
  assert.deepEqual(loadIdentity(storage, () => "id-2"), { clientId: "id-1", nickname: "小明" });
});

test("localStorage 不能用時仍然可以運作", () => {
  const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(loadIdentity(broken, () => "id-3"), { clientId: "id-3", nickname: null });
  assert.deepEqual(loadIdentity(null, () => "id-4"), { clientId: "id-4", nickname: null });
  assert.equal(saveNickname(broken, "  阿華 "), "阿華");
  assert.equal(saveNickname(null, ""), "訪客");
});

test("withClient 與 reportBody 帶上 client", () => {
  const client = { id: "c1", name: "小明" };
  assert.deepEqual(withClient({ a: 1 }, client), { a: 1, client });
  assert.deepEqual(withClient({ a: 1 }, null), { a: 1 });
  const results = {
    fixed: { outcome: "timeout", latencyMs: 10000, instanceId: null },
    auto: { outcome: "ok", latencyMs: 412.4, instanceId: "i1" },
  };
  assert.equal(reportBody("o1", results, client).client, client);
  assert.equal("client" in reportBody("o1", results), false);
});

const TARGETS_SNAP = {
  fixed: {
    machines: [{ id: "77e0", status: "active", phones: [{ name: "阿華", tag: "aaaaaaaa" }], phone_count: 1, loadtest_requests: 41 }],
    requests: [{ tag: "cccccccc", name: "小明", machine: null, outcome: "timeout" }],
  },
  auto: {
    machines: [{ id: "3f2a", status: "active", phones: [{ name: "阿華", tag: "aaaaaaaa" }, { name: "訪客", tag: "bbbbbbbb" }], phone_count: 2, loadtest_requests: 312 }],
    requests: [],
  },
};

test("graphPhones 合併兩邊、依暱稱排序、標出自己", () => {
  assert.deepEqual(graphPhones(TARGETS_SNAP, "cccccccc"), [
    { tag: "cccccccc", name: "小明", you: true },
    { tag: "bbbbbbbb", name: "訪客", you: false },
    { tag: "aaaaaaaa", name: "阿華", you: false },
  ]);
});

test("graphPhones 超過上限時一定保留自己", () => {
  const phones = Array.from({ length: 30 }, (_, i) => ({ name: `同學${String(i).padStart(2, "0")}`, tag: `t${i}` }));
  const snap = { fixed: { machines: [{ id: "x", status: "active", phones, phone_count: 30, loadtest_requests: 0 }], requests: [] },
                 auto: { machines: [], requests: [] } };
  const list = graphPhones(snap, "t29", 20);
  assert.equal(list.length, 20);
  assert.equal(list.some((p) => p.tag === "t29" && p.you), true);
});

test("columnPositions 平均分配", () => {
  assert.deepEqual(columnPositions(0, 0, 100), []);
  assert.deepEqual(columnPositions(1, 0, 100), [50]);
  assert.deepEqual(columnPositions(3, 0, 100), [0, 50, 100]);
});

test("edgeWidth 依筆數開根號，沒有筆數為 0", () => {
  assert.equal(edgeWidth(0, 10), 0);
  assert.equal(edgeWidth(null, 10), 0);
  assert.equal(edgeWidth(10, 10), 6);
  assert.equal(edgeWidth(1, 100), 1.5);
});

test("loadtestDotCount 約每 20 筆一顆、最多 5 顆", () => {
  assert.equal(loadtestDotCount(0), 0);
  assert.equal(loadtestDotCount(null), 0);
  assert.equal(loadtestDotCount(5), 1);
  assert.equal(loadtestDotCount(312), 2);
  assert.equal(loadtestDotCount(5000), 5);
});

test("machineNodeState：新開機、閒置、沒有回應", () => {
  assert.deepEqual(machineNodeState({ status: "active" }, false), { tone: "active", badge: null });
  assert.deepEqual(machineNodeState({ status: "new" }, false), { tone: "new", badge: "新開機" });
  assert.deepEqual(machineNodeState({ status: "idle" }, false), { tone: "idle", badge: "閒置" });
  assert.deepEqual(machineNodeState({ status: "idle" }, true), { tone: "down", badge: "沒有回應" });
});

test("sideUnresponsive：有失敗卻沒有任何機器在接單", () => {
  assert.equal(sideUnresponsive([{ status: "idle" }], { timeout: 5, busy: 0, error: 0 }), true);
  assert.equal(sideUnresponsive([{ status: "idle" }], { timeout: 0, busy: 0, error: 0 }), false);
  assert.equal(sideUnresponsive([{ status: "active" }], { timeout: 5, busy: 0, error: 0 }), false);
  assert.equal(sideUnresponsive([], { timeout: 1, busy: 0, error: 0 }), true);
});

test("unservedText 只在有失敗時顯示", () => {
  assert.equal(unservedText("固定版", { timeout: 520, busy: 0, error: 2 }), "固定版沒接到的請求：逾時 520 · 錯誤 2");
  assert.equal(unservedText("擴展版", { timeout: 0, busy: 0, error: 0 }), "");
});
