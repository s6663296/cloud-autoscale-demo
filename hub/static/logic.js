// 前端的純邏輯：不碰 DOM，供 app.js / map.js / dashboard.js 使用，並以 node --test 測試。

export const TARGETS = ["fixed", "auto"];
export const TIMEOUT_LATENCY_MS = 15000;
export const PLAYBACK_MS = 30000;

const round1 = (n) => Math.round(n * 10) / 10;

// --- 下單與回報 ------------------------------------------------------------

export function makeOrderId(c = globalThis.crypto) {
  // randomUUID 只在安全環境（HTTPS、localhost）可用；以區網 IP 開啟時改用 getRandomValues
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const b = c.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = Array.from(b, (v) => v.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function buildOrderBody({ orderId, restaurantId, menu, qty, customer = {} }) {
  const field = (k) => (customer[k] || "").trim();
  return {
    order_id: orderId,
    restaurant_id: restaurantId,
    items: menu.filter((m) => (qty[m.id] || 0) > 0).map((m) => ({ item_id: m.id, qty: qty[m.id] })),
    customer: { name: field("name"), phone: field("phone"), address: field("address"), note: field("note") },
    seed: null,
  };
}

export function outcomeFromStatus(status) {
  if (status === 200) return "ok";
  if (status === 429) return "busy";
  return "error";
}

export function reportBody(orderId, results) {
  return {
    order_id: orderId,
    results: TARGETS.map((target) => {
      const r = results[target];
      return {
        target,
        outcome: r.outcome,
        latency_ms: r.outcome === "timeout" ? TIMEOUT_LATENCY_MS : Math.round(r.latencyMs),
        instance_id: r.instanceId ?? null,
      };
    }),
  };
}

// --- 外送員動畫 ------------------------------------------------------------

export function simSeconds(elapsedMs, deliverS, playMs = PLAYBACK_MS) {
  return Math.min(elapsedMs / playMs, 1) * deliverS;
}

function along(nodes, frac) {
  if (nodes.length === 1) return nodes[0];
  const pos = Math.max(0, Math.min(1, frac)) * (nodes.length - 1);
  const i = Math.min(Math.floor(pos), nodes.length - 2);
  const t = pos - i;
  const [ax, ay] = nodes[i];
  const [bx, by] = nodes[i + 1];
  return [ax + (bx - ax) * t, ay + (by - ay) * t];
}

/** 外送員在模擬第 t 秒的位置；路線上以等速前進。 */
export function riderPosition(route, schedule, t) {
  const { arrive_restaurant_s: arrive, pickup_s: pickup, deliver_s: deliver } = schedule;
  const toR = route.to_restaurant;
  const toC = route.to_customer;
  let point;
  let phase;
  if (t >= deliver) {
    point = toC[toC.length - 1];
    phase = "delivered";
  } else if (t < arrive) {
    point = along(toR, t / arrive);
    phase = "to_restaurant";
  } else if (t < pickup) {
    point = toR[toR.length - 1];
    phase = "waiting";
  } else {
    point = along(toC, (t - pickup) / (deliver - pickup));
    phase = "to_customer";
  }
  return { x: point[0], y: point[1], phase };
}

// --- 地圖 ------------------------------------------------------------------

/** 把格狀路網轉成三條 SVG path：一般道路、主幹道、封閉路段；連續路段合併成一筆。 */
export function roadPaths(map) {
  const { size } = map;
  const closed = new Set(map.closed.map(([[x1, y1], [x2, y2]]) => `${x1},${y1},${x2},${y2}`));
  const rows = new Set(map.arterials.rows);
  const cols = new Set(map.arterials.cols);
  const out = { roads: "", arterials: "", closed: "" };

  const scan = (horizontal) => {
    for (let line = 0; line < size; line++) {
      const arterial = horizontal ? rows.has(line) : cols.has(line);
      let runStart = 0;
      let runClosed = null;
      const flush = (end) => {
        if (runClosed === null) return;
        const d = horizontal ? `M${runStart} ${line}H${end}` : `M${line} ${runStart}V${end}`;
        if (runClosed) out.closed += d;
        else if (arterial) out.arterials += d;
        else out.roads += d;
      };
      for (let i = 0; i < size - 1; i++) {
        const key = horizontal ? `${i},${line},${i + 1},${line}` : `${line},${i},${line},${i + 1}`;
        const isClosed = closed.has(key);
        if (isClosed !== runClosed) {
          flush(i);
          runStart = i;
          runClosed = isClosed;
        }
      }
      flush(size - 1);
    }
  };
  scan(true);
  scan(false);
  return out;
}

/** 涵蓋所有點的正方形 viewBox [x, y, w, h]。 */
export function fitView(points, size, { minSpan = 16, pad = 2 } = {}) {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const span = Math.max(maxX - minX + 2 * pad, maxY - minY + 2 * pad, minSpan);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  return [cx - span / 2, cy - span / 2, span, span];
}

// --- 儀表板 ----------------------------------------------------------------

/** 折線 path；值為 null 時斷開。 */
export function chartPath(values, x, y) {
  let d = "";
  let penDown = false;
  values.forEach((v, i) => {
    if (v === null || v === undefined) {
      penDown = false;
      return;
    }
    d += `${penDown ? "L" : "M"}${round1(x(i))} ${round1(y(v))}`;
    penDown = true;
  });
  return d;
}

export function formatPct(rate) {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`;
}

export function formatMs(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}
