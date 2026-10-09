// 外送地圖：以 SVG 畫成 Google 地圖風格。
// 外送員位置由後端的追蹤回應決定，前端只在兩次回應之間沿 trail 平滑移動。

import { decorations, fitView, outskirtPaths, polylineAt, roadPaths } from "./logic.js";

const NS = "http://www.w3.org/2000/svg";
const OUTSKIRTS = 40; // 城市外圍裝飾道路的寬度（格），需大於視野可能超出城市的範圍
const ROW_NAMES = ["中正路", "民生路", "忠孝路", "仁愛路"];
const COL_NAMES = ["中山路", "復興路", "光復路", "敦化路"];
const PIN_PATH = "M0 0C-3 -7 -14 -12 -14 -24A14 14 0 1 1 14 -24C14 -12 3 -7 0 0Z";

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

const nonScaling = { "vector-effect": "non-scaling-stroke" };

export class CityMapView {
  constructor(svg, map) {
    this.svg = svg;
    this.map = map;
    this.frame = 0;
    this.result = null;
    this.track = null;
    this.riderPos = null;
    this.restaurant = null;
    this.glyph = "🏪";
    this.mode = "idle";

    const { size } = map;
    const deco = decorations(map);
    const paths = roadPaths(map);
    const outskirts = outskirtPaths(map, OUTSKIRTS);
    const base = el("g", {}, svg);
    el("rect", {
      x: -OUTSKIRTS - 1, y: -OUTSKIRTS - 1, width: size + 2 * OUTSKIRTS + 2, height: size + 2 * OUTSKIRTS + 2,
      fill: "var(--map-bg)",
    }, base);
    // 小路在下、綠地與河流在中、主幹道在上：河上只有主幹道跨過，像橋一樣
    el("path", { d: paths.roads + outskirts.roads, class: "road", ...nonScaling }, base);
    for (const p of deco.parks) {
      el("rect", { x: p.x + 0.1, y: p.y + 0.1, width: p.w - 0.2, height: p.h - 0.2, rx: 0.4, class: "park" }, base);
    }
    // 河流兩端水平延伸到外圍邊緣
    const river = deco.river;
    const ends = [[-OUTSKIRTS, river[0][1]], ...river, [size - 1 + OUTSKIRTS, river[river.length - 1][1]]];
    el("path", {
      d: ends.map(([x, y], i) => `${i ? "L" : "M"}${x} ${y}`).join(""),
      class: "water", "stroke-width": 2.2,
    }, base);
    const arterials = paths.arterials + outskirts.arterials;
    el("path", { d: arterials, class: "arterial-edge", ...nonScaling }, base);
    el("path", { d: arterials, class: "arterial", ...nonScaling }, base);
    this.labels = el("g", {}, svg);
    this.overlay = el("g", {}, svg);
    this.setView([[size / 2, size / 2]], size);
    // 視窗縮放或跨過 CSS 斷點時地圖寬高比會變，重算視野，否則 viewBox 比例不符會在上下或左右留白
    this.resizer = new ResizeObserver(() => this.fit && this.setView(...this.fit));
    this.resizer.observe(svg);
  }

  // --- 狀態 ---------------------------------------------------------------

  showWaiting(restaurant, glyph) {
    this.stop();
    Object.assign(this, { restaurant, glyph, result: null, mode: "waiting" });
    this.setView([restaurant.node], 12);
  }

  showFailed() {
    this.stop();
    this.mode = "failed";
    this.draw();
  }

  showResult(result) {
    this.stop();
    this.result = result;
    this.track = null;
    this.mode = "result";
    const { to_restaurant: toR, to_customer: toC } = result.route;
    this.riderPos = toR[0];
    this.setView([...toR, ...toC], 12);
  }

  /** 套用一次追蹤回應：更新路線，並在 durationMs 內沿 trail 移動外送員。 */
  applyTrack(track, durationMs) {
    this.stop();
    this.track = track;
    this.follow();
    this.draw();
    const trail = track.trail;
    const started = performance.now();
    const step = () => {
      const frac = Math.min(1, (performance.now() - started) / durationMs);
      this.moveRider(polylineAt(trail, frac));
      if (frac < 1) this.frame = requestAnimationFrame(step);
    };
    step();
  }

  stop() {
    cancelAnimationFrame(this.frame);
  }

  /**
   * 外送員改道後，走過的路、接下來的路線或目的地可能落在目前視野外，這時才重新取景。
   * 判斷用視野本身的邊界（取景時已留邊），否則每次更新都會重取景、越縮越近；
   * 重新取景時也不比目前更近，只平移或拉遠，避免畫面一直縮放。
   */
  follow() {
    const { current, upcoming } = this.routes();
    const points = [...this.track.trail, ...(current || []), ...(upcoming || []), this.result.customer_node];
    const [vx, vy, vw, vh] = this.view;
    const inside = ([x, y]) => x >= vx && x <= vx + vw && y >= vy && y <= vy + vh;
    if (!points.every(inside)) this.setView(points, Math.max(12, vh / 1.2));
  }

  /** 不再使用這張地圖時呼叫。 */
  destroy() {
    this.stop();
    this.resizer.disconnect();
  }

  // --- 繪製 ---------------------------------------------------------------

