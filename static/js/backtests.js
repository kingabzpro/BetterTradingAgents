const byId = (id) => document.getElementById(id);
const setup = byId('setup');
const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const chicago = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', dateStyle: 'short', timeStyle: 'medium' });
const presetNames = { intraday_7_14_dte: '7–14 DTE · same session', swing_5_sessions: '14–21 DTE · 5 sessions', swing_10_sessions: '28–35 DTE · 10 sessions' };
const names = { stock: 'Stock', options_swing_v1: 'Options swing', options_intraday_v1: 'Options intraday' };
const fields = [
  ['starting_cash', 'Starting equity ($)', '0.01', '0.01', '99999999'],
  ['premium_budget', 'Shared premium budget (fraction)', '0.0001', '0.0001', '1'],
  ['commission', 'Commission per side ($)', '0.01', '0', '9999'],
  ['other_fee_per_side', 'Other fees per side ($)', '0.01', '0', '9999'],
  ['slippage', 'Adverse slippage per unit ($)', '0.0001', '0', '99'],
  ['stress_slippage', 'Stress slippage per unit ($)', '0.0001', '0', '99'],
];
let meta;
let readinessVersion = 0;
let activeRun = null;
let viewing = null;
let submitting = false;

function element(tag, content, parent) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  if (parent) parent.append(node);
  return node;
}

