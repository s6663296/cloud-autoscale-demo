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

export function reportBody(orderId, results, client = null) {
  return withClient({ order_id: orderId, results: TARGETS.map((target) => trackReportBody(target, results[target])) }, client);
}

/** 回報 web 時附上手機的暱稱與隨機編號；沒有時不帶這個欄位。 */
export function withClient(body, client) {
  return client ? { ...body, client } : body;
}

// --- 暱稱 ------------------------------------------------------------------

const NICKNAME_MAX = 12;
export const GUEST_NAME = "訪客";

/** 與 web 端 clean_nickname 相同的規則：去控制字元與頭尾空白、截 12 個字（以 Unicode 字元計），空的當「訪客」。 */
export function normalizeNickname(s) {
  const text = String(s ?? "").replace(/\p{C}/gu, "").trim();
  return Array.from(text).slice(0, NICKNAME_MAX).join("") || GUEST_NAME;
}

/** 讀取手機的身分；localStorage 不能用（無痕模式等）時只在這次開啟期間有效。 */
export function loadIdentity(storage, newId = makeOrderId) {
  let clientId = null;
  let nickname = null;
  try {
    clientId = storage.getItem("client_id");
    nickname = storage.getItem("nickname");
  } catch {
    // 存不了就當第一次來
  }
  if (!clientId) {
    clientId = newId();
    try {
      storage.setItem("client_id", clientId);
    } catch {
      // 下次打開會再產生一個新的
    }
  }
  return { clientId, nickname };
}

export function saveNickname(storage, name) {
  const clean = normalizeNickname(name);
  try {
    storage.setItem("nickname", clean);
  } catch {
    // 存不了只影響下次打開要不要再問
  }
  return clean;
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

// --- 請求流向圖 -------------------------------------------------------------

/** 中間一欄的同學：兩邊 machines[].phones 與 requests 的聯集，依暱稱排序；超過上限時一定保留自己。 */
export function graphPhones(targets, myTag, max = 20) {
  const byTag = new Map();
  for (const target of TARGETS) {
    for (const m of targets[target]?.machines ?? []) for (const p of m.phones ?? []) byTag.set(p.tag, p.name);
    for (const r of targets[target]?.requests ?? []) byTag.set(r.tag, r.name);
  }
  const order = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.tag < b.tag ? -1 : 1);
  let list = [...byTag].map(([tag, name]) => ({ tag, name, you: myTag !== null && tag === myTag })).sort(order);
  if (list.length > max) {
    const mine = list.filter((p) => p.you);
    list = [...mine, ...list.filter((p) => !p.you).slice(0, max - mine.length)].sort(order);
  }
  return list;
}

/** n 個節點在 top～bottom 之間平均分配；只有一個時放中間。 */
export function columnPositions(n, top, bottom) {
  if (n <= 0) return [];
  if (n === 1) return [(top + bottom) / 2];
  return Array.from({ length: n }, (_, i) => top + (i * (bottom - top)) / (n - 1));
}

/** 模擬顧客的線寬：依筆數開根號，最粗 6；沒有筆數不畫。 */
export function edgeWidth(n, max) {
  if (!n || n <= 0) return 0;
  return round1(1 + 5 * Math.sqrt(n / Math.max(max, 1)));
}

/** 模擬顧客每台機器每秒飛幾顆小點：約每 20 筆一顆，最多 5 顆。 */
export function loadtestDotCount(requests) {
  if (!requests || requests <= 0) return 0;
  return Math.min(5, Math.ceil(requests / 10 / 20));
}

const MACHINE_BADGE = { new: "新開機", idle: "閒置" };

/** 機器節點的樣式與標籤。unresponsive：這一邊有失敗卻沒有機器在接單，閒置的機器改標「沒有回應」。 */
export function machineNodeState(m, unresponsive) {
  if (unresponsive && m.status === "idle") return { tone: "down", badge: "沒有回應" };
  return { tone: m.status, badge: MACHINE_BADGE[m.status] ?? null };
}

const hasFailures = (failures) => Object.values(failures ?? {}).some((n) => n > 0);

export function sideUnresponsive(machines, failures) {
  return hasFailures(failures) && !machines.some((m) => m.status === "new" || m.status === "active");
}

export function unservedText(name, failures) {
  return hasFailures(failures) ? `${name}沒接到的請求：${failureText(failures)}` : "";
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
