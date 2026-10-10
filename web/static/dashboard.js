// Debug 儀表板：展開時建立 EventSource，收起時關閉。

import { chartPath, failureText, formatLatency, formatPct, latencyText, TARGETS } from "./logic.js";

const NS = "http://www.w3.org/2000/svg";
const NAME = { fixed: "固定版", auto: "擴展版" };
const RPS_HINT = "最近 10 秒平均每秒完成的請求數，含下單與外送員位置更新。兩邊收到的新訂單一樣多，但下單失敗的顧客不會有後續的位置更新，所以撐不住的一方請求數會少很多";
const FAILURE_HINT = "最近 10 秒內失敗的請求數。逾時：超過 10 秒沒有回應；忙碌：服務回覆 429 拒絕；錯誤：其他錯誤或無法連線";
const LATENCY_HINT = "最近 10 秒內，下單與外送員位置更新的每一筆請求，從送出到收到回應的平均時間；逾時以 10 秒計，忙碌與錯誤不計入";
// 圖表高度固定、寬度依實際版面，字級不會跟著縮放
const H = 72;
const PAD = { left: 34, right: 4, top: 6, bottom: 14 };

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function html(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString("zh-TW", { hour12: false });
}

const METRICS = {
  success_rate: {
    max: () => 1,
    ticks: [0, 0.5, 1],
    tickLabel: (v) => `${Math.round(v * 100)}%`,
    format: formatPct,
  },
  avg_ms: {
    // 對齊幾個好讀的上限，避免數值小幅變動時刻度一直跳
    max: (values) => [1000, 2000, 5000, 10000, 15000, 20000].find((m) => m >= Math.max(0, ...values)) ?? 30000,
    ticks: null,
    tickLabel: (v) => (v === 0 ? "0" : v < 1000 ? `${v} ms` : `${v / 1000} s`),
    format: formatLatency,
  },
};

export function initDashboard({ toggle, panel }) {
  const status = panel.querySelector("#dash-status");
  const charts = [...panel.querySelectorAll(".chart")].map(setupChart);
  // 手機版預設收起趨勢圖，讓儀表板與兩個追蹤區塊擠得進同一個畫面
  panel.querySelector("#dash-charts").open = matchMedia("(min-width: 768px)").matches;
  let source = null;
  let latest = null;

  function open() {
    panel.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    status.textContent = "連線中…";
    source = new EventSource("/api/stream");
    source.addEventListener("snapshot", (e) => {
      latest = JSON.parse(e.data);
      render(latest);
    });
    source.onopen = () => { status.textContent = "已連線"; };
    source.onerror = () => { status.textContent = "連線中斷，自動重新連線…"; };
  }

  function close() {
    source?.close();
    source = null;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  }

  toggle.addEventListener("click", () => (source ? close() : open()));

  function render(snap) {
    status.textContent = `最近 10 秒 · ${clock(snap.now)}`;
    for (const target of TARGETS) {
      renderStats(panel.querySelector(`.dash-target[data-target="${target}"] .stats`), snap.targets[target]);
    }
    for (const chart of charts) chart.update(snap.series);
  }
}

function renderStats(dl, t) {
  const [latency, small] = latencyText(t);
  const failed = Object.values(t.failures).some((n) => n > 0);
  const rows = [
    { label: "個體數", value: String(t.instances) },
    { label: "每秒請求", value: t.rps.toFixed(t.rps < 10 ? 1 : 0), hint: RPS_HINT },
    { label: "成功率", value: formatPct(t.success_rate) },
    { label: "平均回應時間", value: latency, cls: small ? "small" : "", hint: LATENCY_HINT },
    { label: "失敗次數", value: failureText(t.failures), cls: failed ? "small fail" : "small", hint: FAILURE_HINT, wide: true },
  ];
  dl.replaceChildren(...rows.map(({ label, value, cls, hint, wide }) => {
    const div = document.createElement("div");
    if (hint) div.title = hint; // 滑鼠停在欄位上時說明怎麼算
    if (wide) div.className = "wide";
    div.append(html("dt", label), html("dd", value, cls || undefined));
    return div;
  }));
}