async function api(url, options) {
  const response = await fetch(`/api/backtests${url}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.map((e) => e.msg).join(' ') || 'Backtest request failed.');
  return data;
}

function amount(value) { return value == null ? 'Unavailable' : money.format(value); }
function pct(value) { return value == null ? 'Unavailable' : `${Number(value).toFixed(2)}%`; }
function stamp(value) { return value ? chicago.format(new Date(value)) : '—'; }

function table(parent, caption, columns, rows) {
  const outer = element('div', null, parent);
  outer.className = 'bt-table';
  outer.tabIndex = 0;
  outer.setAttribute('role', 'region');
  outer.setAttribute('aria-label', caption);
  const grid = element('table', null, outer);
  element('caption', caption, grid);
  const header = element('tr', null, element('thead', null, grid));
  for (const title of columns) element('th', title, header).scope = 'col';
  const body = element('tbody', null, grid);
  for (const row of rows) {
    const tr = element('tr', null, body);
    for (const cell of row) {
      const td = element('td', null, tr);
      if (cell instanceof Node) td.append(cell);
      else td.textContent = cell ?? '—';
    }
  }
}

function buildFields() {
  for (const [key, title, step, min, max] of fields) {
    const group = element('div', null, byId('execution-fields'));
    const id = `exec-${key}`;
    element('label', title, group).htmlFor = id;
    const input = element('input', null, group);
    Object.assign(input, { id, name: key, type: 'number', step, min, max, value: meta.execution[key], required: true });
  }
}

function payload() {
  const mode = setup.elements.mode.value;
  const common = { mode, start: byId('start').value, end: byId('end').value };
  if (mode === 'stock') return { ...common, depth: byId('depth').value, source: 'mock' };
  const execution = Object.fromEntries(fields.map(([name]) => [name, byId(`exec-${name}`).value]));
  return { ...common, preset: byId('preset').value, source: byId('source').value, execution };
}

function assumptions() {
  const start = Number(byId('exec-starting_cash').value);
  const budget = Number(byId('exec-premium_budget').value);
  byId('assumptions').textContent = `${amount(start)} per account · ${pct(budget * 100)} shared premium and fee budget (${amount(start * budget)} initially) · one contract per ticker`;
}

async function coverage() {
  const version = ++readinessVersion;
  const { mode, preset, source, start, end } = payload();
  const status = byId('readiness');
  const details = byId('coverage-content');
  details.replaceChildren();
  status.classList.remove('bt-warning');
  byId('run').disabled = !!activeRun || submitting;
  if (mode === 'stock') {
    status.textContent = `Stock replay uses the existing mock engine on ${meta.tickers.join(', ')}. Saved stock LLM reports are in history.`;
    return;
  }
  const rules = meta.presets.find((item) => item.preset === preset);
  byId('signal-source').textContent = source === 'synthetic' ? 'Signal source: invented fixtures.' : `Signal source: ${meta.signal_source} · ${meta.model}.${mode.endsWith('intraday_v1') ? ' Archived daily-signal intraday options replay.' : ''}`;
  if (rules) byId('signal-source').textContent += ` Target ${rules.target_dte} DTE.`;
  assumptions();
  if (!start || !end || start > end || !preset) return;
  status.textContent = 'Checking coverage…';
  byId('run').disabled = true;
  try {
    const ready = await api(`/readiness?${new URLSearchParams({ mode, preset, source, start, end })}`);
    if (version !== readinessVersion) return;
    status.textContent = `${ready.label} · ${ready.supported.length} of ${ready.scheduled_dates} scheduled dates supported.${ready.issue ? ` ${ready.issue}` : ''}`;
    status.classList.toggle('bt-warning', !ready.ready || !!ready.unsupported.length);
    byId('run').disabled = !ready.ready || !!activeRun || submitting;
    if (ready.provider) element('p', `${ready.provider} · ${ready.entitlement}`, details);
    const rows = [...ready.supported.map((r) => [r, 'Supported']), ...ready.unsupported.map((r) => [r, Object.entries(r.gaps).map(([key, gaps]) => `${key}: ${gaps.join(', ')}`).join(' · ')])];
    rows.sort((a, b) => a[0].signal_date.localeCompare(b[0].signal_date));
    table(details, 'Calendar and data windows (Chicago)', ['Signal date', 'Selection target', 'Exit target', 'Coverage'], rows.map(([r, label]) => [r.signal_date, stamp(r.entry_target), stamp(r.exit_target), label]));
  } catch (error) {
    if (version === readinessVersion) status.textContent = error.message;
  }
}

function updateMode() {
  const mode = setup.elements.mode.value;
  const stock = mode === 'stock';
  for (const node of document.querySelectorAll('[data-stock]')) node.hidden = !stock;
  for (const node of document.querySelectorAll('[data-option]')) node.hidden = stock;
  const select = byId('preset');
  select.replaceChildren();
  for (const item of meta.presets.filter((p) => p.mode === mode)) select.add(new Option(presetNames[item.preset], item.preset));
  for (const [key] of fields) byId(`exec-${key}`).disabled = stock;
  byId('start').min = stock ? '1990-01-01' : meta.signal_dates[0];
  byId('end').max = stock ? '2049-12-31' : meta.signal_dates.at(-1);
  coverage();
}

function chart(parent, marks, starting) {
  const canvas = element('canvas', null, parent);
  canvas.className = 'bt-chart';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.width = 960;
  canvas.height = 230;
  const context = canvas.getContext('2d');
  const arms = ['fast', 'momentum'];
  const lines = arms.map((arm) => [starting[arm], ...marks.map((m) => m[arm].marked_equity)].map((v) => v == null ? null : Number(v)));
  const values = lines.flat().filter(Number.isFinite);
  const low = Math.min(...values) - 1;
  const high = Math.max(...values) + 1;
  for (let i = 0; i < lines.length; i++) {
    const color = i ? '#7ab8ff' : '#6fe0a0';
    context.strokeStyle = color;
    context.lineWidth = 3;
    context.beginPath();
    let connected = false;
    for (const [j, v] of lines[i].entries()) {
      if (v == null) { connected = false; continue; }
      const x = 12 + (j / Math.max(1, marks.length)) * 936;
      const y = 186 - ((v - low) / (high - low)) * 165;
      if (connected) context.lineTo(x, y);
      else context.moveTo(x, y);
      connected = true;
    }
    context.stroke();
    context.fillStyle = color;
    context.font = '16px sans-serif';
    context.fillText(i ? 'Momentum' : 'Fast', 12 + i * 72, 217);
  }
}

function optionsResult(parent, data) {
  const arms = ['fast', 'momentum'];
  const { primary, stress, manifest } = data;
  if (manifest.signal_label) element('p', manifest.signal_label, parent);
  element('p', `${presetNames[manifest.rules.preset]} · ${manifest.provider} · ${data.coverage.supported.length}/${data.coverage.scheduled_dates} supported dates`, parent);
  const metrics = [
    ['Starting equity', 'starting_equity', amount], ['Ending equity (all-in)', 'ending_equity', amount], ['Net return (all-in)', 'net_return_pct', pct],
    ['Trading-only return (same fills)', 'trading_only_return_pct_same_fills', pct], ['Realized trade P&L', 'realized_pnl', amount],
    ['Cash available', 'available_cash', amount], ['Trading fees', 'fees', amount], ['Research costs', 'research_costs', amount],
    ['BUY signals', 'buy_signals'], ['Eligible contracts', 'eligible_contracts'], ['Affordable individually', 'affordable_candidates'],
    ['Positions opened', 'positions_opened'], ['Closed trades', 'closed_trades'], ['Unresolved positions', 'unresolved_positions'],
    ['Same-day breaches', 'intraday_breaches'], ['Premium committed', 'premium_committed_total', amount], ['Peak batch exposure', 'peak_batch_exposure_pct', pct],
    ['Mean trade P&L', 'average_trade_pnl', amount], ['Median trade P&L', 'median_trade_pnl', amount], ['Cohort-end drawdown', 'cohort_end_drawdown_pct', pct], ['Executability', 'executability'],
  ];
  table(parent, 'Portfolio comparison', ['Metric', 'Fast', 'Momentum', 'Fast · stress', 'Momentum · stress'], metrics.map(([label, key, format = String]) => [label, ...[primary, stress].flatMap((scenario) => arms.map((arm) => format(scenario.accounts[arm][key])))]));
  element('h3', 'Cohort-end equity', parent);
  chart(parent, primary.cohort_end_marks, Object.fromEntries(arms.map((arm) => [arm, primary.accounts[arm].starting_equity])));
  const eq = element('details', null, parent);
  element('summary', 'Equity and paired return differences', eq);
  table(eq, 'Cohort marks', ['Date', 'Fast equity', 'Momentum equity', 'Fast − momentum'], primary.cohort_end_marks.map((r, i) => [r.signal_date, amount(r.fast.marked_equity), amount(r.momentum.marked_equity), primary.paired_differences[i]?.difference_pp == null ? 'Unavailable' : `${Number(primary.paired_differences[i].difference_pp).toFixed(2)} pp`]));
  for (const [title, scenario] of [['Primary execution', primary], ['Stressed execution', stress]]) {
    const drill = element('details', null, parent);
    element('summary', `${title} trades and skips`, drill);
    table(drill, 'Trades (Chicago time)', ['Arm', 'Contract', 'Entry', 'Exit', 'Trade P&L', 'Execution status'], arms.flatMap((arm) => scenario.accounts[arm].trades.map((t) => [arm, t.contract_id, stamp(t.entry_at), stamp(t.exit_at), amount(t.realized_pnl), t.unresolved_reason || (t.same_day_breach ? 'Same-day exit breached' : t.delayed_exit ? 'Delayed exit' : 'Closed')])));
    table(drill, 'Skipped BUY signals', ['Arm', 'Ticker', 'Reason', 'Count'], arms.flatMap((arm) => scenario.accounts[arm].skips.map((s) => [arm, s.ticker, s.reason.replaceAll('_', ' '), s.count])));
  }
  const limitations = element('details', null, parent);
  element('summary', 'Coverage, limitations and provenance', limitations);
  element('p', data.limitations.join(' '), limitations);
  element('pre', JSON.stringify({ coverage_gaps: data.coverage.unsupported, manifest }, null, 2), limitations);
}

function renderResult(row) {
  const body = byId('result-body');
  body.replaceChildren();
  byId('result').hidden = false;
  const breach = ['primary', 'stress'].some((scenario) => Object.values(row.result[scenario]?.accounts || {}).some((arm) => arm.intraday_breaches > 0));
  byId('classification').textContent = `${row.label}${row.status === 'incomplete' ? ' · Incomplete' : ''}${breach ? ' · Same-day rule breached' : ''}`;
  byId('classification').classList.toggle('bt-warning', row.status === 'incomplete' || breach || row.result?.manifest?.request?.source === 'synthetic');
  byId('export').href = `/api/backtests/runs/${encodeURIComponent(row.run_id)}/export`;
  if (row.result.primary) optionsResult(body, row.result);
  else {
    const { config, overall } = row.result;
    element('p', `${config.mode} · ${config.tickers.join(', ')} · ${config.start} to ${config.end}`, body);
    table(body, 'Existing stock grading', ['Decisions', 'Mean net', 'Cumulative score'], [[overall.decisions, pct(overall.avg_net_pct), pct(overall.cumulative_pct)]]);
  }
}

async function history() {
  const rows = await api('/runs');
  const target = byId('history');
  target.replaceChildren();
  table(target, 'History', ['Mode', 'Preset', 'Result type', 'Status', 'Run'], rows.map((r) => {
    const link = element('a', 'Open');
    link.href = `?run=${encodeURIComponent(r.run_id)}`;
    link.addEventListener('click', (event) => {
      event.preventDefault();
      window.history.replaceState(null, '', link.href);
      inspect(r.run_id);
    });
    return [names[r.mode], presetNames[r.preset] || r.preset, r.label, r.status, link];
  }));
}

async function inspect(runId) {
  viewing = runId;
  try {
    const row = await api(`/runs/${encodeURIComponent(runId)}`);
    if (viewing !== runId) return;
    const running = row.status === 'running';
    const progress = byId('progress');
    progress.hidden = !running;
    if (row.total) { progress.max = row.total; progress.value = row.progress; }
    else progress.removeAttribute('value');
    byId('run-status').textContent = row.message || (running ? `Running ${row.label}…` : `${row.label} · ${row.status}`);
    if (running) {
      activeRun = runId;
      byId('run').disabled = true;
      byId('cancel').disabled = false;
      window.setTimeout(() => { if (viewing === runId) inspect(runId); }, 500);
      return;
    }
    if (activeRun === runId) activeRun = null;
    byId('cancel').disabled = !activeRun;
    if (row.result) renderResult(row);
    else byId('result').hidden = true;
    await Promise.all([history(), coverage()]);
  } catch (error) { byId('run-status').textContent = error.message; }
}

setup.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (submitting || activeRun) return;
  submitting = true;
  byId('run').disabled = true;
  byId('result').hidden = true;
  try {
    const row = await api('/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()) });
    activeRun = row.run_id;
    window.history.replaceState(null, '', `?run=${row.run_id}`);
    inspect(row.run_id);
  } catch (error) {
    byId('run-status').textContent = error.message;
  } finally {
    submitting = false;
    coverage();
  }
});

byId('cancel').addEventListener('click', async () => {
  if (!activeRun) return;
  try { await api(`/runs/${activeRun}/cancel`, { method: 'POST' }); await inspect(activeRun); }
  catch (error) { byId('run-status').textContent = error.message; }
});
byId('refresh').addEventListener('click', () => history().catch((e) => { byId('run-status').textContent = e.message; }));

try {
  meta = await api('');
  buildFields();
  [byId('start').value, byId('end').value] = [meta.signal_dates[0], meta.signal_dates.at(-1)];
  setup.addEventListener('change', (event) => {
    if (event.target.name === 'mode') updateMode();
    else if (['preset', 'source', 'start', 'end'].includes(event.target.name)) coverage();
    else if (['starting_cash', 'premium_budget'].includes(event.target.name)) assumptions();
  });
  updateMode();
  await history();
  const run = new URLSearchParams(location.search).get('run');
  if (run) inspect(run);
} catch (error) { byId('run-status').textContent = error.message; }
