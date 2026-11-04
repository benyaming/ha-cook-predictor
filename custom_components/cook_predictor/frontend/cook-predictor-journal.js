// cook-predictor-journal: past cooks from the Cook Predictor journal. A list of cooks, and a
// detail view with the full chart, the events, the forecast against the actual finish, and
// an editable name and notes. Through the integration's websocket commands (any signed-in
// user may rename, annotate or delete; a deleted cook stays in its trash folder on disk).
//
//   type: custom:cook-predictor-journal
//   cook_view: /lovelace/cook      # where "Cooking now" links to
//
// The cook card embeds it (config { embedded: true }) behind its Journal button; then it has
// no frame, title or "Cooking now" link of its own. Cooks recorded before probes had names
// (schema 1) show them as Probe 1, 2, ... and their single pit as Pit.

const COLORS = ['#f97316', '#0ea5e9', '#a855f7', '#22c55e', '#ef4444', '#eab308', '#14b8a6', '#ec4899'];
const PIT_COLORS = ['#94a3b8', '#a8a29e', '#78716c', '#64748b'];
const ENDED = {
  finish: 'Finished', new_cook: 'Next cook started', unplugged: 'Probes unplugged', device_off: 'Thermometer off',
  channels_removed: 'Probes removed',
};
const DOMAIN = 'cook_predictor';

const fmtDur = (s) => {
  if (s == null) return '–';
  const m = Math.round(s / 60);
  const h = Math.floor(m / 60);
  return h ? `${h}h ${String(m % 60).padStart(2, '0')}m` : `${m} min`;
};
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
const fmtDate = (t) => new Date(t * 1000).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
const fmtDay = (t) => new Date(t * 1000).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const c100 = (v) => (v == null ? null : v / 100);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const colorOf = (key) => COLORS[(Math.max(1, parseInt(key, 10) || 1) - 1) % COLORS.length];

const STYLE = `
  :host { display: block; }
  [hidden] { display: none !important; }
  ha-card { padding: 16px; }
  .head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .title { font-size: 1.35em; font-weight: 600; margin-right: auto; }
  button, a.btn { font: inherit; color: inherit; cursor: pointer; border: 1px solid var(--divider-color);
    background: transparent; border-radius: 999px; padding: 4px 12px; text-decoration: none; }
  button.danger.armed { background: var(--error-color, #db4437); color: #fff; border-color: transparent; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  .pill { display: inline-flex; gap: 6px; align-items: center; padding: 4px 10px; border-radius: 999px;
    background: var(--secondary-background-color); font-size: .88em; }
  .live { background: color-mix(in srgb, #f97316 20%, transparent); }
  .msg { color: var(--secondary-text-color); padding: 24px 4px; text-align: center; }
  .err { color: var(--error-color, #db4437); }
  .list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .cook { border: 1px solid var(--divider-color); border-radius: 16px; padding: 12px 14px; cursor: pointer;
    display: flex; flex-direction: column; gap: 6px; }
  .cook:hover { border-color: var(--primary-color); }
  .cook.open { border-color: #f97316; }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .when { font-weight: 600; }
  .sub { color: var(--secondary-text-color); font-size: .85em; }
  .name { font-size: 1.05em; }
  .probe { display: flex; align-items: center; gap: 6px; font-size: .88em; }
  .probe i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex: none; }
  svg.spark { width: 100%; height: 46px; display: block; }
  .detail-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  .detail-head input { font: inherit; font-size: 1.25em; font-weight: 600; flex: 1; min-width: 180px;
    border: 1px solid transparent; border-radius: 8px; padding: 4px 6px; background: transparent; color: var(--primary-text-color); }
  .detail-head input:hover, .detail-head input:focus { border-color: var(--divider-color); outline: none; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 10px; }
  .chart-wrap { position: relative; }
  svg.chart { display: block; width: 100%; height: 300px; touch-action: none; }
  .chart text { fill: var(--secondary-text-color); font-size: 11px; }
  .chart .grid { stroke: var(--divider-color); }
  .tip { position: absolute; pointer-events: none; background: var(--card-background-color, #222); border: 1px solid var(--divider-color);
    border-radius: 8px; padding: 6px 8px; font-size: .8em; display: none; white-space: nowrap; box-shadow: 0 2px 8px #0003; }
  .tip div { display: flex; gap: 6px; align-items: center; }
  .tip i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: .8em; color: var(--secondary-text-color); margin: 6px 0 12px; }
  .legend i { width: 14px; height: 3px; display: inline-block; vertical-align: middle; margin-right: 4px; border-radius: 2px; }
  .cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
  h3 { font-size: 1em; margin: 8px 0 6px; }
  table { border-collapse: collapse; width: 100%; font-size: .88em; }
  td, th { padding: 4px 6px; text-align: left; border-bottom: 1px solid var(--divider-color); }
  th { color: var(--secondary-text-color); font-weight: 500; }
  .events li { margin: 3px 0; font-size: .9em; }
  .events { padding-left: 18px; margin: 0; }
  textarea { width: 100%; box-sizing: border-box; min-height: 90px; font: inherit; padding: 8px; border-radius: 8px;
    border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); }
  .actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; flex-wrap: wrap; }
  @media (max-width: 500px) { svg.chart { height: 230px; } }
`;

