// 點餐流程：首頁 → 店家 → 結帳 → 追蹤。
// 下單時平行呼叫兩個派單服務，各自逾時；兩邊結束後回報 web，才允許再點一單。
// 派單成功後，每個版本各自向自己的後端追蹤外送員位置，直到送達。

import { initDashboard } from "./dashboard.js";
import {
  buildOrderBody, cartLines, cartSummary, etaText, makeOrderId,
  outcomeFromStatus, progressFromTrack, reportBody, TARGETS, trackReportBody,
} from "./logic.js";
import { CityMapView } from "./map.js";

const MAX_QTY = 9;
const LOOKS = {
  r1: { emoji: "🍜", category: "牛肉麵・麵食", tint: ["#ffe0b2", "#ffab91"] },
  r2: { emoji: "🍱", category: "便當・台式", tint: ["#fff3c4", "#ffd180"] },
  r3: { emoji: "🍗", category: "鹹酥雞・炸物", tint: ["#ffe6d5", "#ffb74d"] },
  r4: { emoji: "🥪", category: "早午餐", tint: ["#e0f7fa", "#80deea"] },
  r5: { emoji: "🍝", category: "義式料理", tint: ["#fce4ec", "#f48fb1"] },
  r6: { emoji: "🧋", category: "飲料・手搖", tint: ["#efebe9", "#bcaaa4"] },
};
const DEFAULT_LOOK = { emoji: "🍽️", category: "餐廳", tint: ["#eeeeee", "#cccccc"] };
const TRACK_INFO = {
  fixed: { title: "固定容量版", tag: "固定 1 台" },
  auto: { title: "自動擴展版", tag: "自動擴展" },
};
const STEP_TEXT = [
  "外送夥伴正在前往餐廳取餐",
  "餐廳正在準備你的餐點",
  "外送夥伴正在前往你的位置，再等等！",
  "餐點已送達，祝你用餐愉快！",
];

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const look = (r) => LOOKS[r.id] || DEFAULT_LOOK;

const state = {
  config: null,
  map: null,
  restaurant: null,
  qty: {},
  busy: false,
  screen: "home",
  tracks: {},
};

