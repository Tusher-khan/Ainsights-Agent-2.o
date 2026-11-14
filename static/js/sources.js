/* AInsight — Data Sources manager and connection wizard. */
(() => {
  'use strict';
  const { api, integer, when, escapeHtml, toast } = AInsight;
  const $ = (s, r = document) => r.querySelector(s);
  const KIND_LABEL = { postgresql: 'PostgreSQL', mysql: 'MySQL', sqlserver: 'SQL Server', sqlite: 'SQLite file', rest: 'REST API' };
  const DEFAULT_PORT = { postgresql: 5432, mysql: 3306, sqlserver: 1433 };

  /* ----------------------------------------------------------- list */

  function statusLine(s) {
    if (s.needs_credentials) return `<div class="src-status bad">⚠ Credentials need to be entered again</div>`;
    if (s.status === 'connected') return `<div class="src-status ok">✓ Connected${s.last_checked_at ? ' · last checked ' + when(s.last_checked_at) : ''}</div>`;
    if (s.status === 'failed') return `<div class="src-status bad">⚠ Connection failed · ${escapeHtml(s.status_message || '')}</div>`;
    if (s.status === 'disabled') return `<div class="src-status off">⏸ Disabled</div>`;
    return `<div class="src-status off">● Not tested yet</div>`;
  }

  function card(s) {
    const warn = (s.warnings || []).map((w) => `<div class="src-warn">${escapeHtml(w)}</div>`).join('');
    const detail = s.kind === 'rest' ? escapeHtml((s.config.base_url || '') + (s.config.endpoint || ''))
      : s.kind === 'sqlite' ? escapeHtml(s.config.original_name || 'uploaded file')
      : escapeHtml(`${s.config.host || ''}${s.config.database ? ' / ' + s.config.database : ''}`);
    return `
      <article class="card src-card" data-id="${s.id}">
        <div class="body">
          <div class="title"><h3>${escapeHtml(s.name)}</h3><span class="tag" style="margin-left:auto">${KIND_LABEL[s.kind] || s.kind}</span></div>
          <div class="small muted" style="margin-top:.25rem;word-break:break-all">${detail}</div>
          ${statusLine(s)}${warn}
          <div class="small muted" style="margin-top:.5rem">${integer(s.dataset_count || 0)} table${s.dataset_count === 1 ? '' : 's'} imported
            ${s.read_only_session ? '· read-only session enforced' : ''}</div>
        </div>
        <div class="src-actions">
          <button class="btn btn-sm" data-a="test">Test connection</button>
          <button class="btn btn-sm" data-a="tables">Tables</button>
          <button class="btn btn-sm" data-a="refresh">Refresh</button>
          <button class="btn btn-sm" data-a="edit">Edit</button>
          <button class="btn btn-sm" data-a="toggle">${s.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-sm btn-danger" data-a="delete">Delete</button>
        </div>
      </article>`;
  }

  let sources = [];
  async function load() {
    const grid = $('#src-grid');
    try {
      sources = (await api.get('/api/data-sources')).sources;
    } catch (e) { grid.innerHTML = `<div class="card card-pad muted">Could not load data sources: ${escapeHtml(e.message)}</div>`; return; }
    if (!sources.length) {
      grid.innerHTML = `<div class="card empty" style="grid-column:1/-1"><h3>No data sources yet</h3>
        <p>Connect PostgreSQL, MySQL, SQL Server, a SQLite file or a REST API — or upload a spreadsheet.</p>
        <button class="btn btn-primary" id="empty-add">Connect your first data source</button></div>`;
      $('#empty-add').addEventListener('click', () => openWizard());
      return;
    }
    grid.innerHTML = sources.map(card).join('');
    grid.querySelectorAll('[data-a]').forEach((b) => b.addEventListener('click', () => act(b)));
  }

  async function act(button) {
    const id = button.closest('.src-card').dataset.id;
    const s = sources.find((x) => x.id === id);
    const a = button.dataset.a;
    try {
      if (a === 'test') {
        button.disabled = true; button.textContent = 'Testing…';
        const r = await api.post(`/api/data-sources/${id}/test`);
        toast(r.ok ? 'Connected successfully.' : r.message, r.ok ? 'success' : 'error');
        load();
      } else if (a === 'refresh') {
        button.disabled = true; button.textContent = 'Refreshing…';
        const r = await api.post(`/api/data-sources/${id}/refresh`);
        toast(`Refreshed ${r.datasets.length} table(s).` + ((r.failures || []).length ? ` ${r.failures.length} problem(s).` : ''), 'success');
        load();
      } else if (a === 'toggle') {
        await api.post(`/api/data-sources/${id}/${s.enabled ? 'disable' : 'enable'}`);
        load();
      } else if (a === 'delete') {
        if (!window.confirm(`Delete "${s.name}"? Its imported tables are removed from AInsight. Your original database is not touched.`)) return;
        await api.del(`/api/data-sources/${id}`);
        toast('Data source deleted.', 'success'); load();
      } else if (a === 'edit') openWizard({ edit: s });
      else if (a === 'tables') openWizard({ manage: s });
    } catch (e) { toast(e.message, 'error'); load(); }
  }

  /* --------------------------------------------------------- wizard */

  const STEPS = ['Type', 'Details', 'Test', 'Choose data', 'Review', 'Done'];

  function openWizard(opts = {}) {
    const st = { step: 1, kind: null, name: '', settings: {}, edit: opts.edit || null, source: null, tables: [], picked: new Set(), upload: null, tested: null, result: null };
    if (st.edit) { st.kind = st.edit.kind; st.name = st.edit.name; st.settings = { ...st.edit.config }; st.step = 2; }
    if (opts.manage) { st.source = opts.manage; st.kind = opts.manage.kind; st.step = 4; }
    const root = $('#wizard-root');
    const close = () => { root.innerHTML = ''; load(); };
    const render = () => {
      root.innerHTML = `<div class="wiz-back" role="dialog" aria-modal="true" aria-label="Connect a data source"><div class="wiz">
        <div class="wiz-head"><h3>${st.edit ? 'Edit connection' : 'Add a data source'}</h3><button class="btn btn-ghost btn-sm" id="w-x" aria-label="Close">✕</button></div>
        <div class="wiz-steps">${STEPS.map((_, i) => `<span class="${i < st.step ? 'on' : ''}"></span>`).join('')}</div>
        <div class="wiz-body" id="w-body"></div><div class="wiz-foot" id="w-foot"></div></div></div>`;
      $('#w-x').addEventListener('click', close);
      steps[st.step](st, $('#w-body'), $('#w-foot'), { render, close });
    };
    render();
  }

  const field = (id, label, value = '', attrs = '') =>
    `<div><label for="${id}">${label}</label><input class="input" id="${id}" value="${escapeHtml(String(value ?? ''))}" ${attrs}></div>`;
  const select = (id, label, options, value) =>
    `<div><label for="${id}">${label}</label><select class="select" id="${id}">${options.map(([v, l]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
  const val = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };

  function collect(st) {
    const s = { ...st.settings };
    if (['postgresql', 'mysql', 'sqlserver'].includes(st.kind)) {
      Object.assign(s, { host: val('f-host'), port: Number(val('f-port')) || DEFAULT_PORT[st.kind], database: val('f-db'), username: val('f-user'), ssl_mode: val('f-ssl') });
      if (val('f-pass')) s.password = val('f-pass');
    } else if (st.kind === 'rest') {
      Object.assign(s, { base_url: val('f-url'), endpoint: val('f-ep'), method: val('f-method'), auth_type: val('f-auth'), records_path: val('f-rec'),
        auth_name: val('f-authname') || undefined, username: val('f-user') || undefined });
      if (val('f-secret')) { const k = { bearer: 'token', api_key_header: 'api_key', api_key_query: 'api_key', basic: 'password' }[s.auth_type]; if (k) s[k] = val('f-secret'); }
      s.pagination = { type: val('f-pg') || 'none', page_size: Number(val('f-pgsize')) || 100, page_param: val('f-pgparam') || undefined, size_param: val('f-szparam') || undefined,
        cursor_param: val('f-cparam') || undefined, next_cursor_path: val('f-cpath') || undefined, next_url_path: val('f-lpath') || undefined };
      Object.keys(s.pagination).forEach((k) => s.pagination[k] === undefined && delete s.pagination[k]);
      s.read_only_confirmed = !!($('#f-ro') && $('#f-ro').checked);
    }
    st.name = val('f-name') || st.name;
    st.settings = s;
    return s;
  }

  const steps = {
    1: (st, body, foot, ctl) => {
      body.innerHTML = `<p class="hint">What kind of data do you want to analyse?</p><div class="type-grid">
        <button class="type-tile" data-k="file"><b>Excel or CSV file</b><small>Upload a spreadsheet</small></button>
        ${Object.keys(KIND_LABEL).map((k) => `<button class="type-tile" data-k="${k}"><b>${KIND_LABEL[k]}</b><small>${{ postgresql: 'Database', mysql: 'Database', sqlserver: 'Database', sqlite: 'Upload a .sqlite / .db file', rest: 'JSON web service' }[k]}</small></button>`).join('')}</div>`;
      body.querySelectorAll('.type-tile').forEach((b) => b.addEventListener('click', () => {
        if (b.dataset.k === 'file') { window.location.href = '/upload'; return; }
        st.kind = b.dataset.k; st.step = 2; ctl.render();
      }));
      foot.innerHTML = '';
    },

    2: (st, body, foot, ctl) => {
      const s = st.settings; let html = field('f-name', 'Connection name', st.name || KIND_LABEL[st.kind], 'maxlength="80"');
      const keep = st.edit ? ' <span class="hint">(leave blank to keep the saved one)</span>' : '';
      if (['postgresql', 'mysql', 'sqlserver'].includes(st.kind)) {
        html += `<p class="hint tip">For analytics, use a dedicated <b>read-only</b> database account whenever possible. AInsight never needs administrator credentials.</p>
          <div class="row2">${field('f-host', 'Server address', s.host, 'placeholder="db.example.com" autocomplete="off"')}${field('f-port', 'Port', s.port || DEFAULT_PORT[st.kind], 'inputmode="numeric"')}</div>
          <div class="row2">${field('f-db', 'Database name', s.database)}${field('f-user', 'User name', s.username, 'autocomplete="off"')}</div>
          <div class="row2"><div><label for="f-pass">Password${keep}</label><input class="input" id="f-pass" type="password" autocomplete="new-password"></div>
          ${select('f-ssl', 'Encryption (TLS)', [['require', 'Required (recommended)'], ['verify-full', 'Required + verify certificate'], ['disable', 'Off (not recommended)']], s.ssl_mode || 'require')}</div>`;
      } else if (st.kind === 'sqlite') {
        html += `<p class="hint tip">Upload a copy of your SQLite database. AInsight opens it read-only and never changes it.</p>
          <div><label for="f-file">SQLite file</label><input class="input" id="f-file" type="file" accept=".sqlite,.sqlite3,.db"></div>
          <div class="hint" id="f-fileinfo">${st.settings.original_name ? 'Uploaded: ' + escapeHtml(st.settings.original_name) : 'Up to the server upload limit.'}</div>`;
      } else {
        html += `<p class="hint tip">AInsight calls this endpoint to read data. Keys and tokens are stored encrypted and are never shown to the AI.</p>
          ${field('f-url', 'API base address', s.base_url, 'placeholder="https://api.example.com"')}
          <div class="row2">${field('f-ep', 'Endpoint path', s.endpoint, 'placeholder="/v1/orders"')}${select('f-method', 'Method', [['GET', 'GET (read)'], ['POST', 'POST (read-only report)']], s.method || 'GET')}</div>
          <div class="row2">${select('f-auth', 'Authentication', [['none', 'None'], ['bearer', 'Bearer token'], ['api_key_header', 'API key in header'], ['api_key_query', 'API key in query string'], ['basic', 'User name + password']], s.auth_type || 'none')}
          <div><label for="f-secret">Key / token / password${keep}</label><input class="input" id="f-secret" type="password" autocomplete="new-password"></div></div>
          <div class="row2">${field('f-authname', 'Header / parameter name (API key only)', s.auth_name, 'placeholder="X-API-Key"')}${field('f-user', 'User name (basic auth only)', s.username)}</div>
          ${field('f-rec', 'Where is the list of records? (path in the JSON)', s.records_path, 'placeholder="data.items — leave blank if the response is the list"')}
          <div class="row2">${select('f-pg', 'Pagination', [['none', 'None'], ['page', 'Page number'], ['offset', 'Offset'], ['cursor', 'Cursor'], ['link', 'Next-page link']], (s.pagination || {}).type || 'none')}${field('f-pgsize', 'Page size', (s.pagination || {}).page_size || 100, 'inputmode="numeric"')}</div>
          <div class="row2">${field('f-pgparam', 'Page / offset / cursor parameter name', (s.pagination || {}).page_param || (s.pagination || {}).cursor_param)}${field('f-szparam', 'Page-size parameter name', (s.pagination || {}).size_param)}</div>
          <div class="row2">${field('f-cpath', 'Next-cursor path (cursor)', (s.pagination || {}).next_cursor_path)}${field('f-lpath', 'Next-link path (link)', (s.pagination || {}).next_url_path)}</div>
          <label style="display:flex;gap:.5rem;align-items:center;font-weight:450"><input type="checkbox" id="f-ro" ${s.read_only_confirmed ? 'checked' : ''}> This endpoint only reads data (required for POST)</label>`;
      }
      body.innerHTML = html;
      const fileInput = $('#f-file');
      if (fileInput) fileInput.addEventListener('change', async () => {
        const f = fileInput.files[0]; if (!f) return;
        const fd = new FormData(); fd.append('file', f);
        $('#f-fileinfo').textContent = 'Uploading…';
        try { const r = await api.upload('/api/data-sources/upload-sqlite', fd); st.settings = { stored_file: r.stored_file, original_name: r.original_name }; $('#f-fileinfo').textContent = 'Uploaded: ' + r.original_name; }
        catch (e) { $('#f-fileinfo').textContent = e.message; }
      });
      foot.innerHTML = `<button class="btn" id="w-prev">${st.edit ? 'Cancel' : 'Back'}</button><button class="btn btn-primary" id="w-next">${st.edit ? 'Save changes' : 'Test connection'}</button>`;
      $('#w-prev').addEventListener('click', () => { if (st.edit) ctl.close(); else { st.step = 1; ctl.render(); } });
      $('#w-next').addEventListener('click', async () => {
        const settings = collect(st);
        if (st.edit) {
          try { await api.patch(`/api/data-sources/${st.edit.id}`, { name: st.name, settings }); toast('Saved. Test the connection to confirm.', 'success'); ctl.close(); }
          catch (e) { toast(e.message, 'error'); }
          return;
        }
        st.step = 3; ctl.render();
      });
    },

    3: async (st, body, foot, ctl) => {
      body.innerHTML = `<p>Testing the connection…</p><div class="skeleton" style="width:60%"></div>`;
      foot.innerHTML = '';
      let r;
      try { r = await api.post('/api/data-sources/test', { kind: st.kind, settings: st.settings }); } catch (e) { r = { ok: false, message: e.message, warnings: [] }; }
      st.tested = r;
      body.innerHTML = r.ok
        ? `<div class="src-status ok" style="font-size:var(--step-0)">✓ Connected${r.server_version ? ' · ' + escapeHtml(r.server_version) : ''}</div>${(r.warnings || []).map((w) => `<div class="src-warn">${escapeHtml(w)}</div>`).join('')}`
        : `<div class="src-status bad" style="font-size:var(--step-0)">⚠ Connection failed</div><p>${escapeHtml(r.message)}</p><p class="hint">Check the details and try again. Nothing was saved.</p>`;
      foot.innerHTML = `<button class="btn" id="w-prev">Back</button>${r.ok ? '<button class="btn btn-primary" id="w-next">Save and discover data</button>' : '<button class="btn btn-primary" id="w-retry">Try again</button>'}`;
      $('#w-prev').addEventListener('click', () => { st.step = 2; ctl.render(); });
      const retry = $('#w-retry'); if (retry) retry.addEventListener('click', () => ctl.render());
      const next = $('#w-next');
      if (next) next.addEventListener('click', async () => {
        next.disabled = true;
        try { st.source = (await api.post('/api/data-sources', { kind: st.kind, name: st.name, settings: st.settings })).source; await api.post(`/api/data-sources/${st.source.id}/test`); st.step = 4; ctl.render(); }
        catch (e) { toast(e.message, 'error'); next.disabled = false; }
      });
    },

    4: async (st, body, foot, ctl) => {
      body.innerHTML = `<p>Discovering what is in <b>${escapeHtml(st.source.name)}</b>…</p><div class="skeleton" style="width:60%"></div>`;
      foot.innerHTML = '';
      try { st.tables = (await api.get(`/api/data-sources/${st.source.id}/tables`)).tables; }
      catch (e) { body.innerHTML = `<p class="src-status bad">${escapeHtml(e.message)}</p>`; foot.innerHTML = '<button class="btn" id="w-c">Close</button>'; $('#w-c').addEventListener('click', ctl.close); return; }
      if (st.kind === 'rest') { st.picked = new Set(st.tables.map((t) => t.ref)); st.step = 5; ctl.render(); return; }
      body.innerHTML = `<p class="hint">Choose the tables you want to analyse. Tables already imported can be re-imported to refresh them.</p>
        <div class="tbl-pick">${st.tables.map((t) => `<label><input type="checkbox" value="${escapeHtml(t.ref)}" ${st.picked.has(t.ref) || t.imported ? 'checked' : ''}> <span>${escapeHtml(t.ref)}</span> <span class="tag" style="margin-left:auto">${t.kind}${t.imported ? ' · imported' : ''}</span></label>`).join('')}</div>`;
      foot.innerHTML = `<button class="btn" id="w-c">Close</button><button class="btn btn-primary" id="w-next">Review</button>`;
      $('#w-c').addEventListener('click', ctl.close);
      $('#w-next').addEventListener('click', () => {
        st.picked = new Set([...body.querySelectorAll('input:checked')].map((i) => i.value));
        if (!st.picked.size) { toast('Choose at least one table.', 'error'); return; }
        st.step = 5; ctl.render();
      });
    },

    5: (st, body, foot, ctl) => {
      body.innerHTML = `<p><b>${escapeHtml(st.source.name)}</b> · ${KIND_LABEL[st.kind]}</p>
        <p class="hint">AInsight will read ${st.kind === 'rest' ? 'the API and keep a snapshot you can refresh' : 'these tables live using read-only queries'}:</p>
        <ul>${[...st.picked].map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>
        <p class="hint">Each table is profiled (row counts, column types, missing values) so questions can be answered accurately.</p>`;
      foot.innerHTML = `<button class="btn" id="w-prev">Back</button><button class="btn btn-primary" id="w-next">Import and finish</button>`;
      $('#w-prev').addEventListener('click', () => { st.step = st.kind === 'rest' ? 3 : 4; ctl.render(); });
      $('#w-next').addEventListener('click', async (e) => {
        e.target.disabled = true; e.target.textContent = 'Importing…';
        try { st.result = await api.post(`/api/data-sources/${st.source.id}/import`, { tables: [...st.picked] }); st.step = 6; ctl.render(); }
        catch (err) { toast(err.message, 'error'); e.target.disabled = false; e.target.textContent = 'Import and finish'; }
      });
    },

    6: (st, body, foot, ctl) => {
      const r = st.result || { datasets: [], failures: [] };
      body.innerHTML = `<div class="src-status ok" style="font-size:var(--step-0)">✓ ${r.datasets.length} table(s) ready to analyse</div>
        <ul>${r.datasets.map((d) => `<li>${escapeHtml(d.name)} — ${integer(d.row_count)} rows, ${integer(d.column_count)} columns</li>`).join('')}</ul>
        ${(r.failures || []).map((f) => `<div class="src-warn">${escapeHtml(f.table)}: ${escapeHtml(f.message)}</div>`).join('')}`;
      const first = r.datasets[0];
      foot.innerHTML = `<button class="btn" id="w-c">Close</button>${first ? `<a class="btn btn-primary" href="/chat?dataset=${first.id}">Ask a question</a>` : ''}`;
      $('#w-c').addEventListener('click', ctl.close);
    },
  };

  document.addEventListener('DOMContentLoaded', () => {
    $('#add-source').addEventListener('click', () => openWizard());
    load();
  });
})();
