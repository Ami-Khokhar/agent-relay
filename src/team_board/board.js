(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = { token: '', teams: [], selectedId: null, detail: null, activeThread: null, activeThreadId: null, timer: null, inFlight: false, lastGood: 0, selectionVersion: 0 };
  const listEl = $('#run-list'), content = $('#run-content'), errorEl = $('#error-banner');
  const text = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const arr = value => Array.isArray(value) ? value : [];
  const date = value => { if (value === null || value === undefined || value === '') return 'Unknown'; const d = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(+d) ? text(value) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); };
  const element = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; };
  const field = (obj, ...keys) => { for (const key of keys) if (obj && obj[key] !== undefined && obj[key] !== null) return obj[key]; return undefined; };
  const list = (value, keys) => arr(field(value, ...keys));

  async function request(path) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 8000);
    let response;
    try { response = await fetch(path, { headers: { Authorization: `Bearer ${state.token}`, Accept: 'application/json' }, cache: 'no-store', signal: controller.signal }); }
    catch (error) { if (error.name === 'AbortError') throw new Error('Request timed out after 8 seconds'); throw error; }
    finally { clearTimeout(timeout); }
    if (!response.ok) throw new Error(`${response.status} ${response.statusText || 'Request failed'}`);
    return response.json();
  }
  function setConnection(label, connected) {
    $('#connection-status').textContent = label;
    $('#connection-dot').classList.toggle('is-live', !!connected);
  }
  function showError(message) {
    errorEl.hidden = !message;
    errorEl.textContent = message || '';
    if (message && state.lastGood) errorEl.textContent += ` Showing last successful update from ${date(state.lastGood)}.`;
    setConnection(message ? 'Connection issue' : 'Connected', !message);
  }
  function unwrapTeams(data) { return Array.isArray(data) ? data : arr(field(data, 'teams', 'runs', 'items')); }
  function identity(team) { return text(field(team, 'id', 'team_id', 'run_id'), ''); }
  function title(team) { return text(field(team, 'objective', 'title', 'name'), 'Untitled run'); }
  function renderRuns() {
    listEl.replaceChildren();
    if (!state.teams.length) { listEl.append(element('p', 'empty-note', 'No team runs yet.')); return; }
    state.teams.forEach(team => {
      const id = identity(team), button = element('button', 'run-item' + (id === state.selectedId ? ' selected' : ''));
      button.type = 'button'; button.setAttribute('aria-current', id === state.selectedId ? 'page' : 'false');
      button.append(element('strong', '', title(team)), element('span', 'run-phase', text(field(team, 'phase', 'status'), 'Unknown phase')));
      button.addEventListener('click', () => selectRun(id)); listEl.append(button);
    });
  }
  function section(titleText, data, renderRow) {
    const sectionEl = element('section', 'data-section'), head = element('div', 'section-title');
    head.append(element('h2', '', titleText), element('span', 'count', String(data.length)));
    sectionEl.append(head);
    if (!data.length) sectionEl.append(element('p', 'empty-note', 'Nothing recorded yet.'));
    else { const rows = element('div', 'rows'); data.forEach(item => rows.append(renderRow(item))); sectionEl.append(rows); }
    return sectionEl;
  }
  function card(name, subtitle, body) { const node = element('article', 'record-card'); node.append(element('strong', 'record-name', text(name, 'Untitled')), element('span', 'record-sub', text(subtitle))); if (body) node.append(element('p', 'record-body', text(body))); return node; }
  function personRow(person) {
    const role = field(person, 'role'), harness = field(person, 'harness'), model = field(person, 'model');
    const participantId = field(person, 'id', 'participant_id', 'agentId');
    const assignment = list(state.detail, ['assignments']).find(a => field(a, 'participant_id', 'participantId') === participantId);
    const row = card(field(person, 'name', 'participant_id', 'id'), [role, harness, model].filter(Boolean).join(' · '), field(person, 'assignment', 'assignment_title') || field(assignment, 'text', 'assignment'));
    const id = field(person, 'assignment_id') || field(assignment, 'id') || participantId;
    const attempts = assignment ? list(state.detail, ['attempts']).filter(item => field(item, 'assignment_id') === field(assignment, 'id') && field(item, 'assignment_revision') === field(assignment, 'revision')) : [];
    const latestAttempt = attempts.sort((a, b) => Number(field(b, 'updated', 'created') || 0) - Number(field(a, 'updated', 'created') || 0))[0];
    const status = element('span', 'status-pill', role === 'lead' ? 'External lead' : text(field(person, 'execution_status', 'status') || field(latestAttempt, 'status'), 'Not started')); row.append(status);
    row.tabIndex = 0; row.setAttribute('role', 'button'); row.setAttribute('aria-label', `Open conversation for ${text(field(person, 'name', 'participant_id', 'id'))}`);
    row.addEventListener('click', () => openThread(id)); row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openThread(id); } });
    return row;
  }
  function assignmentRow(item) {
    const owner = field(item, 'owner', 'assignee', 'participant_id');
    const row = card(field(item, 'title', 'name', 'text', 'id'), [field(item, 'status'), owner].filter(Boolean).join(' · '), field(item, 'progress_summary', 'summary', 'description'));
    const blockers = list(item, ['blockers']); if (blockers.length) row.append(element('p', 'blocker', `Blocked: ${blockers.map(x => typeof x === 'string' ? x : text(field(x, 'title', 'description'))).join(' · ')}`));
    const deps = list(item, ['dependencies', 'depends_on']); if (deps.length) row.append(element('p', 'record-sub', `Depends on: ${deps.map(x => typeof x === 'string' ? x : text(field(x, 'title', 'id'))).join(', ')}`));
    row.tabIndex = 0; row.setAttribute('role', 'button'); row.addEventListener('click', () => openThread(field(item, 'id', 'assignment_id'))); row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openThread(field(item, 'id', 'assignment_id')); } }); return row;
  }
  function renderRun(run) {
    const header = element('div', 'run-header');
    const phase = element('span', 'phase-tag', text(field(run, 'phase'), 'Phase unknown'));
    const status = field(run, 'status') || (run.closed === true ? 'closed' : 'open');
    const summary = field(run, 'summary');
    const summaryText = typeof summary === 'string' ? summary : field(summary, 'text', 'body');
    header.append(element('span', 'eyebrow', `TEAM RUN · ${text(status, 'STATUS UNKNOWN').toUpperCase()}`), phase, element('h1', '', title(run)), element('p', 'lead-summary', text(field(run, 'lead_summary') || summaryText, 'No lead summary recorded.')));
    const summaryTime = field(summary, 'updated', 'updated_at', 'created') ?? field(run, 'summary_updated_at', 'updated_at');
    if (summaryText || field(run, 'lead_summary')) header.append(element('p', 'updated', `Summary updated ${date(summaryTime)}`));
    const columns = element('div', 'content-grid');
    const roster = list(run, ['roster', 'participants', 'members']);
    const assignments = list(run, ['assignments', 'tasks']);
    const questions = list(run, ['questions', 'pending_questions', 'decisions']);
    if (!questions.length) questions.push(...list(run, ['events']).filter(e => /user.?question|decision.?request/i.test(text(field(e, 'kind'), ''))).map(e => ({ question: field(e, 'body'), asked_by: field(e, 'actor'), status: 'Recorded', context: field(e, 'thread_id') })));
    const artifacts = list(run, ['artifacts', 'results']);
    if (run.spec) artifacts.unshift({ title: 'Recorded specification', type: run.approved ? 'Approved specification' : 'Draft specification', description: run.spec });
    columns.append(section('Team', roster, personRow), section('Assignments', assignments, assignmentRow));
    const lower = element('div', 'content-grid lower-grid');
    lower.append(section('Needs a decision', questions, q => card(field(q, 'title', 'question', 'prompt', 'body'), [field(q, 'status'), field(q, 'asked_by', 'actor')].filter(Boolean).join(' · '), field(q, 'context', 'description', 'thread_id'))));
    lower.append(section('Artifacts', artifacts, a => { const c = card(field(a, 'title', 'name', 'type'), field(a, 'kind', 'type'), field(a, 'description', 'summary', 'uri')); const url = field(a, 'url', 'href', 'uri'); if (url && /^https?:\/\//i.test(url)) { const link = element('a', 'artifact-link', 'Open artifact'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; c.append(link); } return c; }));
    const limits = field(run, 'limits', 'resource_limits') || {}, usage = field(run, 'usage', 'resource_usage') || {};
    const resourceSection = element('section', 'resource-section'); resourceSection.append(element('h2', '', 'Resources'));
    [['Concurrency', field(limits, 'concurrency', 'max_concurrency'), field(usage, 'concurrency', 'active')], ['Messages', field(limits, 'max_messages'), field(usage, 'messages')], ['Elapsed time', field(limits, 'elapsed_seconds', 'max_elapsed_seconds'), field(usage, 'elapsed_seconds')], ['Spend', field(limits, 'spend', 'max_spend'), field(usage, 'spend', 'estimated_spend')]].forEach(([label, cap, used]) => { const row = element('div', 'resource-row'); row.append(element('span', '', label), element('span', '', `Limit: ${text(cap, 'Unknown')} · Used: ${text(used, 'Unknown')}`)); resourceSection.append(row); });
    columns.append(lower, resourceSection);
    content.replaceChildren(header, columns);
  }
  async function selectRun(id) {
    if (!id) return; state.selectedId = id; state.activeThread = null; renderRuns();
    content.replaceChildren(element('p', 'loading', 'Loading team details…'));
    const version = ++state.selectionVersion;
    try { const result = await request(`/v1/teams/${encodeURIComponent(id)}`); if (version !== state.selectionVersion || state.selectedId !== id) return; state.detail = field(result, 'team', 'run') || result; renderRun(state.detail); showError(''); }
    catch (error) { if (version !== state.selectionVersion || state.selectedId !== id) return; content.replaceChildren(element('p', 'error-inline', `Could not load this team: ${error.message}`)); showError(`Could not load team details. ${error.message}`); }
  }
  function openThread(id, focus = true) {
    state.activeThreadId = id || null;
    const run = state.detail || {}, threads = list(run, ['threads', 'conversations']);
    let thread = threads.find(t => [field(t, 'assignment_id'), field(t, 'participant_id'), field(t, 'id')].includes(id));
    const allMessages = list(run, ['messages', 'events']);
    if (!thread) {
      const related = allMessages.filter(m => !id || [field(m, 'assignment_id'), field(m, 'participant_id'), field(m, 'thread_id'), field(m, 'actor'), field(m, 'recipient')].includes(id));
      const threadIds = new Set(related.map(m => field(m, 'thread_id')).filter(Boolean));
      thread = { messages: id ? allMessages.filter(m => related.includes(m) || threadIds.has(field(m, 'thread_id'))) : allMessages };
    }
    state.activeThread = thread;
    const assignment = list(run, ['assignments']).find(a => field(a, 'id') === id);
    const participant = list(run, ['participants', 'roster', 'members']).find(p => field(p, 'id', 'agentId') === id);
    $('#drawer-title').textContent = text(field(thread, 'title', 'name'), assignment ? `Thread · ${text(field(assignment, 'text'), 'Assignment')}` : participant ? `Thread · ${text(field(participant, 'name', 'id', 'agentId'))}` : id ? `Thread · ${id}` : 'Run conversation');
    const body = $('#drawer-body'); body.replaceChildren();
    const messages = list(thread, ['messages', 'events']);
    const inbox = Object.values(field(run, 'inbox', 'inbox_by_participant') || {}).flat();
    if (!messages.length) body.append(element('p', 'empty-note', 'No conversation messages recorded for this thread.'));
    messages.forEach(message => {
      const msg = element('article', 'message');
      const author = field(message, 'author', 'sender', 'participant_name', 'participant_id', 'actor');
      msg.append(element('div', 'message-meta', `${text(author, 'Unknown author')} · ${date(field(message, 'created_at', 'timestamp', 'created'))}`));
      let bodyText = field(message, 'text', 'body', 'content');
      if (/conclusion/i.test(text(field(message, 'kind'), '')) && typeof bodyText === 'string') {
        try { const parsed = JSON.parse(bodyText); if (parsed && typeof parsed === 'object') { const conclusion = element('section', 'conclusion'); conclusion.append(element('strong', '', 'Recorded conclusion'), element('p', '', text(field(parsed, 'summary', 'conclusion', 'text'))), element('p', 'record-sub', `Rationale: ${text(field(parsed, 'rationale'))} · Action: ${text(field(parsed, 'action', 'owner'))}`)); msg.append(conclusion); bodyText = ''; } } catch (_) { /* Keep the recorded body visible when it is not structured JSON. */ }
      }
      if (bodyText) msg.append(element('p', 'message-text', text(bodyText, '')));
      const tags = list(message, ['tags', 'mentions']); const recipient = field(message, 'recipient'); if (recipient) tags.push(`@${recipient}`); if (tags.length) { const tagrow = element('div', 'tag-row'); tags.forEach(tag => tagrow.append(element('span', 'tag', typeof tag === 'string' ? tag : `@${text(field(tag, 'name', 'participant_id'))}`))); msg.append(tagrow); }
      const kind = field(message, 'kind', 'type'); if (kind) msg.append(element('span', `event-kind ${String(kind).toLowerCase()}`, String(kind)));
      const inboxRecord = inbox.find(item => field(item, 'id', 'event_id') === field(message, 'id'));
      const acknowledged = field(inboxRecord, 'acknowledged');
      const delivery = field(message, 'delivery_state', 'delivery_status') || (inboxRecord ? acknowledged ? `Acknowledged ${date(acknowledged)}` : 'Addressed · awaiting acknowledgment' : 'Unknown');
      msg.append(element('p', 'record-sub', `Delivery: ${text(delivery)} · Response: ${text(field(message, 'response_state', 'response_status'))}`)); body.append(msg);
    });
    list(thread, ['conclusions']).forEach(c => { const box = element('section', 'conclusion'); box.append(element('strong', '', 'Recorded conclusion'), element('p', '', text(field(c, 'text', 'summary', 'conclusion'))), element('p', 'record-sub', `Rationale: ${text(field(c, 'rationale'))} · Action: ${text(field(c, 'owner', 'action_owner'))}`)); body.append(box); });
    list(thread, ['sessions', 'attempts']).filter(s => field(s, 'replacement', 'is_replacement')).forEach(s => body.append(element('p', 'replacement-note', `Replacement session started · ${date(field(s, 'created_at', 'started_at'))}`)));
    const attempts = list(run, ['attempts']).filter(attempt => field(attempt, 'assignment_id') === id);
    attempts.forEach(attempt => {
      const item = element('article', 'message attempt');
      item.append(element('div', 'message-meta', `Execution attempt · ${text(field(attempt, 'status'), 'Unknown status')} · ${date(field(attempt, 'updated', 'created'))}`));
      const result = field(attempt, 'result');
      const replacement = field(result, 'replacement_context');
      if (replacement) item.append(element('p', 'replacement-note', 'Replacement context recorded · native session not resumed'));
      if (result !== undefined && result !== null) item.append(element('p', 'message-text', typeof result === 'string' ? result : JSON.stringify(result, null, 2)));
      body.append(item);
    });
    $('#conversation-drawer').classList.add('open'); $('#conversation-drawer').setAttribute('aria-hidden', 'false'); $('#scrim').hidden = false; if (focus) $('.close-drawer').focus();
  }
  function closeDrawer() { $('#conversation-drawer').classList.remove('open'); $('#conversation-drawer').setAttribute('aria-hidden', 'true'); $('#scrim').hidden = true; }
  async function refresh() {
    if (state.inFlight || !state.token) return; state.inFlight = true;
    try {
      const teams = unwrapTeams(await request('/v1/teams')); state.teams = teams;
      const chosen = state.selectedId && teams.some(t => identity(t) === state.selectedId) ? state.selectedId : identity(teams[0]);
      state.lastGood = Math.floor(Date.now() / 1000); renderRuns();
      if (chosen && chosen === state.selectedId) {
        const version = state.selectionVersion;
        const result = await request(`/v1/teams/${encodeURIComponent(chosen)}`);
        if (version !== state.selectionVersion || state.selectedId !== chosen) return;
        state.detail = field(result, 'team', 'run') || result; renderRun(state.detail);
        if (state.activeThread) openThread(state.activeThreadId, false);
      } else if (chosen) await selectRun(chosen); else { state.selectedId = null; content.replaceChildren(element('p', 'empty-note', 'No team runs are available yet.')); }
      showError('');
    } catch (error) { showError(`Refresh failed. ${error.message}`); }
    finally { state.inFlight = false; }
  }
  function connect(token) { state.token = token.trim(); if (!state.token) return; $('#connect-panel').classList.add('connected'); $('#run-layout').hidden = false; refresh(); clearInterval(state.timer); state.timer = setInterval(refresh, 15000); }
  $('#connect-form').addEventListener('submit', e => { e.preventDefault(); connect($('#token').value); });
  $('#connect-toggle').addEventListener('click', () => { $('#connect-panel').classList.toggle('connected'); $('#token').focus(); });
  $('#refresh').addEventListener('click', refresh); $('.close-drawer').addEventListener('click', closeDrawer); $('#scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
})();