function html(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} HTTP ${res.status}`);
  return res.json();
}

// --- 畫面切換（支援手機返回鍵）-------------------------------------------

function show(name, { push = true } = {}) {
  // 離開追蹤頁（再點一單或按返回）就放棄原本的訂單，不再追蹤，避免現場連續下單讓追蹤請求越積越多
  if (state.screen === "tracking" && name !== "tracking") stopTracks();
  state.screen = name;
  document.body.dataset.screen = name;
  for (const screen of document.querySelectorAll(".screen")) screen.hidden = screen.id !== `screen-${name}`;
  $("#back").hidden = name === "home" || (name === "tracking" && state.busy);
  window.scrollTo(0, 0);
  if (push) history.pushState({ screen: name }, "");
}

window.addEventListener("popstate", (e) => {
  if (state.busy) {
    // 派單進行中留在追蹤頁
    history.pushState({ screen: state.screen }, "");
    return;
  }
  const target = e.state?.screen || "home";
  if ((target === "restaurant" || target === "checkout" || target === "tracking") && !state.restaurant) {
    show("home", { push: false });
    return;
  }
  if (target === "restaurant") renderMenu();
  if (target === "checkout") renderCheckout();
  show(target, { push: false });
});

// --- 首頁 ------------------------------------------------------------------

function renderRestaurants() {
  $("#restaurants").replaceChildren(...state.map.restaurants.map((r) => {
    const l = look(r);
    const preps = r.menu.map((m) => m.prep_min);
    const card = html("button", undefined, "restaurant-card");
    card.type = "button";
    const thumb = html("div", l.emoji, "thumb");
    thumb.style.background = `linear-gradient(135deg, ${l.tint[0]}, ${l.tint[1]})`;
    const body = html("div", undefined, "body");
    body.append(
      html("strong", r.name),
      html("div", `${l.category} · 備餐 ${Math.min(...preps)}–${Math.max(...preps)} 分鐘`, "meta"),
      html("span", "免外送費", "chip"),
    );
    card.append(thumb, body);
    card.addEventListener("click", () => openRestaurant(r));
    return card;
  }));
}

// --- 店家頁 ----------------------------------------------------------------

function openRestaurant(r) {
  const current = state.restaurant;
  if (current && current.id !== r.id) {
    if (cartSummary(current.menu, state.qty).count > 0
      && !confirm(`購物車裡有「${current.name}」的餐點，要清空並改點「${r.name}」嗎？`)) return;
    state.qty = {};
  }
  state.restaurant = r;
  renderMenu();
  show("restaurant");
}

function renderMenu() {
  const r = state.restaurant;
  const l = look(r);
  const cover = $("#r-cover");
  cover.textContent = l.emoji;
  cover.style.background = `linear-gradient(135deg, ${l.tint[0]}, ${l.tint[1]})`;
  $("#r-name").textContent = r.name;
  $("#r-meta").textContent = `${l.category} · 免外送費`;
  $("#menu").replaceChildren(...r.menu.map(menuRow));
  updateCartBar();
}

function menuRow(item) {
  const li = document.createElement("li");
  const info = html("div");
  info.append(
    html("div", item.name, "item-name"),
    html("div", `備餐 ${item.prep_min} 分鐘`, "item-meta"),
    html("div", `NT$ ${item.price}`, "item-price"),
  );
  const control = html("div");
  const set = (n) => {
    state.qty[item.id] = n;
    render();
    updateCartBar();
  };
  const render = () => {
    const n = state.qty[item.id] || 0;
    if (n === 0) {
      const add = html("button", "+", "add-btn");
      add.type = "button";
      add.setAttribute("aria-label", `加入 ${item.name}`);
      add.addEventListener("click", () => set(1));
      control.replaceChildren(add);
      return;
    }
    const stepper = html("div", undefined, "stepper");
    const minus = html("button", "−", "minus");
    const plus = html("button", "+");
    minus.type = plus.type = "button";
    minus.setAttribute("aria-label", `減少 ${item.name}`);
    plus.setAttribute("aria-label", `增加 ${item.name}`);
    plus.disabled = n >= MAX_QTY;
    minus.addEventListener("click", () => set(n - 1));
    plus.addEventListener("click", () => set(Math.min(MAX_QTY, n + 1)));
    stepper.append(minus, html("output", String(n)), plus);
    control.replaceChildren(stepper);
  };
  render();
  li.append(info, control);
  return li;
}

function updateCartBar() {
  const { count, total } = cartSummary(state.restaurant.menu, state.qty);
  $("#cart-bar").hidden = count === 0;
  $("#cart-count").textContent = String(count);
  $("#cart-total").textContent = `NT$ ${total}`;
}

// --- 結帳 ------------------------------------------------------------------

function fillLines(list, lines) {
  list.replaceChildren(...lines.map((l) => {
    const li = document.createElement("li");
    li.append(html("span", `${l.qty}x`, "qty"), html("span", l.name), html("span", `NT$ ${l.subtotal}`));
    return li;
  }));
}

function renderCheckout() {
  const r = state.restaurant;
  const { total } = cartSummary(r.menu, state.qty);
  $("#co-restaurant").textContent = r.name;
  fillLines($("#co-lines"), cartLines(r.menu, state.qty));
  $("#co-total").textContent = `NT$ ${total}`;
  const btn = $("#place-order");
  btn.textContent = `下訂單 · NT$ ${total}`;
  btn.disabled = state.busy || total === 0;
}

// --- 呼叫派單服務 ----------------------------------------------------------

async function postJSON(base, path, body, timeoutMs, cancel) {
  if (!base) return { outcome: "error", detail: "未設定服務網址", latencyMs: 0, instanceId: null };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  cancel?.addEventListener("abort", () => ctrl.abort());
  const start = performance.now();
  try {
    const res = await fetch(`${base.replace(/\/$/, "")}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    const outcome = outcomeFromStatus(res.status);
    if (outcome !== "ok") {
      await res.body?.cancel();
      return { outcome, detail: `HTTP ${res.status}`, latencyMs: performance.now() - start, instanceId: null };
    }
    const data = await res.json();
    return { outcome, data, latencyMs: performance.now() - start, instanceId: data.instance_id };
  } catch (err) {
    const latencyMs = performance.now() - start;
    if (ctrl.signal.aborted) return { outcome: "timeout", latencyMs, instanceId: null };
    // 瀏覽器無法區分服務未啟動與 CORS 被擋，兩者都會走到這裡
    return { outcome: "error", detail: "無法連線", latencyMs, instanceId: null };
  } finally {
    clearTimeout(timer);
  }
}

