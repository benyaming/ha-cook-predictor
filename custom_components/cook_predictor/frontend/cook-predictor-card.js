// cook-predictor-card: the Cook Predictor integration's dashboard card. One tile per food
// probe (ring, temperature, ETA, target), a chip per cooker's ambient sensor and per
// thermometer battery, and a chart of every probe and pit with the predicted curve to each
// finish (in a stall, two: at the current pace, and if the stall ended now). The chart zooms
// (pinch, Ctrl/⌘ + wheel, drag to move, chips for the whole cook or its last hours) and the
// legend hides lines; the temperature axis fits what is in view. The bell under a target
// opens the probe's alert settings: the heads-up before the target, and a second
// temperature to alert at with its own heads-up. The Journal button shows past cooks.
//
//   type: custom:cook-predictor-card
//   title: Cook            # optional
//
// Everything else comes from the integration: which sensors are probes and pits, their
// names, the setting entities, the series (the websocket subscription cook_predictor/subscribe).
// Temperatures arrive in °C and show in the user's unit; settings are number entities, which
// Home Assistant already shows in that unit.

const VERSION = new URL(import.meta.url).search;
// the journal view, loaded with this file's version so a browser never mixes old and new
import(new URL(`./cook-predictor-journal.js${VERSION}`, import.meta.url).href);

const COLORS = ['#f97316', '#0ea5e9', '#a855f7', '#22c55e', '#ef4444', '#eab308', '#14b8a6', '#ec4899'];
const PIT_COLORS = ['#94a3b8', '#a8a29e', '#78716c', '#64748b'];
const MIN_SPAN = 20 * 60e3; // the chart zooms in to 20 minutes at most
const THUMB_MIN = 40; // px: the scrollbar thumb's least width, grips included
const DOMAIN = 'cook_predictor';

// Settings per probe. tap: what − and + change, in the shown unit. off: the row's Off button;
// heads-ups are minutes where 0 is off (below `below` writes 0, + from off jumps to `jump`);
// the second temperature has a switch of its own, so its Off turns that off.
const FIELDS = [
  { key: 'target', tap: 1 },
  { key: 'warn_min', tap: 5, off: { below: 5, jump: 5 } },
  { key: 'alert_temp', tap: 1, switch: true },
  { key: 'alert_min', tap: 5, off: { below: 5, jump: 5 } },
];
const stepper = (key, label, unit = '') => `
  <div class="stepper" data-f="${key}"><button class="minus" aria-label="Lower ${label}">−</button>
    <input type="number" inputmode="decimal" aria-label="${label}">${unit}
    <button class="plus" aria-label="Raise ${label}">+</button></div>`;

const STATUS = {
  unplugged: 'Not connected',
  warming: 'Warming up',
  predicting: 'Predicting',
  stall: 'Stall',
  unstable: 'Unsettled',
  stale: 'No reading',
  done: 'Done',
  no_target: 'No target',
};
const REASON = {
  'ambient below target': 'Pit is cooler than the target',
  'ambient dropped': 'Pit temperature dropped',
  'probe moved': 'Probe moved, re-reading',
  'no ambient probe': 'No pit sensor, using the trend',
  'fit failed': 'Rough estimate',
  'not rising': 'Not rising',
  'no recent reading': 'No report from the probe for 10 min',
  'waiting for a reading': 'Waiting for the next reading',
  'readings resumed': 'Readings resumed, re-reading',
  'pit readings resumed': 'Pit sensor back, using the trend for now',
  'probe unavailable': 'The sensor is unavailable',
  'loading history': 'Loading the cook so far…',
};

