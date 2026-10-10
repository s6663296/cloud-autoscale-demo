// 前端的純邏輯：不碰 DOM，供 app.js / map.js / dashboard.js 使用，並以 node --test 測試。

export const TARGETS = ["fixed", "auto"];
export const TIMEOUT_LATENCY_MS = 10000;

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

export function trackReportBody(target, r) {
  return {
    target,
    outcome: r.outcome,
    latency_ms: r.outcome === "timeout" ? TIMEOUT_LATENCY_MS : Math.round(r.latencyMs),
    instance_id: r.instanceId ?? null,
  };
}

export function reportBody(orderId, results) {
  return { order_id: orderId, results: TARGETS.map((target) => trackReportBody(target, results[target])) };
}

// --- 外送員動畫 ------------------------------------------------------------

/** 依路徑長度取得比例 frac（0–1）處的位置，用於在兩次追蹤回應之間平滑移動。 */
export function polylineAt(points, frac) {
  if (points.length === 1) return points[0];
  const lengths = points.slice(1).map((p, i) => Math.hypot(p[0] - points[i][0], p[1] - points[i][1]));
  const total = lengths.reduce((a, b) => a + b, 0);
  if (total === 0 || frac <= 0) return points[0];
  let remaining = Math.min(frac, 1) * total;
  for (let i = 0; i < lengths.length; i++) {
    if (remaining <= lengths[i] || i === lengths.length - 1) {
      const t = lengths[i] === 0 ? 0 : Math.min(1, remaining / lengths[i]);
      const [ax, ay] = points[i];
      const [bx, by] = points[i + 1];
      return [ax + (bx - ax) * t, ay + (by - ay) * t];
    }
    remaining -= lengths[i];
  }
  return points[points.length - 1];
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

/**
 * 河流折線：後端的 river[x] = r 表示第 x 行的河道在第 r 與 r+1 列之間（那裡的路段已封閉，主幹道是橋），
 * 畫在兩列中間，兩端水平延伸 margin 格到外圍。
 */
export function riverLine(map, margin) {
  const rows = map.river || [];
  if (!rows.length) return [];
  const pts = rows.map((r, x) => [x, r + 0.5]);
  return [[-margin, pts[0][1]], ...pts, [map.size - 1 + margin, pts[pts.length - 1][1]]];
}

/**
 * 城市外圍的裝飾道路（向外延伸 margin 格），讓視野超出城市時仍看得到街道而不是空白。
 * 只畫城市範圍外的部分，不會蓋掉城市內的封閉路段；主幹道沿原本的行列延伸出去。
 */
export function outskirtPaths(map, margin) {
  const { size } = map;
  const lo = -margin;
  const hi = size - 1 + margin;
  const rows = new Set(map.arterials.rows);
  const cols = new Set(map.arterials.cols);
  const out = { roads: "", arterials: "" };
  for (let line = lo; line <= hi; line++) {
    const inside = line >= 0 && line < size;
    // 城市內的行列只補左右（上下）兩段外圍；城市外的行列整條畫
    const spans = inside ? [[lo, 0], [size - 1, hi]] : [[lo, hi]];
    for (const [a, b] of spans) {
      const h = `M${a} ${line}H${b}`;
      const v = `M${line} ${a}V${b}`;
      if (inside && rows.has(line)) out.arterials += h;
      else out.roads += h;
      if (inside && cols.has(line)) out.arterials += v;
      else out.roads += v;
    }
  }
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

/** 延遲：逾時一律記為 TIMEOUT_LATENCY_MS，全部逾時時代表至少等了這麼久，不是剛好這麼久。 */
export function formatLatency(ms) {
  if (ms !== null && ms !== undefined && ms >= TIMEOUT_LATENCY_MS) return `≥${TIMEOUT_LATENCY_MS / 1000} s`;
  return formatMs(ms);
}

const FAILURE_NAMES = [["timeout", "逾時"], ["busy", "忙碌"], ["error", "錯誤"]];

/** 失敗次數：只列出有發生的種類，例如「逾時 173 · 錯誤 2」；全部為 0 時顯示「無」。 */
export function failureText(failures) {
  const parts = FAILURE_NAMES.filter(([key]) => failures[key] > 0).map(([key, name]) => `${name} ${failures[key]}`);
  return parts.length ? parts.join(" · ") : "無";
}

/** 卡片的平均回應時間文字與是否用小字：有請求卻沒有延遲（全是忙碌或錯誤）時明講，不要顯示成沒有資料。 */
export function latencyText(t) {
  if (t.avg_ms !== null && t.avg_ms !== undefined) return [formatLatency(t.avg_ms), false];
  return t.rps > 0 ? ["全部失敗", true] : ["—", false];
}

// --- 購物車 ----------------------------------------------------------------

export function cartLines(menu, qty) {
  return menu
    .filter((m) => (qty[m.id] || 0) > 0)
    .map((m) => ({ id: m.id, name: m.name, qty: qty[m.id], subtotal: m.price * qty[m.id] }));
}

export function cartSummary(menu, qty) {
  const lines = cartLines(menu, qty);
  return {
    count: lines.reduce((n, l) => n + l.qty, 0),
    total: lines.reduce((n, l) => n + l.subtotal, 0),
  };
}

// --- 追蹤進度 --------------------------------------------------------------

/**
 * 依追蹤回應計算四段進度：0 前往店家、1 備餐中、2 外送中、3 已送達；frac 為目前這段的完成比例。
 * 階段以伺服器為準；段內比例參考派單時的行程估算（改道後可能與原估算不同，故前往店家最多顯示 95%）。
 */
export function progressFromTrack(phase, simS, etaS, schedule) {
  const clamp = (v, hi = 1) => Math.min(hi, Math.max(0, v));
  const { arrive_restaurant_s: arrive, pickup_s: pickup } = schedule;
  if (phase === "delivered") return { step: 3, frac: 1 };
  if (phase === "to_customer") {
    const done = simS - pickup;
    return { step: 2, frac: done + etaS > 0 ? clamp(done / (done + etaS)) : 0 };
  }
  if (phase === "waiting") return { step: 1, frac: pickup > arrive ? clamp((simS - arrive) / (pickup - arrive)) : 1 };
  return { step: 0, frac: arrive > 0 ? clamp(simS / arrive, 0.95) : 0 };
}

export function etaText(deliverS, t) {
  const remaining = deliverS - t;
  if (remaining <= 0) return "已送達";
  if (remaining < 300) return "少於 5 分鐘";
  return `${Math.ceil(remaining / 60)} 分鐘`;
}

// --- 地圖裝飾 --------------------------------------------------------------

function seeded(seed) {
  // mulberry32：每支手機畫出相同的綠地與河流
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 純裝飾的綠地與河流，畫在道路下方，不影響路網。 */
export function decorations(map) {
  const { size } = map;
  const rand = seeded(size * 7919 + 17);
  const span = (lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));
  const parks = [];
  const count = Math.max(3, Math.round(size / 12));
  for (let i = 0; i < count; i++) {
    const w = span(2, Math.max(2, Math.round(size / 12)));
    const h = span(2, Math.max(2, Math.round(size / 12)));
    parks.push({ x: span(0, size - 1 - w), y: span(0, size - 1 - h), w, h });
  }
  return { parks };
}