// --- 追蹤頁：每個版本一個區塊，各有自己的地圖與動畫時鐘 -------------------

function stopTracks() {
  for (const t of Object.values(state.tracks)) {
    t.stopped = true;
    t.cancel.abort(); // 中斷還在等回應的追蹤請求
    t.view.destroy();
  }
}

function createTracks() {
  stopTracks();
  const template = $("#track-template");
  const box = $("#tracks");
  box.replaceChildren();
  state.tracks = {};
  for (const target of TARGETS) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.target = target;
    node.querySelector(".swatch").classList.add(`swatch-${target}`);
    node.querySelector(".track-title").textContent = TRACK_INFO[target].title;
    node.querySelector(".tag").textContent = TRACK_INFO[target].tag;
    box.appendChild(node);
    const track = {
      node,
      eta: node.querySelector(".eta"),
      etaLabel: node.querySelector(".eta-label"),
      progress: node.querySelector(".progress"),
      status: node.querySelector(".status-text"),
      trackMeta: node.querySelector(".track-meta"),
      rider: node.querySelector(".rider-card"),
      target,
      result: null,
      stopped: false,
      cancel: new AbortController(),
    };
    track.view = new CityMapView(node.querySelector(".map"), state.map);
    state.tracks[target] = track;
  }
}

function setSegments(track, step, frac) {
  [...track.progress.children].forEach((seg, i) => {
    const fill = i < step ? 1 : i === step ? frac : 0;
    seg.style.setProperty("--fill", `${Math.round(fill * 100)}%`);
  });
}

function setWaiting(track) {
  track.view.showWaiting(state.restaurant, look(state.restaurant).emoji);
  track.etaLabel.textContent = "派單中";
  track.eta.textContent = "正在為你尋找外送夥伴";
  track.eta.className = "eta small";
  track.progress.className = "progress searching";
  setSegments(track, 0, 0);
  track.waitText = html("span", "已等待 0.0 秒");
  track.status.replaceChildren(html("span", undefined, "spinner"), track.waitText);
  track.trackMeta.textContent = "";
  track.rider.hidden = true;
}

function setSuccess(track, r) {
  const d = r.data;
  track.result = d;
  track.etaLabel.textContent = "預估外送時間";
  track.eta.className = "eta";
  track.progress.className = "progress";
  track.rider.hidden = false;
  track.rider.querySelector(".rider-name").textContent = d.rider.name;
  track.view.showResult(d);
  showProgress(track, "to_restaurant", 0, d.schedule.deliver_s);
}

function showProgress(track, phase, simS, etaS) {
  const p = progressFromTrack(phase, simS, etaS, track.result.schedule);
  setSegments(track, p.step, p.frac);
  track.eta.textContent = etaText(simS + etaS, simS);
  track.status.textContent = STEP_TEXT[p.step];
}

/** 把單次追蹤的結果回報 web，讓儀表板在配送期間也看得到這台執行個體；不等待回應。 */
function reportTrack(target, r) {
  fetch("/api/reports/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(trackReportBody(target, r)),
  }).catch((err) => console.warn("回報 web 失敗", err));
}

/** 每個版本各自的追蹤迴圈：同一時間只有一個追蹤請求，回應後隔 track_interval_ms 再送下一個。 */
async function trackLoop(track, base, orderId) {
  const interval = state.config.track_interval_ms;
  let tracking = track.result.tracking;
  let failures = 0;
  while (!track.stopped) {
    await sleep(interval);
    if (track.stopped) return;
    const r = await postJSON(
      base, "/api/track", { order_id: orderId, tracking }, state.config.timeout_ms, track.cancel.signal,
    );
    if (track.stopped) return;
    reportTrack(track.target, r);
    if (r.outcome !== "ok") {
      failures += 1;
      const reason = { timeout: "逾時", busy: "服務忙碌", error: r.detail.split("（")[0] }[r.outcome];
      track.trackMeta.textContent = `⚠ 位置更新延遲（${reason}），已重試 ${failures} 次`;
      track.trackMeta.className = "track-meta stale";
      continue;
    }
    failures = 0;
    const d = r.data;
    tracking = d.tracking;
    track.view.applyTrack(d, interval);
    showProgress(track, d.phase, d.sim_s, d.eta_s);
    track.trackMeta.textContent = "";
    if (d.phase === "delivered") return;
  }
}