const fmtDur = (min) => {
  if (min == null) return '–';
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m} min`;
};
const fmtTime = (t) => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

const STYLE = `
  :host { display: block; }
  [hidden] { display: none !important; }
  ha-card { padding: 16px; overflow: hidden; }
  .head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
  .title { font-size: 1.35em; font-weight: 600; margin-right: auto; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
          background: var(--secondary-background-color); font-size: .9em; white-space: nowrap; }
  .pill b { font-weight: 600; }
  .pill.battery { cursor: pointer; gap: 4px; }
  .pill.battery ha-icon { --mdc-icon-size: 18px; }
  .pill.elapsed { gap: 4px; }
  .pill.elapsed ha-icon { --mdc-icon-size: 16px; color: var(--secondary-text-color); }
  button.journal[aria-pressed="true"] { background: var(--secondary-background-color); }
  button.sensors { width: 32px; height: 32px; padding: 0; display: inline-flex; align-items: center; justify-content: center; }
  button.sensors ha-icon { --mdc-icon-size: 18px; }
  .pill.battery:hover, .pill.battery:focus-visible { outline: 1px solid var(--divider-color); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
  button { font: inherit; color: inherit; cursor: pointer; border: 1px solid var(--divider-color);
           background: transparent; border-radius: 999px; padding: 4px 12px; }
  button.armed { background: var(--error-color, #db4437); border-color: transparent; color: #fff; }
  button.done { background: #22c55e; border-color: transparent; color: #fff; cursor: default; }
  button.busy { opacity: .7; cursor: progress; }
  button.failed { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
  .pill.elapsed.finished ha-icon { color: #22c55e; }
  .notice { color: var(--secondary-text-color); padding: 24px 4px; text-align: center; }
  .probes { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
  .tile { border: 1px solid var(--divider-color); border-radius: 16px; padding: 14px; display: flex;
          flex-direction: column; align-items: center; gap: 8px; position: relative; }
  .tile.off { opacity: .55; }
  .tile.off .ring-wrap, .tile.off .eta, .tile.off .target, .tile.off .meta, .tile.off .alert-note,
  .tile.off .bell, .tile.off .alerts { display: none; }
  .tile-head { width: 100%; display: flex; justify-content: space-between; align-items: center; gap: 6px; }
  .name { font-weight: 600; display: flex; align-items: center; gap: 6px; min-width: 0; }
  .name span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name i { width: 10px; height: 10px; border-radius: 50%; background: var(--c); display: inline-block; flex: none; }
  .cooker { font-size: .78em; color: var(--secondary-text-color); margin-top: -6px; }
  .chip { font-size: .78em; padding: 2px 8px; border-radius: 999px; background: var(--secondary-background-color);
          color: var(--secondary-text-color); white-space: nowrap; }
  .chip.predicting { background: color-mix(in srgb, var(--c) 18%, transparent); color: var(--primary-text-color); }
  .chip.stall { background: #a855f733; color: var(--primary-text-color); }
  .chip.done { background: #22c55e; color: #fff; }
  .chip.unstable { background: #ef444433; color: var(--primary-text-color); }
  .ring-wrap { position: relative; width: 150px; height: 150px; }
  .ring { width: 100%; height: 100%; transform: rotate(-90deg); }
  .ring circle { fill: none; stroke-width: 10; }
  .ring .track { stroke: var(--divider-color); }
  .ring .arc { stroke: var(--c); stroke-linecap: round; transition: stroke-dashoffset .6s; }
  .ring-text { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
               justify-content: center; }
  .temp { font-size: 2.1em; font-weight: 700; letter-spacing: -.02em; }
  .of { color: var(--secondary-text-color); font-size: .9em; }
  .eta { text-align: center; min-height: 3.2em; }
  .eta-main { font-size: 1.5em; font-weight: 600; }
  .eta-main.low { opacity: .6; }
  .eta-sub { color: var(--secondary-text-color); font-size: .88em; }
  .hint { font-size: .78em; color: #a855f7; min-height: 1em; text-align: center; }
  .stepper { display: flex; align-items: center; gap: 6px; }
  .stepper button { width: 32px; height: 32px; padding: 0; font-size: 1.1em; }
  .stepper input { width: 64px; text-align: center; font: inherit; font-size: 1.05em; padding: 4px;
                   border: 1px solid var(--divider-color); border-radius: 8px; background: transparent;
                   color: var(--primary-text-color); }
  .stepper span { color: var(--secondary-text-color); font-size: .85em; }
  .alert-note { font-size: .8em; color: var(--secondary-text-color); text-align: center; }
  .alert-note:empty { display: none; }
  .bell { display: inline-flex; align-items: center; gap: 6px; font-size: .8em; padding: 3px 10px;
          color: var(--secondary-text-color); }
  .bell ha-icon { --mdc-icon-size: 16px; }
  .bell[aria-expanded="true"] { color: var(--primary-text-color); }
  .alerts { width: 100%; display: flex; flex-direction: column; gap: 6px; padding-top: 4px;
            border-top: 1px solid var(--divider-color); }
  .alerts .row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .alerts .lbl { font-size: .82em; color: var(--secondary-text-color); }
  .alerts .stepper { gap: 4px; margin-left: auto; }
  .alerts .stepper button { width: 28px; height: 28px; font-size: 1em; }
  .alerts .stepper input { width: 52px; font-size: .95em; padding: 3px; }
  .alerts .off { height: 28px; padding: 0 8px; font-size: .78em; border-radius: 8px; color: var(--secondary-text-color); }
  .alerts .off[hidden] { display: inline-block !important; visibility: hidden; } /* keeps the steppers lined up */
  .meta { color: var(--secondary-text-color); font-size: .82em; min-height: 1em; }
  .chart-bar { display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 14px; }
  .ranges, .zoom { display: flex; gap: 6px; }
  .ranges button { font-size: .8em; padding: 2px 10px; color: var(--secondary-text-color); }
  .ranges button[aria-pressed="true"] { background: var(--secondary-background-color); color: var(--primary-text-color); }
  .zoom button { width: 30px; height: 26px; padding: 0; font-size: 1.05em; line-height: 1; }
  .chart-bar button:disabled { opacity: .35; cursor: default; }
  .chart-wrap { position: relative; margin-top: 6px; }
  .scroll { padding: 6px 0; touch-action: none; cursor: pointer; outline: none; }
  .scroll .track { position: relative; height: 12px; border-radius: 6px; background: var(--secondary-background-color); }
  .scroll .thumb { position: absolute; top: 0; bottom: 0; border-radius: 6px; cursor: grab;
                   background: color-mix(in srgb, var(--primary-text-color) 30%, transparent); }
  .scroll:focus-visible .thumb { outline: 2px solid var(--primary-color, #03a9f4); }
  /* the grips take the outer 8 px of the thumb and 10 px beyond it; the thumb is at least
     THUMB_MIN wide, so its middle (at least 24 px) always pans */
  .scroll .grip { position: absolute; top: -6px; bottom: -6px; width: 18px; cursor: ew-resize; }
  .scroll .grip.l { left: -10px; }
  .scroll .grip.r { right: -10px; }
  .scroll .grip::after { content: ''; position: absolute; top: 9px; bottom: 9px; width: 2px; border-radius: 1px;
                         background: var(--card-background-color, #fff); opacity: .8; }
  .scroll .grip.l::after { left: 13px; }
  .scroll .grip.r::after { left: 3px; }
  svg.chart { display: block; width: 100%; height: 280px; touch-action: none; }
  .chart text { fill: var(--secondary-text-color); font-size: 11px; }
  .chart .grid { stroke: var(--divider-color); stroke-width: 1; }
  .chart .now { stroke: var(--secondary-text-color); stroke-dasharray: 2 3; }
  .tip { position: absolute; pointer-events: none; background: var(--card-background-color, #222);
         border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px 8px; font-size: .8em;
         display: none; white-space: nowrap; box-shadow: 0 2px 8px #0003; }
  .tip div { display: flex; gap: 6px; align-items: center; }
  .tip i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: .8em; color: var(--secondary-text-color);
            margin-top: 6px; }
  .legend i { width: 14px; height: 3px; display: inline-block; vertical-align: middle; margin-right: 4px; border-radius: 2px; }
  .legend i.mark { width: 10px; height: 10px; box-sizing: border-box; border-radius: 50%; }
  .legend .toggle { cursor: pointer; user-select: none; }
  .legend .toggle.off { opacity: .45; text-decoration: line-through; }
  @media (max-width: 500px) { svg.chart { height: 220px; } .ring-wrap { width: 130px; height: 130px; } }
`;

class CookPredictorCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  static getConfigElement() {
    return document.createElement('cook-predictor-card-editor');
  }

  setConfig(config) {
    this._config = { hours: 30, ...config };
    this._hidden = this._hidden || new Set();
    this._view = null;
    this._built = null;
    // A display-option change (from the visual editor) must re-render against the last
    // snapshot, since no new stream event is coming just because the card config changed.
    if (this._cfg && this._hass) this._setConfig(this._cfg);
  }

  getCardSize() {
    return 12;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._hass && this._cfg && this._update(), 30000);
    // Detaching stops the resize observer; a card re-attached with the same config keeps its
    // build (see _setConfig), so the chart needs it back here, not from _build.
    const wrap = this._ro && this._built && this.shadowRoot && this.shadowRoot.querySelector('.chart-wrap');
    if (wrap) this._ro.observe(wrap);
    if (this._hass) this._subscribe();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    if (this._ro) this._ro.disconnect();
    this._unsubscribe();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._journal) this._journal.hass = hass;
    this._subscribe();
    if (this._cfg && this._built) this._update();
  }

  // ---- the integration's stream ----

  async _subscribe() {
    if (this._sub || !this._hass || !this.isConnected) return;
    this._sub = 'pending';
    try {
      const unsub = await this._hass.connection.subscribeMessage((m) => this._onStream(m), { type: `${DOMAIN}/subscribe` });
      if (this._sub !== 'pending') { unsub(); return; } // disconnected meanwhile
      this._sub = unsub;
    } catch (err) {
      this._sub = null;
      this._cfg = null;
      this._notice(err && err.code === 'not_loaded' || err && err.code === 'unknown_command'
        ? 'Cook Predictor is not set up. Add it under Settings › Devices & services.'
        : `Cannot reach Cook Predictor: ${esc(err && err.message || err)}`);
      clearTimeout(this._retry);
      this._retry = setTimeout(() => this._subscribe(), 30000);
    }
  }

  _unsubscribe() {
    clearTimeout(this._retry);
    if (typeof this._sub === 'function') this._sub();
    this._sub = null;
  }

  // A snapshot replaces everything; points and series follow it in order. Another generation
  // (the integration reloaded) or a gap in the sequence: subscribe again for a new snapshot.
  _onStream(m) {
    if (m.type === 'closed') { this._unsubscribe(); setTimeout(() => this._subscribe(), 1000); return; }
    if (m.type === 'snapshot') {
      this._gen = m.generation;
      this._seq = m.seq;
      this._series = {};
      for (const [k, pts] of Object.entries(m.series || {})) this._series[k] = pts.map(([t, v]) => [t * 1000, v]);
      this._setConfig(m.config);
      return;
    }
    if (m.generation !== this._gen || m.seq !== this._seq + 1) {
      this._unsubscribe();
      this._subscribe();
      return;
    }
    this._seq = m.seq;
    if (m.type === 'points') {
      for (const [k, pts] of Object.entries(m.points)) {
        const s = (this._series[k] = this._series[k] || []);
        for (const [t, v] of pts) s.push([t * 1000, v]);
      }
      if (this._built) this._update();
    } else if (m.type === 'series') {
      for (const [k, pts] of Object.entries(m.series)) this._series[k] = pts.map(([t, v]) => [t * 1000, v]);
      if (this._built) this._drawChart();
    }
  }

  // Channel keys the card config hides (chosen in the visual editor).
  _hiddenChannels() {
    return new Set((this._config.hidden_channels || []).map((k) => String(k)));
  }

  _setConfig(cfg) {
    this._cfg = cfg;
    // Every subscribe gets a snapshot, so the same config comes back on each resubscribe (a
    // websocket reconnect, the card re-attached, a gap in the stream). The tiles, their
    // handlers and each probe's _fields are bound to the objects derived below, so an
    // identical config keeps them: fresh objects without a rebuild have no _fields, _update
    // throws, and Home Assistant swaps the card for "Configuration error". Any difference
    // (the whole config, entity ids from the registry included, or a display option) rebuilds.
    const shape = JSON.stringify(cfg) + this._gen
      + JSON.stringify([this._config.title, this._config.hidden_channels, this._config.show_journal,
        this._config.show_actions, this._config.show_settings, this._config.show_batteries, this._config.show_chart]);
    if (this._built === shape) {
      this._update();
      return;
    }
    const hidden = this._hiddenChannels();
    const shown = (c) => !hidden.has(String(c.key));
    const active = cfg.channels.filter((c) => c.active !== false);
    const color = (c) => COLORS[(Math.max(1, parseInt(c.key, 10) || 1) - 1) % COLORS.length];
    // Resolve a probe's cooker against ALL active ambients, so hiding a cooker chip never makes
    // its probes read "no pit sensor"; the hidden set only filters what is DISPLAYED.
    const allAmbients = active.filter((c) => c.role === 'ambient');
    // Based on all active channels, not the displayed subset, so hiding a chip keeps the probe
    // captions and never makes a hidden-everything card look like nothing is set up.
    this._activeCookers = allAmbients.length;
    this._hasInternal = active.some((c) => c.role === 'internal');
    // All active food probes, including ones hidden from this card, so the cook's start time
    // (and thus the chart's "Whole cook" range) never shrinks just because a probe is hidden.
    this._allProbes = active.filter((c) => c.role === 'internal');
    const ambients = allAmbients.filter(shown);
    this._pits = ambients.map((c, i) => ({ ...c, color: PIT_COLORS[i % PIT_COLORS.length] }));
    this._probes = active.filter((c) => c.role === 'internal' && shown(c)).map((c) => ({
      ...c, color: color(c), pit: allAmbients.find((a) => a.key === c.ambient) || null,
    }));
    // removed lately: a line on the chart for the cook they were in, no tile
    this._retired = cfg.channels.filter((c) => c.active === false && shown(c)).map((c, i) => ({
      ...c, color: c.role === 'internal' ? color(c) : PIT_COLORS[(ambients.length + i) % PIT_COLORS.length],
    }));
    this._build(shape);
    this._update();
  }

  // ---- units: °C inside, the user's unit on screen ----

  get _unit() {
    const u = this._hass && this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature;
    return u === '°F' ? '°F' : '°C';
  }

  _u(c) {
    return c == null ? null : this._unit === '°F' ? c * 9 / 5 + 32 : c;
  }

  _du(c) { // a rate or a difference
    return c == null ? null : this._unit === '°F' ? c * 9 / 5 : c;
  }

  _fmt(c) {
    const v = this._u(c);
    return v == null ? '–' : `${v.toFixed(1)}°`;
  }

  _notice(html) {
    const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
    this._built = null;
    root.innerHTML = `<style>${STYLE}</style><ha-card><div class="head"><div class="title">${esc(this._config.title || 'Cook')}</div></div><div class="notice">${html}</div></ha-card>`;
  }

  _build(shape) {
    // a rebuild starts with the probes showing and no journal; pending mounts are void
    this._buildGen = (this._buildGen || 0) + 1;
    this._journal = null;
    this._journalOpen = false;
    this._journalWait = null;
    const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
    const cfg = this._cfg;
    root.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title"></div>
          ${this._pits.map((p) => `<span class="pill amb" data-key="${esc(p.key)}" style="--c:${p.color}"><span class="dot"></span>${esc(p.name)} <b class="amb-val">–</b></span>`).join('')}
          ${cfg.batteries.map((b, i) => `<span class="pill battery" data-i="${i}" role="button" tabindex="0"><ha-icon icon="mdi:battery"></ha-icon><b class="bat-val">–</b></span>`).join('')}
          <span class="pill elapsed" hidden title="Since the cook started"><ha-icon icon="mdi:timer-outline"></ha-icon><b class="elapsed-val"></b></span>
          <button class="journal" title="Past cooks" aria-pressed="false">Journal</button>
          <button class="finish" title="End this cook and save it to the journal">Finish cook</button>
          <button class="reset" title="Start a new cook on every plugged probe">New cook</button>
          <button class="sensors" title="Sensors: add, rename or remove" aria-label="Sensors"><ha-icon icon="mdi:cog-outline"></ha-icon></button>
        </div>
        <div class="cook-view">
          <div class="probes"></div>
          <div class="chart-bar">
            <div class="zoom"><button class="zoom-out" aria-label="Zoom out" title="Zoom out">−</button><button class="zoom-in" aria-label="Zoom in" title="Zoom in">+</button></div>
            <div class="ranges" title="Pinch or Ctrl + scroll to zoom, drag to move">
              <button data-range="all">Whole cook</button><button data-range="3">Last 3 h</button><button data-range="1">Last hour</button></div>
          </div>
          <div class="chart-wrap"><svg class="chart"></svg><div class="tip"></div></div>
          <div class="scroll" role="scrollbar" aria-orientation="horizontal" aria-label="Move through the cook"
            aria-valuemin="0" aria-valuemax="100" tabindex="0"><div class="track"><div class="thumb"><span class="grip l"></span><span class="grip r"></span></div></div></div>
          <div class="legend"></div>
        </div>
        <div class="journal-wrap" hidden></div>
      </ha-card>`;
    root.querySelector('.title').textContent = this._config.title || cfg.title || 'Cook';
    if (!this._hasInternal) {
      // Genuinely no food probe configured (not merely all hidden by the card config).
      root.querySelector('.probes').outerHTML = `<div class="notice">No food probe yet. ${this._hass.user && this._hass.user.is_admin
        ? 'Add one with the gear button: Add sensor, then pick it as Internal.' : 'Ask an administrator to add one.'}</div>`;
    }

    // Two-tap buttons: the first arms for 4 s, the second presses the button entity and shows
    // `busy` until Home Assistant answers, then for 3 s what came of it. The integration
    // answers once the press is on disk; the card then waits (up to 10 s) for the journal
    // to show it before it says the cook finished or started, and otherwise says the press
    // is recorded and the journal will catch up.
    const twoTap = (btn, entity, label, armed, busy, ok, kind) => {
      if (!entity) { btn.hidden = true; return; }
      const show = (cls, text, ms) => {
        btn.className = `${btn.className.split(' ')[0]} ${cls}`.trim();
        btn.textContent = text;
        btn.disabled = cls === 'busy';
        clearTimeout(btn._arm);
        if (ms) btn._arm = setTimeout(() => { show('', label); this._update(); }, ms);
      };
      btn.addEventListener('click', async () => {
        if (btn.classList.contains('busy') || btn.classList.contains('done')) return;
        if (!btn.classList.contains('armed')) { show('armed', armed, 4000); return; }
        show('busy', busy);
        const before = this._journalMark(kind);
        const pressedBefore = this._hass.states[entity] && this._hass.states[entity].state;
        try {
          await this._hass.callService('button', 'press', { entity_id: entity });
        } catch (err) {
          show('failed', 'Did not go through: tap again', 5000);
          return;
        }
        const stamp = await this._pressStamp(entity, pressedBefore);
        const shown = stamp != null && (await this._journalShows(kind, stamp, before));
        show('done', shown ? ok : 'Recorded; the journal will catch up', 3000);
      });
    };
    twoTap(root.querySelector('.reset'), cfg.buttons.new_cook, 'New cook', 'Tap again to restart', 'Starting…', 'New cook started ✓', 'new_cook');
    twoTap(root.querySelector('.finish'), cfg.buttons.finish, 'Finish cook', 'Tap again to finish', 'Finishing…', 'Finished ✓', 'finish');
    root.querySelector('.journal').addEventListener('click', () => this._toggleJournal());
    const sensors = root.querySelector('.sensors');
    sensors.hidden = !(this._hass.user && this._hass.user.is_admin);
    sensors.addEventListener('click', () => this._navigate(`/config/integrations/integration/${DOMAIN}`));

    // A battery chip opens its thermometer's device page.
    for (const chip of root.querySelectorAll('.battery')) {
      const b = cfg.batteries[Number(chip.dataset.i)];
      const open = () => this._navigate(`/config/devices/device/${b.device_id}`);
      chip.addEventListener('click', open);
      chip.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    }

    const wrap = root.querySelector('.probes');
    this._tiles = this._probes.map((p) => {
      const el = document.createElement('div');
      el.className = 'tile';
      el.style.setProperty('--c', p.color);
      el.innerHTML = `
        <div class="tile-head"><span class="name"><i></i><span></span></span><span class="chip"></span></div>
        ${this._activeCookers > 1 ? `<div class="cooker">${p.pit ? `in ${esc(p.pit.name)}` : 'no pit sensor'}</div>` : ''}
        <div class="ring-wrap">
          <svg class="ring" viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="52"/>
            <circle class="arc" cx="60" cy="60" r="52" stroke-dasharray="326.7" stroke-dashoffset="326.7"/></svg>
          <div class="ring-text"><div class="temp">–</div><div class="of"></div></div>
        </div>
        <div class="eta"><div class="eta-main"></div><div class="eta-sub"></div></div>
        <div class="hint"></div>
        <div class="target">${stepper('target', 'target temperature', `<span class="unit"></span>`)}</div>
        <div class="alert-note"></div>
        <button class="bell" aria-expanded="false" title="Alerts for this probe">
          <ha-icon icon="mdi:bell-outline"></ha-icon><span></span></button>
        <div class="alerts" hidden>
          <div class="row" data-row="warn_min"><span class="lbl">Heads-up before target, min</span>
            ${stepper('warn_min', 'minutes before the target')}<button class="off" aria-label="Turn off the heads-up before the target">Off</button></div>
          <div class="row" data-row="alert_temp"><span class="lbl">Also alert at, <span class="unit"></span></span>
            ${stepper('alert_temp', 'second alert temperature')}<button class="off" aria-label="Turn off the second alert">Off</button></div>
          <div class="row" data-row="alert_min"><span class="lbl">Heads-up before it, min</span>
            ${stepper('alert_min', 'minutes before the second alert')}<button class="off" aria-label="Turn off the heads-up before the second alert">Off</button></div>
        </div>
        <div class="meta"></div>`;
      el.querySelector('.name span').textContent = p.name;
      for (const u of el.querySelectorAll('.unit')) u.textContent = this._unit;
      p._fields = {};
      for (const f of FIELDS) {
        const box = el.querySelector(`[data-f="${f.key}"]`);
        const row = el.querySelector(`[data-row="${f.key}"]`) || box.parentElement;
        const entity = p.numbers && p.numbers[f.key];
        if (!entity) { row.hidden = true; continue; }
        const field = { ...f, entity, input: box.querySelector('input'), pending: false, debounce: null };
        if (f.off || f.switch) field.input.placeholder = 'off';
        p._fields[f.key] = field;
        const set = (v) => {
          if (!Number.isFinite(v)) return;
          const s = this._hass.states[field.entity];
          const lo = s ? Number(s.attributes.min) : -Infinity;
          const hi = s ? Number(s.attributes.max) : Infinity;
          const step = (s && Number(s.attributes.step)) || 1;
          if (f.off && v < f.off.below) v = 0;
          v = f.off && v === 0 ? 0 : Math.min(hi, Math.max(lo, Math.round(v / step) * step));
          field.shownOff = false;
          this._show(field, v);
          field.pending = true; // keep the typed value until HA reports it back
          clearTimeout(field.debounce);
          field.debounce = setTimeout(async () => {
            try {
              if (f.switch && !this._isOn(p)) await this._hass.callService('switch', 'turn_on', { entity_id: p.alert_on });
              await this._hass.callService('number', 'set_value', { entity_id: field.entity, value: v });
            } finally { field.pending = false; }
          }, 500);
          this._update();
        };
        const turnOff = () => {
          clearTimeout(field.debounce);
          if (f.switch) {
            field.shownOff = true;
            this._show(field, NaN);
            field.pending = true;
            this._hass.callService('switch', 'turn_off', { entity_id: p.alert_on }).finally(() => { field.pending = false; });
            this._update();
          } else set(0);
        };
        const cur = () => this._value(field, p);
        field.input.addEventListener('change', () => (field.input.value === '' && (f.off || f.switch) ? turnOff() : set(parseFloat(field.input.value))));
        box.querySelector('.minus').addEventListener('click', () => {
          if (f.switch && !this._fieldOn(field, p)) return;
          if (f.switch && cur() - f.tap < this._num(field.entity, 'min')) { turnOff(); return; }
          set(cur() - f.tap);
        });
        box.querySelector('.plus').addEventListener('click', () => {
          if (f.switch && !this._fieldOn(field, p)) { set(cur()); return; } // on, at the value it had
          set(f.off && !cur() ? f.off.jump : cur() + f.tap);
        });
        const off = row.querySelector('.off');
        if (off) off.addEventListener('click', turnOff);
      }
      const bell = el.querySelector('.bell');
      const panel = el.querySelector('.alerts');
      bell.hidden = !['warn_min', 'alert_temp', 'alert_min'].some((k) => p._fields[k]);
      bell.addEventListener('click', () => {
        panel.hidden = !panel.hidden;
        bell.setAttribute('aria-expanded', String(!panel.hidden));
      });
      wrap.appendChild(el);
      return el;
    });

    // Tapping a probe or a pit in the legend hides its line (and a probe's forecast), and
    // the temperature axis refits to what is left.
    const legend = root.querySelector('.legend');
    const toggle = (id, name, color) => `<span class="toggle${this._hidden.has(id) ? ' off' : ''}" data-id="${esc(id)}" role="button" tabindex="0"
      aria-pressed="${!this._hidden.has(id)}" title="Show or hide ${esc(name)}"><i style="background:${color}"></i>${esc(name)}</span>`;
    legend.innerHTML = this._probes.map((p) => toggle(p.key, p.name, p.color)).join('')
      + this._pits.map((p) => toggle(p.key, p.name, p.color)).join('')
      + this._retired.map((p) => toggle(p.key, `${p.name} (removed)`, p.color)).join('')
      + '<span><i style="background:repeating-linear-gradient(90deg,currentColor 0 4px,transparent 4px 7px)"></i>Forecast</span>'
      + '<span class="stall-key" hidden><i class="mark" style="border:2px solid currentColor"></i>If the stall ends now</span>'
      + '<span class="stall-key" hidden><i class="mark" style="background:currentColor"></i>At current pace</span>';

    const flip = (el) => {
      const id = el.dataset.id;
      if (this._hidden.has(id)) this._hidden.delete(id); else this._hidden.add(id);
      el.classList.toggle('off', this._hidden.has(id));
      el.setAttribute('aria-pressed', String(!this._hidden.has(id)));
      this._hover(null);
      this._drawChart();
    };
    legend.addEventListener('click', (e) => { const el = e.target.closest('.toggle'); if (el) flip(el); });
    legend.addEventListener('keydown', (e) => {
      const el = e.target.closest('.toggle');
      if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); flip(el); }
    });
    root.querySelector('.ranges').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      this._view = b.dataset.range === 'all' ? null : { last: Number(b.dataset.range) * 3600e3 };
      this._drawChart();
    });

    // − and + zoom around now while it is in view, otherwise around the middle.
    const zoomBy = (factor) => {
      const sc = this._scale;
      if (!sc) return;
      const now = Date.now();
      this._zoomAt(sc.x(now >= sc.t0 && now <= sc.t1 ? now : (sc.t0 + sc.t1) / 2), factor);
    };
    root.querySelector('.zoom-in').addEventListener('click', () => zoomBy(0.5));
    root.querySelector('.zoom-out').addEventListener('click', () => zoomBy(2));
    this._bindScroll(root.querySelector('.scroll'));

    const svg = root.querySelector('svg.chart');
    this._bindChart(svg);
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => this._drawChart());
    this._ro.observe(root.querySelector('.chart-wrap'));

    // Visual-editor display toggles: hide whatever the card config turns off. These only ever
    // hide (never reveal), so a button already hidden for lack of an entity stays hidden.
    const hide = (sel) => root.querySelectorAll(sel).forEach((el) => { el.hidden = true; });
    if (this._config.show_journal === false) hide('.journal');
    if (this._config.show_actions === false) { hide('.finish'); hide('.reset'); }
    if (this._config.show_settings === false) hide('.sensors');
    if (this._config.show_batteries === false) hide('.battery');
    if (this._config.show_chart === false) { hide('.chart-bar'); hide('.chart-wrap'); hide('.scroll'); hide('.legend'); }

    this._built = shape;
  }

  _navigate(path) {
    history.pushState(null, '', path);
    window.dispatchEvent(new CustomEvent('location-changed'));
  }

  // What the journal sensor said about the last press of this kind before a new one: a new
  // acknowledgment must differ from it.
  _journalMark(kind) {
    const js = this._cfg.journal && this._hass.states[this._cfg.journal];
    const a = (js && js.attributes) || {};
    return kind === 'finish' ? a.press_at || null : a.start || null;
  }

  // The press's own time: Home Assistant stamps the button entity with it before the
  // integration takes the press (the integration's press is at that moment too). Up to 5 s.
  async _pressStamp(entity, before) {
    const until = Date.now() + 5000;
    while (Date.now() < until) {
      const s = this._hass.states[entity];
      if (s && s.state !== before && Number.isFinite(Date.parse(s.state))) return Date.parse(s.state);
      await new Promise((r) => setTimeout(r, 250));
    }
    return null;
  }

  // Whether the journal sensor shows this very press (stamp: its time, ms) within 10 s: a
  // cook ended by Finish with that press (its end may be earlier, at the last reading), or
  // a cook started by New cook at that press; new since `before`, and within 2 s of the
  // stamp, so neither an earlier press nor another start or end nearby confirms it.
  async _journalShows(kind, stamp, before) {
    const until = Date.now() + 10000;
    const same = (iso) => iso && iso !== before && Math.abs(Date.parse(iso) - stamp) < 2000;
    while (Date.now() < until) {
      const js = this._cfg.journal && this._hass.states[this._cfg.journal];
      const a = (js && js.attributes) || {};
      if (kind === 'finish' && js && js.state === 'finished' && a.ended_by === 'finish' && same(a.press_at)) return true;
      if (kind === 'new_cook' && js && js.state === 'open' && a.started_by === 'new_cook' && same(a.start)) return true;
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  }

  // The journal opens in place of the probes and the chart (cook-predictor-journal, embedded).
  async _toggleJournal() {
    const root = this.shadowRoot;
    const btn = root.querySelector('.journal');
    const wrap = root.querySelector('.journal-wrap');
    this._journalOpen = !this._journalOpen;
    root.querySelector('.cook-view').hidden = this._journalOpen;
    wrap.hidden = !this._journalOpen;
    btn.textContent = this._journalOpen ? '← Cook' : 'Journal';
    btn.setAttribute('aria-pressed', String(this._journalOpen));
    if (!this._journalOpen) { this._drawChart(); return; }
    await this._mountJournal(wrap);
  }

  // One journal element per built card. Calls while its code is still loading share one
  // wait; after it, nothing mounts if the card was rebuilt meanwhile or another call already
  // mounted it.
  async _mountJournal(wrap) {
    if (this._journal && this._journal.parentNode === wrap) return;
    const gen = this._buildGen;
    if (!this._journalWait) {
      this._journalWait = Promise.race([
        customElements.whenDefined('cook-predictor-journal').then(() => true),
        new Promise((r) => setTimeout(() => r(false), 5000)),
      ]);
    }
    const ready = await this._journalWait;
    if (gen !== this._buildGen || (this._journal && this._journal.parentNode === wrap)) return;
    wrap.replaceChildren();
    if (!ready) {
      this._journalWait = null; // try again on the next open
      wrap.innerHTML = '<div class="meta" style="padding:16px">The journal view did not load. Reload the page.</div>';
      return;
    }
    this._journal = document.createElement('cook-predictor-journal');
    this._journal.setConfig({ embedded: true });
    wrap.appendChild(this._journal);
    this._journal.hass = this._hass;
  }

  // A battery chip: a level with an icon that fills with it and a warning colour below 40 %
  // and 20 % (not while charging), or Low/OK for a thermometer that only says that. On the
  // charger an Inkbird reports its level times 1000 (58 % reads 58000): with its decoder a
  // reading of 1000 or more is that level, charging.
  _updateBatteries() {
    for (const chip of this.shadowRoot.querySelectorAll('.battery')) {
      const b = this._cfg.batteries[Number(chip.dataset.i)];
      const s = this._hass.states[b.entity_id];
      const val = chip.querySelector('.bat-val');
      const icon = chip.querySelector('ha-icon');
      const who = b.device || 'Thermometer';
      chip.style.color = '';
      if (b.kind === 'binary') {
        const low = s && s.state === 'on';
        const known = s && (s.state === 'on' || s.state === 'off');
        val.textContent = known ? (low ? 'Low' : 'OK') : '–';
        icon.setAttribute('icon', !known ? 'mdi:battery-unknown' : low ? 'mdi:battery-alert-variant-outline' : 'mdi:battery');
        if (low) chip.style.color = 'var(--error-color, #db4437)';
        chip.title = `${who} battery ${known ? (low ? 'low' : 'OK') : 'unknown'}: open the device`;
        continue;
      }
      const raw = s ? parseFloat(s.state) : NaN;
      const charging = b.decoder === 'inkbird' && raw >= 1000;
      const level = charging ? raw / 1000 : raw;
      if (!(level >= 0 && level <= 100)) {
        val.textContent = '–';
        icon.setAttribute('icon', 'mdi:battery-unknown');
        chip.title = `${who} battery unknown: open the device`;
        continue;
      }
      const tens = Math.round(level / 10) * 10;
      const base = charging ? 'mdi:battery-charging' : 'mdi:battery';
      icon.setAttribute('icon', tens >= 100 ? (charging ? 'mdi:battery-charging-100' : 'mdi:battery')
        : tens <= 0 ? `${base}-outline` : `${base}-${tens}`);
      val.textContent = `${Math.round(level)}%`;
      chip.style.color = charging ? '' : level < 20 ? 'var(--error-color, #db4437)' : level < 40 ? 'var(--warning-color, #f59e0b)' : '';
      chip.title = `${who} battery ${Math.round(level)}%${charging ? ', charging' : ''}: open the device`;
    }
  }

  // The newest reading of a channel (°C), or null when it is not reading now.
  _now(key) {
    const s = this._series[key];
    const last = s && s[s.length - 1];
    return last ? last[1] : null;
  }

  _pred(p) {
    const s = p.status && this._hass.states[p.status];
    return s ? { status: s.state, a: s.attributes } : null;
  }

  _num(entity, attr) {
    const s = entity && this._hass.states[entity];
    if (!s) return NaN;
    return parseFloat(attr ? s.attributes[attr] : s.state);
  }

  _isOn(p) {
    const s = p.alert_on && this._hass.states[p.alert_on];
    return !!s && s.state === 'on';
  }

  _fieldOn(field, p) {
    return field.switch ? (field.pending ? !field.shownOff : this._isOn(p)) : true;
  }

  _update() {
    const hass = this._hass;
    const root = this.shadowRoot;
    if (!root || !this._built) return;
    for (const pill of root.querySelectorAll('.amb')) {
      pill.querySelector('.amb-val').textContent = this._fmt(this._now(pill.dataset.key));
    }
    this._updateBatteries();

    // The cook's start is the earliest session among ALL active food probes, including ones
    // hidden from this card, so hiding probes never shortens the chart's "Whole cook" range.
    let earliest = null;
    for (const p of (this._allProbes || this._probes)) {
      const pr = this._pred(p);
      const ss = pr && pr.status !== 'unplugged' && pr.a && pr.a.session_start;
      if (ss) { const t = Date.parse(ss); if (earliest == null || t < earliest) earliest = t; }
    }
    this._probes.forEach((p, i) => {
      const el = this._tiles[i];
      const T = this._now(p.key);
      const pr = this._pred(p);
      const a = (pr && pr.a) || {};
      const status = pr ? pr.status : T == null ? 'unplugged' : 'warming';
      const targetShown = this._num(p.numbers && p.numbers.target); // in the shown unit
      const target = a.target ?? null; // °C, what the predictor used

      el.classList.toggle('off', T == null);
      const chip = el.querySelector('.chip');
      chip.className = `chip ${status}`;
      chip.textContent = STATUS[status] || status;
      el.querySelector('.temp').textContent = this._fmt(T);
      el.querySelector('.of').textContent = Number.isFinite(targetShown) ? `of ${targetShown}°` : '';

      const start = a.start_temp ?? T;
      const tgtC = target ?? (Number.isFinite(targetShown) ? (this._unit === '°F' ? (targetShown - 32) * 5 / 9 : targetShown) : null);
      const frac = T == null || tgtC == null || tgtC <= start ? (status === 'done' ? 1 : 0)
        : Math.min(1, Math.max(0, (T - start) / (tgtC - start)));
      el.querySelector('.arc').setAttribute('stroke-dashoffset', String(326.7 * (1 - frac)));

      const main = el.querySelector('.eta-main');
      const sub = el.querySelector('.eta-sub');
      main.classList.toggle('low', a.confidence === 'low');
      let mainText = '';
      let subText = '';
      if (status === 'done') {
        mainText = 'Ready';
        subText = a.reached_at ? `reached ${fmtTime(Date.parse(a.reached_at))}` : '';
      } else if (status === 'stall') {
        const since = a.stall_since ? (Date.now() - Date.parse(a.stall_since)) / 60000 : null;
        mainText = `Stall ${since != null ? fmtDur(since) : ''}`;
        subText = a.eta_after_stall_min != null ? `then about ${fmtDur(a.eta_after_stall_min)}` : `at ${this._fmt(a.stall_temp)}`;
      } else if (a.done_at && status === 'predicting') {
        const left = (Date.parse(a.done_at) - Date.now()) / 60000;
        mainText = `${a.confidence === 'low' ? '~' : ''}${fmtDur(Math.max(0, left))}`;
        subText = `done around ${fmtTime(Date.parse(a.done_at))}`;
      } else if (status === 'warming') {
        mainText = 'Estimating…';
        subText = REASON[a.reason] || '';
      } else if (status !== 'unplugged') {
        mainText = '–';
        subText = REASON[a.reason] || a.reason || '';
      }
      main.textContent = mainText;
      sub.textContent = subText;
      el.querySelector('.hint').textContent = a.stall_ahead && status === 'predicting'
        ? 'A stall is still likely; expect this to stretch' : '';

      for (const f of Object.values(p._fields)) {
        const s = hass.states[f.entity];
        if (!s || root.activeElement === f.input || f.pending) continue;
        this._show(f, f.switch && !this._isOn(p) ? NaN : parseFloat(s.state));
        f.input.min = s.attributes.min;
        f.input.max = s.attributes.max;
        f.input.step = s.attributes.step;
      }
      this._alerts(p, el, a, status, T);
      const meta = [];
      if (a.rate != null && status !== 'done' && status !== 'unplugged') {
        const r = this._du(a.rate);
        meta.push(`${r > 0 ? '+' : ''}${r.toFixed(1)}°/h`);
      }
      if (a.elapsed_min != null && status !== 'unplugged') meta.push(`on for ${fmtDur(a.elapsed_min)}`);
      el.querySelector('.meta').textContent = meta.join(' · ');
    });

    // The header follows the journal: a cook is open, the timer runs from its start and
    // Finish shows; one finished in the last 12 h shows its length with a flag and Finish
    // hides; otherwise the timer runs from the probes' sessions, if any.
    const js = this._cfg.journal && hass.states[this._cfg.journal];
    const jstate = js ? js.state : null;
    const ja = (js && js.attributes) || {};
    const finished = jstate === 'finished' && ja.start && ja.end;
    const from = jstate === 'open' && ja.start ? Date.parse(ja.start) : earliest;
    const elapsed = root.querySelector('.elapsed');
    elapsed.hidden = !finished && from == null;
    elapsed.classList.toggle('finished', !!finished);
    elapsed.querySelector('ha-icon').setAttribute('icon', finished ? 'mdi:flag-checkered' : 'mdi:timer-outline');
    elapsed.title = finished ? `Finished at ${fmtTime(Date.parse(ja.end))}; in the journal` : 'Since the cook started';
    if (finished) elapsed.querySelector('.elapsed-val').textContent = fmtDur((Date.parse(ja.end) - Date.parse(ja.start)) / 60000);
    else if (from != null) elapsed.querySelector('.elapsed-val').textContent = fmtDur((Date.now() - from) / 60000);
    const finBtn = root.querySelector('.finish');
    const showing = ['armed', 'busy', 'done', 'failed'].some((c) => finBtn.classList.contains(c));
    if (this._cfg.buttons.finish) finBtn.hidden = this._config.show_actions === false || (jstate !== 'open' && !showing);
    this._sessionStart = earliest;
    // The chart's cook is the one the journal shows (open, or finished lately): its start
    // bounds the chart, so neither the lead-in nor a probe still in an older session reaches
    // back into the cook before it (New cook, or a probe plugged in again, ends one cook and
    // starts the next while the probes keep reading).
    this._cookStart = (jstate === 'open' || finished) && ja.start ? Date.parse(ja.start) : null;

    const bounds = `${this._sessionStart}|${this._cookStart}`;
    if (bounds !== this._bounds || !this._drawnAt || Date.now() - this._drawnAt > 5000) {
      this._bounds = bounds;
      this._drawChart();
    }
  }

  // A setting's input shows '' (placeholder "off") for an off value; _value reads it back.
  _show(f, v) {
    f.input.value = Number.isFinite(v) && !(f.off && v <= 0) ? String(v) : '';
  }

  _value(f, p) {
    if (f.input.value === '') {
      if (f.off) return 0;
      if (f.switch) return this._num(f.entity); // off, at the value it keeps
    }
    return parseFloat(f.input.value);
  }

  // The bell's summary, the second alert's heads-up row and the note under the target, from
  // the values on screen (shown unit) so a pending change shows at once. A second
  // temperature at or above the target has no heads-up (the predictor stops at the target).
  _alerts(p, el, a, status, T) {
    const val = (k) => (p._fields[k] ? this._value(p._fields[k], p) : null);
    const f = p._fields.alert_temp;
    const on = f ? this._fieldOn(f, p) : false;
    const warn = val('warn_min');
    const at = on ? val('alert_temp') : 0;
    const lead = val('alert_min');
    const tgt = p._fields.target ? val('target') : null;
    const below = at > 0 && !(tgt <= at);
    if (p._fields.alert_min) el.querySelector('[data-row="alert_min"]').hidden = !below;
    if (p._fields.warn_min) el.querySelector('[data-row="warn_min"] .off').hidden = !(warn > 0);
    if (p._fields.alert_temp) el.querySelector('[data-row="alert_temp"] .off').hidden = !(at > 0);
    if (p._fields.alert_min) el.querySelector('[data-row="alert_min"] .off').hidden = !(lead > 0);
    const parts = [];
    if (warn > 0) parts.push(`${fmtDur(warn)} before`);
    if (at > 0) parts.push(`${at}°${below && lead > 0 ? ` (${fmtDur(lead)} early)` : ''}`);
    el.querySelector('.bell span').textContent = parts.length ? parts.join(' · ') : 'At target only';

    let note = '';
    const atC = at > 0 ? (this._unit === '°F' ? (at - 32) * 5 / 9 : at) : null;
    if (atC != null && T != null && status !== 'unplugged') {
      const current = a.alert_temp != null && Math.abs(a.alert_temp - atC) < 0.05; // the predictor has this setting
      if (current && a.alert_reached_at) note = `${at}° reached ${fmtTime(Date.parse(a.alert_reached_at))}`;
      else if (T >= atC) note = `Above ${at}°`;
      else if (!below) note = `Alert at ${at}° when reached (no heads-up above the target)`;
      else if (current && a.alert_at) {
        const left = (Date.parse(a.alert_at) - Date.now()) / 60000;
        note = `${at}° in ${fmtDur(Math.max(0, left))}, around ${fmtTime(Date.parse(a.alert_at))}`;
      } else note = `Alert at ${at}°`;
    }
    el.querySelector('.alert-note').textContent = note;
  }

  // Newton forecast from the predictor's fitted k toward its predicted finish, anchored at
  // the reading it was computed from (all °C). Only for the Newton model: a linear
  // fallback's k is a failed fit and its curve would not end at the finish dot. A stall has
  // no single finish, so its forecast is two scenarios: `early`, the finish if the stall
  // ended now (the session's best k, which eta_after_stall_min is computed with), and
  // `pace`, the finish at the current window's k while that fit holds (model newton).
  _forecast(p, now) {
    const pr = this._pred(p);
    if (!pr || (pr.status !== 'predicting' && pr.status !== 'stall')) return null;
    const a = pr.a;
    if (!a.observed_at || a.ambient == null || a.temperature == null || a.target == null) return null;
    const t0 = Date.parse(a.observed_at);
    const start = Math.max(now, t0);
    const curve = (k, end) => {
      if (!(k > 0) || !(end > start) || end - start > 14 * 3600e3) return null;
      const pts = [];
      for (let i = 0; i <= 40; i++) {
        const t = start + ((end - start) * i) / 40;
        pts.push([t, a.ambient - (a.ambient - a.temperature) * Math.exp((-k * (t - t0)) / 1000)]);
      }
      return { pts, end };
    };
    const f = { target: a.target, color: p.color, low: a.confidence === 'low' };
    if (pr.status === 'predicting') {
      if (a.model !== 'newton' || !a.done_at || a.k == null) return null;
      const pace = curve(a.k, Date.parse(a.done_at));
      return pace && { ...f, pace, end: pace.end };
    }
    if (!(a.ambient > a.target && a.target > a.temperature)) return null;
    const gap = Math.log((a.ambient - a.temperature) / (a.ambient - a.target)); // k × time to target
    const pace = a.model === 'newton' && a.k > 0 ? curve(a.k, t0 + (gap / a.k) * 1000) : null;
    const early = a.eta_after_stall_min > 0
      ? curve(gap / (a.eta_after_stall_min * 60), t0 + a.eta_after_stall_min * 60e3) : null;
    if (!pace && !early) return null;
    return { ...f, stall: true, pace, early, end: Math.max(pace ? pace.end : 0, early ? early.end : 0) };
  }

  _drawChart() {
    const root = this.shadowRoot;
    if (!root || !this._hass || !this._built) return;
    if (this._config.show_chart === false) return; // chart hidden by the card config
    const svg = root.querySelector('svg.chart');
    const W = svg.clientWidth;
    const H = svg.clientHeight;
    if (!W || !H) return;
    this._drawnAt = Date.now();
    const now = Date.now();
    const M = { l: 34, r: 12, t: 10, b: 22 };
    const shown = (id) => !this._hidden.has(id);
    const probes = this._probes.filter((p) => shown(p.key));
    const u = (c) => this._u(c);

    // The whole cook runs from the session start (with a 10-minute lead-in, never before the
    // journal's cook) to past the last forecast; the view is that, its last stretch up to now
    // plus the forecasts (a range chip), or a zoomed span.
    const forecasts = probes.map((p) => this._forecast(p, now)).filter(Boolean);
    const lead = this._sessionStart != null ? this._sessionStart - 10 * 60e3 : now - 6 * 3600e3;
    const full = [
      this._cookStart != null ? Math.max(lead, this._cookStart) : lead,
      Math.max(now + 15 * 60e3, ...forecasts.map((f) => f.end + 10 * 60e3)),
    ];
    this._full = full;
    let [t0, t1] = full;
    const v = this._view;
    if (v && v.last) t0 = Math.max(full[0], Math.min(now - 15 * 60e3, now - v.last));
    else if (v) {
      // A span against the end of the cook stays there as the end moves (it follows now).
      const span = Math.min(v.t1 - v.t0, full[1] - full[0]);
      t0 = Math.max(full[0], Math.min(v.pinned ? full[1] - span : v.t0, full[1] - span));
      t1 = t0 + span;
    }

    // Each line keeps one reading either side of the view, so it runs to the edges.
    const series = [
      ...this._pits.map((p) => ({ id: p.key, color: p.color, width: 1.5, name: p.name })),
      ...this._probes.map((p) => ({ id: p.key, color: p.color, width: 2.5, name: p.name })),
      ...this._retired.map((p) => ({ id: p.key, color: p.color, width: p.role === 'internal' ? 2 : 1.2, name: p.name })),
    ].filter((s) => shown(s.id)).map((s) => {
      const all = (this._series[s.id] || []).map(([t, val]) => [t, u(val)]);
      const last = all[all.length - 1];
      if (last && last[1] != null && now > last[0]) all.push([now, last[1]]);
      let first = all.findIndex((pt) => pt[0] >= t0);
      if (first < 0) first = all.length;
      const after = all.findIndex((pt) => pt[0] > t1);
      return { ...s, pts: all.slice(Math.max(0, first - 1), after < 0 ? all.length : after + 1) };
    });
    const fc = forecasts.map((f) => ({
      ...f, target: u(f.target),
      pace: f.pace && { ...f.pace, pts: f.pace.pts.map(([t, val]) => [t, u(val)]) },
      early: f.early && { ...f.early, pts: f.early.pts.map(([t, val]) => [t, u(val)]) },
    }));

    // The temperature axis fits what is drawn in view: readings inside it and where a line
    // crosses its edges (lines are straight between readings, with gaps at nulls), the
    // forecasts the same way, and the targets while the view reaches now (the forecasts head
    // for them).
    let lo = Infinity;
    let hi = -Infinity;
    const take = (val) => { lo = Math.min(lo, val); hi = Math.max(hi, val); };
    const fit = (pts) => {
      for (let i = 0; i < pts.length; i++) {
        const [t, val] = pts[i];
        if (val == null) continue;
        if (t >= t0 && t <= t1) take(val);
        const nx = pts[i + 1];
        if (!nx || nx[1] == null) continue;
        for (const edge of [t0, t1]) if (t < edge && nx[0] > edge) take(val + ((nx[1] - val) * (edge - t)) / (nx[0] - t));
      }
    };
    for (const s of series) fit(s.pts);
    for (const f of fc) for (const c of [f.pace, f.early]) if (c) fit(c.pts);
    const targetOf = (p) => this._num(p.numbers && p.numbers.target);
    if (t1 >= now) {
      for (const p of probes) {
        const tv = targetOf(p);
        if (Number.isFinite(tv) && this._now(p.key) != null) take(tv);
      }
    }
    if (!Number.isFinite(lo)) { lo = 0; hi = 100; }
    const pad = Math.max(1, (hi - lo) * 0.05);
    const range = hi - lo + 2 * pad;
    const yStep = range > 240 ? 50 : range > 120 ? 40 : range > 60 ? 20 : range > 30 ? 10 : range > 12 ? 5 : 2;
    const snap = Math.min(10, yStep); // the axis ends on tens (or the finer step); grid lines on yStep
    lo = Math.floor((lo - pad) / snap) * snap;
    hi = Math.ceil((hi + pad) / snap) * snap;

    const x = (t) => M.l + ((t - t0) / (t1 - t0)) * (W - M.l - M.r);
    const y = (val) => M.t + (1 - (val - lo) / (hi - lo)) * (H - M.t - M.b);
    this._scale = { x, y, t0, t1, M, W, H, series };

    const parts = [];
    for (let val = Math.ceil(lo / yStep) * yStep; val <= hi; val += yStep) {
      parts.push(`<line class="grid" x1="${M.l}" x2="${W - M.r}" y1="${y(val)}" y2="${y(val)}"/>`,
        `<text x="${M.l - 6}" y="${y(val) + 4}" text-anchor="end">${val}°</text>`);
    }
    const span = (t1 - t0) / 3600e3;
    const hStep = span > 16 ? 4 : span > 8 ? 2 : span > 3 ? 1 : span > 1.5 ? 0.5 : span > 0.6 ? 0.25 : 1 / 6;
    const stepMs = Math.round(hStep * 3600e3); // whole ms, so ticks land on round minutes
    for (let t = Math.ceil(t0 / stepMs) * stepMs; t <= t1; t += stepMs) {
      parts.push(`<text x="${x(t)}" y="${H - 5}" text-anchor="middle">${fmtTime(t)}</text>`);
    }

    for (const p of probes) {
      if (this._now(p.key) == null) continue;
      const tv = targetOf(p);
      if (tv > lo && tv < hi) {
        parts.push(`<line x1="${M.l}" x2="${W - M.r}" y1="${y(tv)}" y2="${y(tv)}" stroke="${p.color}" stroke-opacity=".45" stroke-dasharray="1 4"/>`);
      }
      const av = this._isOn(p) ? this._num(p.numbers && p.numbers.alert_temp) : NaN;
      if (av > lo && av < hi) {
        parts.push(`<line x1="${M.l}" x2="${W - M.r}" y1="${y(av)}" y2="${y(av)}" stroke="${p.color}" stroke-opacity=".3" stroke-dasharray="6 6"/>`);
      }
    }

    // Lines, forecasts and their dots are clipped to the plot by a nested svg viewport.
    const pw = W - M.l - M.r;
    const ph = H - M.t - M.b;
    parts.push(`<svg x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" viewBox="${M.l} ${M.t} ${pw} ${ph}" overflow="hidden">`);
    for (const s of series) {
      let d = '';
      let pen = false;
      for (const [t, val] of s.pts) {
        if (val == null) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${x(t).toFixed(1)},${y(val).toFixed(1)}`;
        pen = true;
      }
      if (d) parts.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width}" stroke-linejoin="round"/>`);
    }

    // Finish labels sit above the target line, or below or higher when they would overlap
    // one already placed (two probes on one target); none for a finish out of view.
    const placed = [];
    const labels = [];
    const label = (xc, yl, text) => {
      if (xc < M.l || xc > W - M.r || yl < M.t || yl > H - M.b) return;
      const w = text.length * 6.2;
      const cx = Math.max(M.l + w / 2, Math.min(xc, W - M.r - w / 2));
      const ly = [yl - 8, yl + 16, yl - 22, yl + 30]
        .find((val) => !placed.some((b) => Math.abs(b.y - val) < 12 && Math.abs(b.x - cx) < (b.w + w) / 2 + 4)) ?? yl - 8;
      placed.push({ x: cx, y: ly, w });
      labels.push(`<text x="${cx}" y="${ly}" text-anchor="middle">${text}</text>`);
    };
    const path = (pts) => pts.map(([t, val], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(val).toFixed(1)}`).join('');
    for (const f of fc) {
      const op = f.low ? 0.45 : 0.9;
      if (!f.stall) {
        parts.push(`<path d="${path(f.pace.pts)}" fill="none" stroke="${f.color}" stroke-width="2" stroke-dasharray="5 4" stroke-opacity="${op}"/>`,
          `<circle cx="${x(f.end)}" cy="${y(f.target)}" r="4" fill="${f.color}"/>`);
        label(x(f.end), y(f.target), fmtTime(f.end));
        continue;
      }
      // Stall: two scenarios, not bounds (the stall can deepen, the pit can rise), each with
      // its own time; the legend names the markers. Current pace dashed with a filled dot,
      // the stall ending now dotted with a hollow one.
      if (f.pace) {
        parts.push(`<path d="${path(f.pace.pts)}" fill="none" stroke="${f.color}" stroke-width="2" stroke-dasharray="5 4" stroke-opacity="${op}"/>`,
          `<circle cx="${x(f.pace.end)}" cy="${y(f.target)}" r="4" fill="${f.color}"/>`);
        label(x(f.pace.end), y(f.target), fmtTime(f.pace.end));
      }
      if (f.early) {
        parts.push(`<path d="${path(f.early.pts)}" fill="none" stroke="${f.color}" stroke-width="1.5" stroke-dasharray="1.5 3.5" stroke-opacity=".8"/>`,
          `<circle cx="${x(f.early.end)}" cy="${y(f.target)}" r="3.5" fill="none" stroke="${f.color}" stroke-width="2"/>`);
        label(x(f.early.end), y(f.target), fmtTime(f.early.end));
      }
    }
    parts.push('</svg>', ...labels);
    for (const el of root.querySelectorAll('.stall-key')) el.hidden = !fc.some((f) => f.stall);

    if (now >= t0 && now <= t1) parts.push(`<line class="now" x1="${x(now)}" x2="${x(now)}" y1="${M.t}" y2="${H - M.b}"/>`);
    parts.push('<line class="cursor" x1="0" x2="0" y1="0" y2="0" stroke="var(--primary-text-color)" stroke-opacity=".35" visibility="hidden"/>');
    svg.innerHTML = parts.join('');
    this._markRange(t0, t1, full, M);
  }

  // The controls: which chip is on, what − and + can still do, and the scrollbar's thumb
  // (the view within the whole cook; the track lines up with the plot).
  _markRange(t0, t1, [f0, f1], M) {
    const root = this.shadowRoot;
    const v = this._view;
    for (const b of root.querySelectorAll('.ranges button')) {
      const r = b.dataset.range;
      b.setAttribute('aria-pressed', String(r === 'all' ? !v : !!(v && v.last === Number(r) * 3600e3)));
    }
    root.querySelector('.zoom-in').disabled = t1 - t0 <= MIN_SPAN + 1000;
    root.querySelector('.zoom-out').disabled = !v;
    const sb = root.querySelector('.scroll');
    sb.style.margin = `0 ${M.r}px 0 ${M.l}px`;
    const g = this._thumbGeom(t0, t1, [f0, f1], sb.querySelector('.track').clientWidth);
    const thumb = sb.querySelector('.thumb');
    thumb.style.left = `${g.left}px`;
    thumb.style.width = `${g.width}px`;
    sb.setAttribute('aria-valuenow', String(Math.round(g.share * 100)));
    sb.setAttribute('aria-valuetext', `${fmtTime(t0)} to ${fmtTime(t1)}`);
  }

  // The thumb in px: as wide as the view's share of the track but at least THUMB_MIN, placed
  // by the view's position in the free range (the cook less the view) over the track less the
  // thumb; msPerPx turns a thumb move into time.
  _thumbGeom(t0, t1, [f0, f1], trackW) {
    const width = Math.min(trackW, Math.max(THUMB_MIN, ((t1 - t0) / (f1 - f0)) * trackW));
    const freeT = f1 - f0 - (t1 - t0);
    const freePx = trackW - width;
    const share = freeT > 0 ? Math.min(1, Math.max(0, (t0 - f0) / freeT)) : 0;
    return { width, left: share * freePx, share, msPerPx: freePx > 0 ? freeT / freePx : 0 };
  }

  // The scrollbar: drag the thumb to move, its ends to zoom, tap the track to jump there
  // (the thumb's middle to the tap); arrows, Page Up/Down, Home and End from the keyboard.
  // One pointer owns a drag: other fingers and other mouse buttons are ignored.
  _bindScroll(sb) {
    const track = sb.querySelector('.track');
    let drag = null;
    sb.addEventListener('pointerdown', (e) => {
      const sc = this._scale;
      const full = this._full;
      if (!sc || !full || drag || (e.pointerType === 'mouse' && e.button !== 0)) return;
      e.preventDefault();
      const r = track.getBoundingClientRect();
      const grip = e.target.closest('.grip');
      let t0 = sc.t0;
      let t1 = sc.t1;
      const span = t1 - t0;
      const g = this._thumbGeom(t0, t1, full, r.width);
      if (!grip && !e.target.closest('.thumb')) {
        t0 = Math.max(full[0], Math.min(full[0] + (e.clientX - r.left - g.width / 2) * g.msPerPx, full[1] - span));
        t1 = t0 + span;
        this._setView(t0, t1);
      }
      drag = {
        id: e.pointerId, x: e.clientX, t0, t1, full,
        mode: grip ? (grip.classList.contains('l') ? 'l' : 'r') : 'move',
        per: grip ? (full[1] - full[0]) / r.width : g.msPerPx,
      };
      try { sb.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
      this._hover(null);
    });
    sb.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dt = (e.clientX - drag.x) * drag.per;
      const [f0, f1] = drag.full;
      if (drag.mode === 'move') this._setView(drag.t0 + dt, drag.t1 + dt);
      else if (drag.mode === 'l') this._setView(Math.max(f0, Math.min(drag.t0 + dt, drag.t1 - MIN_SPAN)), drag.t1);
      else this._setView(drag.t0, Math.min(f1, Math.max(drag.t1 + dt, drag.t0 + MIN_SPAN)));
    });
    const end = (e) => { if (drag && e.pointerId === drag.id) drag = null; };
    sb.addEventListener('pointerup', end);
    sb.addEventListener('pointercancel', end);
    sb.addEventListener('lostpointercapture', end);
    sb.addEventListener('keydown', (e) => {
      const sc = this._scale;
      const full = this._full;
      if (!sc || !full) return;
      const span = sc.t1 - sc.t0;
      const step = { ArrowLeft: -0.1, ArrowRight: 0.1, PageUp: -1, PageDown: 1 }[e.key];
      if (step != null) this._setView(sc.t0 + step * span, sc.t1 + step * span);
      else if (e.key === 'Home') this._setView(full[0], full[0] + span);
      else if (e.key === 'End') this._setView(full[1] - span, full[1]);
      else return;
      e.preventDefault();
    });
  }

  _redraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this._drawChart(); });
  }

  // A zoomed or panned span, kept inside the whole cook; zooming out to all of it is the
  // whole-cook view again.
  _setView(t0, t1) {
    const [f0, f1] = this._full || [t0, t1];
    const span = Math.max(MIN_SPAN, Math.min(t1 - t0, f1 - f0));
    if (span >= f1 - f0) this._view = null;
    else {
      const a = Math.max(f0, Math.min(t0, f1 - span));
      this._view = { t0: a, t1: a + span, pinned: a + span >= f1 - 1000 };
    }
    this._redraw();
  }

  // Zoom by `factor` (below 1: in) keeping the time under x = px in place.
  _zoomAt(px, factor) {
    const sc = this._scale;
    if (!sc) return;
    const frac = Math.min(1, Math.max(0, (px - sc.M.l) / (sc.W - sc.M.l - sc.M.r)));
    const at = sc.t0 + frac * (sc.t1 - sc.t0);
    const span = Math.max(MIN_SPAN, (sc.t1 - sc.t0) * factor);
    this._setView(at - frac * span, at - frac * span + span);
  }

  // Touch: one finger shows the readings under it, two fingers pinch and pan. Mouse: hover
  // shows the readings, drag pans, Ctrl/⌘ + wheel (and a trackpad pinch) zooms, as in HA's
  // own charts, so a plain wheel still scrolls the page; double-click shows the whole cook.
  _bindChart(svg) {
    const pts = new Map();
    const plotW = () => this._scale.W - this._scale.M.l - this._scale.M.r;
    svg.addEventListener('pointerdown', (e) => {
      pts.set(e.pointerId, e.clientX);
      try { svg.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
      const sc = this._scale;
      if (!sc) return;
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        this._gesture = { pinch: true, d: Math.max(24, Math.abs(a - b)), mid: (a + b) / 2 - svg.getBoundingClientRect().left, t0: sc.t0, t1: sc.t1 };
        this._hover(null);
      } else if (e.pointerType === 'mouse' && e.button === 0) {
        this._gesture = { drag: e.clientX, t0: sc.t0, t1: sc.t1 };
      }
    });
    svg.addEventListener('pointermove', (e) => {
      if (pts.has(e.pointerId)) pts.set(e.pointerId, e.clientX);
      const g = this._gesture;
      const sc = this._scale;
      if (!g || !sc) { this._hover(e); return; }
      if (g.pinch) {
        if (pts.size < 2) return;
        const [a, b] = [...pts.values()];
        const span0 = g.t1 - g.t0;
        const at = g.t0 + ((g.mid - sc.M.l) / plotW()) * span0; // the time under the fingers at the start
        const span = Math.max(MIN_SPAN, span0 * (g.d / Math.max(24, Math.abs(a - b))));
        const frac = ((a + b) / 2 - svg.getBoundingClientRect().left - sc.M.l) / plotW();
        this._setView(at - frac * span, at - frac * span + span);
        return;
      }
      if (!g.moved && Math.abs(e.clientX - g.drag) < 4) { this._hover(e); return; }
      g.moved = true;
      this._hover(null);
      const dt = ((e.clientX - g.drag) / plotW()) * (g.t1 - g.t0);
      this._setView(g.t0 - dt, g.t1 - dt);
    });
    const end = (e) => {
      pts.delete(e.pointerId);
      if (this._gesture && (!this._gesture.pinch || pts.size < 2)) this._gesture = null;
      if (e.type === 'pointercancel') this._hover(null);
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
    svg.addEventListener('pointerleave', () => { if (!this._gesture) this._hover(null); });
    svg.addEventListener('wheel', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      this._zoomAt(e.clientX - svg.getBoundingClientRect().left, Math.exp(Math.max(-40, Math.min(40, e.deltaY)) * 0.01));
    }, { passive: false });
    svg.addEventListener('dblclick', () => { this._view = null; this._redraw(); });
  }

  _hover(e) {
    const root = this.shadowRoot;
    const tip = root && root.querySelector('.tip');
    const cursor = root && root.querySelector('.cursor');
    const sc = this._scale;
    if (!tip) return;
    if (!e || !sc || !cursor) {
      tip.style.display = 'none';
      if (cursor) cursor.setAttribute('visibility', 'hidden');
      return;
    }
    const rect = root.querySelector('svg.chart').getBoundingClientRect();
    const px = e.clientX - rect.left;
    const t = sc.t0 + ((px - sc.M.l) / (sc.W - sc.M.l - sc.M.r)) * (sc.t1 - sc.t0);
    if (t < sc.t0 || t > Math.min(sc.t1, Date.now())) { this._hover(null); return; }
    const rows = [];
    for (const s of sc.series) {
      let v = null;
      for (const [pt, pv] of s.pts) { if (pt > t) break; v = pv; }
      if (v != null) rows.push(`<div><i style="background:${s.color}"></i>${esc(s.name)} <b>${v.toFixed(1)}°</b></div>`);
    }
    if (!rows.length) { this._hover(null); return; }
    cursor.setAttribute('x1', px);
    cursor.setAttribute('x2', px);
    cursor.setAttribute('y1', sc.M.t);
    cursor.setAttribute('y2', sc.H - sc.M.b);
    cursor.setAttribute('visibility', 'visible');
    tip.innerHTML = `<div><b>${fmtTime(t)}</b></div>${rows.join('')}`;
    tip.style.display = 'block';
    const left = px + 12 + tip.offsetWidth > sc.W ? px - 12 - tip.offsetWidth : px + 12;
    tip.style.left = `${left}px`;
    tip.style.top = '8px';
  }
}

if (!customElements.get('cook-predictor-card')) customElements.define('cook-predictor-card', CookPredictorCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === 'cook-predictor-card')) {
  window.customCards.push({
    type: 'cook-predictor-card',
    name: 'Cook Predictor',
    description: 'Your probes with their predicted finish, alerts, a cook chart and the journal',
    preview: false,
  });
}

/* ------------------------------- visual editor -------------------------------
 * Lets you choose which probes and cookers show and which parts of the card appear,
 * without touching YAML. The probe list is read live from the integration (the same
 * snapshot the card uses); with no integration yet, only the part-toggles are offered.
 */
const EDITOR_LABELS = {
  title: 'Card title',
  show_journal: 'Journal button',
  show_actions: 'Finish / New-cook buttons',
  show_settings: 'Settings (gear) button',
  show_batteries: 'Battery chips',
  show_chart: 'Chart, zoom and legend',
};
const EDITOR_TOGGLES = ['show_journal', 'show_actions', 'show_settings', 'show_batteries', 'show_chart'];

class CookPredictorCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._form) this._form.hass = hass;
    if (first && hass) this._loadChannels();
  }

  connectedCallback() {
    // Retry if an earlier attempt was cancelled before any snapshot arrived.
    if (!this._channels && this._hass) this._loadChannels();
  }

  disconnectedCallback() {
    if (this._unsub) { this._callUnsub(this._unsub); this._unsub = null; }
  }

  // subscribeMessage's unsubscribe returns a promise; swallow both sync throws and rejections.
  _callUnsub(unsub) {
    try { const r = unsub(); if (r && typeof r.then === 'function') r.catch(() => {}); } catch (e) { /* ignore */ }
  }

  // Grab the channel list once from the integration's snapshot, then stop listening.
  async _loadChannels() {
    if (this._channels || this._loading || !this._hass) return;
    this._loading = true;
    try {
      let got = false;
      const unsub = await this._hass.connection.subscribeMessage(
        (m) => {
          if (got || !m || m.type !== 'snapshot' || !m.config) return;
          got = true;
          this._channels = (m.config.channels || []).filter((c) => c.active !== false);
          if (this._unsub) { this._callUnsub(this._unsub); this._unsub = null; }
          this._render();
        },
        { type: `${DOMAIN}/subscribe` },
      );
      if (got || !this.isConnected) this._callUnsub(unsub); else this._unsub = unsub;
    } catch (e) {
      // Integration unavailable -> stable empty (part-toggles only). A transient failure leaves
      // channels unset so a later connectedCallback (DOM re-attach) retries, rather than caching
      // an empty list forever.
      const code = e && e.code;
      if (code === 'unknown_command' || code === 'not_loaded' || code === 'not_found') this._channels = [];
      this._render();
    } finally {
      this._loading = false;
    }
  }

  _label(s) {
    if (s.name && s.name.indexOf('ch_') === 0) {
      const key = s.name.slice(3);
      const c = (this._channels || []).find((ch) => String(ch.key) === key);
      const base = c ? c.name : `Channel ${key}`;
      return `Show ${base}${c && c.role === 'ambient' ? ' (cooker)' : ''}`;
    }
    return EDITOR_LABELS[s.name] || s.name;
  }

  _schema() {
    const base = [
      { name: 'title', selector: { text: {} } },
      ...EDITOR_TOGGLES.map((name) => ({ name, selector: { boolean: {} } })),
    ];
    const chans = (this._channels || []).map((c) => ({ name: `ch_${c.key}`, selector: { boolean: {} } }));
    return base.concat(chans);
  }

  _data() {
    const c = this._config || {};
    const hidden = new Set((c.hidden_channels || []).map((k) => String(k)));
    const d = { title: c.title || '' };
    EDITOR_TOGGLES.forEach((k) => { d[k] = c[k] !== false; });
    (this._channels || []).forEach((ch) => { d[`ch_${ch.key}`] = !hidden.has(String(ch.key)); });
    return d;
  }

  _valueChanged(ev) {
    ev.stopPropagation();
    if (!this._config) return;
    const v = ev.detail.value || {};
    const cfg = { ...this._config }; // keep type and any keys the editor doesn't manage
    if (v.title && v.title.trim()) cfg.title = v.title; else delete cfg.title;
    EDITOR_TOGGLES.forEach((k) => { if (v[k] === false) cfg[k] = false; else delete cfg[k]; });
    // hidden_channels: channels toggled off here, plus any previously-hidden key we don't know yet.
    const known = new Set((this._channels || []).map((ch) => String(ch.key)));
    const offKnown = (this._channels || []).filter((ch) => v[`ch_${ch.key}`] === false).map((ch) => String(ch.key));
    const keepUnknown = (this._config.hidden_channels || []).map(String).filter((k) => !known.has(k));
    const hidden = Array.from(new Set([...offKnown, ...keepUnknown]));
    if (hidden.length) cfg.hidden_channels = hidden; else delete cfg.hidden_channels;
    this._config = cfg;
    this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: cfg }, bubbles: true, composed: true }));
  }

  async _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    if (!customElements.get('ha-form') && window.loadCardHelpers) {
      try {
        const h = await window.loadCardHelpers();
        const c = h.createCardElement({ type: 'entities', entities: [] });
        if (c && c.constructor.getConfigElement) await c.constructor.getConfigElement();
      } catch (e) { /* ignore */ }
    }
    if (!customElements.get('ha-form')) {
      this.shadowRoot.innerHTML = '<div style="padding:12px;color:var(--error-color)">The form editor could not load. Reopen the card editor, or edit this card in YAML.</div>';
      this._form = null;
      return;
    }
    if (!this._form) {
      this.shadowRoot.innerHTML = '';
      this._note = document.createElement('div');
      this._note.style.cssText = 'font-size:.85em;color:var(--secondary-text-color);margin:4px 2px 12px;line-height:1.4';
      this._form = document.createElement('ha-form');
      this._form.computeLabel = (s) => this._label(s);
      this._form.addEventListener('value-changed', (e) => this._valueChanged(e));
      this.shadowRoot.append(this._note, this._form);
    }
    this._note.textContent = (this._channels && this._channels.length)
      ? 'Choose which probes and cookers show, and which parts of the card appear. Leave everything on for the default.'
      : 'Set up Cook Predictor to pick individual probes; you can still toggle the card’s parts below.';
    this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = this._data();
  }
}

if (!customElements.get('cook-predictor-card-editor')) {
  customElements.define('cook-predictor-card-editor', CookPredictorCardEditor);
}