class CookPredictorJournal extends HTMLElement {
  setConfig(config) {
    this._config = { cook_view: '/lovelace/cook', title: 'Cook journal', ...config };
    this._view = 'list';
  }

  getCardSize() { return 10; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) { this._build(); this._load(); }
  }

  // Refresh the list every minute while it is on screen (hidden when the cook card shows its probes).
  connectedCallback() {
    this._timer = setInterval(() => this._view === 'list' && this._hass && this.offsetParent !== null && this._load(true), 60e3);
  }
  disconnectedCallback() { clearInterval(this._timer); if (this._ro) this._ro.disconnect(); }

  _ws(type, extra = {}) {
    return this._hass.callWS({ type: `${DOMAIN}/journal/${type}`, ...extra });
  }

  get _unit() {
    const u = this._hass && this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature;
    return u === '°F' ? '°F' : '°C';
  }

  _u(c) { return c == null ? null : this._unit === '°F' ? c * 9 / 5 + 32 : c; }

  _deg(c, d = 1) {
    const v = this._u(c);
    return v == null ? '–' : `${v.toFixed(d)}°`;
  }

  // Embedded (inside the cook card, which has the frame and the title): no card of its own.
  _build() {
    const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
    root.innerHTML = this._config.embedded
      ? `<style>${STYLE}</style><div class="body"></div>`
      : `<style>${STYLE}</style><ha-card><div class="body"></div></ha-card>`;
    this._body = root.querySelector('.body');
  }

  _nav(path) {
    history.pushState(null, '', path);
    window.dispatchEvent(new CustomEvent('location-changed'));
  }

  async _load(quiet) {
    if (!quiet) this._body.innerHTML = '<div class="msg">Loading the journal…</div>';
    try {
      this._list = await this._ws('list');
      if (this._view === 'list') this._renderList();
    } catch (err) {
      if (!quiet || !this._list) this._body.innerHTML = `<div class="msg err">Can't reach the cook journal: ${esc(err.message)}</div>`;
    }
  }

  // A summary's probes and pits, whatever its schema.
  _sumProbes(s) {
    return s.probes.map((p) => {
      const key = p.key ?? String(p.n);
      return { ...p, key, name: p.name || `Probe ${key}`, color: colorOf(key) };
    });
  }

  _sumPits(s) {
    const pits = s.pits || (s.pit ? { pit: { name: 'Pit', ...s.pit } } : {});
    return Object.entries(pits).map(([key, p], i) => ({ key, ...p, name: p.name || 'Pit', color: PIT_COLORS[i % PIT_COLORS.length] }));
  }

  // ---- list ----

  _renderList() {
    const { open, cooks, ready } = this._list;
    const items = (open ? [open] : []).concat(cooks);
    const embedded = this._config.embedded;
    this._body.innerHTML = `
      <div class="head"><div class="title">${esc(embedded ? 'Past cooks' : this._config.title)}</div>
        <span class="pill">${cooks.length} ${cooks.length === 1 ? 'cook' : 'cooks'}</span>
        ${open && !embedded ? `<a class="btn live-link pill live" href="${esc(this._config.cook_view)}">● Cooking now</a>` : ''}</div>
      ${ready ? '' : '<div class="msg">The journal is catching up with Home Assistant\'s history…</div>'}
      ${items.length ? '<div class="list"></div>' : '<div class="msg">No cooks yet. Each cook is saved here when it ends: New cook, Finish cook, or unplugging the probes.</div>'}`;
    const link = this._body.querySelector('.live-link');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); this._nav(this._config.cook_view); });
    const list = this._body.querySelector('.list');
    for (const s of items) {
      const el = document.createElement('div');
      el.className = `cook${s.status === 'open' ? ' open' : ''}`;
      const probes = this._sumProbes(s).map((p) => {
        const reached = p.reached != null ? ` · ${this._deg(p.target, 0)} at ${fmtTime(s.start + p.reached)}` : p.target != null ? ` · target ${this._deg(p.target, 0)}` : '';
        return `<div class="probe"><i style="background:${p.color}"></i>${esc(p.name)}: ${this._deg(p.start_temp)} → ${this._deg(p.max)}${reached}${p.stall_s >= 600 ? ` · stall ${fmtDur(p.stall_s)}` : ''}</div>`;
      }).join('');
      const pits = this._sumPits(s).filter((p) => p.avg != null);
      el.innerHTML = `
        <div class="row"><span class="when">${fmtDate(s.start)} · ${fmtTime(s.start)}</span>
          <span class="sub">${s.status === 'open' ? 'cooking' : fmtDur(s.duration_s)}</span></div>
        ${s.name ? `<div class="name">${esc(s.name)}</div>` : ''}
        ${this._spark(s)}
        ${probes}
        <div class="row sub"><span>${pits.length ? pits.map((p) => `${esc(p.name)} avg ${this._deg(p.avg, 0)}`).join(' · ') : 'No pit sensor'}</span>
          <span>${s.status === 'open' ? 'In progress' : ENDED[s.ended_by] || ''}${s.watchdog ? ' · over 36 h' : ''}</span></div>`;
      el.addEventListener('click', () => this._open(s.id));
      list.appendChild(el);
    }
  }

  _spark(s) {
    const W = 300;
    const H = 46;
    const series = [
      ...this._sumPits(s).map((p) => ({ pts: p.spark || [], color: p.color, w: 1 })),
      ...this._sumProbes(s).map((p) => ({ pts: p.spark || [], color: p.color, w: 1.6 })),
    ];
    let lo = Infinity;
    let hi = -Infinity;
    for (const x of series) for (const [, v] of x.pts) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (!Number.isFinite(lo) || !s.duration_s) return '';
    const y = (v) => H - 3 - ((v - lo) / (hi - lo || 1)) * (H - 6);
    const x = (t) => (t / s.duration_s) * W;
    const paths = series.map(({ pts, color, w }) => {
      let d = '';
      let pen = false;
      for (const [t, v] of pts) { if (v == null) { pen = false; continue; } d += `${pen ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`; pen = true; }
      return d ? `<path d="${d}" fill="none" stroke="${color}" stroke-width="${w}" vector-effect="non-scaling-stroke"/>` : '';
    }).join('');
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${paths}</svg>`;
  }

  // ---- detail ----

  async _open(id) {
    this._view = 'detail';
    this._body.innerHTML = '<div class="msg">Loading…</div>';
    try {
      // the record, and what the journal derives from it (hits, stalls, forecasts, stats)
      const r = await this._ws('get', { cook_id: id });
      this._cook = r.cook;
      this._derived = r.derived;
      this._renderDetail();
    } catch (err) {
      this._body.innerHTML = `<div class="msg err">${esc(err.message)}</div><div class="actions"><button class="back">Back</button></div>`;
      this._body.querySelector('.back').addEventListener('click', () => this._back());
    }
  }

  _back() {
    this._view = 'list';
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    this._load();
  }

  // A cook record's probes and pits, whatever its schema.
  _probesOf(k) {
    return Object.entries(k.probes).map(([key, p]) => ({
      key, p, color: colorOf(key), name: (k.channels && k.channels[key] && k.channels[key].name) || `Probe ${key}`,
    }));
  }

  _pitsOf(k) {
    const pits = k.pits || (k.pit ? { pit: k.pit } : {});
    return Object.entries(pits).map(([key, pts], i) => ({
      key, pts, color: PIT_COLORS[i % PIT_COLORS.length], name: (k.channels && k.channels[key] && k.channels[key].name) || 'Pit',
    }));
  }

  _renderDetail() {
    const k = this._cook;
    const open = k.status === 'open';
    const end = this._derived.end;
    const probes = this._probesOf(k);
    const pits = this._pitsOf(k);
    this._body.innerHTML = `
      <div class="detail-head"><button class="back">← Journal</button>
        <input class="name" maxlength="120" placeholder="Name this cook (e.g. Brisket 6 kg)" value="${esc(k.name)}"></div>
      <div class="chips">
        <span class="pill">${fmtDay(k.start)}</span>
        <span class="pill">${fmtTime(k.start)} – ${open ? 'now' : fmtTime(k.end)}</span>
        <span class="pill">${open ? 'Cooking' : fmtDur(end)}</span>
        ${open ? '' : `<span class="pill">${ENDED[k.ended_by] || ''}</span>`}
        ${k.fills && k.fills.some((f) => f[2] === 'partial') ? '<span class="pill">Part of the history could not be recovered</span>' : ''}
      </div>
      <div class="chart-wrap"><svg class="chart"></svg><div class="tip"></div></div>
      <div class="legend">${probes.map((x) => `<span><i style="background:${x.color}"></i>${esc(x.name)}</span>`).join('')}
        ${pits.map((x) => `<span><i style="background:${x.color}"></i>${esc(x.name)}</span>`).join('')}
        <span><i style="background:#a855f733;height:10px"></i>Stall</span></div>
      <div class="cols">
        <div><h3>Probes</h3><table>
          <tr><th></th><th>Start</th><th>Max</th><th>Target</th><th>Reached</th><th>Stall</th></tr>
          ${probes.map((x) => this._probeRow(k, x)).join('')}
        </table>
        <h3>Forecast vs actual</h3>${this._forecastTable(k, probes)}</div>
        <div><h3>What happened</h3><ul class="events">${this._events(k, probes, end)}</ul></div>
      </div>
      <h3>Notes</h3>
      <textarea class="notes" maxlength="5000" placeholder="Wood, rub, weight, wrap, how it turned out…">${esc(k.notes)}</textarea>
      <div class="actions">
        ${open ? '' : '<button class="danger delete">Delete</button>'}
        <button class="primary save">Save</button>
      </div>`;
    this._body.querySelector('.back').addEventListener('click', () => this._back());
    this._body.querySelector('.save').addEventListener('click', () => this._save());
    const del = this._body.querySelector('.delete');
    if (del) {
      del.addEventListener('click', async () => {
        if (!del.classList.contains('armed')) {
          del.classList.add('armed');
          del.textContent = 'Tap again to delete';
          setTimeout(() => { del.classList.remove('armed'); del.textContent = 'Delete'; }, 4000);
          return;
        }
        try { await this._ws('delete', { cook_id: k.id }); this._back(); } catch (err) { del.textContent = `Failed: ${err.message}`; }
      });
    }
    const svg = this._body.querySelector('svg.chart');
    svg.addEventListener('pointermove', (e) => this._hover(e));
    svg.addEventListener('pointerleave', () => this._hover(null));
    this._ro = new ResizeObserver(() => this._drawChart());
    this._ro.observe(this._body.querySelector('.chart-wrap'));
  }

  async _save() {
    const btn = this._body.querySelector('.save');
    const name = this._body.querySelector('input.name').value;
    const notes = this._body.querySelector('textarea.notes').value;
    btn.textContent = 'Saving…';
    try {
      await this._ws('update', { cook_id: this._cook.id, name, notes });
      Object.assign(this._cook, { name, notes });
      btn.textContent = 'Saved';
      setTimeout(() => { btn.textContent = 'Save'; }, 1500);
    } catch (err) { btn.textContent = `Failed: ${err.message}`; }
  }

  _valueAt(series, rel) {
    let v = null;
    for (const [r, x] of series) { if (r <= rel) v = x; else break; }
    return c100(v);
  }

  // Per probe, from the journal: stats { max }, reached { rel, target } (the target in force
  // at the end), hits (every target reached while in force), stalls [[from, to]], forecasts.
  _d(key) {
    return this._derived.probes[key] || { hits: [], reached: null, stalls: [], forecasts: [], stats: {} };
  }

  _probeRow(k, { key, name, color }) {
    const d = this._d(key);
    const stall = d.stalls.reduce((x, [a, b]) => x + (b - a), 0);
    const target = (k.targets[key] || []).filter(([r]) => r <= this._derived.end).at(-1)?.[1];
    return `<tr><td><span class="probe"><i style="background:${color}"></i>${esc(name)}</span></td><td>${this._deg(d.start_temp ?? k.probes[key].first)}</td>
      <td>${this._deg(d.stats.max)}</td><td>${target != null ? this._deg(target, 0) : '–'}</td>
      <td>${d.reached ? `${fmtTime(k.start + d.reached.rel)} (${fmtDur(d.reached.rel)})` : '–'}</td><td>${stall ? fmtDur(stall) : '–'}</td></tr>`;
  }

  // What the predictor said at set times before the probe reached its final target (same
  // target, same probe session), against when it did.
  _forecastTable(k, probes) {
    const rows = [];
    for (const { key, name, color } of probes) {
      for (const f of this._d(key).forecasts) {
        const off = Math.round(f.off / 60);
        rows.push(`<tr><td><span class="probe"><i style="background:${color}"></i>${esc(name)}</span></td><td>${fmtDur(f.before)} before</td>
          <td>${fmtTime(k.start + f.said)}</td><td>${off === 0 ? 'on time' : `${Math.abs(off)} min ${off > 0 ? 'late' : 'early'}`}</td></tr>`);
      }
    }
    return rows.length ? `<table><tr><th></th><th>Asked</th><th>Predicted</th><th>Off by</th></tr>${rows.join('')}</table>`
      : '<div class="sub">No forecast for a reached target in this cook.</div>';
  }

  _events(k, probes, end) {
    const ev = [[0, `Started (${k.started_by === 'new_cook' ? 'New cook' : 'probe heating'})`]];
    for (const { key, name } of probes) {
      const d = this._d(key);
      for (const [a, b] of d.stalls) ev.push([a, `${name}: stall at ${this._deg(this._valueAt(k.probes[key].series, a))}, ${fmtDur(b - a)}`]);
      for (const h of d.hits) ev.push([h.rel, `${name} reached ${this._deg(h.target, 0)}`]);
      let last = null;
      for (const [rel, g] of k.targets[key] || []) { if (rel > 0 && g !== last) ev.push([rel, `${name} target set to ${this._deg(g, 0)}`]); last = g; }
      const retired = k.retired && k.retired[key];
      if (retired != null) ev.push([retired, `${name} removed`]);
    }
    for (const f of k.fills || []) if (f[2] === 'partial') ev.push([f[0], 'Recorder history incomplete from here']);
    if (k.status !== 'open') ev.push([end, ENDED[k.ended_by] || 'Ended']);
    return ev.sort((a, b) => a[0] - b[0]).map(([rel, text]) => `<li><b>${fmtTime(k.start + rel)}</b> ${esc(text)}</li>`).join('');
  }

  _drawChart() {
    const svg = this._body && this._body.querySelector('svg.chart');
    if (!svg || !this._cook) return;
    const W = svg.clientWidth;
    const H = svg.clientHeight;
    if (!W || !H) return;
    const k = this._cook;
    const end = Math.max(60, this._derived.end);
    const M = { l: 34, r: 12, t: 10, b: 22 };
    const u = (v100) => (v100 == null ? null : this._u(v100 / 100));
    const series = [
      ...this._pitsOf(k).map((p) => ({ name: p.name, color: p.color, w: 1.5, pts: p.pts })),
      ...this._probesOf(k).map((p) => ({ name: p.name, key: p.key, color: p.color, w: 2.5, pts: p.p.series })),
    ];
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of series) for (const [, v] of s.pts) if (v != null) { lo = Math.min(lo, u(v)); hi = Math.max(hi, u(v)); }
    for (const key of Object.keys(k.probes)) for (const [, g] of k.targets[key] || []) hi = Math.max(hi, this._u(g));
    if (!Number.isFinite(lo)) { lo = 0; hi = 100; }
    lo = lo >= 0 ? Math.max(0, Math.floor((lo - 3) / 10) * 10) : Math.floor((lo - 3) / 10) * 10;
    hi = Math.ceil((hi + 3) / 10) * 10;
    const x = (rel) => M.l + (rel / end) * (W - M.l - M.r);
    const y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
    this._scale = { x, y, end, M, W, H, series };
    const parts = [];
    const yStep = hi - lo > 240 ? 50 : hi - lo > 120 ? 40 : hi - lo > 60 ? 20 : 10;
    for (let v = lo; v <= hi; v += yStep) {
      parts.push(`<line class="grid" x1="${M.l}" x2="${W - M.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${M.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}°</text>`);
    }
    const hours = end / 3600;
    const hStep = hours > 16 ? 4 : hours > 8 ? 2 : hours > 3 ? 1 : 0.5;
    const first = Math.ceil(k.start / (hStep * 3600)) * hStep * 3600;
    for (let t = first; t <= k.start + end; t += hStep * 3600) {
      parts.push(`<text x="${x(t - k.start)}" y="${H - 5}" text-anchor="middle">${fmtTime(t)}</text>`);
    }
    for (const s of series.filter((x2) => x2.key != null)) {
      for (const [a, b] of this._d(s.key).stalls) {
        parts.push(`<rect x="${x(a)}" y="${M.t}" width="${Math.max(1, x(b) - x(a))}" height="${H - M.t - M.b}" fill="#a855f7" fill-opacity=".1"/>`);
      }
      const tg = k.targets[s.key] || [];
      for (let i = 0; i < tg.length; i++) {
        const [rel, g] = tg[i];
        const to = i + 1 < tg.length ? tg[i + 1][0] : end;
        parts.push(`<line x1="${x(rel)}" x2="${x(to)}" y1="${y(this._u(g))}" y2="${y(this._u(g))}" stroke="${s.color}" stroke-opacity=".5" stroke-dasharray="1 4"/>`);
      }
    }
    for (const s of series) {
      let d = '';
      let pen = false;
      for (const [rel, v] of s.pts) {
        if (v == null) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${x(rel).toFixed(1)},${y(u(v)).toFixed(1)}`;
        pen = true;
      }
      if (d) parts.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w}" stroke-linejoin="round"/>`);
    }
    for (const s of series.filter((x2) => x2.key != null)) {
      for (const h of this._d(s.key).hits) parts.push(`<circle cx="${x(h.rel)}" cy="${y(this._u(h.target))}" r="4" fill="${s.color}"/>`);
    }
    parts.push('<line class="cursor" x1="0" x2="0" y1="0" y2="0" stroke="var(--primary-text-color)" stroke-opacity=".35" visibility="hidden"/>');
    svg.innerHTML = parts.join('');
  }

  _hover(e) {
    const tip = this._body.querySelector('.tip');
    const cursor = this._body.querySelector('.cursor');
    const sc = this._scale;
    if (!e || !sc || !cursor || !tip) {
      if (tip) tip.style.display = 'none';
      if (cursor) cursor.setAttribute('visibility', 'hidden');
      return;
    }
    const rect = this._body.querySelector('svg.chart').getBoundingClientRect();
    const px = e.clientX - rect.left;
    const rel = ((px - sc.M.l) / (sc.W - sc.M.l - sc.M.r)) * sc.end;
    if (rel < 0 || rel > sc.end) { this._hover(null); return; }
    const rows = sc.series.map((s) => {
      const v = this._valueAt(s.pts, rel);
      return v == null ? '' : `<div><i style="background:${s.color}"></i>${esc(s.name)} <b>${this._deg(v)}</b></div>`;
    }).join('');
    if (!rows) { this._hover(null); return; }
    for (const [a, v] of [['x1', px], ['x2', px], ['y1', sc.M.t], ['y2', sc.H - sc.M.b]]) cursor.setAttribute(a, v);
    cursor.setAttribute('visibility', 'visible');
    tip.innerHTML = `<div><b>${fmtTime(this._cook.start + rel)}</b>&nbsp;<span class="sub">${fmtDur(rel)} in</span></div>${rows}`;
    tip.style.display = 'block';
    tip.style.left = `${px + 12 + tip.offsetWidth > sc.W ? px - 12 - tip.offsetWidth : px + 12}px`;
    tip.style.top = '8px';
  }
}

if (!customElements.get('cook-predictor-journal')) customElements.define('cook-predictor-journal', CookPredictorJournal);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === 'cook-predictor-journal')) {
  window.customCards.push({ type: 'cook-predictor-journal', name: 'Cook Predictor journal', description: 'Past cooks: charts, forecasts, notes' });
}
