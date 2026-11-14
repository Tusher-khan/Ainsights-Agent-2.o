/* AInsight — dashboard builder pages. */
(() => {
  'use strict';
  const { api, escapeHtml, toast, when } = AInsight;

  async function listPage() {
    const sel = document.getElementById('d-dataset');
    const box = document.getElementById('dash-list');
    await AInsight.loadDatasetOptions('d-dataset', { includeAll: false });
    async function load() {
      const data = await api.get('/api/dashboards');
      box.innerHTML = (data.dashboards || []).length ? data.dashboards.map((d) => `
        <div class="activity-row" data-id="${d.id}">
          <a class="q" href="/dashboards/${d.id}">${escapeHtml(d.name)}</a>
          <span class="meta">updated ${when(d.updated_at)}</span>
          <button class="btn btn-sm btn-ghost" data-act="rename">Rename</button>
          <button class="btn btn-sm btn-ghost" data-act="delete">Delete</button>
        </div>`).join('') : '<div class="empty"><h3>Turn any analysis into a reusable dashboard</h3><p>Pick a dataset and create one, or ask in chat: “Create a sales dashboard”.</p><a class="btn btn-primary" href="/chat">Ask AInsight</a></div>';
      box.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', async () => {
        const id = b.closest('.activity-row').dataset.id;
        try {
          if (b.dataset.act === 'delete') { await api.del(`/api/dashboards/${id}`); }
          else { const n = window.prompt('Dashboard name'); if (!n) return; await api.patch(`/api/dashboards/${id}`, { name: n }); }
          load();
        } catch (e) { toast(e.message, 'error'); }
      }));
    }
    document.getElementById('d-create').addEventListener('click', async () => {
      try {
        const r = await api.post('/api/dashboards', { dataset_id: sel.value });
        window.location.href = `/dashboards/${r.dashboard.id}`;
      } catch (e) { toast(e.message, 'error'); }
    });
    load();
  }

  document.addEventListener('DOMContentLoaded', () => { if (!window.DASHBOARD_ID) listPage(); });
})();