function setFailed(track, r) {
  track.view.showFailed();
  track.etaLabel.textContent = "派單結果";
  track.eta.textContent = "派單失敗";
  track.eta.className = "eta small fail";
  track.progress.className = "progress";
  setSegments(track, 0, 0);
  track.status.textContent = {
    timeout: `✗ 逾時：超過 ${state.config.timeout_ms / 1000} 秒沒有回應`,
    busy: `✗ 服務忙碌（${r.detail}）`,
    error: `✗ 錯誤：${r.detail}`,
  }[r.outcome];
}

function renderOrderDetail(body) {
  const r = state.restaurant;
  $("#od-restaurant").textContent = r.name;
  $("#od-address").textContent = body.customer.address || "未填寫（系統隨機指定位置）";
  fillLines($("#od-lines"), cartLines(r.menu, state.qty));
  $("#od-total").textContent = `NT$ ${cartSummary(r.menu, state.qty).total}`;
}

async function placeOrder() {
  if (state.busy) return;
  const r = state.restaurant;
  if (!r || cartSummary(r.menu, state.qty).count === 0) return;
  state.busy = true;

  const orderId = makeOrderId();
  const form = Object.fromEntries(new FormData($("#customer-form")));
  const body = buildOrderBody({ orderId, restaurantId: r.id, menu: r.menu, qty: state.qty, customer: form });

  renderOrderDetail(body);
  const again = $("#again");
  again.disabled = true;
  again.textContent = "派單中…";
  show("tracking");
  createTracks();
  for (const t of TARGETS) setWaiting(state.tracks[t]);

  const started = performance.now();
  const results = {};
  const ticker = setInterval(() => {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    for (const t of TARGETS) if (!results[t]) state.tracks[t].waitText.textContent = `已等待 ${secs} 秒`;
  }, 100);

  const urls = { fixed: state.config.fixed_url, auto: state.config.auto_url };
  await Promise.all(TARGETS.map(async (t) => {
    const res = await postJSON(urls[t], "/api/orders", body, state.config.timeout_ms);
    results[t] = res;
    const track = state.tracks[t];
    if (res.outcome === "ok") {
      setSuccess(track, res);
      trackLoop(track, urls[t], orderId);
    } else {
      setFailed(track, res);
    }
  }));
  clearInterval(ticker);

  try {
    await fetch("/api/reports/order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reportBody(orderId, results)),
    });
  } catch (err) {
    console.warn("回報 web 失敗", err);
  }

  state.busy = false;
  again.disabled = false;
  again.textContent = "再點一單";
  $("#back").hidden = false;
}

function orderAgain() {
  state.qty = {};
  state.restaurant = null;
  $("#customer-form").reset();
  $(".more-fields").open = false;
  show("home");
}

// --- 啟動 ------------------------------------------------------------------

async function init() {
  initDashboard({ toggle: $("#debug-toggle"), panel: $("#dashboard") });
  history.replaceState({ screen: "home" }, "");
  $("#back").addEventListener("click", () => history.back());
  $("#view-cart").addEventListener("click", () => {
    renderCheckout();
    show("checkout");
  });
  $("#place-order").addEventListener("click", placeOrder);
  $("#again").addEventListener("click", orderAgain);
  try {
    [state.config, state.map] = await Promise.all([getJSON("/api/config"), getJSON("/api/map")]);
  } catch (err) {
    const msg = $("#load-error");
    msg.textContent = `載入失敗：${err.message}。請重新整理頁面。`;
    msg.hidden = false;
    return;
  }
  renderRestaurants();
}

init();
