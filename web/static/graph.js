// 請求流向圖：同學與模擬顧客在中間，固定版的機器在左、擴展版的機器在右。
// 每一筆手機請求是一顆從手機飛到處理它那台機器的小點；模擬顧客是粗細代表筆數的線。

import {
  columnPositions, edgeWidth, graphPhones, loadtestDotCount, machineNodeState, sideUnresponsive, TARGETS,
} from "./logic.js";

const NS = "http://www.w3.org/2000/svg";
const ROW = 24; // 每個節點佔的高度
const TOP = 40; // 欄標題在 y=12，第一個節點的名字畫在節點上方 8px，要留空間
const BOTTOM = 28; // 「模擬顧客」字樣畫在方塊下方 19px，要留在圖內
const FLIGHT_MS = 900;
const MAX_DOTS = 300;
const SIDE_X = { fixed: 0.1, auto: 0.9 };
const CENTER_X = 0.5;

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function label(parent, x, y, text, cls, anchor = "middle") {
  const t = el("text", { x, y, "text-anchor": anchor, class: cls }, parent);
  t.textContent = text;
  return t;
}

export function createRequestGraph(svg) {
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const lines = el("g", { class: "graph-lines" }, svg);
  const nodes = el("g", { class: "graph-nodes" }, svg);
  const flying = el("g", { class: "graph-dots" }, svg);
  let dots = [];
  let frame = null;

  function update(snap, myTag) {
    const W = svg.clientWidth || 640;
    const phones = graphPhones(snap.targets, myTag);
    const rows = Math.max(phones.length + 1, ...TARGETS.map((t) => snap.targets[t].machines.length), 3);
    const H = TOP + rows * ROW + BOTTOM;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.style.height = `${H}px`;

    // 節點位置：中間一欄是同學，最下面是模擬顧客；左右兩欄是機器
    const centerYs = columnPositions(phones.length + 1, TOP, H - BOTTOM);
    const phonePos = new Map(phones.map((p, i) => [p.tag, { x: W * CENTER_X, y: centerYs[i] }]));
    const loadtest = { x: W * CENTER_X, y: centerYs[phones.length] };
    const machinePos = {};
    for (const target of TARGETS) {
      const ms = snap.targets[target].machines;
      const ys = columnPositions(ms.length, TOP, H - BOTTOM);
      machinePos[target] = new Map(ms.map((m, i) => [m.id, { x: W * SIDE_X[target], y: ys[i] }]));
    }

    drawLines(snap, phonePos, loadtest, machinePos);
    drawNodes(snap, phones, phonePos, loadtest, machinePos, W);
    schedule(snap, phonePos, loadtest, machinePos, W, H);
  }

  function drawLines(snap, phonePos, loadtest, machinePos) {
    lines.replaceChildren();
    for (const target of TARGETS) {
      const ms = snap.targets[target].machines;
      const max = Math.max(1, ...ms.map((m) => m.loadtest_requests ?? 0));
      for (const m of ms) {
        const to = machinePos[target].get(m.id);
        const width = edgeWidth(m.loadtest_requests, max);
        if (width) {
          el("line", { x1: loadtest.x, y1: loadtest.y, x2: to.x, y2: to.y, "stroke-width": width, class: `edge-lt ${target}-stroke` }, lines);
        }
        for (const p of m.phones) {
          const from = phonePos.get(p.tag);
          if (from) el("line", { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: `edge-phone ${target}-stroke` }, lines);
        }
      }
    }
  }

  function drawNodes(snap, phones, phonePos, loadtest, machinePos, W) {
    nodes.replaceChildren();
    label(nodes, W * SIDE_X.fixed, 12, "固定版", "graph-title fixed-text");
    label(nodes, W * CENTER_X, 12, "同學與模擬顧客", "graph-title");
    label(nodes, W * SIDE_X.auto, 12, "擴展版", "graph-title auto-text");
    for (const p of phones) {
      const { x, y } = phonePos.get(p.tag);
      el("circle", { cx: x, cy: y, r: p.you ? 6 : 4.5, class: `phone-dot${p.you ? " you" : ""}` }, nodes);
      label(nodes, x, y - 8, p.you ? `${p.name}（你）` : p.name, `node-label${p.you ? " you" : ""}`);
    }
    el("rect", { x: loadtest.x - 7, y: loadtest.y - 7, width: 14, height: 14, rx: 3, class: "loadtest-node" }, nodes);
    label(nodes, loadtest.x, loadtest.y + 19, "模擬顧客", "node-label");
    for (const target of TARGETS) {
      const t = snap.targets[target];
      const unresponsive = sideUnresponsive(t.machines, t.failures);
      for (const m of t.machines) {
        const { x, y } = machinePos[target].get(m.id);
        const state = machineNodeState(m, unresponsive);
        el("circle", { cx: x, cy: y, r: 9, class: `machine-node ${target}-fill ${state.tone}` }, nodes);
        const anchor = target === "fixed" ? "start" : "end";
        const dx = target === "fixed" ? 13 : -13;
        label(nodes, x + dx, y + 4, `#${m.id}`, "node-label machine-label", anchor);
        if (state.badge) label(nodes, x + dx, y + 15, state.badge, `badge ${state.tone}`, anchor);
      }
    }
  }

  function schedule(snap, phonePos, loadtest, machinePos, W, H) {
    const now = performance.now();
    const add = (from, to, cls, fail = false) => {
      if (dots.length >= MAX_DOTS) return;
      // 減少動態時小點不移動，停在線上隨機一處淡出
      const at = reduceMotion ? 0.3 + Math.random() * 0.5 : null;
      dots.push({ from, to, cls, fail, at, start: now + Math.random() * 1000 });
    };
    for (const target of TARGETS) {
      const t = snap.targets[target];
      const sideCenter = { x: W * SIDE_X[target], y: H / 2 };
      for (const r of t.requests ?? []) {
        const from = phonePos.get(r.tag) ?? { x: W * CENTER_X, y: TOP + Math.random() * (H - TOP - BOTTOM) };
        const to = r.machine ? machinePos[target].get(r.machine) : null;
        add(from, to ?? sideCenter, `flow-dot ${target}-fill`, !to);
      }
      for (const m of t.machines) {
        for (let i = 0; i < loadtestDotCount(m.loadtest_requests); i++) {
          add(loadtest, machinePos[target].get(m.id), `flow-dot lt-dot ${target}-fill`);
        }
      }
    }
    if (frame === null) frame = requestAnimationFrame(animate);
  }

  function animate(now) {
    dots = dots.filter((d) => now - d.start < FLIGHT_MS);
    const circles = [];
    for (const d of dots) {
      const p = (now - d.start) / FLIGHT_MS;
      if (p < 0) continue;
      const pos = d.at ?? p;
      const x = d.from.x + (d.to.x - d.from.x) * pos;
      const y = d.from.y + (d.to.y - d.from.y) * pos;
      const failing = d.fail && (d.at !== null || p > 0.6);
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", x);
      c.setAttribute("cy", y);
      c.setAttribute("r", 3);
      c.setAttribute("class", failing ? "flow-dot fail" : d.cls);
      if (d.at !== null) c.setAttribute("opacity", String(1 - p));
      else if (failing) c.setAttribute("opacity", String(Math.max(0, 1 - (p - 0.6) / 0.4)));
      circles.push(c);
    }
    flying.replaceChildren(...circles);
    frame = dots.length ? requestAnimationFrame(animate) : null;
  }

  return { update };
}