  setView(points, minSpan) {
    this.fit = [points, minSpan];
    const [x, y, span] = fitView(points, this.map.size, { minSpan, pad: 2 });
    // 圖釘往上突出約 40px，上方多留一些空間避免被切掉
    const h = span * 1.2;
    // 寬高比跟著 app.css 的 .map aspect-ratio（手機正方形、寬螢幕 4:3）
    const ratio = this.svg.clientWidth / this.svg.clientHeight;
    const w = h * (Number.isFinite(ratio) && ratio > 0 ? ratio : 1);
    // 以點為中心取景，不限制在城市範圍內：超出城市的部分由外圍裝飾道路填滿。
    // 若硬把視野夾在城市內，靠近城市邊緣的外送員與圖釘會被推到畫面邊上切掉一半。
    this.view = [x + span / 2 - w / 2, y - span * 0.15, w, h];
    this.svg.setAttribute("viewBox", this.view.join(" "));
    // 縮小時小路變細，避免擠成一片
    const pxPerUnit = (this.svg.clientWidth || 360) / w;
    this.svg.dataset.zoom = pxPerUnit >= 20 ? "near" : pxPerUnit >= 12 ? "mid" : "far";
    this.draw();
  }

  /** 每個螢幕像素對應的地圖單位，讓圖釘與文字在任何縮放下維持相同大小。 */
  px() {
    return this.view[2] / (this.svg.clientWidth || 360);
  }

  draw() {
    const k = this.px();
    this.drawLabels(k);
    this.overlay.replaceChildren();
    if (this.result) {
      const line = (nodes) => nodes.map(([x, y], i) => `${i ? "L" : "M"}${x} ${y}`).join("");
      const { current, upcoming } = this.routes();
      if (upcoming) el("path", { d: line(upcoming), class: "route-upcoming", ...nonScaling }, this.overlay);
      if (current && current.length > 1) el("path", { d: line(current), class: "route-current", ...nonScaling }, this.overlay);
      const [cx, cy] = this.result.customer_node;
      this.customerPin(cx, cy, k);
    }
    if (this.restaurant) {
      const [x, y] = this.restaurant.node;
      this.restaurantPin(x, y, k, this.mode === "waiting");
    }
    if (this.result) {
      this.rider = el("g", {}, this.overlay);
      el("circle", { r: 14, class: "rider-bg" }, this.rider);
      const t = el("text", { "text-anchor": "middle", "dominant-baseline": "central", "font-size": 16, y: 1 }, this.rider);
      t.textContent = "🛵";
      this.moveRider(this.riderPos);
    }
  }

  /** 目前這段要走的路線（實線）與之後的路線（虛線）。 */
  routes() {
    const planned = this.result.route;
    if (!this.track) return { current: planned.to_restaurant, upcoming: planned.to_customer };
    switch (this.track.phase) {
      case "to_restaurant": return { current: this.track.route, upcoming: planned.to_customer };
      case "waiting": return { current: null, upcoming: planned.to_customer };
      case "to_customer": return { current: this.track.route, upcoming: null };
      default: return { current: null, upcoming: null };
    }
  }

  moveRider(pos) {
    this.riderPos = pos;
    if (this.rider && pos) this.rider.setAttribute("transform", `translate(${pos[0]} ${pos[1]}) scale(${this.px()})`);
  }

  drawLabels(k) {
    this.labels.replaceChildren();
    const [vx, vy, vw, vh] = this.view;
    const label = (text, x, y, rotate) => {
      const node = el("text", {
        x, y, class: "road-label", "font-size": 10 * k, "stroke-width": 3 * k,
        "text-anchor": "middle", "dominant-baseline": "central",
        ...(rotate ? { transform: `rotate(-90 ${x} ${y})` } : {}),
      }, this.labels);
      node.textContent = text;
    };
    this.map.arterials.rows.forEach((y, i) => {
      if (y > vy && y < vy + vh) label(ROW_NAMES[i % ROW_NAMES.length], vx + vw * 0.25, y, false);
    });
    this.map.arterials.cols.forEach((x, i) => {
      if (x > vx && x < vx + vw) label(COL_NAMES[i % COL_NAMES.length], x, vy + vh * 0.72, true);
    });
  }

  restaurantPin(x, y, k, pulsing) {
    const g = el("g", { transform: `translate(${x} ${y}) scale(${k})` }, this.overlay);
    if (pulsing) {
      const ring = el("circle", { cy: -24, r: 16, class: "pin-ring" }, g);
      el("animate", { attributeName: "r", values: "16;30;16", dur: "1.6s", repeatCount: "indefinite" }, ring);
    }
    el("path", { d: PIN_PATH, class: "pin-restaurant" }, g);
    const t = el("text", { y: -23, "text-anchor": "middle", "dominant-baseline": "central", "font-size": 15 }, g);
    t.textContent = this.glyph;
  }

  customerPin(x, y, k) {
    const g = el("g", { transform: `translate(${x} ${y}) scale(${k})` }, this.overlay);
    el("path", { d: PIN_PATH, class: "pin-customer" }, g);
    el("circle", { cy: -28, r: 4, fill: "#fff" }, g);
    el("path", { d: "M-7 -16A7 7 0 0 1 7 -16Z", fill: "#fff" }, g);
  }
}