function setupChart(figure) {
  const metric = METRICS[figure.dataset.metric];
  const key = figure.dataset.metric;
  const svg = figure.querySelector("svg");
  const legend = figure.querySelector(".legend");
  const tooltip = figure.querySelector(".tooltip");
  let series = [];
  let hover = null;
  let x = () => 0;
  let y = () => 0;
  let W = 320;

  function draw() {
    W = svg.clientWidth || W;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const values = series.flatMap((p) => TARGETS.map((t) => p[t][key])).filter((v) => v !== null);
    const max = metric.max(values);
    const n = Math.max(series.length - 1, 1);
    x = (i) => PAD.left + (i * (W - PAD.left - PAD.right)) / n;
    y = (v) => H - PAD.bottom - (v / max) * (H - PAD.top - PAD.bottom);

    svg.replaceChildren();
    const ticks = metric.ticks ?? [0, max / 2, max];
    for (const v of ticks) {
      el("line", { x1: PAD.left, x2: W - PAD.right, y1: y(v), y2: y(v), class: v === 0 ? "baseline" : "grid" }, svg);
      const label = el("text", { x: PAD.left - 6, y: y(v) + 3, "text-anchor": "end", class: "tick" }, svg);
      label.textContent = metric.tickLabel(v);
    }
    [[0, "-10 分"], [Math.round(n / 2), "-5 分"], [n, "現在"]].forEach(([i, text], k) => {
      const label = el("text", {
        x: x(i), y: H - 4, class: "tick", "text-anchor": ["start", "middle", "end"][k],
      }, svg);
      label.textContent = text;
    });
    for (const target of TARGETS) {
      const values = series.map((p) => p[target][key]);
      const d = chartPath(values, x, y);
      if (d) el("path", { d, class: `line ${target}-stroke` }, svg);
      // 前後都沒有資料的點連不成線，改畫一個點
      values.forEach((v, i) => {
        if (v !== null && (values[i - 1] ?? null) === null && (values[i + 1] ?? null) === null) {
          el("circle", { cx: x(i), cy: y(v), r: 3.5, class: `hover-dot ${target}-fill` }, svg);
        }
      });
    }

    legend.replaceChildren(...TARGETS.map((target) => {
      const last = [...series].reverse().find((p) => p[target][key] !== null);
      const item = html("span");
      item.title = NAME[target];
      item.append(html("i", undefined, `swatch swatch-${target}`), html("strong", metric.format(last ? last[target][key] : null)));
      return item;
    }));
    drawHover();
  }

  function drawHover() {
    if (hover === null || !series[hover]) {
      tooltip.hidden = true;
      return;
    }
    const p = series[hover];
    el("line", { x1: x(hover), x2: x(hover), y1: PAD.top, y2: H - PAD.bottom, class: "crosshair" }, svg);
    for (const target of TARGETS) {
      const v = p[target][key];
      if (v !== null) el("circle", { cx: x(hover), cy: y(v), r: 4, class: `hover-dot ${target}-fill` }, svg);
    }
    tooltip.replaceChildren(
      html("div", `${clock(p.t)}–${clock(p.t + 10)}`, "muted"),
      ...TARGETS.map((target) => {
        const row = html("div");
        row.append(html("i", undefined, `swatch swatch-${target}`), html("strong", metric.format(p[target][key])), NAME[target]);
        return row;
      }),
    );
    tooltip.hidden = false;
    const scale = svg.getBoundingClientRect().width / W;
    const left = x(hover) * scale;
    const width = tooltip.offsetWidth;
    tooltip.style.left = `${Math.max(0, Math.min(left - width / 2, figure.clientWidth - width))}px`;
  }

  svg.addEventListener("pointermove", (e) => {
    if (!series.length) return;
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const n = series.length - 1;
    hover = Math.max(0, Math.min(n, Math.round(((px - PAD.left) / (W - PAD.left - PAD.right)) * n)));
    draw();
  });
  svg.addEventListener("pointerleave", () => {
    hover = null;
    draw();
  });
  // 寬度改變（含趨勢圖從收起展開）時依新寬度重畫
  new ResizeObserver(() => series.length && draw()).observe(svg);

  return {
    update(next) {
      series = next;
      draw();
    },
  };
}
