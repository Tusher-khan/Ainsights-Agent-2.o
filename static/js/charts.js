/* AInsight — Plotly rendering, theme awareness and the chart toolbar.

   Figures always arrive fully specified from the server. This module applies theme colours,
   adds interaction (expand, zoom, download, data view, drill-down, ask-about-chart) and never
   invents or recalculates a business number: tables and CSV show the figure's own data, and
   Excel / full-result downloads come from the server's stored, validated result. */

const AInsightCharts = (() => {
  'use strict';

  const registry = new Map();      // element id -> { figure, ctx }
  let canAsk = false;              // the chat page switches this on; other pages have nothing to ask
  let modal = null;

  const ICONS = {
    expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    zoom: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2M11 8.5v5M8.5 11h5"/>',
    reset: '<path d="M4 12a8 8 0 1 0 2.5-5.8L4 8.5"/><path d="M4 4v4.5h4.5"/>',
    data: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16M10 10v9"/>',
    png: '<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m5 17 5-4 3 2.5 3-3 3 3"/>',
    csv: '<path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/>',
    xls: '<path d="M6 3h8l4 4v14H6z"/><path d="m9.5 12 5 6m0-6-5 6"/>',
    ask: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-5.5A8 8 0 1 1 21 12Z"/><path d="M12 9v3.2M12 15h.01"/>',
    full: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
  };
  const icon = (name) => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const plotlyReady = () => typeof Plotly !== 'undefined';

  function palette() {
    const styles = getComputedStyle(document.documentElement);
    return {
      font: styles.getPropertyValue('--ink-2').trim() || '#33415C',
      grid: styles.getPropertyValue('--line').trim() || '#E3E8EF',
      accent: styles.getPropertyValue('--accent').trim() || '#2F5AE0',
    };
  }

  function themed(figure, extra) {
    const colors = palette();
    const layout = JSON.parse(JSON.stringify(figure.layout || {}));
    layout.paper_bgcolor = 'rgba(0,0,0,0)';
    layout.plot_bgcolor = 'rgba(0,0,0,0)';
    layout.font = Object.assign({}, layout.font, { color: colors.font });
    ['xaxis', 'yaxis'].forEach((axis) => {
      if (layout[axis]) {
        layout[axis].gridcolor = colors.grid;
        layout[axis].linecolor = colors.grid;
        layout[axis].zerolinecolor = colors.grid;
      }
    });
    if (layout.title && layout.title.font) layout.title.font.color = colors.font;
    return Object.assign(layout, extra || {});
  }

  /* ----------------------------------------------------------- figure data */

  function kindOf(figure) {
    const t = ((figure.data || [])[0] || {}).type || 'scatter';
    if (t === 'pie') return 'pie';
    if (t === 'bar') return 'bar';
    if (t === 'scatter' || t === 'scattergl') return ((figure.data[0].mode || '').indexOf('lines') >= 0 ? 'line' : 'scatter');
    return t;
  }

  function titleOf(figure) {
    const t = figure.layout && figure.layout.title;
    return (t && (t.text || (typeof t === 'string' ? t : ''))) || 'Chart';
  }

  /** The chart's own points as a flat table. A re-presentation of the figure, not a recalculation. */
  function tabulate(figure, ctx) {
    if (ctx && ctx.table && ctx.table.columns) return ctx.table;   // the server's own validated table (full, not just the plotted subset)
    const traces = figure.data || [];
    const rows = [];
    let columns;
    if (traces.length && traces[0].type === 'pie') {
      columns = ['Category', 'Value'];
      (traces[0].labels || []).forEach((l, i) => rows.push({ Category: l, Value: (traces[0].values || [])[i] }));
      return { columns, rows };
    }
    const multi = traces.length > 1;
    columns = multi ? ['Series', 'X', 'Y'] : ['X', 'Y'];
    traces.forEach((t) => {
      const horizontal = t.orientation === 'h';
      const xs = t.x || [];
      const ys = t.y || [];
      const n = Math.max(xs.length, ys.length);
      for (let i = 0; i < n; i += 1) {
        const row = {};
        if (multi) row.Series = t.name || '';
        row.X = xs[i];
        row.Y = ys[i];
        rows.push(row);
      }
      if (horizontal) columns = multi ? ['Series', 'Value', 'Category'] : ['Value', 'Category'];
    });
    if (columns.indexOf('Value') >= 0) rows.forEach((r) => { const x = r.X; r.Value = x; r.Category = r.Y; delete r.X; delete r.Y; });
    return { columns, rows };
  }

  function csvOf(table) {
    const cell = (v) => {
      let s = v == null ? '' : String(v);
      if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;          // neutralise spreadsheet formulas
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [table.columns.map(cell).join(',')].concat(table.rows.map((r) => table.columns.map((c) => cell(r[c])).join(','))).join('\n');
  }

  function saveBlob(name, text, type) {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const slug = (s) => String(s || 'chart').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'chart';

  /* --------------------------------------------------------------- actions */

  const actions = {
    png(el, entry) {
      if (!plotlyReady() || !Plotly.downloadImage) return;
      Plotly.downloadImage(el, { format: 'png', filename: slug(titleOf(entry.figure)), width: 1280, height: 720, scale: 2 });
    },
    csv(el, entry) {
      const ctx = entry.ctx || {};
      if (ctx.historyId) { window.location.href = `/api/download/${encodeURIComponent(ctx.historyId)}.csv`; return; }
      saveBlob(`${slug(titleOf(entry.figure))}.csv`, csvOf(tabulate(entry.figure, entry.ctx)), 'text/csv;charset=utf-8');
    },
    xlsx(el, entry) {
      const ctx = entry.ctx || {};
      if (ctx.historyId) window.location.href = `/api/download/${encodeURIComponent(ctx.historyId)}.xlsx`;
    },
    zoom(el, entry, button) {
      if (!plotlyReady() || !Plotly.relayout) return;
      const pan = entry.dragmode === 'pan';
      entry.dragmode = pan ? 'zoom' : 'pan';
      Plotly.relayout(el, { dragmode: entry.dragmode });
      if (button) { button.setAttribute('aria-pressed', String(!pan)); button.querySelector('span').textContent = pan ? 'Zoom' : 'Pan'; }
    },
    reset(el) {
      if (plotlyReady() && Plotly.relayout) Plotly.relayout(el, { 'xaxis.autorange': true, 'yaxis.autorange': true });
    },
    ask(el, entry) {
      if (entry.ctx && typeof entry.ctx.onAsk === 'function') { entry.ctx.onAsk(entry.ctx); return; }
      document.dispatchEvent(new CustomEvent('ainsight:chart-ask', { detail: { historyId: (entry.ctx || {}).historyId, title: titleOf(entry.figure) } }));
    },
    expand(el, entry) { openModal(entry); },
    data(el, entry, button) {
      const host = el.closest('.chart-card');
      if (!host) return;
      let panel = host.querySelector('.chart-data');
      if (panel) { panel.remove(); if (button) button.setAttribute('aria-pressed', 'false'); return; }
      panel = document.createElement('div');
      panel.className = 'chart-data';
      host.appendChild(panel);
      dataTable(panel, tabulate(entry.figure, entry.ctx));
      if (button) button.setAttribute('aria-pressed', 'true');
    },
  };

  const TOOLS = [
    ['expand', 'Expand', true], ['zoom', 'Zoom', false], ['reset', 'Reset zoom', false],
    ['data', 'Show data', true], ['png', 'PNG', true], ['csv', 'CSV', true], ['xlsx', 'Excel', true], ['ask', 'Ask AInsight', true],
  ];

  function toolbarHtml(kind, ctx, skip) {
    const cartesian = kind !== 'pie';
    return TOOLS.filter(([name]) => {
      if (skip && skip.indexOf(name) >= 0) return false;
      if ((name === 'zoom' || name === 'reset') && !cartesian) return false;
      if (name === 'xlsx' && !(ctx && ctx.historyId)) return false;
      if (name === 'ask' && !((canAsk && ctx && ctx.historyId) || (ctx && typeof ctx.onAsk === 'function'))) return false;
      return true;
    }).map(([name, label]) => `
      <button type="button" class="chart-tool${name === 'ask' ? ' chart-tool-ask' : ''}" data-chart-action="${name}" title="${esc(label)}" aria-label="${esc(label)}"${name === 'zoom' || name === 'data' ? ' aria-pressed="false"' : ''}>
        ${icon(name === 'xlsx' ? 'xls' : name)}<span>${esc(label)}</span>
      </button>`).join('');
  }

  /* --------------------------------------------------------- data table UI */

  /** Sortable, searchable, paginated table with column visibility. Works on whatever rows it is given. */
  function dataTable(host, table, pageSize, digits) {
    const size = pageSize || 10;
    const maxDigits = digits == null ? 4 : digits;
    const state = { sort: null, dir: 1, q: '', page: 0, hidden: new Set() };
    host.classList.add('dt');
    host.innerHTML = `
      <div class="dt-bar">
        <input type="search" class="input dt-search" placeholder="Search rows" aria-label="Search rows">
        <details class="dt-cols"><summary>Columns</summary><div class="dt-cols-list"></div></details>
        <span class="dt-count small muted"></span>
      </div>
      <div class="table-wrap"><table class="data"><thead></thead><tbody></tbody></table></div>
      <div class="dt-pager"><button type="button" class="btn btn-sm" data-dt="prev">Previous</button><span class="small muted dt-page"></span><button type="button" class="btn btn-sm" data-dt="next">Next</button></div>`;
    const head = host.querySelector('thead');
    const body = host.querySelector('tbody');
    host.querySelector('.dt-cols-list').innerHTML = table.columns.map((c) =>
      `<label><input type="checkbox" checked data-col="${esc(c)}"> ${esc(c)}</label>`).join('');

    function view() {
      const q = state.q.toLowerCase();
      let rows = table.rows.filter((r) => !q || table.columns.some((c) => String(r[c] == null ? '' : r[c]).toLowerCase().indexOf(q) >= 0));
      if (state.sort) {
        const c = state.sort;
        rows = rows.slice().sort((a, b) => {
          const x = a[c]; const y = b[c];
          const nx = typeof x === 'number'; const ny = typeof y === 'number';
          if (nx && ny) return (x - y) * state.dir;
          return String(x == null ? '' : x).localeCompare(String(y == null ? '' : y), undefined, { numeric: true }) * state.dir;
        });
      }
      return rows;
    }

    function paint() {
      const rows = view();
      const pages = Math.max(1, Math.ceil(rows.length / size));
      state.page = Math.min(state.page, pages - 1);
      const cols = table.columns.filter((c) => !state.hidden.has(c));
      head.innerHTML = `<tr>${cols.map((c) => `<th scope="col" tabindex="0" role="columnheader" aria-sort="${state.sort === c ? (state.dir > 0 ? 'ascending' : 'descending') : 'none'}" data-sort="${esc(c)}">${esc(c)}${state.sort === c ? (state.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr>`;
      body.innerHTML = rows.slice(state.page * size, (state.page + 1) * size).map((r) =>
        `<tr>${cols.map((c) => `<td>${esc(typeof r[c] === 'number' ? r[c].toLocaleString(undefined, { maximumFractionDigits: maxDigits }) : r[c])}</td>`).join('')}</tr>`).join('')
        || `<tr><td colspan="${Math.max(cols.length, 1)}" class="muted">No rows match.</td></tr>`;
      host.querySelector('.dt-count').textContent = `${rows.length} row${rows.length === 1 ? '' : 's'}`;
      host.querySelector('.dt-page').textContent = `Page ${state.page + 1} of ${pages}`;
      host.querySelector('[data-dt="prev"]').disabled = state.page === 0;
      host.querySelector('[data-dt="next"]').disabled = state.page >= pages - 1;
      host.querySelector('.dt-pager').hidden = pages <= 1;
    }

    host.querySelector('.dt-search').addEventListener('input', (e) => { state.q = e.target.value; state.page = 0; paint(); });
    head.addEventListener('click', (e) => {
      const th = e.target.closest('[data-sort]'); if (!th) return;
      const c = th.dataset.sort; state.dir = state.sort === c ? -state.dir : 1; state.sort = c; paint();
    });
    head.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.target.click(); } });
    host.querySelector('.dt-cols-list').addEventListener('change', (e) => {
      const c = e.target.dataset.col;
      if (e.target.checked) state.hidden.delete(c); else if (table.columns.length - state.hidden.size > 1) state.hidden.add(c); else e.target.checked = true;
      paint();
    });
    host.querySelector('[data-dt="prev"]').addEventListener('click', () => { state.page -= 1; paint(); });
    host.querySelector('[data-dt="next"]').addEventListener('click', () => { state.page += 1; paint(); });
    paint();
  }

  /* ------------------------------------------------------------ expand view */

  function ensureModal() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.className = 'chart-modal';
    modal.hidden = true;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'chart-modal-title');
    modal.innerHTML = `
      <div class="chart-modal-panel">
        <header class="chart-modal-head">
          <div class="chart-modal-titles"><h2 id="chart-modal-title"></h2><p class="chart-modal-sub muted"></p></div>
          <div class="chart-modal-actions">
            <button type="button" class="btn btn-sm" data-modal="fullscreen">${icon('full')} <span>Fullscreen</span></button>
            <button type="button" class="btn btn-sm btn-icon" data-modal="close" aria-label="Close expanded chart">${icon('close')}</button>
          </div>
        </header>
        <div class="chart-modal-body">
          <div class="chart-modal-main">
            <div class="chart-modal-tools"></div>
            <div class="chart-modal-plot" id="chart-modal-plot"></div>
            <div class="chart-modal-data" hidden></div>
          </div>
          <aside class="chart-modal-side"></aside>
        </div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    modal.querySelector('[data-modal="close"]').addEventListener('click', closeModal);
    modal.querySelector('[data-modal="fullscreen"]').addEventListener('click', () => {
      const panel = modal.querySelector('.chart-modal-panel');
      if (document.fullscreenElement) document.exitFullscreen();
      else if (panel.requestFullscreen) panel.requestFullscreen().catch(() => {});
      setTimeout(() => { const p = document.getElementById('chart-modal-plot'); if (plotlyReady() && Plotly.Plots) Plotly.Plots.resize(p); }, 250);
    });
    document.addEventListener('keydown', (e) => {
      if (modal.hidden) return;
      if (e.key === 'Escape' && !document.fullscreenElement) { closeModal(); return; }
      if (e.key === 'Tab') {
        const f = Array.from(modal.querySelectorAll('button, [href], input, select, summary, [tabindex]:not([tabindex="-1"])')).filter((n) => !n.disabled && n.offsetParent !== null);
        if (!f.length) return;
        const first = f[0]; const last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    return modal;
  }

  let lastFocus = null;
  function closeModal() {
    if (!modal || modal.hidden) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    modal.hidden = true;
    document.documentElement.classList.remove('chart-modal-open');
    const plot = document.getElementById('chart-modal-plot');
    if (plotlyReady() && Plotly.purge && plot) Plotly.purge(plot);
    registry.delete('chart-modal-plot');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function kv(label, value) { return value ? `<div class="kv"><span class="k">${esc(label)}</span><span class="v">${value}</span></div>` : ''; }

  function openModal(entry) {
    const m = ensureModal();
    const { figure } = entry;
    const ctx = entry.ctx || {};
    lastFocus = document.activeElement;
    m.querySelector('#chart-modal-title').textContent = titleOf(figure);
    m.querySelector('.chart-modal-sub').textContent = ctx.question || ctx.subtitle || '';
    const kind = kindOf(figure);

    const tools = m.querySelector('.chart-modal-tools');
    tools.innerHTML = toolbarHtml(kind, ctx, ['expand', 'data']) + (kind ? `<button type="button" class="chart-tool" data-modal-data aria-pressed="false" title="Show data" aria-label="Show data">${icon('data')}<span>Show data</span></button>` : '');

    const side = m.querySelector('.chart-modal-side');
    const traces = figure.data || [];
    const legendItems = kind === 'pie'
      ? ((traces[0] || {}).labels || []).map((l, i) => ({ label: l, index: i }))
      : (traces.length > 1 ? traces.map((t, i) => ({ label: t.name || `Series ${i + 1}`, index: i })) : []);
    const filters = (ctx.filters || []).map((f) => `<span class="chip">${esc(f.column)} ${esc(f.operator || '=')} ${esc(f.value)}</span>`).join('');
    side.innerHTML = `
      ${ctx.insight ? `<section><h3>Key insight</h3><p>${esc(ctx.insight)}</p></section>` : ''}
      ${legendItems.length ? `<section><h3>${kind === 'pie' ? 'Categories' : 'Series'}</h3><div class="legend-list">${legendItems.map((it) =>
        `<label><input type="checkbox" checked data-series="${it.index}"> ${esc(it.label)}</label>`).join('')}</div></section>` : ''}
      ${filters ? `<section><h3>Filters</h3><div class="chips">${filters}</div></section>` : ''}
      ${ctx.dateRange ? `<section><h3>Date range</h3><p>${esc(ctx.dateRange)}</p></section>` : ''}
      <section><h3>Source</h3>
        ${kv('Dataset', esc(ctx.dataset))}${kv('Operation', ctx.operation ? `<code>${esc(ctx.operation)}</code>` : '')}${kv('Rows', ctx.rows != null ? esc(ctx.rows) : '')}
      </section>`;
    side.querySelectorAll('[data-series]').forEach((box) => box.addEventListener('change', () => {
      const plot = document.getElementById('chart-modal-plot');
      if (!plotlyReady()) return;
      if (kind === 'pie') {
        const hidden = Array.from(side.querySelectorAll('[data-series]')).filter((b) => !b.checked).map((b) => Number(b.dataset.series));
        if (Plotly.relayout) Plotly.relayout(plot, { hiddenlabels: (traces[0].labels || []).filter((_l, i) => hidden.indexOf(i) >= 0) });
      } else if (Plotly.restyle) {
        Plotly.restyle(plot, { visible: box.checked ? true : 'legendonly' }, [Number(box.dataset.series)]);
      }
    }));

    const dataBox = m.querySelector('.chart-modal-data');
    dataBox.hidden = true; dataBox.innerHTML = '';
    m.hidden = false;
    document.documentElement.classList.add('chart-modal-open');

    const plot = document.getElementById('chart-modal-plot');
    const modalEntry = { figure, ctx, dragmode: 'zoom', modal: true };
    registry.set('chart-modal-plot', modalEntry);
    if (plotlyReady()) {
      const layout = themed(figure, { autosize: true, height: undefined, width: undefined, showlegend: legendItems.length === 0 ? false : true });
      delete layout.height; delete layout.width;
      Plotly.newPlot(plot, figure.data, layout, Object.assign({ displayModeBar: false, responsive: true }, figure.config || {}));
      wireDrill(plot, modalEntry);
    } else {
      plot.innerHTML = '<p class="muted small">The chart library could not load, so this result is shown as a table only.</p>';
    }
    wireToolbar(tools, plot, modalEntry);
    const dataButton = tools.querySelector('[data-modal-data]');
    if (dataButton) dataButton.addEventListener('click', () => {
      if (dataBox.hidden) { dataBox.hidden = false; dataTable(dataBox, tabulate(figure, ctx)); dataButton.setAttribute('aria-pressed', 'true'); }
      else { dataBox.hidden = true; dataBox.innerHTML = ''; dataButton.setAttribute('aria-pressed', 'false'); }
    });
    // Ask AInsight from the expanded view: close it so the conversation is visible.
    const ask = tools.querySelector('[data-chart-action="ask"]');
    if (ask) ask.addEventListener('click', () => closeModal());
    m.querySelector('[data-modal="close"]').focus();
  }

  /* -------------------------------------------------------------- drill-down */

  let popover = null;
  function closePopover() { if (popover) { popover.remove(); popover = null; } }
  document.addEventListener('click', (e) => { if (popover && !popover.contains(e.target) && !e.target.closest('.js-plotly-plot')) closePopover(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePopover(); });

  function wireDrill(el, entry) {
    const ctx = entry.ctx || {};
    const kind = kindOf(entry.figure);
    if (typeof ctx.onPoint === 'function' && typeof el.on === 'function') {
      el.on('plotly_click', (ev) => {
        const p = ev && ev.points && ev.points[0];
        if (!p) return;
        const label = p.label != null ? p.label : (p.data && p.data.orientation === 'h' ? p.y : (p.x != null ? p.x : p.id));
        if (label == null) return;
        const evt = ev.event || {};
        ctx.onPoint({ label: String(label), value: p.value != null ? p.value : (p.data && p.data.orientation === 'h' ? p.x : p.y), kind, x: evt.clientX, y: evt.clientY, point: p });
      });
      return;
    }
    if (!canAsk || !ctx.historyId || (kind !== 'bar' && kind !== 'pie') || typeof el.on !== 'function') return;
    el.on('plotly_click', (ev) => {
      const p = ev && ev.points && ev.points[0];
      if (!p) return;
      const label = p.label != null ? p.label : (p.data && p.data.orientation === 'h' ? p.y : p.x);
      if (typeof label !== 'string' && typeof label !== 'number') return;
      const evt = ev.event || {};
      showDrill(el, String(label), ctx, evt.clientX, evt.clientY);
    });
  }

  async function showDrill(el, label, ctx, x, y) {
    closePopover();
    popover = document.createElement('div');
    popover.className = 'drill-pop';
    popover.setAttribute('role', 'dialog');
    popover.setAttribute('aria-label', `Analyze ${label}`);
    popover.innerHTML = `<div class="drill-title">Analyze <strong>${esc(label)}</strong></div><div class="drill-body muted small">Finding valid follow-ups…</div>`;
    document.body.appendChild(popover);
    const w = 300;
    const left = Math.max(8, Math.min((x || window.innerWidth / 2) - 20, window.innerWidth - w - 8));
    popover.style.left = `${left}px`;
    popover.style.top = `${Math.max(8, Math.min((y || 200) + 12, window.innerHeight - 220))}px`;
    try {
      const data = await AInsight.api.get(`/api/chart/${encodeURIComponent(ctx.historyId)}/drilldown?label=${encodeURIComponent(label)}`);
      const body = popover && popover.querySelector('.drill-body');
      if (!body) return;
      if (!data.questions.length) { body.textContent = data.note || 'Nothing to drill into here.'; return; }
      body.className = 'drill-body';
      body.innerHTML = data.questions.map((q) => `<button type="button" class="drill-q">${esc(q)}</button>`).join('') +
        `<p class="small muted drill-note">${esc(data.note || '')}</p>`;
      body.querySelectorAll('.drill-q').forEach((b) => b.addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent('ainsight:ask', { detail: { question: b.textContent, historyId: ctx.historyId } }));
        closePopover(); if (modal && !modal.hidden) closeModal();
      }));
      const first = body.querySelector('.drill-q'); if (first) first.focus();
    } catch (err) {
      const body = popover && popover.querySelector('.drill-body');
      if (body) body.textContent = err.message || 'Could not load follow-ups.';
    }
  }

  /* ------------------------------------------------------------- wiring */

  function wireToolbar(bar, el, entry) {
    bar.querySelectorAll('[data-chart-action]').forEach((button) => {
      button.addEventListener('click', () => {
        const fn = actions[button.dataset.chartAction];
        if (fn) fn(el, entry, button);
      });
    });
  }

  function render(elementId, figure, ctx) {
    const element = document.getElementById(elementId);
    if (!element || !figure) return;
    if (typeof Plotly === 'undefined') {
      // The charting library could not load (offline or blocked CDN). Say so
      // rather than leaving an empty panel; the result table still has the data.
      element.innerHTML =
        '<p class="muted small">The chart library could not load, so this result is shown as a table only.</p>';
      return;
    }
    const entry = { figure, ctx: ctx || {}, dragmode: 'zoom' };
    const wantsToolbar = !(ctx && ctx.toolbar === false);
    if (wantsToolbar && !element.closest('.chart-card')) {
      const card = document.createElement('div');
      card.className = 'chart-card';
      const bar = document.createElement('div');
      bar.className = 'chart-toolbar';
      bar.setAttribute('role', 'toolbar');
      bar.setAttribute('aria-label', 'Chart tools');
      bar.innerHTML = toolbarHtml(kindOf(figure), entry.ctx);
      element.parentNode.insertBefore(card, element);
      card.appendChild(bar);
      card.appendChild(element);
      wireToolbar(bar, element, entry);
    }
    const config = Object.assign({ displayModeBar: false, responsive: true }, figure.config || {});
    Plotly.newPlot(element, figure.data, themed(figure), config);
    registry.set(elementId, entry);
    wireDrill(element, entry);
  }

  function retheme() {
    registry.forEach((entry, elementId) => {
      const element = document.getElementById(elementId);
      if (element && typeof Plotly !== 'undefined') Plotly.relayout(element, themed(entry.figure));
    });
  }

  document.addEventListener('ainsight:theme', retheme);
  window.addEventListener('resize', () => {
    registry.forEach((_entry, elementId) => {
      const element = document.getElementById(elementId);
      if (element && typeof Plotly !== 'undefined') Plotly.Plots.resize(element);
    });
  });

  return {
    render, retheme, dataTable, tabulate, csvOf, openModal, closeModal,
    enableAsk() { canAsk = true; },
  };
})();
