/* AInsight — AI auto-analytics dashboard view.

   The server decides which visualizations the data supports and computes every number; this file renders
   them and wires the interaction (tabs, filters, cross-filtering, chart menu, drill-down, export, personalisation).
   Nothing here calculates a business figure: tables, CSV and chart text come from the server's payload. */
(() => {
  'use strict';
  if (!window.DASHBOARD_ID) return;
  const { api, escapeHtml: esc, toast } = AInsight;
  const id = window.DASHBOARD_ID;
  const $ = (s, r) => (r || document).querySelector(s);
  const state = { data: null, filters: {}, from: '', to: '', tab: 'overview', measure: '', custom: false, layout: null, variant: {} };
  const dsId = () => state.data && state.data.dataset.id;

  /* ------------------------------------------------------------ helpers */
  const chatHref = (q) => `/chat?dataset=${encodeURIComponent(dsId())}&q=${encodeURIComponent(q)}`;
  const withFilters = (q) => {
    const parts = Object.entries(state.filters).map(([d, v]) => `${d} = ${v.join(' or ')}`);
    if (state.from || state.to) parts.push(`date ${state.from || '…'} to ${state.to || '…'}`);
    return parts.length ? `${q} (filters: ${parts.join(', ')})` : q;
  };
  const query = (extra) => {
    const p = new URLSearchParams();
    Object.entries(state.filters).forEach(([d, vals]) => vals.forEach((v) => p.append(`f_${d}`, v)));
    if (state.from) p.set('date_from', state.from);
    if (state.to) p.set('date_to', state.to);
    if (state.measure) p.set('measure', state.measure);
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
    const s = p.toString();
    return s ? `?${s}` : '';
  };
  const arrow = (c) => (c.direction === 'up' ? '▲' : c.direction === 'down' ? '▼' : '■');
  const icons = {
    menu: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="12" cy="19" r="1.8"/></svg>',
  };

  /* ---------------------------------------------------------- loading */
  function skeleton() {
    $('#dv-kpis').innerHTML = Array.from({ length: 4 }, () => '<div class="card kpi dv-skel"><div class="sk sk-s"></div><div class="sk sk-l"></div></div>').join('');
    $('#dv-charts').innerHTML = '<div class="card card-pad dv-skel dv-wide"><div class="sk sk-chart"></div></div>';
  }

  async function load(opts) {
    opts = opts || {};
    if (!opts.quiet) skeleton();
    try {
      // One request (the server computes once and caches). Paint order is the priority order:
      // KPIs and header -> insights -> the hero chart, then every other chart as it scrolls into view.
      const data = await api.get(`/api/dashboards/${id}/auto${query()}`);
      state.data = data; state.layout = data.layout || state.layout || {};
      if (!state.measure && data.layout && data.layout.measure) state.measure = data.layout.measure;
      paintHead();
      if (!data.ok) { paintEmpty(data); return; }
      paintAll();
    } catch (e) {
      $('#dv-name').textContent = 'Dashboard unavailable';
      $('#dv-sub').textContent = e.message;
    }
  }

  /* --------------------------------------------------------------- head */
  function when(ts) { try { return new Date(ts).toLocaleString(); } catch (_e) { return ts; } }

  function paintHead() {
    const d = state.data;
    $('#dv-name').textContent = d.name;
    const rows = d.rows != null ? d.rows : 0;
    $('#dv-sub').textContent = `${d.dataset.name} · ${rows.toLocaleString()} rows in view` + (d.rows_total != null && d.rows_total !== rows ? ` of ${d.rows_total.toLocaleString()}` : '') + ` · last updated ${when(d.last_refreshed || d.updated_at)}`;
    $('#dv-actions').innerHTML = `
      <button class="btn btn-sm" data-a="refresh" title="Re-read the source and recompute everything">Refresh data</button>
      <button class="btn btn-sm" data-a="story">Business story</button>
      <details class="dv-menu"><summary class="btn btn-sm">Export</summary><div class="dv-menu-pop" role="menu">
        <button data-a="exp-csv" role="menuitem">Data as CSV</button><button data-a="exp-xlsx" role="menuitem">Data as Excel</button>
        <button data-a="exp-pdf" role="menuitem">Dashboard as PDF (print)</button><button data-a="exp-png" role="menuitem">Main chart as PNG</button></div></details>
      <details class="dv-menu"><summary class="btn btn-sm">More</summary><div class="dv-menu-pop" role="menu">
        <button data-a="rename" role="menuitem">Rename</button><button data-a="dup" role="menuitem">Duplicate</button>
        <button data-a="custom" role="menuitem">${state.custom ? 'Done customizing' : 'Customize charts'}</button></div></details>`;
    $('#dv-actions').querySelectorAll('[data-a]').forEach((b) => b.addEventListener('click', () => { b.closest('details') && b.closest('details').removeAttribute('open'); action(b.dataset.a); }));
    paintFilters();
  }

  async function action(a) {
    try {
      if (a === 'refresh') { toast('Refreshing from the source…'); await api.post(`/api/dashboards/${id}/refresh`, {}); await load({ quiet: true }); toast('Dashboard refreshed.'); }
      else if (a === 'story') openStory();
      else if (a === 'exp-csv' || a === 'exp-xlsx') window.location.href = `/api/dashboards/${id}/export.${a === 'exp-csv' ? 'csv' : 'xlsx'}${query()}`;
      else if (a === 'exp-pdf') window.print();
      else if (a === 'exp-png') { const hero = (state.data.charts || []).find((c) => c.role === 'hero'); const el = hero && document.getElementById(`plot-${hero.id}`); if (el && typeof Plotly !== 'undefined') Plotly.downloadImage(el, { format: 'png', filename: hero.id, width: 1280, height: 720, scale: 2 }); else toast('The chart library is not available, so PNG export is not possible right now.', 'error'); }
      else if (a === 'rename') { const n = window.prompt('Dashboard name', state.data.name); if (n) { await api.patch(`/api/dashboards/${id}`, { name: n }); state.data.name = n; paintHead(); } }
      else if (a === 'dup') { const r = await api.post(`/api/dashboards/${id}/duplicate`, {}); window.location.href = `/dashboards/${r.dashboard.id}`; }
      else if (a === 'custom') { state.custom = !state.custom; paintHead(); paintCharts(); }
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ------------------------------------------------------------ filters */
  const hasFilters = () => Object.keys(state.filters).length > 0 || state.from || state.to;

  function paintFilters() {
    const d = state.data;
    const f = d.filters || { dims: [], date: null };
    const saved = (state.layout && state.layout.saved_filters) || [];
    const measures = d.measures || [];
    $('#dv-filters').innerHTML = `
      ${f.dims.map((dim) => {
        const sel = state.filters[dim.name] || [];
        return `<label class="small">${esc(dim.label)} <select class="select" data-dim="${esc(dim.name)}"><option value="">All</option>
          ${sel.length > 1 ? `<option value="__multi" selected>${sel.length} selected</option>` : ''}
          ${dim.values.map((v) => `<option ${sel.length === 1 && sel[0] === v ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>`;
      }).join('')}
      ${f.date ? `<label class="small">From <input type="date" class="input" id="dv-from" min="${f.date.min}" max="${f.date.max}" value="${esc(state.from)}"></label>
        <label class="small">To <input type="date" class="input" id="dv-to" min="${f.date.min}" max="${f.date.max}" value="${esc(state.to)}"></label>` : ''}
      ${measures.length > 1 ? `<label class="small">Measure <select class="select" id="dv-measure">${measures.map((m) => `<option value="${esc(m.name)}" ${(state.measure || (d.primary && d.primary.name)) === m.name ? 'selected' : ''}>${esc(m.label)}</option>`).join('')}</select></label>` : ''}
      ${!f.dims.length && !f.date ? '<span class="muted small">No filters available for this dataset.</span>' : ''}
      <span class="dv-filter-tools">
        ${saved.length ? `<select class="select" id="dv-saved" aria-label="Saved filter sets"><option value="">Saved views</option>${saved.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('')}</select>` : ''}
        <button class="btn btn-sm btn-ghost" id="dv-save-view" ${hasFilters() ? '' : 'disabled'}>Save view</button>
        <button class="btn btn-sm btn-ghost" id="dv-clear" ${hasFilters() ? '' : 'disabled'}>Clear all</button>
      </span>`;
    const root = $('#dv-filters');
    root.querySelectorAll('select[data-dim]').forEach((s) => s.addEventListener('change', () => {
      if (s.value === '__multi') return;
      if (s.value) state.filters[s.dataset.dim] = [s.value]; else delete state.filters[s.dataset.dim];
      load({ quiet: true });
    }));
    const m = $('#dv-measure'); if (m) m.addEventListener('change', async () => { state.measure = m.value; try { await api.put(`/api/dashboards/${id}/layout`, { measure: m.value }); } catch (_e) { /* non-fatal */ } load({ quiet: true }); });
    ['from', 'to'].forEach((k) => { const el = $(`#dv-${k}`); if (el) el.addEventListener('change', () => { state[k] = el.value; load({ quiet: true }); }); });
    $('#dv-clear').addEventListener('click', clearAll);
    $('#dv-save-view').addEventListener('click', saveView);
    const sv = $('#dv-saved'); if (sv) sv.addEventListener('change', () => { const s = saved[Number(sv.value)]; if (!s) return; state.filters = JSON.parse(JSON.stringify(s.filters || {})); state.from = s.date_from || ''; state.to = s.date_to || ''; load({ quiet: true }); });
    paintChips();
  }

  function paintChips() {
    const chips = [];
    Object.entries(state.filters).forEach(([dim, vals]) => vals.forEach((v) => chips.push(`<button class="chip dv-chip" data-dim="${esc(dim)}" data-val="${esc(v)}" aria-label="Remove filter ${esc(dim)} ${esc(v)}">${esc(dim)}: ${esc(v)} <span aria-hidden="true">×</span></button>`)));
    if (state.from || state.to) chips.push(`<button class="chip dv-chip" data-date="1" aria-label="Remove date filter">${esc(state.from || '…')} → ${esc(state.to || '…')} <span aria-hidden="true">×</span></button>`);
    $('#dv-chips').innerHTML = chips.length ? chips.join('') + '<button class="btn btn-sm btn-ghost" data-clear>Clear all</button>' : '';
    $('#dv-chips').querySelectorAll('.dv-chip').forEach((c) => c.addEventListener('click', () => {
      if (c.dataset.date) { state.from = ''; state.to = ''; }
      else { state.filters[c.dataset.dim] = state.filters[c.dataset.dim].filter((x) => x !== c.dataset.val); if (!state.filters[c.dataset.dim].length) delete state.filters[c.dataset.dim]; }
      load({ quiet: true });
    }));
    const cl = $('#dv-chips [data-clear]'); if (cl) cl.addEventListener('click', clearAll);
  }

  function clearAll() { state.filters = {}; state.from = ''; state.to = ''; load({ quiet: true }); }

  async function saveView() {
    const name = window.prompt('Name this filter view');
    if (!name) return;
    const saved = ((state.layout && state.layout.saved_filters) || []).concat([{ name, filters: state.filters, date_from: state.from || null, date_to: state.to || null }]);
    try { const r = await api.put(`/api/dashboards/${id}/layout`, { saved_filters: saved }); state.layout = r.layout; paintFilters(); toast('Filter view saved.'); } catch (e) { toast(e.message, 'error'); }
  }

  function toggleFilter(dim, value) {
    const cur = state.filters[dim] || [];
    state.filters[dim] = cur.includes(value) ? cur.filter((x) => x !== value) : cur.concat([value]);
    if (!state.filters[dim].length) delete state.filters[dim];
    load({ quiet: true });
  }

  /* --------------------------------------------------------------- KPIs */
  function spark(values) {
    if (!values || values.length < 2) return '';
    const w = 96; const h = 28; const lo = Math.min(...values); const hi = Math.max(...values); const span = hi - lo || 1;
    const pts = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${(h - 3 - (v - lo) / span * (h - 6)).toFixed(1)}`).join(' ');
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Trend over the last ${values.length} periods"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }

  function paintKpis() {
    $('#dv-kpis').innerHTML = state.data.kpis.map((k) => {
      const c = k.change;
      const tone = c ? (k.tone === 'good-up' ? (c.direction === 'up' ? 'good' : c.direction === 'down' ? 'bad' : 'flat') : 'flat') : '';
      const tip = `${k.exact} · ${k.how.operation} over ${k.how.rows.toLocaleString()} rows · source: ${k.source}` + (c ? ` · ${c.note}` : '');
      return `<a class="card kpi dv-kpi" href="${chatHref(withFilters(k.open_q))}" title="${esc(tip)}" data-kpi="${esc(k.id)}">
        <div class="kpi-label">${esc(k.label)}</div>
        <div class="kpi-value" data-exact="${esc(k.exact)}">${esc(k.display)}</div>
        <div class="dv-kpi-row">
          ${c ? `<span class="kpi-delta ${tone}" title="${esc(c.note)}">${arrow(c)} ${Math.abs(c.pct)}% <span class="kpi-prev">${esc(c.label)}</span></span>` : '<span class="kpi-delta flat">&nbsp;</span>'}
          <span class="dv-spark ${tone}">${spark(k.sparkline)}</span>
        </div>
        <div class="kpi-note small muted">${esc(k.how.operation)} · ${esc(k.source)}</div></a>`;
    }).join('');
  }

  /* ------------------------------------------------------- insight cards */
  function insightCard(i) {
    return `<div class="card card-pad dv-insight dv-${esc(i.kind)}"><div class="dv-insight-title">${esc(i.title)}</div><p>${esc(i.text)}</p>
      <div class="dv-insight-actions">${i.filter ? `<button class="btn btn-sm btn-ghost" data-filter-dim="${esc(Object.keys(i.filter)[0])}" data-filter-val="${esc(Object.values(i.filter)[0])}">Filter to ${esc(Object.values(i.filter)[0])}</button>` : ''}
      <a class="btn btn-sm btn-ghost" href="${chatHref(withFilters(i.ask))}">Why? Ask AInsight →</a></div></div>`;
  }

  function wireInsightFilters(root) {
    root.querySelectorAll('[data-filter-dim]').forEach((b) => b.addEventListener('click', () => { state.filters[b.dataset.filterDim] = [b.dataset.filterVal]; load({ quiet: true }); }));
  }

  function whatChanged() {
    const w = state.data.what_changed;
    if (!w) return `<div class="card card-pad dv-changed"><h3>What changed?</h3>${emptyBody({ title: 'No change analysis', why: 'A date column with at least two complete periods is needed to say what changed.', try: ['Upload data with an order or transaction date', 'Clear date filters'] })}</div>`;
    return `<div class="card card-pad dv-changed"><h3>What changed?</h3><p class="dv-headline">${esc(w.headline)}</p>
      <ul class="dv-changes">${w.items.map((it) => `<li><strong>${esc(it.title)}</strong> ${esc(it.text)} <a href="${chatHref(withFilters(it.ask))}" class="small">Why did this change? →</a></li>`).join('')}</ul></div>`;
  }

  function paintInsights() {
    const host = $('#dv-insights');
    const d = state.data;
    if (state.tab === 'overview') {
      const top = d.insights.slice(0, 4);
      host.innerHTML = `<section class="dv-section"><h3 class="dv-h">AI-detected business insights</h3><div class="dv-insights">${top.map(insightCard).join('') || '<p class="muted small">No grounded insight could be derived from this selection.</p>'}</div></section>`;
      wireInsightFilters(host);
    } else if (state.tab === 'insights') {
      host.innerHTML = `<section class="dv-section"><h3 class="dv-h">AI-detected business insights</h3><div class="dv-insights">${d.insights.map(insightCard).join('') || '<p class="muted small">No grounded insight could be derived from this selection.</p>'}</div></section>
        <section class="dv-section">${whatChanged()}</section>`;
      wireInsightFilters(host);
    } else host.innerHTML = '';
  }

  /* --------------------------------------------------------- empty states */
  function emptyBody(u) {
    return `<div class="dv-empty"><div class="dv-empty-title">${esc(u.title)}</div><p><strong>Why?</strong> ${esc(u.why)}</p>
      ${(u.try || []).length ? `<p class="dv-try"><strong>Try:</strong></p><ul>${u.try.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      ${u.action ? (u.action.kind === 'clear-filters' ? '<button class="btn btn-sm" data-clear-filters>Clear filters</button>' : `<a class="btn btn-sm" href="${esc(u.action.href || '#')}">${esc(u.action.label)}</a>`) : ''}</div>`;
  }

  const TAB_OF = { trend: 'trends', breakdown: 'breakdown', composition: 'breakdown', sankey: 'breakdown', relationship: 'performance', waterfall: 'performance', gauge: 'performance', funnel: 'performance', distribution: 'distribution' };

  function paintEmpty(d) {
    $('#dv-kpis').innerHTML = ''; $('#dv-tabs').innerHTML = ''; $('#dv-insights').innerHTML = '';
    $('#dv-charts').innerHTML = `<div class="card card-pad dv-wide">${emptyBody({ title: d.empty ? 'No records match these filters' : 'Dashboard cannot be built', why: d.reason, try: d.empty ? ['Remove a filter', 'Widen the date range'] : ['Upload a dataset with at least one numeric column'], action: d.empty ? { kind: 'clear-filters', label: 'Clear filters' } : null })}</div>`;
    $('#dv-extra').innerHTML = '';
    wireEmpty();
  }

  function wireEmpty() { document.querySelectorAll('[data-clear-filters]').forEach((b) => b.addEventListener('click', clearAll)); }

  /* ----------------------------------------------------------------- tabs */
  function paintTabs() {
    const tabs = state.data.tabs;
    if (!tabs.find((t) => t.id === state.tab)) state.tab = 'overview';
    $('#dv-tabs').innerHTML = tabs.map((t) => `<button role="tab" class="dv-tab" aria-selected="${t.id === state.tab}" data-tab="${t.id}">${esc(t.label)}${t.count ? ` <span class="dv-count">${t.count}</span>` : ''}</button>`).join('');
    $('#dv-tabs').querySelectorAll('.dv-tab').forEach((b) => b.addEventListener('click', () => { state.tab = b.dataset.tab; paintTabs(); paintInsights(); paintCharts(); paintExtra(); }));
  }

  /* --------------------------------------------------------------- charts */
  const ctxFor = (c) => {
    const d = state.data;
    return {
      toolbar: false, table: c.table, dataset: d.dataset.name, operation: c.how && c.how.operation, rows: c.how && c.how.rows,
      insight: (c.explain || []).join(' '), subtitle: c.why_chosen, dateRange: d.quality && d.quality.date_start ? `${d.quality.date_start} → ${d.quality.date_end}` : '',
      onAsk: () => { window.location.href = chatHref(withFilters(c.ask_q)); },
      onPoint: c.drill ? (pt) => pointMenu(c, pt) : null,
    };
  };

  function orderedCharts() {
    const L = state.layout || {}; const hidden = new Set(L.hidden || []); const fav = new Set(L.favorites || []);
    const order = L.order || [];
    let list = state.data.charts.filter((c) => c.tab === state.tab);
    if (state.tab === 'overview') list = state.data.charts.filter((c) => c.tab === 'overview');
    const rank = (c) => (order.indexOf(c.id) >= 0 ? order.indexOf(c.id) : 999 + state.data.charts.indexOf(c));
    list.sort((a, b) => (a.role === 'hero' ? -1 : b.role === 'hero' ? 1 : (fav.has(b.id) - fav.has(a.id)) || rank(a) - rank(b)));
    return { visible: list.filter((c) => !hidden.has(c.id)), hidden: list.filter((c) => hidden.has(c.id)) };
  }

  function cardHtml(c) {
    const L = state.layout || {}; const fav = (L.favorites || []).includes(c.id); const size = (L.sizes || {})[c.id] || 'medium';
    const variants = c.variants ? ['top5', 'top10', 'bottom5', 'all'].filter((k) => c.variants[k]) : [];
    const cur = state.variant[c.id] || c.default_variant;
    const names = { top5: 'Top 5', top10: 'Top 10', bottom5: 'Bottom 5', all: 'View all' };
    return `<article class="card dv-card ${c.wide || c.role === 'hero' ? 'dv-wide' : ''} dv-size-${size} ${c.role === 'hero' ? 'dv-hero' : ''}" data-chart="${esc(c.id)}">
      <header class="dv-card-head"><div><h4>${esc(c.title)}</h4>${c.role === 'hero' ? '<span class="dv-badge">Main view</span>' : ''}</div>
        <div class="dv-card-tools">
          <button class="btn btn-sm btn-ghost dv-star ${fav ? 'on' : ''}" data-act="fav" aria-pressed="${fav}" title="${fav ? 'Unfavorite' : 'Favorite'}">${fav ? '★' : '☆'}</button>
          <details class="dv-menu"><summary class="btn btn-sm btn-ghost" aria-label="Chart menu for ${esc(c.title)}">${icons.menu}</summary>
            <div class="dv-menu-pop dv-menu-right" role="menu">
              <button role="menuitem" data-act="expand">Expand</button><button role="menuitem" data-act="full">Fullscreen</button>
              <button role="menuitem" data-act="png">Download PNG</button><button role="menuitem" data-act="csv">Download CSV</button>
              <button role="menuitem" data-act="data">View data</button><button role="menuitem" data-act="open">Open analysis</button>
              <button role="menuitem" data-act="ask">Ask AInsight</button><button role="menuitem" data-act="hide">Hide chart</button></div></details>
        </div></header>
      ${variants.length ? `<div class="dv-variants" role="group" aria-label="Show">${variants.map((k) => `<button class="btn btn-sm ${k === cur ? 'btn-primary' : ''}" data-variant="${k}" aria-pressed="${k === cur}">${names[k]}</button>`).join('')}</div>` : ''}
      <div class="dv-plot" id="plot-${esc(c.id)}"></div>
      ${state.custom ? `<div class="dv-custom" role="group" aria-label="Customize"><button class="btn btn-sm" data-act="up">↑ Earlier</button><button class="btn btn-sm" data-act="down">↓ Later</button>
        ${['small', 'medium', 'large'].map((s) => `<button class="btn btn-sm ${s === size ? 'btn-primary' : ''}" data-size="${s}">${s[0].toUpperCase() + s.slice(1)}</button>`).join('')}</div>` : ''}
      <details class="dv-more"><summary>How calculated · Explain this chart</summary>
        <dl class="dv-how"><dt>Operation</dt><dd><code>${esc(c.how.operation)}</code></dd><dt>Rows</dt><dd>${c.how.rows.toLocaleString()}</dd><dt>Engine</dt><dd>${esc(c.how.engine)} · ${esc(c.how.dataset)}</dd>
        <dt>Why this chart</dt><dd>${esc(c.why_chosen || '')}</dd></dl>
        <ul class="dv-explain">${(c.explain || []).map((t) => `<li>${esc(t)}</li>`).join('')}</ul></details></article>`;
  }

  function renderPlot(c) {
    const fig = (c.variants && (state.variant[c.id] || c.default_variant) && c.variants[state.variant[c.id] || c.default_variant]) || c.figure;
    AInsightCharts.render(`plot-${c.id}`, fig, ctxFor(c));
  }

  function paintCharts() {
    const host = $('#dv-charts'); const heroHost = $('#dv-hero');
    heroHost.innerHTML = '';
    if (state.tab === 'details' || state.tab === 'insights') { host.innerHTML = ''; return; }
    const { visible, hidden } = orderedCharts();
    const hero = visible.find((c) => c.role === 'hero');
    const rest = visible.filter((c) => c !== hero);
    const unavailable = state.data.unavailable.filter((u) => (TAB_OF[u.kind] || 'overview') === state.tab || (state.tab === 'overview' && !visible.length));
    if (hero) heroHost.innerHTML = cardHtml(hero);
    host.innerHTML = rest.map(cardHtml).join('')
      + unavailable.map((u) => `<div class="card card-pad dv-card dv-unavail">${emptyBody(u)}</div>`).join('')
      + (hidden.length ? `<div class="dv-wide dv-hidden"><span class="muted small">Hidden charts:</span> ${hidden.map((c) => `<button class="chip" data-unhide="${esc(c.id)}">${esc(c.title)} · show</button>`).join('')}</div>` : '');
    if (!visible.length && !unavailable.length) host.innerHTML = '<div class="card card-pad dv-wide muted">Nothing to show on this view for the current selection.</div>';
    const lazy = typeof IntersectionObserver !== 'undefined';
    const io = lazy ? new IntersectionObserver((entries) => entries.forEach((en) => {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      const c = state.data.charts.find((x) => x.id === en.target.dataset.chart);
      if (c) renderPlot(c);
    }), { rootMargin: '400px' }) : null;
    if (hero) renderPlot(hero);
    rest.forEach((c) => {
      const card = host.querySelector(`.dv-card[data-chart="${CSS.escape(c.id)}"]`);
      if (!lazy || !card) renderPlot(c); else io.observe(card);
    });
    wireCards(heroHost); wireCards(host);
    wireEmpty();
  }

  async function saveLayout(patch) {
    state.layout = Object.assign({}, state.layout, patch);
    try { await api.put(`/api/dashboards/${id}/layout`, patch); } catch (e) { toast(e.message, 'error'); }
  }

  function wireCards(host) {
    host.querySelectorAll('[data-unhide]').forEach((b) => b.addEventListener('click', async () => { await saveLayout({ hidden: (state.layout.hidden || []).filter((x) => x !== b.dataset.unhide) }); paintCharts(); }));
    host.querySelectorAll('.dv-card[data-chart]').forEach((card) => {
      const c = state.data.charts.find((x) => x.id === card.dataset.chart);
      const el = card.querySelector('.dv-plot');
      const entry = () => ({ figure: (c.variants && c.variants[state.variant[c.id] || c.default_variant]) || c.figure, ctx: ctxFor(c) });
      card.querySelectorAll('[data-variant]').forEach((b) => b.addEventListener('click', () => { state.variant[c.id] = b.dataset.variant; paintCharts(); }));
      card.querySelectorAll('[data-size]').forEach((b) => b.addEventListener('click', async () => { await saveLayout({ sizes: Object.assign({}, state.layout.sizes, { [c.id]: b.dataset.size }) }); paintCharts(); }));
      card.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', async () => {
        const det = b.closest('details'); if (det) det.removeAttribute('open');
        const a = b.dataset.act; const e = entry();
        if (a === 'expand') AInsightCharts.openModal(e);
        else if (a === 'full') { AInsightCharts.openModal(e); const f = document.querySelector('[data-modal="fullscreen"]'); if (f) f.click(); }
        else if (a === 'png') { if (typeof Plotly !== 'undefined' && Plotly.downloadImage) Plotly.downloadImage(el, { format: 'png', filename: c.id, width: 1280, height: 720, scale: 2 }); else toast('The chart library is not available, so PNG download is not possible right now.', 'error'); }
        else if (a === 'csv') { const blob = new Blob([AInsightCharts.csvOf(c.table)], { type: 'text/csv;charset=utf-8' }); const u = URL.createObjectURL(blob); const l = document.createElement('a'); l.href = u; l.download = `${c.id}.csv`; document.body.appendChild(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(u), 1000); }
        else if (a === 'data') { let p = card.querySelector('.chart-data'); if (p) p.remove(); else { p = document.createElement('div'); p.className = 'chart-data'; card.insertBefore(p, card.querySelector('.dv-more')); AInsightCharts.dataTable(p, c.table); } }
        else if (a === 'open') window.location.href = chatHref(withFilters(c.open_q));
        else if (a === 'ask') window.location.href = chatHref(withFilters(c.ask_q));
        else if (a === 'fav') { const f = new Set(state.layout.favorites || []); f.has(c.id) ? f.delete(c.id) : f.add(c.id); await saveLayout({ favorites: [...f] }); paintCharts(); }
        else if (a === 'hide') { await saveLayout({ hidden: (state.layout.hidden || []).concat([c.id]) }); paintCharts(); }
        else if (a === 'up' || a === 'down') {
          const ids = orderedCharts().visible.map((x) => x.id); const i = ids.indexOf(c.id); const j = a === 'up' ? i - 1 : i + 1;
          if (j < 0 || j >= ids.length) return; [ids[i], ids[j]] = [ids[j], ids[i]]; await saveLayout({ order: ids }); paintCharts();
        }
      }));
    });
  }

  /* ------------------------------------------------- chart → drill-down */
  let pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePop(); });
  document.addEventListener('click', (e) => { if (pop && !pop.contains(e.target) && !e.target.closest('.js-plotly-plot')) closePop(); });

  function pointMenu(c, pt) {
    closePop();
    const dim = c.drill.dimension; const label = pt.label;
    if (!c.drill.values.includes(label)) return;
    const other = (state.data.filters.dims || []).find((x) => x.name !== dim);
    const hasDate = !!state.data.quality.date_column;
    const active = (state.filters[dim] || []).includes(label);
    const p = state.data.primary;
    const value = pt.value != null && isFinite(pt.value) ? ` — ${AInsight.number ? AInsight.number(pt.value) : pt.value}` : '';
    pop = document.createElement('div');
    pop.className = 'drill-pop dv-pop'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', `Explore ${label}`);
    pop.innerHTML = `<div class="drill-title">${esc(label)}<span class="muted small">${esc(value)}</span></div><div class="drill-body">
      <button class="drill-q" data-d="filter">${active ? 'Remove filter' : 'Filter dashboard to'} ${esc(label)}</button>
      <button class="drill-q" data-d="analyze">Analyze ${esc(label)}</button>
      ${other ? `<button class="drill-q" data-d="break">Break ${esc(label)} down by ${esc(other.label)}</button>` : ''}
      ${hasDate ? `<button class="drill-q" data-d="trend">View monthly trend for ${esc(label)}</button>` : ''}
      <button class="drill-q" data-d="ask">Ask AInsight about ${esc(label)}</button></div>`;
    document.body.appendChild(pop);
    const w = 300; pop.style.left = `${Math.max(8, Math.min((pt.x || window.innerWidth / 2) - 20, window.innerWidth - w - 8))}px`;
    pop.style.top = `${Math.max(8, Math.min((pt.y || 200) + 12, window.innerHeight - 260))}px`;
    const scoped = `${p.label} for ${A(dim)} ${label}`;
    pop.querySelectorAll('[data-d]').forEach((b) => b.addEventListener('click', () => {
      const k = b.dataset.d; closePop();
      if (k === 'filter') { toggleFilter(dim, label); return; }
      if (k === 'trend') { if (!(state.filters[dim] || []).includes(label)) state.filters[dim] = [label]; state.tab = 'overview'; load({ quiet: true }); return; }
      if (k === 'break') { state.filters[dim] = [label]; state.tab = 'breakdown'; load({ quiet: true }); return; }
      window.location.href = chatHref(withFilters(k === 'analyze' ? `Analyze ${scoped}` : `Tell me about ${scoped}: what is driving it?`));
    }));
    const first = pop.querySelector('.drill-q'); if (first) first.focus();
  }
  const A = (s) => String(s).replace(/_/g, ' ');

  /* ----------------------------------------- details, quality, next steps */
  function paintExtra() {
    const host = $('#dv-extra'); const d = state.data;
    if (state.tab === 'details') {
      host.innerHTML = `<section class="card card-pad"><h3 class="dv-h">Underlying records</h3><p class="muted small">${d.details.total.toLocaleString()} rows match the current filters${d.details.total > d.details.rows.length ? `; the first ${d.details.rows.length} are shown. Export CSV or Excel for all of them.` : '.'}</p><div id="dv-detail-table"></div></section>`;
      AInsightCharts.dataTable($('#dv-detail-table'), { columns: d.details.columns, rows: d.details.rows }, 15, 2);
    } else if (state.tab === 'insights') {
      const q = d.quality;
      const unav = d.unavailable;
      host.innerHTML = `<section class="dv-section"><h3 class="dv-h">Recommended next analyses</h3><div class="dv-recs">${d.recommendations.map((r) => `<a class="btn btn-sm" href="${esc(r.href)}">${esc(r.label)}</a>`).join('') || '<span class="muted small">No further analyses suggested.</span>'}</div></section>
        <section class="card card-pad dv-section dv-quality"><h3 class="dv-h">Data quality</h3><dl class="dv-quality-grid">
          <div><dt>Rows analyzed</dt><dd>${q.rows_analyzed.toLocaleString()} of ${q.rows_total.toLocaleString()}</dd></div>
          <div><dt>Valid numeric values</dt><dd>${q.valid_numeric_pct == null ? '—' : q.valid_numeric_pct + '%'}</dd></div>
          <div><dt>Missing values</dt><dd>${q.missing_pct}%</dd></div>
          <div><dt>Date coverage</dt><dd>${q.date_start ? `${esc(q.date_start)} → ${esc(q.date_end)}` : 'No usable date column'}</dd></div>
          <div><dt>Dimensions</dt><dd>${q.dimensions}</dd></div><div><dt>Measures</dt><dd>${q.measures}</dd></div>
          <div><dt>Duplicate rows</dt><dd>${q.duplicate_rows == null ? '—' : q.duplicate_rows.toLocaleString()}</dd></div></dl></section>
        ${unav.length ? `<section class="dv-section"><h3 class="dv-h">Visualizations the data does not support</h3><div class="dv-unavail-grid">${unav.map((u) => `<div class="card card-pad">${emptyBody(u)}</div>`).join('')}</div></section>` : ''}`;
      wireEmpty();
    } else host.innerHTML = '';
  }

  /* ----------------------------------------------------------- story modal */
  function openStory() {
    const s = state.data.story; if (!s) return;
    let m = $('#dv-story'); if (m) m.remove();
    m = document.createElement('div'); m.id = 'dv-story'; m.className = 'chart-modal'; m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true'); m.setAttribute('aria-label', s.title);
    m.innerHTML = `<div class="chart-modal-panel dv-story-panel"><header class="chart-modal-head"><div class="chart-modal-titles"><h2>${esc(s.title)}</h2><p class="muted small">${esc(s.note)}</p></div>
      <div class="chart-modal-actions"><button class="btn btn-sm" data-s="copy">Copy</button><button class="btn btn-sm btn-icon" data-s="close" aria-label="Close story">✕</button></div></header>
      <div class="dv-story-body">${s.sections.map((x) => `<section><h3>${esc(x.title)}</h3><ul>${x.points.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></section>`).join('')}</div></div>`;
    document.body.appendChild(m);
    const close = () => { m.remove(); document.removeEventListener('keydown', esc2); };
    const esc2 = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', esc2);
    m.addEventListener('click', (e) => { if (e.target === m) close(); });
    m.querySelector('[data-s="close"]').addEventListener('click', close);
    m.querySelector('[data-s="copy"]').addEventListener('click', () => { const t = `${s.title}\n\n` + s.sections.map((x) => `${x.title}\n` + x.points.map((p) => `- ${p}`).join('\n')).join('\n\n'); if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => toast('Story copied.')); });
    m.querySelector('[data-s="close"]').focus();
  }

  /* ---------------------------------------------------------------- paint */
  
  function paintAll() { paintHead(); paintKpis(); paintTabs(); paintInsights(); paintCharts(); paintExtra(); }

  document.addEventListener('DOMContentLoaded', () => load());
  document.addEventListener('ainsight:theme', () => AInsightCharts.retheme());
})();
