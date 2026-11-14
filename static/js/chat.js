/* AInsight — AI Chat workspace. */

(() => {
  'use strict';

  const { api, number, integer, duration, when, escapeHtml, toast, renderTable } = AInsight;

  const scroll = document.getElementById('chat-scroll');
  const inner = document.getElementById('chat-inner');
  const form = document.getElementById('composer');
  const input = document.getElementById('question');
  const sendButton = document.getElementById('send');
  const datasetSelect = document.getElementById('dataset-select');
  const datasetStatus = document.getElementById('dataset-status');
  const welcome = document.getElementById('welcome');
  const followupsBox = document.getElementById('followups');

  let conversationId = null;
  let busy = false;
  let currentQuestion = '';
  let chartContext = null;           // { historyId, title } while "Ask AInsight about this chart" is armed
  AInsightCharts.enableAsk();

  /** Everything the chart view needs, taken from the validated response (no recalculation). */
  function chartCtx(payload) {
    const source = payload.source || {};
    const result = payload.result || {};
    const plan = payload.plan || {};
    return {
      historyId: payload.history_id || '',
      question: currentQuestion,
      insight: (payload.headline && `${payload.headline.label}: ${payload.headline.value}`) || payload.answer || '',
      dataset: source.dataset ? `${source.dataset}${source.sheet ? ' · ' + source.sheet : ''}` : '',
      operation: source.operation || '',
      rows: result.row_count != null ? result.row_count : source.row_count,
      filters: plan.filters || [],
    };
  }

  function setChartContext(ctx) {
    chartContext = ctx && ctx.historyId ? ctx : null;
    let chip = document.getElementById('chart-chip');
    if (!chip) {
      chip = document.createElement('div');
      chip.id = 'chart-chip';
      chip.className = 'chart-chip';
      chip.setAttribute('role', 'status');
      form.insertBefore(chip, form.firstChild);
    }
    if (!chartContext) { chip.className = 'chart-chip'; chip.innerHTML = ''; return; }
    chip.className = 'chart-chip on';
    chip.innerHTML = `<span>Asking about chart: <strong>${escapeHtml(ctx.title || 'Chart')}</strong></span>
      <button type="button" aria-label="Stop asking about this chart">×</button>`;
    chip.querySelector('button').addEventListener('click', () => setChartContext(null));
    input.placeholder = 'Ask about this chart — for example, why is the top value ahead?';
    input.focus();
  }

  document.addEventListener('ainsight:chart-ask', (e) => setChartContext({ historyId: e.detail.historyId, title: e.detail.title }));
  document.addEventListener('ainsight:ask', (e) => {
    setChartContext({ historyId: e.detail.historyId, title: 'Drill-down' });
    ask(e.detail.question);
  });

  /* -------------------------------------------------------- progress UI */

  // Labels for stages the backend reports. Only stages that really ran are ever shown.
  const STAGE_LABELS = {
    classify_intent: 'Understood the question',
    retrieve_metadata: 'Found the relevant data',
    plan_query: 'Planned the analysis',
    investigate: 'Compared the periods',
    execute_query: 'Ran the analysis',
    validate_result: 'Verified the result',
    generate_response: 'Prepared the insight',
  };

  function stageLabel(ev) {
    const name = ev.stage || '';
    if (STAGE_LABELS[name]) return STAGE_LABELS[name];
    return name.replace(/[_:]/g, ' ').replace(/^./, (c) => c.toUpperCase());
  }

  function showProgress() {
    const node = document.createElement('div');
    node.className = 'progress-card';
    node.innerHTML = `
      <div class="title">AInsight is working…</div>
      <ul class="stage-list"></ul>`;
    inner.appendChild(node);
    scrollToEnd();
    const listEl = node.querySelector('.stage-list');
    return {
      node,
      push(ev) {
        const failed = ev.status && ev.status !== 'done' && ev.status !== 'ok';
        const li = document.createElement('li');
        li.className = `stage done${failed ? ' failed' : ''}`;
        li.innerHTML = `<span class="marker" aria-hidden="true">${failed ? '!' : '✓'}</span>
          <span class="text">${escapeHtml(stageLabel(ev))}${ev.detail ? ` <span class="muted small">${escapeHtml(String(ev.detail).slice(0, 120))}</span>` : ''}</span>`;
        listEl.appendChild(li);
        scrollToEnd();
      },
      stop() {},
    };
  }

  async function runJob(body, progress) {
    const started = await api.post('/api/chat/async', body);
    let since = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, 350));
      const job = await api.get(`/api/chat/jobs/${started.job_id}?since=${since}`);
      (job.events || []).forEach((ev) => progress.push(ev));
      since = job.next;
      if (job.status === 'done') return job.result;
      if (job.status === 'error') throw new Error((job.error && job.error.message) || 'The analysis failed.');
    }
  }

  /* ------------------------------------------------------------ records */

  function verificationStrip(payload) {
    const validation = payload.validation || {};
    const groups = ['plan', 'query', 'result'];
    const checks = [];
    groups.forEach((group) => {
      const report = validation[group];
      if (!report || !report.checks) return;
      report.checks.forEach((check) => checks.push(check));
    });
    const warnings = groups.flatMap((g) => (validation[g] && validation[g].warnings) || []);

    if (!checks.length && !warnings.length) return '';
    const items = checks.map((check) => `
      <span class="check ${check.passed ? '' : 'failed'}">
        <span aria-hidden="true">${check.passed ? '✓' : '✕'}</span>
        ${escapeHtml(humanCheck(check.name))}
      </span>`).join('');
    const warns = warnings.map((w) => `<span class="check warn"><span aria-hidden="true">!</span> ${escapeHtml(w)}</span>`).join('');
    return `
      <div class="record-section">
        <h4>Validation</h4>
        <div class="verification">${items}${warns}</div>
      </div>`;
  }

  function humanCheck(name) {
    const map = {
      columns_exist: 'Columns exist in the dataset',
      measures_numeric: 'Measure is numeric',
      plan_shape: 'Analysis plan verified',
      sql_security: 'Query is read-only',
      schema_awareness: 'Query matches the schema',
      row_limit: 'Row limit applied',
      operation_whitelisted: 'Operation is on the allow-list',
      no_dynamic_code: 'No dynamic code executed',
      execution_success: 'Query executed',
      expected_columns: 'Expected columns returned',
      value_integrity: 'Values checked for gaps',
      aggregation_sanity: 'Aggregation sanity checked',
      cardinality_plausible: 'Row count plausible',
      visualization_compatible: 'Chart matches the result',
      result_shape: 'Result shape verified',
      non_empty_result: 'Query matched no rows',
      metadata_only: 'Answered from the dataset profile',
      time_column_exists: 'Date column exists',
      periods_have_data: 'Both periods contain data',
      no_fabricated_values: 'Every figure comes from your data',
    };
    if (map[name]) return map[name];
    if (name.startsWith('reconciles:')) return `Breakdown by ${name.split(':')[1]} adds up to the total`;
    if (name.startsWith('filter_column:')) return `Filter column ${name.split(':')[1]} exists`;
    return name.replace(/_/g, ' ');
  }

  function highlightSql(sql) {
    return escapeHtml(sql)
      .replace(/'([^']*)'/g, '<span class="sql-str">\'$1\'</span>')
      .replace(/\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|JOIN|LEFT|INNER|ON|AS|AND|OR|NOT|IN|BETWEEN|DESC|ASC|WITH|CASE|WHEN|THEN|ELSE|END|DISTINCT)\b/gi,
        '<span class="sql-kw">$1</span>')
      .replace(/\b(SUM|AVG|COUNT|MIN|MAX|ROUND|STRFTIME|CAST|COALESCE)\(/gi, '<span class="sql-fn">$1</span>(')
      .replace(/\b(\d+\.?\d*)\b/g, '<span class="sql-num">$1</span>');
  }


  /* ------------------------------------------------------ investigation */

  function kpiCard(k) {
    const pct = k.change_pct;
    const arrow = k.direction === 'up' ? '▲' : k.direction === 'down' ? '▼' : '■';
    // A fall in a cost-like measure would be good news; we only know direction, so stay neutral on colour for "inverse".
    const tone = k.direction === 'flat' ? 'flat' : (k.direction === 'up') === !k.inverse ? 'good' : 'bad';
    const pctText = pct === null || pct === undefined ? 'n/a' : `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
    return `
      <div class="kpi-card">
        <span class="kpi-label">${escapeHtml(k.label)}</span>
        <span class="kpi-value">${escapeHtml(k.display)}</span>
        <span class="kpi-delta ${tone}"><span aria-hidden="true">${arrow}</span> ${pctText}
          <span class="kpi-prev">vs ${escapeHtml(number(k.previous))}</span></span>
      </div>`;
  }

  function list(items, cls) {
    if (!items || !items.length) return '';
    return `<ul class="inv-list ${cls || ''}">${items.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`;
  }

  function investigationSections(inv, chartIds) {
    const out = [];
    out.push(`
      <div class="record-section">
        <h4>Executive insight</h4>
        <div class="exec-insight">${escapeHtml(inv.summary || inv.headline)}</div>
        <div class="inv-meta">${escapeHtml(inv.current.label)} vs ${escapeHtml(inv.previous.label)} ·
          <span class="rec-confidence rec-confidence-${inv.confidence === 'medium' ? 'medium' : 'low'}">${inv.evidence ? escapeHtml(inv.evidence.toUpperCase()) + ' EVIDENCE' : escapeHtml((inv.confidence || 'low').toUpperCase()) + ' CONFIDENCE'}</span></div>
      </div>`);
    out.push(`<div class="record-section"><h4>Key numbers</h4><div class="kpi-row">${(inv.kpis || []).map(kpiCard).join('')}</div></div>`);
    chartIds.forEach((id) => out.push(`<div class="record-section"><div class="record-chart" id="${id}"></div></div>`));

    if (inv.anomalies && inv.anomalies.length) {
      out.push(`<div class="record-section"><h4>Unusual changes in the series</h4>${list(
        inv.anomalies.map((a) => `${a.period}: ${a.direction} of ${Math.abs(a.change_pct).toFixed(1)}% (${number(a.value)})`), 'warn')}
        <p class="small muted">A statistical flag, not proof of a problem.</p></div>`);
    }
    if (inv.facts && inv.facts.length) {
      out.push(`<div class="record-section"><h4>What the data shows <span class="tag-fact">Fact</span></h4>${list(inv.facts)}</div>`);
    }
    if (inv.interpretation && inv.interpretation.length) {
      out.push(`<div class="record-section"><h4>How to read it <span class="tag-interp">Interpretation</span></h4>${list(inv.interpretation)}</div>`);
    }
    if (inv.recommendations && inv.recommendations.length) {
      out.push(`<div class="record-section"><h4>What to look at next <span class="tag-rec">Suggested action</span></h4>${list(inv.recommendations)}</div>`);
    }
    if (inv.limitations && inv.limitations.length) {
      out.push(`<div class="record-section"><h4>Limits of this analysis</h4>${list(inv.limitations, 'muted')}</div>`);
    }
    out.push(`<details class="sql-panel"><summary>How this was calculated</summary><div class="sql-body">${list(inv.how_calculated)}</div></details>`);
    return out;
  }


  function agentSections(payload, inv) {
    const ag = payload.agent;
    if (!ag) return [];
    const out = [];
    const levels = { strong: 'STRONG EVIDENCE', moderate: 'MODERATE EVIDENCE', limited: 'LIMITED EVIDENCE', insufficient: 'INSUFFICIENT EVIDENCE' };
    const tone = ag.evidence === 'strong' ? 'medium' : 'low';
    const partial = ag.status === 'partial';
    if (!inv) {
      out.push(`<div class="record-section"><h4>Executive summary</h4>
        <div class="exec-insight">${escapeHtml(payload.answer || '')}</div>
        <div class="inv-meta"><span class="rec-confidence rec-confidence-${tone}">${levels[ag.evidence] || 'EVIDENCE: ' + escapeHtml(ag.evidence || 'n/a')}</span></div></div>`);
      if (payload.kpis && payload.kpis.length) {
        out.push(`<div class="record-section"><h4>Key numbers</h4><div class="kpi-row">${payload.kpis.map(kpiCard).join('')}</div></div>`);
      }
    } else {
    }
    if (partial) {
      out.push(`<div class="record-section"><div class="assumption">The analysis reached its execution limit before completing the full investigation. What is shown below is what was verified so far.</div></div>`);
    }
    return out;
  }

  function agentDetailSections(payload, inv) {
    const ag = payload.agent;
    if (!ag) return [];
    const out = [];
    if (!inv) {
      if ((ag.facts || []).length) out.push(`<div class="record-section"><h4>Key findings <span class="tag-fact">Fact</span></h4>${list(ag.facts)}</div>`);
      if ((ag.interpretation || []).length) out.push(`<div class="record-section"><h4>Interpretation <span class="tag-interp">Interpretation</span></h4>${list(ag.interpretation)}</div>`);
      if ((ag.recommendations || []).length) out.push(`<div class="record-section"><h4>Recommendations <span class="tag-rec">Suggested action</span></h4>${list(ag.recommendations)}</div>`);
    }
    if ((ag.withheld || []).length) {
      out.push(`<div class="record-section"><h4>Not concluded</h4>${list(ag.withheld, 'muted')}</div>`);
    }
    const steps = ag.steps || [];
    if (steps.length) {
      const rows = steps.map((st) => `
        <li><strong>${escapeHtml(st.tool)}</strong>${st.reused ? ' <span class="muted">(reused earlier result)</span>' : ''}
          — ${escapeHtml(st.why || '')}
          <span class="muted small">· chosen by ${escapeHtml(st.decided_by || 'rules')} · ${st.ok ? (st.validated ? 'validated' : 'not validated') : 'failed'} · ${duration(st.ms)}</span>
          ${(st.issues || []).length ? `<div class="small muted">${escapeHtml(st.issues.join('; '))}</div>` : ''}</li>`).join('');
      const b = ag.budget || {};
      const u = ag.usage || {};
      const usageLine = u.calls ? (u.usage === 'unavailable' || u.calls_without_usage
        ? `${u.calls} model call(s); token usage unavailable from the provider.`
        : `${u.calls} model call(s); ${integer(u.input_tokens || 0)} in / ${integer(u.output_tokens || 0)} out tokens${u.estimated_cost != null ? '; est. cost $' + Number(u.estimated_cost).toFixed(4) : ''}.`)
        : 'No language-model calls were made.';
      out.push(`<details class="sql-panel"><summary>Analysis steps performed (${steps.length})</summary><div class="sql-body">
        <ol class="inv-list">${rows}</ol>
        <p class="small muted">${escapeHtml(ag.mode_note || '')} Steps used: ${b.steps_used || steps.length} of ${b.max_steps || '—'} · ${duration(b.elapsed_ms)} · ${escapeHtml(usageLine)}</p></div></details>`);
    }
    return out;
  }

  /* ------------------------------------------- narrative & agent panel */

  const AGENT_STEPS = [
    ['classify_intent', 'Understand question'], ['retrieve_metadata', 'Find relevant data'], ['plan_query', 'Select analytical tool'],
    ['execute_query', 'Analyze dataset'], ['validate_result', 'Validate result'], ['generate_visualization', 'Generate visualization'],
    ['generate_response', 'Interpret findings'],
  ];

  function narrativeSection(payload) {
    const n = payload.narrative;
    if (!n) return '';
    const block = (tag, cls, text) => text ? `<div class="narr narr-${cls}"><span class="narr-tag">${tag}</span><p>${escapeHtml(text)}</p></div>` : '';
    return `<div class="record-section narrative">
      ${block('Fact', 'fact', n.fact)}
      ${block('Interpretation', 'interp', n.interpretation)}
      ${block('Recommendation', 'rec', n.recommendation)}
      <p class="small muted narr-note">Facts come straight from the calculated result. Interpretation and recommendation are suggestions to examine, not proven causes.</p>
    </div>`;
  }

  function nextSection(payload) {
    const items = ((payload.narrative && payload.narrative.next) || payload.suggestions || []).slice(0, 4);
    if (!items.length) return '';
    return `<div class="record-section"><h4>Suggested next analysis</h4><div class="next-row">${items.map((q) =>
      `<button type="button" class="next-chip" data-next="${escapeHtml(q)}">${escapeHtml(q)}</button>`).join('')}</div></div>`;
  }

  /** Only what really ran: stage names, tool, operation, dataset, validation, time, row count. No hidden reasoning. */
  function agentPanel(payload) {
    const stages = payload.stages || [];
    if (!stages.length && !payload.agent) return '';
    const ran = new Set(stages.filter((st) => !st.status || st.status === 'done' || st.status === 'ok').map((st) => st.stage));
    const steps = AGENT_STEPS.filter(([key]) => ran.has(key) || (key === 'generate_visualization' && (payload.visualization || payload.extra_visualizations)));
    const source = payload.source || {};
    const result = payload.result || {};
    const checks = ['plan', 'query', 'result'].flatMap((g) => ((payload.validation || {})[g] || {}).checks || []);
    const passed = checks.filter((c) => c.passed).length;
    const tools = ((payload.agent || {}).steps || []).map((st) => st.tool);
    const verified = payload.validation && payload.validation.verified;
    return `<details class="agent-panel">
      <summary><span class="agent-name">AInsight Agent</span>
        <span class="agent-steps">${steps.map(([, label]) => `<span class="agent-step"><b aria-hidden="true">✓</b>${escapeHtml(label)}</span>`).join('')}</span></summary>
      <div class="agent-details">
        ${tools.length ? `<div><span class="k">Tools used</span><span class="v">${escapeHtml(tools.join(' → '))}</span></div>` : ''}
        <div><span class="k">Operation</span><span class="v mono">${escapeHtml(source.operation || '—')}</span></div>
        <div><span class="k">Dataset</span><span class="v">${escapeHtml(source.dataset || '—')}</span></div>
        <div><span class="k">Validation</span><span class="v">${checks.length ? `${passed} of ${checks.length} checks passed` : (verified ? 'Verified' : '—')}</span></div>
        <div><span class="k">Execution time</span><span class="v">${duration(result.execution_ms != null ? result.execution_ms : source.execution_ms)}</span></div>
        <div><span class="k">Rows returned</span><span class="v">${integer(result.row_count != null ? result.row_count : (source.row_count || 0))}</span></div>
      </div></details>`;
  }

  function renderRecord(payload) {
    const record = document.createElement('div');
    const empty = payload.empty || (payload.result && payload.result.row_count === 0);
    record.className = `record${empty ? ' is-empty' : ''}`;

    const head = payload.headline;
    const source = payload.source || {};
    const result = payload.result || {};
    const sections = [];
    const inv = payload.investigation || null;
    sections.push(`<div class="record-head"><span class="ai-avatar" aria-hidden="true">AI</span><strong>AInsight</strong></div>`);
    sections.push(agentPanel(payload));
    const invChartIds = inv ? (inv.charts || []).map(() => `chart-${Math.random().toString(36).slice(2, 9)}`) : [];

    if (inv) {
      sections.push(...investigationSections(inv, invChartIds));
      sections.push(...agentSections(payload, inv));
    } else if (payload.agent) {
      sections.push(...agentSections(payload, null));
    } else if (head) {
      const exact = typeof head.raw_value === 'number' && Math.abs(head.raw_value) >= 1000
        ? `<span class="exact">exact: ${head.raw_value.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>` : '';
      sections.push(`
        <div class="record-section">
          <h4>${escapeHtml(head.label)}</h4>
          <div class="record-headline">
            <span class="value">${escapeHtml(head.value)}</span>
            ${exact}
          </div>
        </div>`);
    } else if (payload.answer) {
      sections.push(`<div class="record-section"><div class="record-headline"><span class="label">${escapeHtml(payload.answer)}</span></div></div>`);
    }

    if (payload.narrative && !inv && !payload.agent) sections.push(narrativeSection(payload));
    sections.push(...agentDetailSections(payload, inv));
    if (payload.dashboard) {
      sections.push(`<div class="record-section"><a class="btn btn-primary" href="${escapeHtml(payload.dashboard.url)}">Open dashboard “${escapeHtml(payload.dashboard.name)}”</a></div>`);
    }

    if (payload.insight && !inv && !payload.agent && !payload.narrative) {
      sections.push(`<div class="record-section"><h4>Insight</h4><div class="record-insight">${escapeHtml(payload.insight)}</div></div>`);
    }

    if (payload.recommendations && payload.recommendations.length) {
      const cards = payload.recommendations.map((rec) => `
        <div class="recommendation-card">
          <div class="rec-row"><span class="rec-label">Finding</span><span class="rec-text">${escapeHtml(rec.finding || '')}</span></div>
          <div class="rec-row"><span class="rec-label">Evidence</span><span class="rec-text mono">${escapeHtml(rec.evidence || '')}</span></div>
          ${rec.explanation ? `<div class="rec-row"><span class="rec-label">Possible explanation</span><span class="rec-text">${escapeHtml(rec.explanation)}</span></div>` : ''}
          <div class="rec-row"><span class="rec-label">Recommendation</span><span class="rec-text">${escapeHtml(rec.recommendation || '')}</span></div>
          ${rec.measurement ? `<div class="rec-row"><span class="rec-label">Measure success by</span><span class="rec-text">${escapeHtml(rec.measurement)}</span></div>` : ''}
          <div class="rec-foot">
            <span class="rec-confidence rec-confidence-${rec.confidence || 'low'}">${(rec.confidence || 'low').toUpperCase()} CONFIDENCE</span>
            ${rec.limitations ? `<span class="rec-limitations">${escapeHtml(rec.limitations)}</span>` : ''}
          </div>
        </div>`).join('');
      sections.push(`<div class="record-section"><h4>Recommendations</h4>${cards}</div>`);
    }

    (payload.assumptions || []).forEach((assumption) => {
      sections.push(`<div class="record-section"><div class="assumption">${escapeHtml(assumption)}</div></div>`);
    });

    const chartId = `chart-${Math.random().toString(36).slice(2, 9)}`;
    if (!inv && payload.visualization && payload.visualization.figure) {
      sections.push(`<div class="record-section"><h4>Visualization</h4><div class="record-chart" id="${chartId}"></div></div>`);
    }
    const extraIds = (payload.extra_visualizations || []).map(() => `chart-${Math.random().toString(36).slice(2, 9)}`);
    extraIds.forEach((id) => sections.push(`<div class="record-section"><div class="record-chart" id="${id}"></div></div>`));
    if (payload.sources_used && payload.sources_used.length) {
      sections.push(`<div class="record-section"><h4>Sources combined</h4><div class="source-grid">${payload.sources_used.map((s) =>
        `<div class="source-item"><span class="k">${escapeHtml(s.role)}</span><span class="v">${escapeHtml(s.dataset)} · ${escapeHtml(s.measure)} <span class="muted">(${s.engine === 'sql' ? 'SQL' : 'pandas'})</span></span></div>`).join('')}</div></div>`);
    }

    if (payload.columns_table) {
      sections.push(`<div class="record-section"><h4>Columns</h4>${renderTable(payload.columns_table.columns, payload.columns_table.rows, 60)}</div>`);
    } else if (result.rows && result.rows.length && !(head && result.row_count === 1)) {
      sections.push(`<div class="record-section"><h4>Result data</h4><div class="result-dt" data-result-table></div></div>`);
    }

    if (source.dataset) {
      const columns = (source.columns_used || []).length ? source.columns_used.join(', ') : '—';
      sections.push(`
        <div class="record-section">
          <h4>Analysis source</h4>
          <div class="source-grid">
            <div class="source-item"><span class="k">Dataset</span><span class="v">${escapeHtml(source.dataset)}${source.sheet ? ' · ' + escapeHtml(source.sheet) : ''}</span></div>
            <div class="source-item"><span class="k">Columns used</span><span class="v">${escapeHtml(columns)}</span></div>
            <div class="source-item"><span class="k">Operation</span><span class="v mono">${escapeHtml(source.operation || '—')}</span></div>
            <div class="source-item"><span class="k">${inv ? 'Rows compared' : 'Rows returned'}</span><span class="v">${integer((inv ? source.row_count : result.row_count) || 0)}</span></div>
            <div class="source-item"><span class="k">Execution time</span><span class="v">${duration(inv ? source.execution_ms : result.execution_ms)}</span></div>
            <div class="source-item"><span class="k">Engine</span><span class="v">${escapeHtml((result.engine || 'pandas') === 'sql' ? 'SQL (read-only)' : 'Pandas analytics')}</span></div>
          </div>
        </div>`);
    }

    sections.push(verificationStrip(payload));

    if (payload.sql) {
      sections.push(`
        <details class="sql-panel">
          <summary>View SQL</summary>
          <div class="sql-body">
            <button class="btn btn-sm copy" data-copy="${escapeHtml(payload.sql)}">Copy</button>
            <pre><code>${highlightSql(payload.sql)}</code></pre>
          </div>
        </details>`);
    }

    sections.push(nextSection(payload));
    const historyId = payload.history_id || '';
    sections.push(`
      <div class="record-foot">
        <span class="small muted">${duration(payload.total_ms)} total${payload.routing_reason ? ' · ' + escapeHtml(payload.routing_reason) : ''}</span>
        <span class="spacer"></span>
        ${historyId && payload.source && payload.source.dataset_id ? `<button class="btn btn-sm" data-make-dashboard="${historyId}" data-dataset="${escapeHtml(payload.source.dataset_id)}">Create a dashboard from this analysis</button>` : ''}
        ${historyId && result.rows && result.rows.length ? `
          <a class="btn btn-sm" href="/api/download/${historyId}.csv">CSV</a>
          <a class="btn btn-sm" href="/api/download/${historyId}.xlsx">Excel</a>` : ''}
        <div class="feedback" data-history="${historyId}">
          <button class="btn btn-sm btn-ghost" data-rating="helpful" aria-label="Mark as helpful">👍</button>
          <button class="btn btn-sm btn-ghost" data-rating="not_helpful" aria-label="Mark as not helpful">👎</button>
        </div>
      </div>`);

    record.innerHTML = sections.join('');
    inner.appendChild(record);
    const tableHost = record.querySelector('[data-result-table]');
    if (tableHost) {
      const cols = result.columns && result.columns.length ? result.columns : Object.keys(result.rows[0]);
      AInsightCharts.dataTable(tableHost, { columns: cols, rows: result.rows }, 10, 2);
    }

    const ctx = chartCtx(payload);
    if (!inv && payload.visualization && payload.visualization.figure) {
      AInsightCharts.render(chartId, payload.visualization.figure, ctx);
    }
    (payload.extra_visualizations || []).forEach((figure, i) => AInsightCharts.render(extraIds[i], figure, ctx));
    if (inv) inv.charts.forEach((figure, i) => AInsightCharts.render(invChartIds[i], figure, ctx));
    wireRecord(record);
    renderFollowups(payload.suggestions || []);
    scrollToEnd();
  }

  function wireRecord(record) {
    record.querySelectorAll('[data-next]').forEach((button) => {
      button.addEventListener('click', () => ask(button.dataset.next));
    });

    record.querySelectorAll('[data-copy]').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(button.dataset.copy);
          button.textContent = 'Copied';
          setTimeout(() => { button.textContent = 'Copy'; }, 1500);
        } catch (e) { toast('Copying is blocked in this browser.', 'error'); }
      });
    });

    record.querySelectorAll('[data-make-dashboard]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const r = await api.post('/api/dashboards', { dataset_id: button.dataset.dataset, from_history: button.dataset.makeDashboard });
          window.location.href = `/dashboards/${r.dashboard.id}`;
        } catch (e) { toast(e.message, 'error'); button.disabled = false; }
      });
    });

    const feedback = record.querySelector('.feedback');
    if (feedback) {
      feedback.querySelectorAll('[data-rating]').forEach((button) => {
        button.addEventListener('click', async () => {
          try {
            await api.post('/api/feedback', {
              history_id: feedback.dataset.history || null,
              rating: button.dataset.rating,
            });
            feedback.innerHTML = '<span class="feedback-done">Thanks — feedback recorded</span>';
          } catch (e) { toast(e.message, 'error'); }
        });
      });
    }
  }

  function renderQuestion(text) {
    if (welcome) welcome.style.display = 'none';
    const node = document.createElement('div');
    node.className = 'turn-question';
    node.innerHTML = `<div class="text">${escapeHtml(text)}</div>`;
    inner.appendChild(node);
    scrollToEnd();
  }

  function renderError(message) {
    const node = document.createElement('div');
    node.className = 'record is-error';
    node.innerHTML = `
      <div class="record-section">
        <h4>AInsight could not run that analysis</h4>
        <div class="record-insight">${escapeHtml(message)}</div>
      </div>`;
    inner.appendChild(node);
    scrollToEnd();
  }

  function renderFollowups(suggestions) {
    if (!followupsBox) return;
    followupsBox.innerHTML = suggestions.slice(0, 4)
      .map((s) => `<button type="button" class="followup">${escapeHtml(s)}</button>`).join('');
    followupsBox.querySelectorAll('.followup').forEach((button) => {
      button.addEventListener('click', () => { input.value = button.textContent; input.focus(); });
    });
  }

  function scrollToEnd() {
    requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
  }

  /* --------------------------------------------------------------- flow */

  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    sendButton.disabled = true;
    renderQuestion(question);
    currentQuestion = question;
    const attached = chartContext;
    setChartContext(null);
    input.placeholder = 'Ask about your data — for example, which region has the highest profit?';
    input.value = '';
    input.style.height = 'auto';
    if (followupsBox) followupsBox.innerHTML = '';

    const progress = showProgress();
    try {
      const payload = await runJob({
        question,
        dataset_id: datasetSelect ? datasetSelect.value || null : null,
        conversation_id: conversationId,
        ...(attached ? { chart_context: { history_id: attached.historyId } } : {}),
      }, progress);
      conversationId = payload.conversation_id || conversationId;
      progress.stop();
      progress.node.remove();
      renderRecord(payload);
    } catch (error) {
      progress.stop();
      progress.node.remove();
      renderError(error.message);
    } finally {
      busy = false;
      sendButton.disabled = false;
      input.focus();
    }
  }

  async function loadSuggestions() {
    const box = document.getElementById('suggestions');
    if (!box) return;
    const datasetId = datasetSelect ? datasetSelect.value : '';
    try {
      const data = await api.get(`/api/chat/suggestions${datasetId ? `?dataset_id=${datasetId}` : ''}`);
      box.innerHTML = (data.suggestions || []).map((s) => `
        <button class="suggestion" type="button" role="listitem">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          ${escapeHtml(s)}
        </button>`).join('');
      box.querySelectorAll('.suggestion').forEach((button) => {
        button.addEventListener('click', () => ask(button.textContent.trim()));
      });
    } catch (e) { /* suggestions are optional */ }
  }

  async function init() {
    const datasets = await AInsight.loadDatasetOptions('dataset-select', { includeAll: true });
    if (datasetStatus) {
      if (datasets.length) {
        datasetStatus.className = 'tag tag-ready';
        datasetStatus.innerHTML = `<span class="dot"></span> ${datasets.length} dataset${datasets.length > 1 ? 's' : ''} ready`;
      } else {
        datasetStatus.className = 'tag tag-warn';
        datasetStatus.innerHTML = '<span class="dot"></span> No data yet';
        if (welcome) { welcome.innerHTML = AInsight.onboardingHtml(); AInsight.wireOnboarding(welcome); }
      }
    }
    await loadSuggestions();
    if (datasetSelect) datasetSelect.addEventListener('change', loadSuggestions);
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    ask(input.value);
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });

  const clearButton = document.getElementById('clear-chat');
  if (clearButton) {
    clearButton.addEventListener('click', async () => {
      if (conversationId) {
        try { await api.del(`/api/conversations/${conversationId}`); } catch (e) {}
      }
      conversationId = null;
      inner.innerHTML = '';
      if (welcome) { inner.appendChild(welcome); welcome.style.display = ''; }
      loadSuggestions();
      toast('Conversation cleared.', 'success');
    });
  }

  const newChat = document.getElementById('new-chat');
  if (newChat) newChat.addEventListener('click', () => clearButton && clearButton.click());

  async function openSaved(historyId, autoRefresh) {
    try {
      const saved = await api.get(`/api/history/${encodeURIComponent(historyId)}/reopen`);
      renderQuestion(saved.question || 'Saved analysis');
      currentQuestion = saved.question || '';
      const banner = document.createElement('div');
      banner.className = 'record-section saved-banner';
      banner.innerHTML = `<span class="tag tag-warn"><span class="dot"></span> Saved analysis</span>
        <span class="small muted"> Data last analyzed: ${escapeHtml(when(saved.data_last_analyzed))} — nothing was re-run.</span>
        <button class="btn btn-sm" id="refresh-saved">Refresh analysis</button>`;
      inner.appendChild(banner);
      if (saved.notice) toast(saved.notice, 'info');
      let savedRecord = null;
      if (saved.response) { saved.response.history_id = historyId; renderRecord(saved.response); savedRecord = inner.querySelector('.record:last-of-type'); }
      document.getElementById('refresh-saved').addEventListener('click', async (e) => {
        e.target.disabled = true;
        const progress = showProgress();
        progress.push({ stage: 'Re-running analysis with current data', status: 'done' });
        try {
          const payload = await api.post(`/api/history/${encodeURIComponent(historyId)}/refresh`, {});
          progress.node.remove();
          banner.remove();
          if (savedRecord) savedRecord.remove();  // the stale result must not stay on screen next to the refreshed one
          renderRecord(payload);
          toast('Analysis refreshed with current data.', 'success');
        } catch (err) { progress.node.remove(); renderError(err.message); e.target.disabled = false; }
      });
      if (autoRefresh) document.getElementById('refresh-saved').click();
    } catch (e) { renderError(e.message); }
  }

  // Deep links: /chat?q=...&dataset=...  and  /chat?history=ID
  init().then(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('history')) { openSaved(params.get('history'), params.get('refresh') === '1'); return; }
    if (params.get('dataset') && datasetSelect) datasetSelect.value = params.get('dataset');
    if (params.get('q')) ask(params.get('q'));
    input.focus();
  });
})();
