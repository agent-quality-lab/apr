(() => {
  'use strict';

  const app = document.querySelector('#app');
  const live = document.querySelector('#live');
  const API = '/app/api';
  const COMMUNITY_URL = 'https://agentperformance.review/community/';
  const SOURCE_LABELS = {
    'user-stated': 'User stated', 'agent-reported': 'Agent reported',
    'runtime-observed': 'Runtime observed', imported: 'Imported'
  };
  const STATUS_LABELS = {
    in_progress: 'In progress', interrupted: 'Interrupted',
    awaiting_review: 'Awaiting review', reviewed: 'Reviewed'
  };
  const SHARE_FIELDS = [
    ['task_text', 'Task text', 'Title, goal, criteria, and result'],
    ['definitions', 'Definitions', 'Harness definitions and terms'],
    ['setup', 'Setup details', 'Runtime, model, and named harness ingredients'],
    ['context', 'Context', 'Context attached to this task'],
    ['process', 'Process', 'Setup, milestone, and criteria events'],
    ['artifacts', 'Artifacts', 'Work products recorded as evidence'],
    ['resources', 'Resources', 'Time, cost, or usage details when available']
  ];
  const GAP_REASONS = [
    ['incomplete', 'Incomplete result'], ['missed_brief', 'Missed the brief'],
    ['incorrect', 'Incorrect result'], ['missing_tool', 'Missing tool or capability']
  ];
  const OPTIONAL_RATINGS = [
    ['quality', 'Quality', 'How good was the result?'],
    ['speed', 'Speed', 'How quickly was it completed?'],
    ['resource_efficiency', 'Resource efficiency', 'How efficiently were resources used?']
  ];
  let state = { tasks: [], current: null, setup: null, error: '', routeKey: '', share: null, selected: new Set(), redactions: {}, busy: false };
  let requestSerial = 0;

  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  };
  const safeText = value => {
    if (value === null || value === undefined || value === '') return 'Not recorded';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    try { return JSON.stringify(value, null, 2); } catch (_) { return '[unavailable]'; }
  };
  const add = (parent, child) => { parent.append(child); return child; };
  const section = (title, body) => {
    const box = node('section', 'panel');
    add(box, node('h2', '', title));
    add(box, body);
    return box;
  };
  const button = (label, action, style = 'button') => {
    const b = node('button', style, label); b.type = 'button';
    b.addEventListener('click', action); return b;
  };
  const linkButton = (label, href, style = 'button') => {
    const a = node('a', style, label); a.href = href; return a;
  };
  const announce = text => { live.textContent = text; };
  const setFocus = selector => requestAnimationFrame(() => document.querySelector(selector)?.focus());
  const currentHash = () => {
    const raw = location.hash.slice(1);
    try { return decodeURIComponent(raw); } catch (_) { return raw; }
  };
  const getRoute = () => {
    const raw = currentHash();
    if (!raw) return { type: 'work' };
    if (raw === 'setup') return { type: 'setup' };
    const [key, ...rest] = raw.split('=');
    const id = rest.join('=');
    if (key === 'task' && id) return { type: 'task', id };
    if (key === 'review' && id) return { type: 'review', id };
    if (key === 'share' && id) return { type: 'share', id };
    return { type: 'work' };
  };
  const navState = type => {
    document.querySelectorAll('[data-nav]').forEach(a => {
      if (a.dataset.nav === (type === 'setup' ? 'setup' : 'work')) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  };
  async function request(path, options = {}) {
    const response = await fetch(API + path, {
      credentials: 'same-origin', cache: 'no-store',
      headers: { 'Accept': 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
      ...options
    });
    let body;
    try { body = await response.json(); } catch (_) { body = null; }
    if (!response.ok) {
      const error = new Error(body && (body.error || body.message) || `Request failed (${response.status})`);
      error.status = response.status; error.body = body; throw error;
    }
    return body;
  }
  function renderError(message, retry) {
    const box = node('div', 'error'); box.setAttribute('role', 'alert');
    add(box, node('strong', '', 'Could not load this view.'));
    add(box, node('p', '', message));
    if (retry) add(box, button('Try again', retry, 'button secondary'));
    return box;
  }
  function loading(label = 'Loading your local workspace…') {
    app.replaceChildren();
    const card = node('div', 'loading-card'); card.setAttribute('role', 'status');
    add(card, node('span', 'spinner')); add(card, node('span', '', label)); app.append(card);
  }
  function valueBlock(label, value) {
    const div = node('div', 'content-block'); add(div, node('h3', '', label));
    const p = node('p', '', safeText(value)); div.append(p); return div;
  }
  function badge(text) { return node('span', 'badge', text); }
  function statusPill(status) {
    const p = node('span', `status-pill ${status === 'reviewed' ? 'reviewed' : status === 'interrupted' ? 'interrupted' : ''}`, STATUS_LABELS[status] || 'Status unavailable');
    return p;
  }
  function sourceBadge(source) {
    return node('span', `source-tag ${(source || '').replace(/[^a-z-]/g, '')}`, SOURCE_LABELS[source] || 'Source unavailable');
  }
  function listData(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') return Object.entries(value).map(([key, val]) => `${key}: ${safeText(val)}`);
    return value ? [value] : [];
  }
  function fillList(parent, label, values) {
    add(parent, node('h3', '', label));
    const items = listData(values);
    if (!items.length) { add(parent, node('p', 'muted small', 'Not recorded')); return; }
    const ul = node('ul', 'criteria');
    items.forEach(item => add(ul, node('li', '', safeText(item)))); parent.append(ul);
  }
  function formatDate(input) {
    if (!input) return 'Date unavailable';
    const date = new Date(input); return Number.isNaN(date.valueOf()) ? 'Date unavailable' : date.toLocaleString();
  }
  function formatNumber(value) {
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
  }
  function formatDuration(value, unit) {
    const normalized = String(unit || '').toLowerCase();
    let seconds = value;
    if (['ms', 'millisecond', 'milliseconds'].includes(normalized)) seconds = value / 1000;
    else if (['m', 'min', 'minute', 'minutes'].includes(normalized)) seconds = value * 60;
    else if (['h', 'hr', 'hour', 'hours'].includes(normalized)) seconds = value * 3600;
    else if (['d', 'day', 'days'].includes(normalized)) seconds = value * 86400;
    if (seconds >= 86400) { const days = seconds / 86400; return `${formatNumber(days)} ${days === 1 ? 'day' : 'days'}`; }
    if (seconds >= 3600) { const hours = seconds / 3600; return `${formatNumber(hours)} ${hours === 1 ? 'hour' : 'hours'}`; }
    if (seconds >= 60) { const minutes = seconds / 60; return `${formatNumber(minutes)} min`; }
    return `${formatNumber(seconds)} sec`;
  }
  function formatResourceValue(key, metric) {
    if (!metric || typeof metric !== 'object' || typeof metric.value !== 'number' || !Number.isFinite(metric.value) || metric.value < 0 || typeof metric.unit !== 'string' || !metric.unit.trim()) return null;
    const unit = metric.unit.trim(); const normalized = unit.toLowerCase();
    if (key === 'elapsed_time') return formatDuration(metric.value, unit);
    if (key === 'cost' && normalized === 'usd') {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 4 }).format(metric.value);
    }
    if (key === 'cost' && /^[A-Za-z]{3}$/.test(unit)) {
      try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: unit.toUpperCase(), minimumFractionDigits: 0, maximumFractionDigits: 4 }).format(metric.value); }
      catch (_) { return `${unit.toUpperCase()} ${formatNumber(metric.value)}`; }
    }
    return `${formatNumber(metric.value)}${unit ? ` ${unit}` : ''}`;
  }
  function resourceRecords(task, key) {
    const resources = task?.resources;
    if (Array.isArray(resources)) return resources.filter(record => record?.name === key).map(record => ({
      value: formatResourceValue(key, { value: record.value, unit: record.unit || '' }), source: record.source
    })).filter(record => record.value !== null);
    const records = resources && typeof resources === 'object' ? Object.values(resources) : [];
    return records.map(record => ({ value: formatResourceValue(key, record?.[key]), source: record?.source }))
      .filter(record => record.value !== null);
  }
  function resourceMetric(task, key) {
    const readings = resourceRecords(task, key);
    return readings.length ? readings.map(reading => reading.value).join(' · ') : 'Not measured';
  }
  function renderResourceSection(task) {
    const body = node('div', 'resource-list');
    [['elapsed_time', 'Elapsed time'], ['cost', 'Cost'], ['tokens', 'Tokens'], ['tool_calls', 'Tool calls']].forEach(([key, label]) => {
      const row = node('div', 'resource-row'); add(row, node('strong', '', label));
      const readings = resourceRecords(task, key);
      const values = node('div', 'resource-readings');
      if (!readings.length) add(values, node('span', 'muted small', 'Not measured'));
      readings.forEach(reading => {
        const item = node('div', 'resource-reading'); add(item, node('span', '', reading.value)); item.append(sourceBadge(reading.source)); values.append(item);
      });
      row.append(values); body.append(row);
    });
    return body;
  }
  function renderPerformance(task) {
    const body = node('div'); const review = task?.review;
    const outcome = node('div', 'performance-outcome');
    add(outcome, node('strong', '', 'Human outcome'));
    add(outcome, node('span', '', review?.completion === 'yes' ? 'Completed' : review?.completion === 'no' ? 'Not completed' : 'Not reviewed'));
    outcome.append(sourceBadge(review ? 'user-stated' : undefined)); body.append(outcome);
    const ratings = node('div', 'saved-ratings');
    [['overall', 'Overall'], ['quality', 'Quality'], ['speed', 'Speed'], ['resource_efficiency', 'Resource efficiency']].forEach(([key, label]) => {
      const value = review?.ratings?.[key]; const item = node('div', 'saved-rating'); add(item, node('span', '', label));
      add(item, node('strong', '', Number.isInteger(value) && value >= 1 && value <= 5 ? `${value} / 5` : 'Not rated')); ratings.append(item);
    }); body.append(ratings);
    const resourceHeading = node('h3', 'performance-subhead', 'Recorded resources'); body.append(resourceHeading, renderResourceSection(task));
    return section('Performance record', body);
  }

  async function loadWork() {
    const serial = ++requestSerial; loading(); navState('work');
    try {
      const data = await request('/tasks');
      if (serial !== requestSerial) return;
      state.tasks = Array.isArray(data?.tasks) ? data.tasks : [];
      state.error = ''; renderWork();
    } catch (error) {
      if (serial !== requestSerial) return;
      app.replaceChildren(renderError(error.message || 'The local service did not return a task list.', loadWork));
    }
  }
  function renderWork() {
    app.replaceChildren();
    const total = state.tasks.length;
    const active = state.tasks.filter(t => t?.status === 'in_progress').length;
    const review = state.tasks.filter(t => ['awaiting_review', 'interrupted'].includes(t?.status)).length;
    const hero = node('div', 'hero'); const copy = node('div', 'hero-copy');
    add(copy, node('p', 'eyebrow', 'Your local workspace'));
    add(copy, node('h1', '', 'Good work deserves a clear record.'));
    add(copy, node('p', 'lede', 'Track the task, the setup around it, and what the result actually delivered. Your local task data stays here until you choose what to share.'));
    hero.append(copy, linkButton('Review setup', '/app/#setup', 'button secondary')); app.append(hero);
    const stats = node('div', 'stats');
    [['Tasks', total], ['In progress', active], ['Ready for review', review]].forEach(([label, amount]) => {
      const item = node('div', 'stat'); add(item, node('strong', '', amount)); add(item, node('span', '', label)); stats.append(item);
    }); app.append(stats);
    const heading = node('div', 'section-heading'); add(heading, node('h2', '', 'My work')); add(heading, node('span', 'small muted', `${total} ${total === 1 ? 'task' : 'tasks'}`)); app.append(heading);
    if (!state.tasks.length) {
      const empty = node('section', 'empty-state'); add(empty, node('p', 'eyebrow', 'A quiet start'));
      add(empty, node('h2', '', 'No tasks have arrived yet.'));
      add(empty, node('p', '', 'Tasks will appear here when a supported agent runtime records work. Set up your harness so reviews can describe the conditions behind each result.'));
      add(empty, linkButton('Set up this workspace', '/app/#setup', 'button')); app.append(empty); setFocus('h1'); return;
    }
    const list = node('div', 'task-list');
    state.tasks.forEach(task => {
      if (!task || typeof task !== 'object') return;
      const a = node('a', 'task-card'); a.href = `#task=${encodeURIComponent(String(task.task_id || ''))}`;
      const details = node('div'); add(details, node('h3', '', safeText(task.title || 'Untitled task')));
      if (task.goal) add(details, node('p', '', safeText(task.goal)));
      const meta = node('div', 'task-meta');
      if (task.category) add(meta, badge(safeText(task.category)));
      if (task.runtime) add(meta, badge(safeText(task.runtime)));
      const evidenceCount = Number.isInteger(task.evidence_count) ? task.evidence_count : Array.isArray(task.evidence) ? task.evidence.length : 0;
      add(meta, badge(`${evidenceCount} evidence items`)); details.append(meta);
      const metrics = node('div', 'task-metrics');
      [
        ['Model', task.model || 'Not recorded'],
        ['Overall rating', Number.isInteger(task.review?.ratings?.overall) ? `${task.review.ratings.overall} / 5` : 'Not rated'],
        ['Elapsed time', resourceMetric(task, 'elapsed_time')],
        ['Cost', resourceMetric(task, 'cost')]
      ].forEach(([label, value]) => { const metric = node('div', 'task-metric'); add(metric, node('strong', '', value)); add(metric, node('span', '', label)); metrics.append(metric); });
      details.append(metrics);
      a.append(details, statusPill(task.status)); list.append(a);
    }); app.append(list); setFocus('h1');
  }

  async function loadSetup() {
    const serial = ++requestSerial; loading('Loading setup…'); navState('setup');
    try { const setup = await request('/setup'); if (serial !== requestSerial) return; state.setup = setup || {}; renderSetup(); }
    catch (error) { if (serial !== requestSerial) return; app.replaceChildren(renderError(error.message, loadSetup)); }
  }
  function formField(labelText, name, value, hint, options = {}) {
    const wrap = node('div', 'form-row'); const id = `setup-${name}`;
    const label = node('label', '', labelText); label.htmlFor = id; wrap.append(label);
    const input = options.textarea ? node('textarea') : node('input'); input.id = id; input.name = name;
    if (options.textarea) input.rows = options.rows || 3; else input.type = 'text';
    input.value = value == null ? '' : String(value); wrap.append(input);
    if (hint) wrap.append(node('span', 'form-help', hint)); return { wrap, input };
  }
  function renderSetup(message = '') {
    app.replaceChildren(); navState('setup');
    const setup = state.setup || {};
    const hero = node('div', 'hero'); const copy = node('div', 'hero-copy');
    add(copy, node('p', 'eyebrow', 'Harness setup'));
    add(copy, node('h1', '', 'Make the working conditions visible.'));
    add(copy, node('p', 'lede', 'Choose the harness path and runtime for this workspace. Capabilities below come from the local service and are shown as read-only status.'));
    hero.append(copy); app.append(hero);
    if (message) { const success = node('div', 'success', message); success.setAttribute('role', 'status'); app.append(success); }
    const form = node('form', 'panel'); form.addEventListener('submit', saveSetup);
    add(form, node('h2', '', 'Current configuration'));
    const modes = node('fieldset', 'form-row'); modes.style.border = '0'; modes.style.padding = '0'; modes.style.margin = '0 0 18px';
    add(modes, node('legend', 'field-label', 'Who is this setup for?'));
    const choices = node('div', 'choice-row');
    [['existing', 'Existing harness', 'Record work from a harness you already use.'], ['newcomer', 'New to agent harnesses', 'Use the same task record format without assuming a harness exists.']].forEach(([v, title, desc]) => {
      const label = node('label', 'choice-card'); const radio = node('input'); radio.type = 'radio'; radio.name = 'mode'; radio.value = v; radio.checked = setup.mode === v || (!setup.mode && v === 'newcomer');
      const text = node('span'); add(text, node('strong', '', title)); add(text, node('small', '', desc)); label.append(radio, text); choices.append(label);
    }); modes.append(choices); form.append(modes);
    const runtime = formField('Agent runtime', 'runtime', setup.runtime || '', 'For example, Codex, Claude Code, or a team-specific runtime.');
    runtime.input.required = true; runtime.input.maxLength = 512;
    form.append(runtime.wrap);
    const installNote = node('div', 'notice');
    add(installNote, node('p', '', 'Saving these preferences labels this recording workspace. It does not install or connect an MCP server or set up an execution harness.'));
    const command = node('code', 'setup-command', 'codex mcp add agent-performance-review -- python3 <checkout>/mcp_server.py');
    add(installNote, node('p', 'small', 'To connect manually, follow the operator guide and replace <checkout> with your local repository path.'));
    const guide = node('a', 'text-button', 'Open the operator guide'); guide.href = 'https://agentperformance.review/docs/#installation'; guide.target = '_blank'; guide.rel = 'noopener noreferrer'; installNote.append(guide);
    installNote.append(document.createTextNode(' '), command); form.append(installNote);
    const capabilityBlock = node('div', 'content-block'); add(capabilityBlock, node('h3', '', 'Service capabilities · read only'));
    const capabilityList = node('dl', 'capability-list');
    const known = setup.capabilities && typeof setup.capabilities === 'object' ? Object.entries(setup.capabilities) : [];
    if (!known.length) add(capabilityBlock, node('p', 'muted small', 'Capability status unavailable.'));
    known.forEach(([key, value]) => {
      const row = node('div', 'capability-row'); const dt = node('dt', '', key.replaceAll('_', ' ')); const dd = node('dd', '', safeText(value)); row.append(dt, dd); capabilityList.append(row);
    });
    if (known.length) capabilityBlock.append(capabilityList);
    form.append(capabilityBlock);
    const buttons = node('div', 'review-actions'); const save = node('button', 'button', 'Save setup'); save.type = 'submit'; buttons.append(save); form.append(buttons);
    const err = node('div', 'inline-error'); err.hidden = true; err.id = 'setup-error'; form.append(err); app.append(form); setFocus('h1');
  }
  async function saveSetup(event) {
    event.preventDefault(); const form = event.currentTarget; const save = form.querySelector('button[type=submit]'); const err = form.querySelector('#setup-error');
    const mode = form.querySelector('input[name=mode]:checked')?.value || 'newcomer';
    const runtime = form.elements.runtime.value.trim();
    save.disabled = true; save.textContent = 'Saving…'; err.hidden = true;
    try {
      const result = await request('/setup', { method: 'POST', body: JSON.stringify({ mode, runtime }) });
      state.setup = result && typeof result === 'object' ? result : { ...state.setup, mode, runtime };
      renderSetup('Setup saved on this device.'); setFocus('h1'); announce('Setup saved.');
    } catch (error) {
      err.textContent = `Setup could not be saved: ${error.message}`; err.hidden = false; save.disabled = false; save.textContent = 'Save setup';
    }
  }

  async function loadTask(id) {
    const serial = ++requestSerial; loading('Loading task details…'); navState('work');
    try {
      const task = await request(`/tasks/${encodeURIComponent(id)}`); if (serial !== requestSerial) return;
      if (!task || typeof task !== 'object') throw new Error('The service returned an invalid task.');
      state.current = task; renderTask(task);
    } catch (error) { if (serial !== requestSerial) return; app.replaceChildren(renderError(error.message, () => loadTask(id))); }
  }
  function renderTask(task) {
    app.replaceChildren();
    const top = node('div', 'task-title-row'); const title = node('div');
    add(title, node('p', 'eyebrow', `${safeText(task.category || 'Task')} · ${safeText(task.runtime || 'Runtime not recorded')}`));
    add(title, node('h1', 'detail-title', safeText(task.title || 'Untitled task')));
    add(title, node('p', 'lede', `Recorded ${formatDate(task.created_at)}${task.model ? ` · ${safeText(task.model)}` : ''}`));
    top.append(title, statusPill(task.status)); app.append(top);
    if (task.status === 'interrupted') {
      const n = node('div', 'notice'); add(n, node('strong', '', 'This task was interrupted.'));
      add(n, node('span', '', ' Review what was captured so far. A missing result is recorded as missing, not treated as a failure.')); app.append(n);
    }
    app.append(renderBeforeAfter(task));
    const columns = node('div', 'split'); const main = node('div'); const side = node('aside');
    main.append(renderTaskBrief(task), renderPerformance(task));
    const details = node('section', 'panel'); add(details, node('h2', '', 'What shaped this run'));
    const groups = [
      ['Setup', ['mode', 'name', 'version', 'roles', 'loops', 'rules', 'tools', 'context_categories', 'definitions']],
      ['Process', null], ['Context', null], ['Artifacts', null]
    ];
    groups.forEach(([label, keys]) => {
      const d = node('details', 'details-group'); const summary = node('summary', '', label); d.append(summary); const body = node('div', 'details-body');
      if (label === 'Setup') {
        const harness = task.harness && typeof task.harness === 'object' ? task.harness : {};
        keys.forEach(key => { if (harness[key] !== undefined && harness[key] !== null && safeText(harness[key]) !== '') body.append(valueBlock(key.replaceAll('_', ' '), harness[key])); });
        if (task.runtime) body.append(valueBlock('Runtime', task.runtime)); if (task.model) body.append(valueBlock('Model', task.model));
        if (!body.childElementCount) body.append(node('p', 'muted small', 'Setup details were not recorded.'));
      } else {
        const kinds = label === 'Process' ? ['milestone', 'setup', 'criteria'] : label === 'Context' ? ['context'] : ['artifact'];
        if (label === 'Context' && task.context !== undefined && task.context !== null) body.append(valueBlock('Task context', task.context));
        const entries = Array.isArray(task.evidence) ? task.evidence.filter(item => kinds.includes(item?.kind)) : [];
        renderEvidence(body, entries, label.toLowerCase());
      }
      d.append(body); details.append(d);
    }); main.append(details);
    const afterReview = Array.isArray(task.post_review_evidence) ? task.post_review_evidence : [];
    if (afterReview.length) {
      const body = node('div'); add(body, node('p', 'small muted', 'Captured after the saved rating. These events are not part of that review.'));
      renderEvidence(body, afterReview, 'post-review');
      main.append(section('After this review', body));
    }
    side.append(section('Review status', (() => { const body = node('div'); add(body, node('p', '', `Updated ${formatDate(task.updated_at)}`));
      if (task.review) add(body, node('p', 'small muted', `Saved ${formatDate(task.review.recorded_at)} · Completion ${task.review.completion === 'yes' ? 'yes' : task.review.completion === 'no' ? 'no' : 'not recorded'}`));
      else add(body, node('p', 'small muted', 'Your review has not been saved yet.')); return body; })()));
    if (task.status === 'reviewed' && task.review) side.append(button('Prepare a share snapshot', () => {
      state.share = null; state.selected = new Set(); state.redactions = {};
      location.hash = `share=${encodeURIComponent(String(task.task_id))}`;
    }, 'button'));
    else if (task.status === 'interrupted' || task.status === 'awaiting_review') side.append(button(task.status === 'interrupted' ? 'Review interrupted task' : 'Review this task', () => { location.hash = `review=${encodeURIComponent(String(task.task_id))}`; }, 'button'));
    else add(side, node('p', 'notice', task.status === 'in_progress' ? 'A review becomes available when this task is finished or interrupted.' : 'A review is unavailable for this task status.'));
    columns.append(main, side); app.append(columns); setFocus('h1');
  }
  function renderEvidence(parent, entries, kind) {
    if (!entries.length) { parent.append(node('p', 'muted small', `No ${kind} details were recorded.`)); return; }
    const list = node('div', 'evidence-list');
    entries.forEach(item => {
      const card = node('article', 'evidence-item'); const header = node('div', 'preview-toolbar');
      add(header, badge(safeText(item.kind || 'evidence'))); header.append(sourceBadge(item.source));
      if (item.event_id) add(header, node('span', 'small muted', `Event ${safeText(item.event_id)}`)); card.append(header);
      const d = node('details'); add(d, node('summary', '', 'Show recorded details'));
      const pre = node('p'); pre.textContent = safeText(item.data); d.append(pre); card.append(d); list.append(card);
    }); parent.append(list);
  }

  function ratingControl(key, label, hint, initial, required) {
    const row = node('div', 'rating-control'); const name = node('div'); add(name, node('strong', '', label)); if (hint) add(name, node('small', '', hint));
    const stars = node('div', 'stars'); stars.setAttribute('role', 'radiogroup'); stars.setAttribute('aria-label', `${label}${required ? ', required' : ', optional'}`);
    const initialValue = Number.isInteger(Number(initial)) && Number(initial) >= 1 && Number(initial) <= 5 ? Number(initial) : 0;
    const update = (score, moveFocus = false, notify = true) => {
      if (score) stars.dataset.value = String(score); else delete stars.dataset.value;
      stars.querySelectorAll('button[role=radio]').forEach(item => {
        const value = Number(item.dataset.rating); const checked = value === score;
        item.setAttribute('aria-checked', String(checked)); item.tabIndex = checked || (!score && value === 1) ? 0 : -1;
        item.classList.toggle('filled', Boolean(score) && value <= score);
        if (moveFocus && checked) item.focus();
      });
      if (score && notify) announce(`${label}: ${score} of 5`);
    };
    for (let score = 1; score <= 5; score++) {
      const star = node('button', 'star-btn', '★'); star.type = 'button'; star.setAttribute('role', 'radio'); star.setAttribute('aria-label', `${score} of 5`); star.setAttribute('aria-checked', 'false'); star.dataset.rating = String(score); star.tabIndex = initialValue === score || (!initialValue && score === 1) ? 0 : -1;
      star.addEventListener('click', () => update(score));
      star.addEventListener('keydown', event => {
        let next = null;
        if (event.key === 'ArrowRight' || event.key === 'ArrowUp') next = Math.min(5, score + 1);
        if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') next = Math.max(1, score - 1);
        if (event.key === 'Home') next = 1; if (event.key === 'End') next = 5;
        if (next !== null) { event.preventDefault(); update(next, true); }
      }); stars.append(star);
    }
    stars.dataset.key = key; stars.dataset.required = String(required); row.append(name, stars);
    if (!required) row.append(button(`Clear ${label.toLowerCase()}`, () => update(0), 'button tertiary clear-rating'));
    if (initialValue) update(initialValue, false, false);
    return row;
  }
  async function loadReview(id) {
    if (!state.current || String(state.current.task_id) !== id) { await loadTaskForReview(id); return; }
    presentReview(state.current);
  }
  async function loadTaskForReview(id) {
    const serial = ++requestSerial; loading('Loading review…');
    try { const task = await request(`/tasks/${encodeURIComponent(id)}`); if (serial !== requestSerial) return; state.current = task; presentReview(task); }
    catch (error) { if (serial !== requestSerial) return; app.replaceChildren(renderError(error.message, () => loadTaskForReview(id))); }
  }
  function presentReview(task) {
    if (task?.status === 'awaiting_review' || task?.status === 'interrupted') { renderReview(task); return; }
    renderReviewStatus(task);
  }
  function renderReviewStatus(task) {
    app.replaceChildren(); navState('work');
    const hero = node('div', 'hero'); const copy = node('div'); add(copy, node('p', 'eyebrow', 'Task review'));
    add(copy, node('h1', '', safeText(task?.title || 'Task review')));
    const text = task?.status === 'reviewed'
      ? 'A human review is already saved for this task. Saved reviews are read-only.'
      : task?.status === 'in_progress'
        ? 'This task is still in progress. A review becomes available after the runtime records a result or interruption.'
        : 'This task is not ready for review yet.';
    add(copy, node('p', 'lede', text)); hero.append(copy); app.append(hero);
    if (task && typeof task === 'object') app.append(renderPerformance(task));
    const actions = node('div', 'review-actions');
    actions.append(button('Open task record', () => { location.hash = `task=${encodeURIComponent(String(task?.task_id || ''))}`; }, 'button'));
    if (task?.status === 'reviewed') actions.append(button('Prepare a share snapshot', () => { location.hash = `share=${encodeURIComponent(String(task.task_id))}`; }, 'button secondary'));
    app.append(actions); setFocus('h1');
  }
  function renderBeforeAfter(task) {
    const group = node('div', 'before-after');
    const before = node('section', 'ba-card before'); const beforeHead = node('div', 'preview-toolbar');
    add(beforeHead, node('h2', '', 'Intended work'));
    beforeHead.append(sourceBadge(task.baseline_source || 'agent-reported')); before.append(beforeHead);
    add(before, node('p', '', task.baseline_missing ? 'Baseline not recorded for this task.' : safeText(task.goal || 'Goal not recorded')));
    const after = node('section', 'ba-card after'); const afterHead = node('div', 'preview-toolbar');
    add(afterHead, node('h2', '', 'Recorded result'));
    afterHead.append(sourceBadge(task.result ? 'agent-reported' : undefined)); after.append(afterHead);
    add(after, node('p', '', safeText(task.result || 'No result recorded')));
    group.append(before, after); return group;
  }
  function renderTaskBrief(task) {
    const body = node('div');
    if (!task.baseline_missing) body.append(valueBlock('Goal', task.goal));
    const criteria = node('div', 'content-block'); fillList(criteria, 'Success criteria', task.success_criteria); body.append(criteria);
    return section('Task brief', body);
  }
  function renderReview(task, message = '') {
    if (!['awaiting_review', 'interrupted'].includes(task?.status)) { renderReviewStatus(task); return; }
    app.replaceChildren(); navState('work');
    const steps = node('div', 'stepper'); ['1 · Review', '2 · Share'].forEach((v, i) => { const s = node('span', i === 0 ? 'active' : '', v); steps.append(s); }); app.append(steps);
    const hero = node('div', 'hero'); const copy = node('div'); add(copy, node('p', 'eyebrow', 'Task review'));
    add(copy, node('h1', '', safeText(task.title || 'Untitled task'))); add(copy, node('p', 'lede', 'Compare the result with the goal and criteria. Use your judgment for ratings; resource readings are shown separately.')); hero.append(copy); app.append(hero);
    const reviewContext = node('div', 'review-context');
    reviewContext.append(renderBeforeAfter(task), renderTaskBrief(task), renderPerformance(task)); app.append(reviewContext);
    if (message) { const notice = node('div', 'error', message); notice.setAttribute('role', 'alert'); app.append(notice); }
    const form = node('form', 'panel'); form.id = 'review-form'; form.addEventListener('submit', saveReview);
    add(form, node('h2', '', 'Did the task reach its goal?'));
    const binary = node('fieldset', 'form-row'); binary.style.border = '0'; binary.style.padding = '0';
    add(binary, node('legend', 'field-label', 'Completion · required'));
    const choices = node('div', 'choice-row');
    [['yes', 'Yes', 'The result met the goal and criteria.'], ['no', 'No', 'The result missed some or all of the goal.']].forEach(([value, title, desc]) => {
      const label = node('label', 'choice-card'); const radio = node('input'); radio.type = 'radio'; radio.name = 'completion'; radio.value = value; radio.required = true; radio.checked = task.review?.completion === value;
      const text = node('span'); add(text, node('strong', '', title)); add(text, node('small', '', desc)); label.append(radio, text); choices.append(label);
      radio.addEventListener('change', () => { const reasons = form.querySelector('#gap-reasons'); reasons.hidden = radio.value !== 'no' || !radio.checked; });
    }); binary.append(choices); form.append(binary);
    const reasonsWrap = node('div', 'form-row'); reasonsWrap.id = 'gap-reasons'; reasonsWrap.hidden = task.review?.completion !== 'no';
    add(reasonsWrap, node('span', 'field-label', 'What got in the way? Choose any that apply.'));
    const reasonGrid = node('div', 'reason-grid');
    GAP_REASONS.forEach(([key, label]) => { const chip = node('label', 'reason-chip'); const check = node('input'); check.type = 'checkbox'; check.name = 'gap_reasons'; check.value = key; check.checked = Array.isArray(task.review?.gap_reasons) && task.review.gap_reasons.includes(key); chip.append(check, document.createTextNode(label)); reasonGrid.append(chip); });
    reasonsWrap.append(reasonGrid); form.append(reasonsWrap);
    add(form, node('h2', '', 'Rate the result'));
    const ratingGroup = node('div', 'rating-group');
    ratingGroup.append(ratingControl('overall', 'Overall', 'Required · overall task performance', task.review?.ratings?.overall, true));
    OPTIONAL_RATINGS.forEach(([key, label, hint]) => ratingGroup.append(ratingControl(key, label, `Optional · ${hint}`, task.review?.ratings?.[key], false)));
    form.append(ratingGroup);
    const help = node('p', 'form-help', 'Ratings use a 1 to 5 scale. Leave optional dimensions unrated if you cannot judge them.'); form.append(help);
    const buttons = node('div', 'review-actions'); const save = node('button', 'button', task.review ? 'Save updated review' : 'Save review'); save.type = 'submit'; buttons.append(save);
    buttons.append(button('Cancel', () => { location.hash = `task=${encodeURIComponent(String(task.task_id))}`; }, 'button secondary')); form.append(buttons);
    const err = node('div', 'inline-error'); err.id = 'review-error'; err.hidden = true; form.append(err); app.append(form); setFocus('h1');
  }
  async function saveReview(event) {
    event.preventDefault(); const form = event.currentTarget; const err = form.querySelector('#review-error'); err.hidden = true;
    const completion = form.querySelector('input[name=completion]:checked')?.value;
    const overall = form.querySelector('[data-key=overall]')?.dataset.value;
    if (!completion) { err.textContent = 'Choose Yes or No before saving.'; err.hidden = false; return; }
    if (!overall) { err.textContent = 'Choose an overall rating from 1 to 5.'; err.hidden = false; return; }
    const ratings = { overall: Number(overall) };
    OPTIONAL_RATINGS.forEach(([key]) => { const value = form.querySelector(`[data-key="${key}"]`)?.dataset.value; if (value) ratings[key] = Number(value); });
    const gap_reasons = completion === 'no' ? [...form.querySelectorAll('input[name=gap_reasons]:checked')].map(input => input.value) : [];
    const save = form.querySelector('button[type=submit]'); save.disabled = true; save.textContent = 'Saving review…';
    try {
      await request(`/tasks/${encodeURIComponent(String(state.current.task_id))}/review`, { method: 'POST', body: JSON.stringify({ completion, ratings, gap_reasons, revision: state.current.revision }) });
      announce('Review saved.'); location.hash = `task=${encodeURIComponent(String(state.current.task_id))}`;
    } catch (error) {
      err.textContent = error.status === 409 ? 'This task changed while you were reviewing it. Your answers are still here. Reload the task to see its latest evidence before saving again.' : `Review could not be saved: ${error.message}. Your answers are still here; you can try again.`;
      err.hidden = false; save.disabled = false; save.textContent = 'Save review';
      if (error.status === 409) {
        const reload = button('Reload latest task', () => {
          if (window.confirm('Reloading replaces these unsaved review answers with the latest task. Continue?')) loadTask(state.current.task_id);
        }, 'button secondary'); err.append(document.createTextNode(' '), reload);
      }
    }
  }

  function shareRoot(task, key) {
    const evidence = Array.isArray(task?.evidence) ? task.evidence : [];
    if (key === 'task_text') return { title: task?.title, goal: task?.goal, success_criteria: task?.success_criteria, result: task?.result };
    if (key === 'definitions') return task?.harness?.definitions || {};
    if (key === 'setup') {
      const harness = task?.harness && typeof task.harness === 'object' ? task.harness : {};
      return { runtime: task?.runtime, model: task?.model, harness: {
        name: harness.name, mode: harness.mode, version: harness.version,
        roles: harness.roles, loops: harness.loops, rules: harness.rules,
        tools: harness.tools, context_categories: harness.context_categories
      } };
    }
    if (key === 'context') return {
      baseline: task?.context || {},
      evidence: evidence.filter(e => e?.kind === 'context').map(e => ({ source: e.source, data: e.data }))
    };
    if (key === 'process') return evidence.filter(e => ['milestone', 'setup', 'criteria'].includes(e?.kind)).map(e => ({ kind: e.kind, source: e.source, data: e.data }));
    if (key === 'artifacts') return evidence.filter(e => e?.kind === 'artifact').map(e => ({ data: e.data, source: e.source }));
    if (key === 'resources') return Object.values(task?.resources || {});
    return {};
  }
  function shareLeafPaths(value, path) {
    if (Array.isArray(value)) return value.flatMap((child, index) => shareLeafPaths(child, `${path}.${index}`));
    if (value && typeof value === 'object') {
      const entries = Object.entries(value);
      if (!entries.length) return [];
      return entries.flatMap(([key, child]) => {
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(key)) return [];
        return shareLeafPaths(child, `${path}.${key}`);
      });
    }
    return path && value !== null && value !== undefined && value !== '' ? [path] : [];
  }
  function redactionLeaves(value, path) {
    if (Array.isArray(value)) return value.flatMap((child, index) => redactionLeaves(child, `${path}.${index}`));
    if (value && typeof value === 'object') return Object.entries(value).flatMap(([key, child]) => {
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(key)) return [];
      return redactionLeaves(child, `${path}.${key}`);
    });
    return path && value !== null && value !== undefined && value !== '' ? [[path, safeText(value)]] : [];
  }
  function pathCovered(path, selected) { return selected.some(item => item === path || path.startsWith(`${item}.`)); }
  function buildShare(id) {
    app.replaceChildren(); navState('work');
    const task = state.current;
    const steps = node('div', 'stepper'); ['1 · Review', '2 · Share'].forEach((v, i) => { const s = node('span', i === 1 ? 'active' : '', v); steps.append(s); }); app.append(steps);
    const hero = node('div', 'hero'); const copy = node('div'); add(copy, node('p', 'eyebrow', 'Share a snapshot'));
    add(copy, node('h1', '', 'Choose what the community can learn.'));
    add(copy, node('p', 'lede', 'Your full local task log stays on this device. Select optional details, redact sensitive values, then inspect the frozen snapshot before downloading it. Nothing is published automatically.')); hero.append(copy); app.append(hero);
    const notice = node('div', 'notice'); add(notice, node('strong', '', 'Core review fields are included.'));
    add(notice, node('span', '', ' Category, completion, ratings, harness structure, and recorded coverage form the community review. The fields below are optional and start unchecked.')); app.append(notice);
    const layout = node('div', 'split'); const form = node('section', 'panel'); const side = node('aside');
    add(form, node('h2', '', 'Snapshot fields'));
    const locked = node('div', 'locked-fields');
    ['Category', 'Completion and ratings', 'Harness structure and version', 'Evidence coverage'].forEach(label => {
      const row = node('label', 'locked-field'); const input = node('input'); input.type = 'checkbox'; input.checked = true; input.disabled = true; row.append(input, document.createTextNode(label)); locked.append(row);
    }); form.append(locked);
    add(form, node('h3', '', 'Optional details'));
    const fields = node('div', 'share-fields');
    SHARE_FIELDS.forEach(([key, label, desc]) => {
      const row = node('div', 'share-field'); const header = node('label', 'share-field-header'); const check = node('input'); check.type = 'checkbox'; check.dataset.shareRoot = key; check.checked = state.selected.has(key);
      const text = node('span'); add(text, node('strong', '', `Include all ${label.toLowerCase()}`)); add(text, node('p', '', desc)); header.append(check, text); row.append(header);
      const fieldChoices = node('details', 'field-choices');
      const leaves = node('div', 'share-leaves');
      const paths = shareLeafPaths(shareRoot(task, key), key).filter(path => path !== key);
      fieldChoices.append(node('summary', '', `Choose individual fields (${paths.length})`));
      fieldChoices.open = paths.some(path => state.selected.has(path));
      paths.forEach(path => {
        const leafLabel = node('label', 'share-leaf'); const leaf = node('input'); leaf.type = 'checkbox'; leaf.dataset.sharePath = path; leaf.checked = state.selected.has(key) || state.selected.has(path); leaf.disabled = state.selected.has(key);
        add(leafLabel, leaf);
        const friendlyPath = path.split('.').map(part => /^\d+$/.test(part)
          ? `Item ${Number(part) + 1}`
          : part.replaceAll('_', ' ').replaceAll('-', ' ')).join(' › ');
        add(leafLabel, node('span', '', friendlyPath)); leaves.append(leafLabel);
      });
      fieldChoices.append(leaves); row.append(fieldChoices);
      const redactions = node('div', 'redaction-list'); redactions.dataset.redactions = key; row.append(redactions); fields.append(row);
      check.addEventListener('change', () => {
        leaves.querySelectorAll('input[data-share-path]').forEach(input => { input.disabled = check.checked; if (check.checked) input.checked = false; });
      });
    }); form.append(fields);
    const actions = node('div', 'review-actions'); const make = button(state.share ? 'Update preview' : 'Create exact preview', () => createSharePreview(id, form), 'button'); actions.append(make);
    actions.append(button('Back to task', () => { location.hash = `task=${encodeURIComponent(String(task?.task_id || id))}`; }, 'button secondary')); form.append(actions);
    const error = node('div', 'inline-error'); error.id = 'share-error'; error.hidden = true; form.append(error); layout.append(form);
    const previewPanel = node('section', 'panel'); previewPanel.dataset.previewPanel = 'true'; add(previewPanel, node('h2', '', 'Preview'));
    if (state.share?.payload) renderPreview(previewPanel, state.share);
    else { add(previewPanel, node('p', 'muted', 'Create a preview to see the exact approved snapshot. No optional fields are included yet.')); }
    side.append(previewPanel); layout.append(side); app.append(layout);
    if (state.share?.payload) discoverRedactions(form, state.share.payload);
    form.addEventListener('change', event => {
      if (event.target.matches('[data-share-root],[data-share-path]')) invalidateSharePreview();
    });
    form.addEventListener('input', event => {
      if (event.target.matches('[data-redact-path]')) invalidateSharePreview();
    });
    setFocus('h1');
  }
  function invalidateSharePreview() {
    if (!state.share) return;
    state.share = null;
    const panel = app.querySelector('[data-preview-panel]'); if (!panel) return;
    panel.replaceChildren(node('h2', '', 'Preview'));
    panel.append(node('p', 'muted', 'The selected fields or redactions changed. Create a new preview to see the exact updated snapshot.'));
  }
  function discoverRedactions(form, payload) {
    const keys = new Set(state.selected);
    SHARE_FIELDS.forEach(([key]) => {
      const wrap = form.querySelector(`[data-redactions="${key}"]`); wrap.replaceChildren();
      const details = payload?.details;
      if (!details || typeof details !== 'object' || !(key in details)) return;
      redactionLeaves(details[key], key).filter(([path]) => pathCovered(path, [...keys])).forEach(([path, original]) => {
        if (typeof original !== 'string' || !original || original === 'Not recorded') return;
        const label = node('label', '', path); const input = node('textarea', 'redaction-input'); input.rows = 2; input.value = state.redactions[path] || '';
        input.placeholder = 'Leave blank to share this value'; input.dataset.redactPath = path;
        const item = node('div', 'redaction-item'); item.append(label, input); wrap.append(item);
      });
    });
  }
  function renderPreview(container, share) {
    container.replaceChildren(); add(container, node('h2', '', 'Exact snapshot preview'));
    const meta = node('div', 'preview-toolbar');
    add(meta, node('span', 'small muted', `Frozen ${formatDate(share.created_at || share.createdAt)}`));
    if (share.digest) add(meta, node('code', 'digest', `SHA-256 ${safeText(share.digest)}`)); container.append(meta);
    const pre = node('pre', 'preview-code'); pre.textContent = safeText(share.payload); container.append(pre);
    const actions = node('div', 'review-actions');
    actions.append(button('Download approved snapshot', () => downloadSnapshot(share), 'button'));
    const publish = linkButton('Open community publishing', COMMUNITY_URL, 'button secondary'); publish.target = '_blank'; publish.rel = 'noopener noreferrer'; actions.append(publish);
    container.append(actions);
    const hint = node('p', 'form-help', 'Download the approved JSON, then choose it on the community import page. The download does not send it.'); container.append(hint);
  }
  function downloadSnapshot(share) {
    if (!share || !share.payload) return;
    const json = JSON.stringify(share.payload, null, 2);
    const blob = new Blob([json], { type: 'application/json' }); const url = URL.createObjectURL(blob);
    const link = node('a'); link.href = url; link.download = 'agentreview-approved-snapshot.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); announce('Approved snapshot downloaded.');
  }
  async function createSharePreview(id, form) {
    const error = form.querySelector('#share-error'); error.hidden = true;
    const selected = [];
    SHARE_FIELDS.forEach(([key]) => {
      if (form.querySelector(`[data-share-root="${key}"]`)?.checked) selected.push(key);
      else form.querySelectorAll(`[data-share-path^="${key}."]:checked`).forEach(input => selected.push(input.dataset.sharePath));
    });
    const redactions = {};
    form.querySelectorAll('[data-redact-path]').forEach(input => { const value = input.value.trim(); if (value) redactions[input.dataset.redactPath] = value; });
    const buttonEl = form.querySelector('.button'); buttonEl.disabled = true; buttonEl.textContent = 'Preparing exact preview…';
    try {
      state.selected = new Set(selected); state.redactions = redactions;
      const created = await request(`/tasks/${encodeURIComponent(id)}/share-preview`, { method: 'POST', body: JSON.stringify({ selected_fields: selected, redactions }) });
      let frozen = created;
      if (created?.preview_id) {
        try { frozen = await request(`/shares/${encodeURIComponent(created.preview_id)}`); }
        catch (readError) { if (readError.status !== 404) throw readError; }
      }
      if (!frozen || !Object.prototype.hasOwnProperty.call(frozen, 'payload')) throw new Error('The service did not return a preview payload.');
      state.share = frozen; state.share.preview_id = created.preview_id || frozen.preview_id;
      renderShareForm(id);
      announce('Exact snapshot preview is ready.');
    } catch (err) {
      error.textContent = `Preview could not be created: ${err.message}. Your selected fields and redactions are still here.`; error.hidden = false;
      buttonEl.disabled = false; buttonEl.textContent = state.share ? 'Update preview' : 'Create exact preview';
    }
  }
  function renderShareForm(id) { buildShare(id); discoverRedactions(app.querySelector('form') || app, state.share?.payload); }
  async function loadShare(id) {
    if (state.current && String(state.current.task_id) === id) { buildShare(id); return; }
    const serial = ++requestSerial; loading('Loading share details…');
    // A share route may identify a task before a frozen preview exists, or a saved preview afterward.
    try {
      const task = await request(`/tasks/${encodeURIComponent(id)}`);
      if (serial !== requestSerial) return;
      state.current = task;
      if (task?.status !== 'reviewed' && !task?.review) { app.replaceChildren(renderError('Save the task review before preparing a community snapshot.', () => loadTask(id))); return; }
      state.share = null; state.selected = new Set(); state.redactions = {}; buildShare(id);
    } catch (taskError) {
      try {
        const preview = await request(`/shares/${encodeURIComponent(id)}`);
        if (serial !== requestSerial) return;
        if (!preview || !Object.prototype.hasOwnProperty.call(preview, 'payload')) throw new Error('This snapshot is invalid.');
        state.share = preview; state.current = null; state.selected = new Set(); buildFrozenShare(preview);
      } catch (error) {
        if (serial !== requestSerial) return;
        app.replaceChildren(renderError(taskError.status === 404 ? `Task or snapshot not found. ${error.message}` : taskError.message, () => loadShare(id)));
      }
    }
  }
  function buildFrozenShare(share) {
    app.replaceChildren(); navState('work'); const hero = node('div', 'hero'); const copy = node('div');
    add(copy, node('p', 'eyebrow', 'Frozen snapshot')); add(copy, node('h1', '', 'Your approved preview is ready.'));
    add(copy, node('p', 'lede', 'This is the saved snapshot returned by the local service. Download it to your device, then choose it from the community import page.')); hero.append(copy); app.append(hero);
    const panel = node('section', 'panel'); renderPreview(panel, share); app.append(panel); setFocus('h1');
  }

  async function renderRoute() {
    const route = getRoute(); state.routeKey = `${route.type}:${route.id || ''}`;
    if (route.type === 'work') return loadWork();
    if (route.type === 'setup') return loadSetup();
    if (route.type === 'task') return loadTask(route.id);
    if (route.type === 'review') return loadReview(route.id);
    if (route.type === 'share') return loadShare(route.id);
  }
  window.addEventListener('hashchange', renderRoute);
  renderRoute();
})();
