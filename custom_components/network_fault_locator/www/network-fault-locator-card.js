/*
 * Network Fault Locator card - one chart with everything the integration measures.
 * Shipped and registered by the integration itself; no other frontend packages needed.
 *
 *   type: custom:network-fault-locator-card
 *   entity: sensor.network_fault_locator_status   # the integration's Status sensor
 *   hours_to_show: 24                              # optional
 *   title: Network                                 # optional
 *   show_device_latency: false                     # optional
 */

const DOMAIN = "network_fault_locator";
const CARD = "network-fault-locator-card";
const SVG_NS = "http://www.w3.org/2000/svg";
const REFETCH_MS = 10 * 60 * 1000;

// Where a problem is -> colour family. Degraded (packet loss) variants are drawn lighter.
const STATUS_COLORS = {
  ok: "var(--success-color, #43a047)",
  home_network: "#fb8c00",
  home_degraded: "#fb8c00",
  device_down: "#8e24aa",
  device_degraded: "#8e24aa",
  isp_line: "#e53935",
  isp_upstream: "#e53935",
  isp_down: "#e53935",
  isp_degraded: "#e53935",
  dns: "#1e88e5",
};
const DEGRADED = new Set(["home_degraded", "device_degraded", "isp_degraded"]);
const LEGEND_GROUPS = [
  ["Home network", "#fb8c00"],
  ["Local device", "#8e24aa"],
  ["ISP", "#e53935"],
  ["DNS", "#1e88e5"],
];
const PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948",
  "#b07aa1", "#ff9da7", "#9c755f", "#17becf", "#bcbd22", "#7f7f7f"];

const H = 260;  // chart height in CSS pixels; width follows the card
const PAD = { left: 44, right: 50, top: 26, bottom: 24 };

// Names and states come from users/entities: never put them into HTML unescaped
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function svg(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function niceMax(value, minimum) {
  const v = Math.max(value, minimum);
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * mag >= v) return step * mag;
  return 10 * mag;
}

/** Value of a step series at time t (seconds), or null. */
function valueAt(points, t) {
  let lo = 0, hi = points.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found < 0 ? null : points[found].v;
}

class NetworkFaultLocatorCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true,
          selector: { entity: { filter: { integration: DOMAIN, domain: "sensor" } } } },
        { name: "title", selector: { text: {} } },
        { name: "hours_to_show", selector: { number: { min: 1, max: 168, mode: "box" } } },
        { name: "show_device_latency", selector: { boolean: {} } },
      ],
      computeLabel: (s) => ({
        entity: "Status sensor",
        title: "Title",
        hours_to_show: "Hours to show",
        show_device_latency: "Show local device latency",
      })[s.name],
    };
  }

  static getStubConfig(hass) {
    const status = Object.values(hass.entities || {}).find(
      (e) => e.platform === DOMAIN && e.translation_key === "status");
    return { entity: status ? status.entity_id : "" };
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("Set 'entity' to the Network Fault Locator status sensor");
    this._config = { hours_to_show: 24, show_device_latency: false, ...config };
    this._history = null;
    this._hidden = this._hidden || new Set();
    this._lastFetch = 0;
    if (this._root) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    this._series = this._findSeries();
    const now = Date.now();
    if (!this._history || now - this._lastFetch > REFETCH_MS) {
      if (!this._fetching) this._fetch();
    } else {
      this._appendLive();
    }
    this._renderHeader();
  }

  getCardSize() { return 7; }

  getGridOptions() { return { columns: 12, min_columns: 6, rows: 6, min_rows: 5 }; }

  // ---- data -------------------------------------------------------------

  /** Related entities of the same device, by translation key. */
  _findSeries() {
    const hass = this._hass;
    const status = hass.entities?.[this._config.entity];
    const statusState = hass.states[this._config.entity];
    if (!status || !statusState) return null;
    const devName = (hass.devices?.[status.device_id]?.name_by_user
      || hass.devices?.[status.device_id]?.name || "").trim();
    const short = (eid, suffix) => {
      let name = hass.states[eid]?.attributes.friendly_name || eid;
      if (devName && name.startsWith(devName)) name = name.slice(devName.length).trim();
      return name.replace(new RegExp(`\\s*${suffix}$`, "i"), "").trim() || name;
    };
    const mine = Object.values(hass.entities).filter(
      (e) => e.device_id === status.device_id && e.platform === DOMAIN && hass.states[e.entity_id]);
    const byKey = (k) => mine.filter((e) => e.translation_key === k).map((e) => e.entity_id);

    const links = [];  // one colour per link: router, internet, then each device
    const add = (lossKey, latKey, fallback) => {
      const loss = byKey(lossKey), lat = byKey(latKey);
      loss.forEach((eid, i) => links.push({
        name: short(eid, "packet loss") || fallback, loss: eid, latency: lat[i] || null,
        device: lossKey === "device_loss",
      }));
    };
    add("router_loss", "router_latency", "Router");
    add("internet_loss", "internet_latency", "Internet");
    // Devices: pair loss and latency by the shared label
    const devLoss = byKey("device_loss"), devLat = byKey("device_latency");
    for (const eid of devLoss) {
      const name = short(eid, "packet loss");
      links.push({ name, loss: eid, device: true,
        latency: devLat.find((l) => short(l, "latency") === name) || null });
    }
    links.forEach((l, i) => { l.color = PALETTE[i % PALETTE.length]; });
    return { status: this._config.entity, links, title: devName };
  }

  _entityIds() {
    if (!this._series) return [];
    const ids = [this._series.status];
    for (const l of this._series.links) {
      ids.push(l.loss);
      if (l.latency) ids.push(l.latency);
    }
    return ids;
  }

  async _fetch() {
    const ids = this._entityIds();
    if (!ids.length) { this._render(); return; }
    this._fetching = true;
    const end = new Date();
    const start = new Date(end.getTime() - this._config.hours_to_show * 3600 * 1000);
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: ids,
        include_start_time_state: true,
        significant_changes_only: false,
        minimal_response: true,
        no_attributes: true,
      });
      const history = {};
      for (const eid of ids) {
        history[eid] = (res[eid] || []).map((s) => ({ t: s.lu ?? s.lc, s: s.s }));
      }
      this._history = history;
      this._lastFetch = Date.now();
      this._error = null;
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._fetching = false;
    }
    this._render();
  }

  /** Add the current states as new points, so the chart moves between fetches. */
  _appendLive() {
    let changed = false;
    for (const eid of this._entityIds()) {
      const st = this._hass.states[eid];
      if (!st) continue;
      const arr = (this._history[eid] = this._history[eid] || []);
      const t = new Date(st.last_updated).getTime() / 1000;
      const last = arr[arr.length - 1];
      if (!last || t > last.t) { arr.push({ t, s: st.state }); changed = true; }
    }
    if (changed || Date.now() - (this._lastDraw || 0) > 60000) this._render();
  }

  // ---- rendering --------------------------------------------------------

  _build() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        ha-card { padding: 12px 16px 8px; }
        .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .title { font-size: 1.15em; font-weight: 500; color: var(--primary-text-color); }
        .pill { border-radius: 12px; padding: 2px 10px; font-size: .85em; color: #fff; white-space: nowrap; }
        .sub { color: var(--secondary-text-color); font-size: .85em; margin-top: 2px; min-height: 1.2em; }
        .wrap { position: relative; }
        svg { width: 100%; height: auto; display: block; touch-action: pan-y; overflow: visible; }
        .axis { font-size: 11px; fill: var(--secondary-text-color); }
        .grid { stroke: var(--divider-color, rgba(127,127,127,.25)); stroke-width: 1; }
        .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: .8em;
                  color: var(--secondary-text-color); margin-top: 6px; }
        .legend span { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; user-select: none; }
        .legend span.off { opacity: .35; }
        .legend i { display: inline-block; width: 16px; height: 0; border-top: 3px solid; }
        .legend i.dash { border-top-style: dashed; }
        .legend i.band { height: 10px; border: 0; opacity: .5; }
        .tip { position: absolute; pointer-events: none; background: var(--card-background-color, #fff);
               color: var(--primary-text-color); border: 1px solid var(--divider-color, #ccc);
               border-radius: 6px; padding: 6px 8px; font-size: .78em; line-height: 1.45;
               box-shadow: 0 2px 6px rgba(0,0,0,.2); display: none; white-space: nowrap; z-index: 1; }
        .tip b { font-weight: 600; }
        .msg { color: var(--secondary-text-color); padding: 24px 0; text-align: center; }
      </style>
      <ha-card>
        <div class="head"><div class="title"></div><div class="pill"></div></div>
        <div class="sub"></div>
        <div class="wrap"><div class="chart"></div><div class="tip"></div></div>
        <div class="legend"></div>
      </ha-card>`;
    this._resize = new ResizeObserver(() => {
      const w = Math.round(this._root.querySelector(".chart").clientWidth);
      if (w && Math.abs(w - (this._drawnWidth || 0)) > 4) this._render();
    });
  }

  connectedCallback() {
    if (this._resize) this._resize.observe(this._root.querySelector(".chart"));
  }

  disconnectedCallback() {
    if (this._resize) this._resize.disconnect();
  }

  _statusText(state) {
    const st = this._hass.states[this._config.entity];
    if (!st) return state;
    if (this._hass.formatEntityState) return this._hass.formatEntityState(st, state);
    return state;
  }

  _renderHeader() {
    const r = this._root;
    const st = this._hass.states[this._config.entity];
    r.querySelector(".title").textContent = this._config.title || this._series?.title || "Network";
    const pill = r.querySelector(".pill");
    const sub = r.querySelector(".sub");
    if (!st) {
      pill.style.display = "none";
      sub.textContent = `Entity not found: ${this._config.entity}`;
      return;
    }
    pill.style.display = "";
    pill.textContent = this._statusText(st.state);
    pill.style.background = STATUS_COLORS[st.state] || "var(--disabled-color, #9e9e9e)";
    const issues = st.attributes.issues || [];
    const extra = issues.slice(1).map((i) => this._statusText(i));
    const parts = [];
    if (extra.length) parts.push(`Also: ${extra.join(", ")}`);
    if (st.attributes.isp_first_hop) parts.push(`ISP first hop ${st.attributes.isp_first_hop}`);
    sub.textContent = parts.join(" · ");
  }

  _render() {
    if (!this._root || !this._hass) return;
    this._lastDraw = Date.now();
    this._renderHeader();
    const chart = this._root.querySelector(".chart");
    const legend = this._root.querySelector(".legend");
    chart.innerHTML = "";
    legend.innerHTML = "";
    if (!this._series) {
      chart.innerHTML = `<div class="msg">Pick the Network Fault Locator status sensor.</div>`;
      return;
    }
    if (this._error) {
      chart.innerHTML = `<div class="msg">Could not load history: ${esc(this._error)}</div>`;
      return;
    }
    if (!this._history) {
      chart.innerHTML = `<div class="msg">Loading…</div>`;
      return;
    }

    // Draw at the real pixel width so text stays readable on phones
    const W = Math.max(Math.round(chart.clientWidth) || 800, 280);
    this._drawnWidth = W;
    const end = Date.now() / 1000;
    const start = end - this._config.hours_to_show * 3600;
    const x0 = PAD.left, x1 = W - PAD.right, y0 = PAD.top, y1 = H - PAD.bottom;
    const X = (t) => x0 + ((t - start) / (end - start)) * (x1 - x0);

    const num = (eid) => (this._history[eid] || []).map((p) => {
      const v = parseFloat(p.s);
      return { t: Math.max(p.t, start), v: Number.isFinite(v) ? v : null };
    });
    const links = this._series.links.map((l) => ({
      ...l,
      lossPts: num(l.loss),
      latPts: l.latency && (!l.device || this._config.show_device_latency) ? num(l.latency) : null,
    }));
    const visible = (key) => !this._hidden.has(key);

    let maxLoss = 0, maxLat = 0;
    for (const l of links) {
      if (visible(l.loss)) for (const p of l.lossPts) if (p.v != null) maxLoss = Math.max(maxLoss, p.v);
      if (l.latPts && visible(l.latency)) for (const p of l.latPts) if (p.v != null) maxLat = Math.max(maxLat, p.v);
    }
    const lossTop = niceMax(maxLoss * 1.1, 5);
    const latTop = niceMax(maxLat * 1.1, 10);
    const YL = (v) => y1 - (v / lossTop) * (y1 - y0);
    const YR = (v) => y1 - (v / latTop) * (y1 - y0);

    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, chart);

    // Status segments: strip on top + shaded bands behind the lines
    const statusPts = (this._history[this._series.status] || []).map((p) => ({ t: Math.max(p.t, start), s: p.s }));
    const segments = statusPts.map((p, i) => ({ s: p.s, a: p.t, b: i + 1 < statusPts.length ? statusPts[i + 1].t : end }));
    for (const seg of segments) {
      const color = STATUS_COLORS[seg.s] || "var(--disabled-color, #9e9e9e)";
      const xa = X(seg.a), w = Math.max(X(seg.b) - xa, 0.5);
      svg("rect", { x: xa, y: 6, width: w, height: 10, fill: color,
        "fill-opacity": DEGRADED.has(seg.s) ? 0.55 : 1 }, root);
      if (seg.s !== "ok" && STATUS_COLORS[seg.s]) {
        svg("rect", { x: xa, y: y0, width: w, height: y1 - y0, fill: color,
          "fill-opacity": DEGRADED.has(seg.s) ? 0.08 : 0.16 }, root);
      }
    }

    // Grid and axes
    for (let i = 0; i <= 4; i++) {
      const y = y0 + (i / 4) * (y1 - y0);
      svg("line", { x1: x0, x2: x1, y1: y, y2: y, class: "grid" }, root);
      svg("text", { x: x0 - 6, y: y + 4, "text-anchor": "end", class: "axis" }, root)
        .textContent = `${+(lossTop * (1 - i / 4)).toFixed(1)}%`;
      svg("text", { x: x1 + 6, y: y + 4, class: "axis" }, root)
        .textContent = `${+(latTop * (1 - i / 4)).toFixed(1)}ms`;
    }
    const hours = this._config.hours_to_show;
    const maxLabels = Math.max(2, Math.floor((x1 - x0) / 70));
    const stepH = [1, 2, 3, 4, 6, 12, 24, 48, 72].find((h) => hours / h <= maxLabels) || 168;
    const first = Math.ceil(start / (stepH * 3600)) * stepH * 3600;
    for (let t = first; t <= end; t += stepH * 3600) {
      const d = new Date(t * 1000);
      const label = stepH >= 24 ? d.toLocaleDateString([], { day: "numeric", month: "short" })
        : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      svg("text", { x: X(t), y: H - 6, "text-anchor": "middle", class: "axis" }, root).textContent = label;
    }

    // Lines: step paths, gaps where unavailable
    const path = (pts, Y) => {
      let d = "", prev = null;
      pts.forEach((p, i) => {
        const xa = X(p.t), xb = X(i + 1 < pts.length ? pts[i + 1].t : end);
        if (p.v == null) { prev = null; return; }
        const y = Y(p.v);
        d += prev == null ? `M${xa},${y}` : `V${y}`;
        d += `H${xb}`;
        prev = y;
      });
      return d;
    };
    for (const l of links) {
      if (l.latPts && visible(l.latency)) {
        svg("path", { d: path(l.latPts, YR), fill: "none", stroke: l.color, "stroke-width": 1.2,
          "stroke-dasharray": "4 3", "stroke-opacity": 0.8 }, root);
      }
    }
    for (const l of links) {
      if (visible(l.loss)) {
        svg("path", { d: path(l.lossPts, YL), fill: "none", stroke: l.color, "stroke-width": 2 }, root);
      }
    }

    // Hover: crosshair + tooltip
    const cross = svg("line", { y1: y0, y2: y1, stroke: "var(--primary-text-color)",
      "stroke-opacity": 0.4, visibility: "hidden" }, root);
    const hit = svg("rect", { x: x0, y: 0, width: x1 - x0, height: H, fill: "transparent" }, root);
    const tip = this._root.querySelector(".tip");
    const move = (ev) => {
      const box = root.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      if (px < x0 || px > x1) { hide(); return; }
      const t = start + ((px - x0) / (x1 - x0)) * (end - start);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      const seg = segments.find((s) => t >= s.a && t < s.b);
      const fmt = (v, unit) => (v == null ? "–" : `${+v.toFixed(unit === "%" ? 1 : 2)} ${unit}`);
      let html = `<b>${new Date(t * 1000).toLocaleString([], { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })}</b>`;
      html += `<br>${seg ? esc(this._statusText(seg.s)) : "–"}`;
      for (const l of links) {
        const parts = [];
        if (visible(l.loss)) parts.push(`loss ${fmt(valueAt(l.lossPts, t), "%")}`);
        if (l.latPts && visible(l.latency)) parts.push(`${fmt(valueAt(l.latPts, t), "ms")}`);
        if (parts.length) html += `<br><span style="color:${l.color}">●</span> ${esc(l.name)}: ${parts.join(", ")}`;
      }
      tip.innerHTML = html;
      tip.style.display = "block";
      const rel = (px / W) * box.width;
      tip.style.left = `${Math.min(rel + 12, box.width - tip.offsetWidth - 4)}px`;
      tip.style.top = "28px";
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", hide);

    // Legend: links (click toggles) + status colours
    const item = (label, cls, color, key) => {
      const span = document.createElement("span");
      span.innerHTML = `<i class="${cls}" style="border-color:${color};background:${cls === "band" ? color : "none"}"></i>`;
      span.appendChild(document.createTextNode(label));
      if (key) {
        if (!visible(key)) span.classList.add("off");
        span.addEventListener("click", () => {
          this._hidden.has(key) ? this._hidden.delete(key) : this._hidden.add(key);
          this._render();
        });
      }
      legend.appendChild(span);
    };
    for (const l of links) {
      item(`${l.name} loss`, "", l.color, l.loss);
      if (l.latPts) item(`${l.name} latency`, "dash", l.color, l.latency);
    }
    for (const [label, color] of LEGEND_GROUPS) item(label, "band", color, null);
  }
}

if (!customElements.get(CARD)) {
  customElements.define(CARD, NetworkFaultLocatorCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD,
    name: "Network Fault Locator",
    description: "Status, packet loss and latency of every monitored link in one chart.",
    preview: true,
    documentationURL: "https://github.com/Sagi-tab/ha-network-fault-locator#dashboard-card",
  });
}
