// 點餐頁：選店家與餐點 → 平行呼叫兩個派單服務 → 顯示結果與路線 → 回報 hub。

import { initDashboard } from "./dashboard.js";
import {
  buildOrderBody, formatMs, makeOrderId, outcomeFromStatus, reportBody, TARGETS,
} from "./logic.js";
import { CityMapView } from "./map.js";

const MAX_QTY = 9;
const FAIL_TEXT = { timeout: "逾時", busy: "服務忙碌", error: "錯誤" };

const $ = (sel) => document.querySelector(sel);

const state = {
  config: null,
  map: null,
  restaurant: null,
  qty: {},
  busy: false,
  results: {},
  mapView: null,
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

// --- 店家與菜單 ------------------------------------------------------------

function renderRestaurants() {
  const box = $("#restaurants");
  box.replaceChildren(...state.map.restaurants.map((r) => {
    const preps = r.menu.map((m) => m.prep_min);
    const btn = html("button", undefined, "restaurant");
    btn.type = "button";
    btn.setAttribute("role", "radio");
    btn.setAttribute("aria-checked", "false");
    btn.dataset.id = r.id;
    btn.append(html("strong", r.name), html("span", `${r.menu.length} 項 · 備餐 ${Math.min(...preps)}–${Math.max(...preps)} 分`));
    btn.addEventListener("click", () => selectRestaurant(r));
    return btn;
  }));
}

function selectRestaurant(r) {
  if (state.busy) return;
  state.restaurant = r;
  state.qty = {};
  for (const btn of $("#restaurants").children) {
    btn.setAttribute("aria-checked", String(btn.dataset.id === r.id));
  }
  $("#menu-title").textContent = `2. ${r.name} 的餐點`;
  $("#menu").replaceChildren(...r.menu.map(menuRow));
  $("#menu-panel").hidden = false;
  state.mapView.highlightRestaurant(r.id);
  updateSubmit();
}

function menuRow(item) {
  const li = document.createElement("li");
  const info = html("div");
  info.append(html("div", item.name, "item-name"), html("div", `NT$ ${item.price} · 備餐 ${item.prep_min} 分`, "item-meta"));

  const stepper = html("div", undefined, "stepper");
  const minus = html("button", "−");
  const plus = html("button", "+");
  const out = html("output", "0");
  minus.type = plus.type = "button";
  minus.setAttribute("aria-label", `減少 ${item.name}`);
  plus.setAttribute("aria-label", `增加 ${item.name}`);
  const set = (n) => {
    state.qty[item.id] = n;
    out.textContent = String(n);
    minus.disabled = n <= 0;
    plus.disabled = n >= MAX_QTY;
    updateSubmit();
  };
  minus.addEventListener("click", () => set(Math.max(0, (state.qty[item.id] || 0) - 1)));
  plus.addEventListener("click", () => set(Math.min(MAX_QTY, (state.qty[item.id] || 0) + 1)));
  set(0);
  stepper.append(minus, out, plus);
  li.append(info, stepper);
  return li;
}

function updateSubmit() {
  const r = state.restaurant;
  const total = r ? r.menu.reduce((sum, m) => sum + m.price * (state.qty[m.id] || 0), 0) : 0;
  $("#total").textContent = `NT$ ${total}`;
  $("#submit").disabled = state.busy || total === 0;
}

// --- 下單 ------------------------------------------------------------------

async function callTarget(url, body, timeoutMs) {
  if (!url) return { outcome: "error", detail: "未設定服務網址", latencyMs: 0, instanceId: null };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const start = performance.now();
  try {
    const res = await fetch(`${url.replace(/\/$/, "")}/api/orders`, {
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
    return { outcome: "error", detail: "無法連線", latencyMs, instanceId: null };
  } finally {
    clearTimeout(timer);
  }
}

function statusBody(target) {
  return $(`.status-card[data-target="${target}"] .status-body`);
}

function renderWaiting(target) {
  const row = html("div", undefined, "status-waiting");
  const text = html("span", "已等待 0.0 秒");
  row.append(html("span", undefined, "spinner"), text);
  statusBody(target).replaceChildren(row);
  return text;
}

function details(rows) {
  const dl = document.createElement("dl");
  for (const [k, v] of rows) dl.append(html("dt", k), html("dd", v));
  return dl;
}

function renderResult(target, r) {
  const body = statusBody(target);
  if (r.outcome === "ok") {
    const d = r.data;
    body.replaceChildren(
      html("div", "✓ 派單成功", "status-headline ok"),
      details([
        ["外送員", d.rider.name],
        ["送達", `約 ${d.eta_minutes} 分鐘`],
        ["回應", formatMs(r.latencyMs)],
        ["運算", `${d.compute_ms} ms`],
        ["執行個體", d.instance_id.length > 12 ? `${d.instance_id.slice(0, 12)}…` : d.instance_id],
      ]),
    );
    return;
  }
  const reason = r.outcome === "timeout"
    ? `超過 ${state.config.timeout_ms / 1000} 秒未回應`
    : r.detail;
  body.replaceChildren(
    html("div", `✗ ${FAIL_TEXT[r.outcome]}`, "status-headline fail"),
    details([["原因", reason], ["等待", formatMs(r.latencyMs)]]),
  );
}

function setMapSwitch(target, enabled, pressed) {
  const btn = document.querySelector(`.map-switch button[data-target="${target}"]`);
  btn.disabled = !enabled;
  btn.setAttribute("aria-pressed", String(pressed));
}

function showOnMap(target) {
  state.shownTarget = target;
  for (const t of TARGETS) setMapSwitch(t, state.results[t]?.outcome === "ok", t === target);
  state.mapView.showResult(target, state.results[target].data);
}

async function submit() {
  if (state.busy) return;
  state.busy = true;
  updateSubmit();
  $("#submit").textContent = "派單中…";

  const orderId = makeOrderId();
  const form = Object.fromEntries(
    [...document.querySelectorAll(".customer input")].map((input) => [input.name, input.value]),
  );
  const body = buildOrderBody({
    orderId,
    restaurantId: state.restaurant.id,
    menu: state.restaurant.menu,
    qty: state.qty,
    customer: form,
  });

  state.results = {};
  state.shownTarget = null;
  for (const t of TARGETS) setMapSwitch(t, false, false);
  state.mapView.clearResult();
  $("#results").hidden = false;

  const started = performance.now();
  const waiting = Object.fromEntries(TARGETS.map((t) => [t, renderWaiting(t)]));
  const ticker = setInterval(() => {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    for (const t of TARGETS) if (!state.results[t]) waiting[t].textContent = `已等待 ${secs} 秒`;
  }, 100);

  const urls = { fixed: state.config.fixed_url, auto: state.config.auto_url };
  await Promise.all(TARGETS.map(async (t) => {
    const r = await callTarget(urls[t], body, state.config.timeout_ms);
    state.results[t] = r;
    renderResult(t, r);
    if (r.outcome === "ok" && !state.shownTarget) showOnMap(t);
    else if (state.shownTarget) setMapSwitch(t, r.outcome === "ok", false);
  }));
  clearInterval(ticker);
  if (!state.shownTarget) $("#map-caption").textContent = "兩個版本都沒有成功派單，沒有路線可以顯示。";

  try {
    await fetch("/api/reports/order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reportBody(orderId, state.results)),
    });
  } catch (err) {
    console.warn("回報 hub 失敗", err);
  }

  state.busy = false;
  $("#submit").textContent = "再下一單";
  updateSubmit();
}

// --- 啟動 ------------------------------------------------------------------

async function init() {
  initDashboard({ toggle: $("#debug-toggle"), panel: $("#dashboard"), layout: $(".layout") });
  try {
    [state.config, state.map] = await Promise.all([getJSON("/api/config"), getJSON("/api/map")]);
  } catch (err) {
    const msg = $("#load-error");
    msg.textContent = `載入失敗：${err.message}。請重新整理頁面。`;
    msg.hidden = false;
    return;
  }
  state.mapView = new CityMapView($("#map"), $("#map-caption"), state.map);
  renderRestaurants();
  $("#submit").addEventListener("click", submit);
  for (const btn of document.querySelectorAll(".map-switch button")) {
    btn.addEventListener("click", () => showOnMap(btn.dataset.target));
  }
}

init();
