// 城市地圖：SVG 畫出路網、店家、顧客與路線，並依 schedule 播放外送員動畫。

import { fitView, PLAYBACK_MS, riderPosition, roadPaths, simSeconds } from "./logic.js";

const NS = "http://www.w3.org/2000/svg";

const PHASE_TEXT = {
  to_restaurant: "前往店家",
  waiting: "在店家等待備餐",
  to_customer: "送餐中",
  delivered: "已送達",
};

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

export class CityMapView {
  constructor(svg, caption, map) {
    this.svg = svg;
    this.caption = caption;
    this.map = map;
    this.activeRestaurant = null;
    this.result = null;
    this.target = null;
    this.frame = 0;

    const paths = roadPaths(map);
    const base = el("g", {}, svg);
    for (const cls of ["roads", "closed", "arterials"]) {
      el("path", { d: paths[cls], class: cls, "vector-effect": "non-scaling-stroke" }, base);
    }
    this.overlay = el("g", {}, svg);
    this.setView([-1, -1, map.size + 1, map.size + 1]);
  }

  setView(view) {
    this.view = view;
    this.svg.setAttribute("viewBox", view.join(" "));
    this.draw();
  }

  highlightRestaurant(id) {
    this.activeRestaurant = id;
    this.draw();
  }

  clearResult() {
    cancelAnimationFrame(this.frame);
    this.result = null;
    this.target = null;
    this.setView([-1, -1, this.map.size + 1, this.map.size + 1]);
    this.caption.textContent = "派單中…";
  }

  showResult(target, result) {
    cancelAnimationFrame(this.frame);
    this.result = result;
    this.target = target;
    const { to_restaurant: toR, to_customer: toC } = result.route;
    this.setView(fitView([...toR, ...toC], this.map.size, { minSpan: 14, pad: 2 }));
    this.started = performance.now();
    this.animate();
  }

  // 依目前視野大小決定標記尺寸，縮放後看起來一樣大
  unit() {
    return this.view[2] / 60;
  }

  draw() {
    const u = this.unit();
    this.overlay.replaceChildren();
    if (this.result) this.drawRoutes(u);
    for (const r of this.map.restaurants) {
      const [x, y] = r.node;
      const active = r.id === (this.result ? this.result.restaurant.id : this.activeRestaurant);
      el("rect", {
        x: x - 1.1 * u, y: y - 1.1 * u, width: 2.2 * u, height: 2.2 * u, rx: 0.5 * u,
        class: `restaurant-pin${active ? " active" : ""}`, "vector-effect": "non-scaling-stroke",
      }, this.overlay);
      const label = el("text", {
        x: x + 1.6 * u, y: y + 0.9 * u, class: "restaurant-label", "font-size": 2.4 * u, "stroke-width": 0.6 * u,
      }, this.overlay);
      label.textContent = r.name;
    }
    if (this.result) {
      const [cx, cy] = this.result.customer_node;
      el("circle", { cx, cy, r: 1.2 * u, class: "customer-pin", "vector-effect": "non-scaling-stroke" }, this.overlay);
      this.rider = el("circle", { r: 1.3 * u, class: `rider ${this.target}-fill`, "vector-effect": "non-scaling-stroke" }, this.overlay);
    }
  }

  drawRoutes() {
    const line = (nodes) => nodes.map(([x, y], i) => `${i ? "L" : "M"}${x} ${y}`).join("");
    el("path", {
      d: line(this.result.route.to_restaurant),
      class: `route-to-restaurant ${this.target}-stroke`, "vector-effect": "non-scaling-stroke",
    }, this.overlay);
    el("path", {
      d: line(this.result.route.to_customer),
      class: `route-to-customer ${this.target}-stroke`, "vector-effect": "non-scaling-stroke",
    }, this.overlay);
  }

  animate() {
    const { schedule, route, rider, eta_minutes: eta } = this.result;
    const t = simSeconds(performance.now() - this.started, schedule.deliver_s);
    const pos = riderPosition(route, schedule, t);
    this.rider.setAttribute("cx", pos.x);
    this.rider.setAttribute("cy", pos.y);
    const minute = Math.floor(t / 60);
    this.caption.textContent = pos.phase === "delivered"
      ? `外送員 ${rider.name} 已送達，共 ${eta} 分鐘（動畫以 ${PLAYBACK_MS / 1000} 秒播放整趟）`
      : `外送員 ${rider.name} ${PHASE_TEXT[pos.phase]} · 第 ${minute} 分鐘`;
    if (pos.phase !== "delivered") this.frame = requestAnimationFrame(() => this.animate());
  }
}
