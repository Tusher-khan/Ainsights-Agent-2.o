/* AInsight — query history. */

(() => {
  'use strict';
  const { api, duration, when, escapeHtml, toast, integer } = AInsight;
  const list = document.getElementById('history-list');

  function row(entry) {
    const title = entry.title || entry.question;
    return `
      <div class="activity-row" data-id="${entry.id}">
        <button class="btn btn-sm btn-ghost" data-action="favorite" aria-pressed="${entry.favorite ? 'true' : 'false'}" aria-label="Favorite">${entry.favorite ? '★' : '☆'}</button>
        <span class="tag ${entry.status === 'success' ? 'tag-ready' : 'tag-danger'}"><span class="dot"></span>${escapeHtml(entry.status || '—')}</span>
        <span class="q" title="${escapeHtml(entry.question)}">${escapeHtml(title)}</span>
        <span class="meta">${escapeHtml(entry.dataset_name || '—')} · ${escapeHtml(entry.intent || '—')} · ${integer(entry.row_count)} rows · ${duration(entry.total_ms)} · ${when(entry.refreshed_at || entry.created_at)}</span>
        ${entry.has_saved_analysis ? '<button class="btn btn-sm btn-primary" data-action="open">Open</button><button class="btn btn-sm" data-action="refresh">Refresh</button>'
          : '<button class="btn btn-sm" data-action="rerun">Run again</button>'}
        <button class="btn btn-sm btn-ghost" data-action="rename">Rename</button>
        <button class="btn btn-sm btn-ghost" data-action="delete" aria-label="Delete this entry">✕</button>
      </div>`;
  }

  function query() {
    const p = new URLSearchParams({ limit: '200' });
    const q = document.getElementById('h-search').value.trim();
    if (q) p.set('q', q);
    if (document.getElementById('h-fav').checked) p.set('favorite', '1');
    const intent = document.getElementById('h-intent').value;
    if (intent) p.set('intent', intent);
    return p.toString();
  }

  async function act(button, node) {
    const id = node.dataset.id;
    const action = button.dataset.action;
    try {
      if (action === 'delete') { await api.del(`/api/history/${id}`); node.remove(); }
      else if (action === 'open') window.location.href = `/chat?history=${encodeURIComponent(id)}`;
      else if (action === 'refresh') window.location.href = `/chat?history=${encodeURIComponent(id)}&refresh=1`;
      else if (action === 'favorite') {
        await api.patch(`/api/history/${id}`, { favorite: button.getAttribute('aria-pressed') !== 'true' });
        load();
      } else if (action === 'rename') {
        const current = node.querySelector('.q').textContent;
        const title = window.prompt('Name this analysis', current);
        if (title && title.trim()) { await api.patch(`/api/history/${id}`, { title: title.trim() }); load(); }
      } else {
        window.location.href = `/chat?q=${encodeURIComponent(node.querySelector('.q').title)}`;
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  async function load() {
    list.innerHTML = '<div class="card-pad"><div class="skeleton" style="width:40%"></div></div>';
    try {
      const data = await api.get(`/api/history?${query()}`);
      const history = data.history || [];
      if (!history.length) {
        const filtered = document.getElementById('h-search').value.trim() || document.getElementById('h-fav').checked || document.getElementById('h-intent').value;
        if (!filtered) {
          list.innerHTML = '<div class="empty"><h3>Your analysis history will appear here</h3><p>Every question you ask is saved, so you can reopen an analysis or refresh it later.</p><a class="btn btn-primary" href="/chat">Ask AInsight</a></div>';
          return;
        }
        list.innerHTML = '<div class="empty"><h3>Nothing here</h3><p>No saved analyses match. Ask AInsight a question and it will be recorded here.</p><a class="btn btn-primary" href="/chat">Ask AInsight</a></div>';
        return;
      }
      list.innerHTML = history.map(row).join('');
      list.querySelectorAll('[data-action]').forEach((button) => {
        button.addEventListener('click', () => act(button, button.closest('.activity-row')));
      });
    } catch (e) {
      list.innerHTML = `<div class="card-pad"><p class="muted">History could not be loaded: ${escapeHtml(e.message)}</p></div>`;
    }
  }

  let timer = null;
  ['h-search', 'h-intent', 'h-fav'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(id === 'h-search' ? 'input' : 'change', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  });

  const clear = document.getElementById('clear-history');
  if (clear) clear.addEventListener('click', async () => {
    if (!window.confirm('Delete every entry in your query history?')) return;
    try { await api.del('/api/history'); toast('History cleared.', 'success'); load(); }
    catch (e) { toast(e.message, 'error'); }
  });

  document.addEventListener('DOMContentLoaded', load);
})();
