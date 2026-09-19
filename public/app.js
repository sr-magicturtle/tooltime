/* TOOLTIME — PS1 track access planner.
   Four steps: load an instance, plan it, explore the result, export the submission. */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = v => String(v ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SCENARIOS = {
  A: { name: 'Strict supply',
       blurb: 'Capacity is fixed and early closures are not allowed. The only thing that can move is a deadline.',
       scored: 'Scored on delay, weighted by contract priority.' },
  B: { name: 'Strict schedule',
       blurb: 'Every deadline must be met. You pay for it with early closures and extra access nights.',
       scored: 'Scored on what hitting the dates costs.' },
  C: { name: 'Balanced',
       blurb: 'Neither is absolute. A little extra capacity is tolerated, and early closures are allowed in a two-week window.',
       scored: 'Scored on delay and spend together.' }
};

const state = {
  step: 'instance', scenario: 'C', snap: null,
  schedule: null, precheck: null, negotiation: null, busy: false,
  // Re-rendering rebuilds the form, so these two live here rather than in the DOM.
  seconds: 20, showNegotiation: false, uploadError: null,
  dashboard: null, calendar: null, week: null, selectedWeek: null
};

/* ────────────────────────────────────────────────────────────── helpers */

function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(window._toast);
  window._toast = setTimeout(() => (el.className = ''), 4600);
}

async function api(path, body) {
  const res = await fetch('/api/' + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}

const plan = () => state.snap?.scenarios?.[state.scenario] || null;
const ready = s => state.snap?.scenarios?.[s]?.feasible;

function openDrawer(title, html) {
  $('#drawer-title').textContent = title;
  $('#drawer-body').innerHTML = html;
  $('#drawer').hidden = false;
  $('#scrim').hidden = false;
}
function closeDrawer() { $('#drawer').hidden = true; $('#scrim').hidden = true; }

function modal(title, bodyHtml, actionLabel, onConfirm) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  $('#modal-foot').innerHTML =
    `<button class="btn secondary" value="cancel">Cancel</button>
     <button class="btn" id="modal-go" value="go">${esc(actionLabel)}</button>`;
  const dlg = $('#modal');
  dlg.showModal();
  $('#modal-go').onclick = async ev => {
    ev.preventDefault();
    try { await onConfirm(); dlg.close(); } catch (err) { toast(err.message, true); }
  };
}

/* ────────────────────────────────────────────────────────────── step 1 */

function viewInstance() {
  const info = state.snap?.instance;
  const pre = state.precheck;
  return `
    <div class="page-head">
      <h1>Load the demand book</h1>
      <p>Eight CSV files describe the network, the contracts and the work to be done.
         Drop them in, or start from the instance packaged with this tool.</p>
    </div>

    <div class="drop" id="drop">
      <h3>Drop the eight CSV files here</h3>
      <p>01_LINES · 02_STATIONS · 03_SECTORS · 04_LOCATION_SUPPLY<br>
         05_BUFFER_LOCATION · 06_PARAMETERS · 07_PROJECT_DETAILS · 08_ACTIVITY_DETAILS</p>
      <div class="row" style="justify-content:center">
        <button class="btn" id="btn-pick">Choose files</button>
        <button class="btn secondary" id="btn-reset">Use the packaged instance</button>
      </div>
      <input type="file" id="files" multiple accept=".csv">
    </div>

    ${state.uploadError ? `<div class="note bad" style="margin-top:14px">
      <b>Those files were not loaded</b>
      <p>${esc(state.uploadError)}</p>
      <p style="margin-top:6px">The previous instance is still in place, so anything you
         plan now uses the old data. Fix the file and upload again.</p>
    </div>` : ''}

    ${info ? `
      <div class="card" style="margin-top:18px">
        <h3>${esc(state.snap.instance_name)}</h3>
        <p>This is what the solver will work with.</p>
        <div class="grid stats">
          <div class="stat"><b>${info.activities}</b><span>activities</span></div>
          <div class="stat"><b>${info.contracts}</b><span>contracts</span></div>
          <div class="stat"><b>${info.access_nights}</b><span>access nights</span></div>
          <div class="stat"><b>${info.locations}</b><span>locations</span></div>
          <div class="stat"><b>${info.horizon_weeks}</b><span>weeks</span></div>
          <div class="stat"><b>${info.lines.length}</b><span>lines</span></div>
        </div>
        <p style="margin-top:14px; font-size:13px; color:var(--muted)">
          Planning starts ${esc(info.horizon_start)}.
          ${info.live_contracts.length
            ? `Live traction work on ${esc(info.live_contracts.join(', '))} —
               those weeks close both tracks and cross to the other line at the interchange.`
            : 'No live traction work in this instance.'}
        </p>
      </div>

      <div class="card">
        <h3>Is it deliverable?</h3>
        <p>A quick arithmetic check before any solving — it finds what no schedule could fix.</p>
        ${pre ? `
          <div class="note ${pre.deliverable ? 'good' : 'bad'}">
            <b>${pre.deliverable ? 'No blocking problems' : 'Not deliverable as specified'}</b>
            <p>${esc(pre.summary)}</p>
          </div>
          ${pre.blocking.map(f => `<div class="note bad"><b>${esc(f.subject)}</b><p>${esc(f.detail)}</p></div>`).join('')}
          ${pre.tight.slice(0, 6).map(f => `<div class="note warn"><b>${esc(f.subject)}</b><p>${esc(f.detail)}</p></div>`).join('')}
          ${pre.tight.length > 6 ? `<p style="font-size:13px;color:var(--muted)">…and ${pre.tight.length - 6} more tight spots.</p>` : ''}
        ` : `<button class="btn secondary" id="btn-precheck">Run the check</button>`}
      </div>

      <div class="row"><button class="btn" id="btn-to-plan">Continue to planning →</button></div>
    ` : ''}`;
}

/* ────────────────────────────────────────────────────────────── step 2 */

function viewPlan() {
  const p = plan();
  return `
    <div class="page-head">
      <h1>Choose a policy, then plan</h1>
      <p>The same work can be scheduled three ways, depending on what you are allowed to bend.
         Each produces its own submission.</p>
    </div>

    <div class="scenarios">
      ${Object.entries(SCENARIOS).map(([key, s]) => {
        const done = state.snap?.scenarios?.[key];
        return `<button class="scenario" data-scenario="${key}" aria-pressed="${state.scenario === key}">
          <span class="tag">Scenario ${key}</span>
          <h3>${esc(s.name)}</h3>
          <p>${esc(s.blurb)}</p>
          <span class="score">
            <span>${done ? (done.feasible ? 'Planned' : 'Has violations') : 'Not planned yet'}</span>
            <b>${done?.score ?? '—'}</b>
          </span>
        </button>`;
      }).join('')}
    </div>

    <div class="card" style="margin-top:18px">
      <h3>Run the scheduler</h3>
      <p>${esc(SCENARIOS[state.scenario].scored)} Lower is better — zero is perfect.</p>
      <div class="grid two">
        <label class="field">
          <span>Time to spend searching</span>
          <input type="number" id="seconds" value="${state.seconds}" min="1" max="120">
        </label>
        <label class="check" style="align-self:end; padding-bottom:14px">
          <input type="checkbox" id="use-agents" ${state.showNegotiation ? 'checked' : ''}>
          <span>Show the negotiation
            <em>Contract agents argue over who concedes. Slower, and you can read the transcript afterwards.</em>
          </span>
        </label>
      </div>
      <button class="btn" id="btn-solve">Plan Scenario ${state.scenario}</button>
    </div>

    ${p ? verdictBlock(p) : `<div class="card"><p class="empty">No schedule for Scenario ${state.scenario} yet.</p></div>`}
  `;
}

function verdictBlock(p) {
  const s = p.soft_scores || {};
  const ok = p.feasible;
  return `
    <div class="verdict ${ok ? 'pass' : 'fail'}">
      <div class="headline">
        <b>${ok ? 'Schedule is valid' : 'Schedule has problems'}</b>
        <small>${ok
          ? 'All work placed, every safety rule respected.'
          : `${(p.hard_violations || []).length} rule breach(es) — see below.`}</small>
      </div>
      <div class="metrics">
        <div><b>${s.objective_score ?? '—'}</b><span>penalty score</span></div>
        <div><b>${s.overrun_days_total ?? '—'}</b><span>days late</span></div>
        <div><b>${s.eclo_nights_total ?? 0}</b><span>early closures</span></div>
        <div><b>${s.excess_access_nights_total ?? 0}</b><span>extra nights</span></div>
        <div><b>${p.seconds ?? '—'}s</b><span>to plan</span></div>
      </div>
    </div>
    ${p.churn ? `<div class="note info"><b>What changed</b><p>${esc(p.churn.summary)}</p></div>` : ''}
    ${p.error ? `<div class="note warn"><b>Note</b><p>${esc(p.error)}</p></div>` : ''}
    ${!p.feasible ? `<div class="card">
        <h3>Rule breaches</h3>
        ${(p.hard_violations || []).map(v =>
          `<div class="note bad"><b>${esc(v.rule)}</b><p>${esc(v.detail)}</p></div>`).join('')}
      </div>` : ''}
    <div class="row">
      <button class="btn" id="btn-to-explore">Explore this schedule →</button>
      ${p.negotiation ? `<button class="btn secondary" id="btn-negotiation">Read the negotiation</button>` : ''}
    </div>`;
}

/* ────────────────────────────────────────────────────────────── step 3 */

function viewExplore() {
  const p = plan();
  if (!p) return `<div class="page-head"><h1>Explore</h1></div>
    <div class="card"><p class="empty">Plan a scenario first.</p></div>`;
  const sc = state.schedule;
  return `
    <div class="page-head">
      <h1>The schedule, and why</h1>
      <p>Every bar is one activity across the planning horizon.
         Click any activity to see why it sits where it does.</p>
    </div>

    ${verdictBlock(p)}
    ${dashboardBlock()}
    ${calendarBlock()}

    <div class="card">
      <h3>Something changed on the ground?</h3>
      <p>Describe it in plain English. The plan is re-solved and you are shown exactly what moved.</p>
      <label class="field">
        <span>For example: “H01 to H02 eastbound on Beta is down to one slot for weeks 12 to 14”</span>
        <textarea id="disrupt" placeholder="Type what happened…"></textarea>
      </label>
      <div class="row">
        <button class="btn secondary" id="btn-disrupt">Apply and re-plan</button>
        ${state.snap.reductions.length
          ? `<button class="btn quiet" id="btn-clear-disrupt">Clear ${state.snap.reductions.length} change(s)</button>`
          : ''}
        <button class="btn quiet" id="btn-override">Add an override…</button>
      </div>
      ${state.snap.locks.length ? `<div style="margin-top:14px">
        ${state.snap.locks.map(l => `<div class="note warn">
          <b>${esc(l.activity)} — ${esc(l.lever === 'pin' ? 'must run in week ' + l.week
            : l.lever === 'forbid' ? 'must not run in week ' + l.week : 'no early closures')}</b>
          <p>${esc(l.reason)} · <span class="linkish" data-unlock="${esc(l.id)}">remove</span></p>
        </div>`).join('')}</div>` : ''}
    </div>

    ${sc ? `
      <div class="legend">
        <span><i style="background:var(--green)"></i>working night</span>
        <span><i style="background:var(--orange)"></i>early closure</span>
        <span><i style="background:var(--red)"></i>past the contract target</span>
        <span><i style="background:#eef1ea"></i>idle</span>
      </div>
      <div class="gantt">
        <table>
          <thead><tr>
            <th style="width:92px">Activity</th><th style="width:118px">Contract</th>
            <th class="num" style="width:62px">Nights</th>
            <th>Week 1 → ${sc.horizon_weeks}</th>
          </tr></thead>
          <tbody>${sc.activities.map(a => ganttRow(a, sc.horizon_weeks)).join('')}</tbody>
        </table>
      </div>

      ${sc.overruns.length ? `<div class="card" style="margin-top:16px">
        <h3>Finishing late</h3>
        <p>Ranked by what the delay costs. Contract tier sets the price.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Activity</th><th>Contract</th><th class="num">Weeks late</th>
            <th class="num">Penalty</th><th>Why it matters</th></tr></thead>
          <tbody>${sc.overruns.map(o => `<tr>
            <td><span class="linkish" data-explain="${esc(o.activity_id)}">${esc(o.activity_id)}</span></td>
            <td>${esc(o.contract)}</td><td class="num">${o.weeks}</td>
            <td class="num">${o.cost}</td>
            <td style="color:var(--muted)">${esc(o.detail)}</td></tr>`).join('')}</tbody>
        </table></div>
      </div>` : ''}

      ${sc.hotspots.length ? `<div class="card">
        <h3>Where the network is tightest</h3>
        <p>These locations ran out of possessions. Co-sharing is what makes them work at all —
           one slot can hold up to four compatible activities.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Location</th><th class="num">Slots per week</th><th class="num">Weeks at capacity</th></tr></thead>
          <tbody>${sc.hotspots.map(h => `<tr><td>${esc(h.location_id)}</td>
            <td class="num">${h.capacity}</td><td class="num">${h.weeks_at_capacity}</td></tr>`).join('')}</tbody>
        </table></div>
      </div>` : ''}
    ` : '<div class="card"><p class="empty">Loading the schedule…</p></div>'}`;
}

function ganttRow(a, horizon) {
  const on = new Set(a.weeks), eclo = new Set(a.eclo_weeks);
  let cells = '';
  for (let w = 1; w <= horizon; w++) {
    let cls = 'wk';
    if (eclo.has(w)) cls += ' eclo';
    else if (on.has(w)) cls += (w > a.deadline_week ? ' late' : ' on');
    if (w % 5 === 0) cls += ' mark';
    cells += `<div class="${cls}" title="Week ${w}"></div>`;
  }
  const tier = a.contract_priority;
  return `<tr>
    <td><span class="linkish" data-explain="${esc(a.activity_id)}">${esc(a.activity_id)}</span></td>
    <td>${esc(a.contract)} <span class="pill ${tier === 1 ? 'p1' : tier === 2 ? 'p2' : ''}">P${tier}</span>
      ${a.nature === 'Live' ? '<span class="pill live">Live</span>' : ''}</td>
    <td class="num">${a.total_accesses}</td>
    <td class="bars"><div class="track">${cells}</div></td></tr>`;
}

function dashboardBlock() {
  const d = state.dashboard;
  if (!d) return '';
  return `<div class="dash">${d.sections.map(sec => `
    <div class="dash-card ${sec.status}">
      <h4><span class="led ${sec.status}"></span>${esc(sec.title)}</h4>
      ${sec.rows.map(r => `<div class="dash-row ${r.status}">
        <span class="txt">${esc(r.label)}<em>${esc(r.note)}</em></span>
        <span class="val">${esc(r.value)}</span>
      </div>`).join('')}
    </div>`).join('')}</div>
  <div class="legend">
    <span><i style="background:var(--green)"></i>valid, with room to spare</span>
    <span><i style="background:var(--orange)"></i>valid, but at capacity or running late</span>
    <span><i style="background:var(--red)"></i>a rule is broken</span>
  </div>`;
}

function calendarBlock() {
  const cal = state.calendar;
  if (!cal) return '<div class="card"><p class="empty">Loading the calendar…</p></div>';
  const month = w => new Date(w.starts + 'T00:00:00')
    .toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  return `<div class="card">
    <h3>The planning horizon, week by week</h3>
    <p>Each cell is one week. Click it to see exactly who is working, what fills up,
       and where the network is under strain.</p>
    <div class="cal">${cal.weeks.map(w => `
      <button data-week="${w.week}" class="${w.status}"
              aria-pressed="${state.selectedWeek === w.week}">
        <span class="wknum">Week ${w.week}</span>
        <span class="wkdate">${esc(month(w))}</span>
        <span class="wkcount">${w.activities ? `<b>${w.activities}</b> activit${w.activities === 1 ? 'y' : 'ies'}` : 'idle'}</span>
        <span class="wkflags">
          ${w.live.length ? '<span class="flag live">live</span>' : ''}
          ${w.locations_over ? `<span class="flag late">over</span>`
            : w.locations_full ? `<span class="flag full">full</span>` : ''}
          ${w.late ? '<span class="flag late">late</span>' : ''}
          ${w.eclo ? '<span class="flag eclo">eclo</span>' : ''}
        </span>
      </button>`).join('')}</div>
  </div>`;
}

function weekDetail(w) {
  const chip = s => `<span class="state ${s.status}">${esc(s.label)}</span>`;
  return `
    <p style="color:var(--muted); font-size:13.5px">
      ${esc(w.starts)} to ${esc(w.ends)} · ${w.working.length} activit${w.working.length === 1 ? 'y' : 'ies'} working</p>

    ${w.live_closures.length ? `<div class="note warn" style="margin-top:14px">
      <b>Live traction work this week</b>
      ${w.live_closures.map(c => `<p>${esc(c.activity_id)} (${esc(c.contract)}) cuts power —
        ${c.closes.length} further locations are closed all week, including the other line
        at the interchange.</p>`).join('')}</div>` : ''}

    <div class="wk-detail">
      <h4>Working this week</h4>
      ${w.working.length ? w.working.map(a => `<div class="wk-item">
        <span class="who">
          <span><span class="linkish" data-explain="${esc(a.activity_id)}">${esc(a.activity_id)}</span>
            · ${esc(a.contract)}
            <span class="pill ${a.contract_priority === 1 ? 'p1' : a.contract_priority === 2 ? 'p2' : ''}">P${a.contract_priority}</span>
            ${a.nature === 'Live' ? '<span class="pill live">Live</span>' : ''}
            ${a.eclo ? '<span class="pill">early closure</span>' : ''}</span>
          <span class="meta">${esc(a.from)} → ${esc(a.to)} · ${a.locations.length} locations
            · night ${esc(a.access_night)} of the contract's allowance</span>
        </span>
        <span class="state ${a.late ? 'red' : 'green'}">${a.late ? 'past target' : 'on time'}</span>
      </div>`).join('') : '<p class="empty">Nothing scheduled this week.</p>'}

      ${w.contract_access.length ? `<h4>Contract access used</h4>
        ${w.contract_access.map(c => `<div class="wk-item">
          <span class="who"><span>${esc(c.contract)} · ${esc(c.activity_type)}</span>
            <span class="meta">${c.used}/${c.granted} nights used</span></span>
          ${chip(c)}</div>`).join('')}` : ''}

      ${w.location_supply.length ? `<h4>Location supply</h4>
        ${w.location_supply.slice(0, 18).map(l => `<div class="wk-item">
          <span class="who"><span>${esc(l.location_id)}</span>
            <span class="meta">${l.used}/${l.capacity} slots · ${esc(l.activities.join(', '))}</span></span>
          ${chip(l)}</div>`).join('')}
        ${w.location_supply.length > 18
          ? `<p class="meta" style="padding-top:8px">…and ${w.location_supply.length - 18} more locations.</p>` : ''}` : ''}
    </div>`;
}

/* ────────────────────────────────────────────────────────────── step 4 */

function viewExport() {
  const done = Object.keys(SCENARIOS).filter(ready);
  return `
    <div class="page-head">
      <h1>Export the submission</h1>
      <p>Three files per scenario: the access nights, the locations occupied, and the
         completion summary.</p>
    </div>

    <div class="scenarios">
      ${Object.entries(SCENARIOS).map(([key, s]) => {
        const p = state.snap?.scenarios?.[key];
        const ok = p?.feasible;
        return `<div class="scenario" aria-pressed="false" style="cursor:default">
          <span class="tag">Scenario ${key}</span>
          <h3>${esc(s.name)}</h3>
          <p>${ok ? 'Valid — no rule breaches.'
                  : p ? 'Has rule breaches; fix before submitting.'
                      : 'Not planned yet.'}</p>
          <span class="score"><span>penalty score</span><b>${p?.score ?? '—'}</b></span>
          <button class="btn ${ok ? '' : 'secondary'}" data-download="${key}" ${ok ? '' : 'disabled'}
            style="margin-top:10px">Download ${key}</button>
        </div>`;
      }).join('')}
    </div>

    <div class="card" style="margin-top:18px">
      <h3>Everything at once</h3>
      <p>All valid scenarios, their validation reports, and the decision log, in one zip.</p>
      <div class="row">
        <button class="btn" id="btn-download-all" ${done.length ? '' : 'disabled'}>
          Download full submission${done.length ? ` (${done.length} scenario${done.length > 1 ? 's' : ''})` : ''}
        </button>
        <button class="btn quiet" id="btn-audit">View the decision log</button>
      </div>
    </div>

    ${done.length < 3 ? `<div class="note warn">
      <b>${3 - done.length} scenario(s) still to plan</b>
      <p>A complete submission covers A, B and C. Go back to step 2 to plan the rest.</p>
    </div>` : `<div class="note good"><b>All three scenarios are ready</b>
      <p>Download the full submission and you are done.</p></div>`}`;
}

/* ────────────────────────────────────────────────────────────── render */

const VIEWS = { instance: viewInstance, plan: viewPlan, explore: viewExplore, export: viewExport };
const TITLES = { instance: 'Instance', plan: 'Plan', explore: 'Explore', export: 'Export' };

function renderChrome() {
  const s = state.snap;
  const info = s?.instance;
  $('#chip-instance').textContent = info
    ? `${info.activities} activities · ${info.contracts} contracts · ${info.horizon_weeks} weeks`
    : 'No instance loaded';
  $('#crumb-step').textContent = TITLES[state.step];
  $('#nav-instance').textContent = info ? `${info.activities} activities` : 'not loaded';
  const planned = Object.keys(SCENARIOS).filter(k => s?.scenarios?.[k]);
  $('#nav-plan').textContent = planned.length ? `${planned.length} of 3 planned` : 'no schedule yet';
  $('#nav-explore').textContent = plan() ? 'ready' : 'run a scenario first';
  const valid = Object.keys(SCENARIOS).filter(ready);
  $('#nav-export').textContent = valid.length ? `${valid.length} ready` : 'nothing ready';

  [...$('#steps').children].forEach(b => {
    b.classList.toggle('active', b.dataset.step === state.step);
    const done = { instance: !!info, plan: planned.length > 0,
                   explore: !!plan(), export: valid.length === 3 }[b.dataset.step];
    b.classList.toggle('done', !!done && b.dataset.step !== state.step);
  });

  const live = s?.gemini;
  $('#engine-dot').classList.toggle('live', !!live);
  $('#engine-name').textContent = live ? 'Solver + language model' : 'Constraint solver';
  $('#engine-detail').textContent = live ? 'CP-SAT · Gemini available' : 'OR-Tools CP-SAT';
}

function render() {
  renderChrome();
  $('#view').innerHTML = (VIEWS[state.step] || viewInstance)();
}

async function refresh() { state.snap = await api('state'); }

async function loadSchedule() {
  const s = state.scenario;
  try {
    const [schedule, dashboard, calendar] = await Promise.all([
      api(`schedule?scenario=${s}`), api(`dashboard?scenario=${s}`), api(`calendar?scenario=${s}`)
    ]);
    state.schedule = schedule; state.dashboard = dashboard; state.calendar = calendar;
  } catch {
    state.schedule = state.dashboard = state.calendar = null;
  }
  state.selectedWeek = null;
}

async function openWeek(week) {
  try {
    const w = await api(`week?scenario=${state.scenario}&week=${week}`);
    state.selectedWeek = week;
    render();
    openDrawer(`Week ${week}`, weekDetail(w));
  } catch (err) { toast(err.message, true); }
}

function goto(step) { state.step = step; window.scrollTo(0, 0); render(); }

/* ────────────────────────────────────────────────────────────── actions */

async function solve() {
  if (state.busy) return;
  state.busy = true;
  const btn = $('#btn-solve');
  state.showNegotiation = !!$('#use-agents')?.checked;
  state.seconds = Number($('#seconds')?.value) || 20;
  const agents = state.showNegotiation;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${agents ? 'Negotiating…' : 'Planning…'}`;
  }
  try {
    const result = await api('solve',
      { scenario: state.scenario, seconds: state.seconds, use_agents: agents });
    (result.refused || []).forEach(r => toast('Override refused: ' + r.why, true));
    await refresh();
    await loadSchedule();
    state.negotiation = null;
    render();
    toast(result.feasible
      ? `Scenario ${state.scenario} planned — penalty score ${result.score}.`
      : `Scenario ${state.scenario}: ${result.hard_violations.length} rule breach(es).`,
      !result.feasible);
  } catch (err) {
    toast(err.message, true);
    render();
  }
  state.busy = false;
}

async function explain(activityId) {
  try {
    const e = await api(`explain?scenario=${state.scenario}&activity=${encodeURIComponent(activityId)}`);
    const blockers = (e.blockers || []).filter(b => b.reason !== 'available');
    openDrawer(`${e.activity_id} — why this week`, `
      <div class="note good"><b>${esc(e.headline)}</b>
        <p>Contract ${esc(e.contract)}, priority ${e.contract_priority}.
           ${e.total_accesses} nights of work. Earliest allowed start is week
           ${e.planned_start_week}; the contract target is week ${e.deadline_week}.</p></div>
      <div class="card card-tight">
        <h4>Scheduled weeks</h4>
        <p style="color:var(--muted); font-size:13.5px">
          ${(e.scheduled_weeks || []).map(w => 'week ' + w).join(', ')}</p>
      </div>
      ${e.overrun ? `<div class="note bad"><b>Finishes late</b><p>${esc(e.overrun.detail)}</p></div>` : ''}
      ${blockers.length ? `<h4 style="margin:20px 0 10px">Why not earlier</h4>
        ${blockers.map(b => `<div class="note warn"><b>Week ${b.week}</b><p>${esc(b.detail)}</p></div>`).join('')}` : ''}
      ${(e.gaps || []).length ? `<h4 style="margin:20px 0 10px">Why the gaps</h4>
        ${e.gaps.map(b => `<div class="note warn"><b>Week ${b.week}</b><p>${esc(b.detail)}</p></div>`).join('')}` : ''}
      <h4 style="margin:20px 0 10px">Worksite</h4>
      <p style="color:var(--muted); font-size:13px; line-height:1.7">
        ${(e.locations || []).map(esc).join('<br>')}</p>`);
  } catch (err) { toast(err.message, true); }
}

async function showNegotiation() {
  try {
    const n = await api(`negotiation?scenario=${state.scenario}`);
    if (!n.ran) return openDrawer('The negotiation', `<div class="note info">
      <b>No negotiation on this plan</b><p>${esc(n.why)}</p></div>`);
    openDrawer('How the agents argued', `
      <p style="color:var(--muted); font-size:13.5px; margin-bottom:16px">
        One agent per contract, each holding its own priority and deadline. The planner asks
        whoever is hurting what they can give up, tests one bundle of concessions, and keeps it
        only if an independent validator scores it better. Proposals came from the
        <strong>${esc(n.negotiator)}</strong>.</p>
      ${n.ledger.map(r => `<div class="note ${r.kept ? 'good' : ''}">
        <b>Round ${r.round} · ${esc(r.bundle)} — ${r.kept ? 'accepted' : 'discarded'}</b>
        <p>${esc(r.why)}</p>
        ${(r.concessions || []).length ? `<p style="margin-top:6px">${r.concessions.map(c =>
          `<span class="pill">${esc(c.lever)} · costs ${c.est_cost}</span>`).join(' ')}</p>
          ${r.concessions.filter(c => c.rationale).map(c =>
            `<p style="margin-top:5px; font-size:13px">“${esc(c.rationale)}”</p>`).join('')}` : ''}
      </div>`).join('')}
      <h4 style="margin:22px 0 10px">Messages exchanged</h4>
      ${n.messages.map(m => `<div class="note"><b>${esc(m.sender)} → ${esc(m.recipient)}</b>
        <p>${esc(m.summary)}</p></div>`).join('')}`);
  } catch (err) { toast(err.message, true); }
}

function overrideDialog() {
  modal('Add an override', `
    <p style="color:var(--muted); font-size:13.5px; margin-bottom:14px">
      An override is a hard rule, not an edit. The whole plan is re-solved around it, so it
      stays valid. If it would break a safety rule it is refused and you are told which one.</p>
    <label class="field"><span>What should happen</span>
      <select id="ov-kind">
        <option value="pin">This activity must run in a particular week</option>
        <option value="forbid">This activity must not run in a particular week</option>
        <option value="no_eclo">This activity may not use an early closure</option>
      </select></label>
    <label class="field"><span>Activity</span>
      <input type="text" id="ov-act" placeholder="A040"></label>
    <label class="field"><span>Week</span>
      <input type="number" id="ov-week" min="1" placeholder="14"></label>
    <label class="field"><span>Reason — this goes in the decision log</span>
      <input type="text" id="ov-why" placeholder="Contractor crew only available that week"></label>`,
    'Add override', async () => {
      await api('lock', {
        kind: $('#ov-kind').value,
        activity: $('#ov-act').value.trim().toUpperCase(),
        week: Number($('#ov-week').value) || null,
        reason: $('#ov-why').value
      });
      await refresh(); render();
      toast('Override added. Re-plan to apply it.');
    });
}

async function showAudit() {
  const log = state.snap?.audit || [];
  openDrawer('Decision log', log.length
    ? log.slice().reverse().map(e => `<div class="note">
        <b>${esc(e.action.replace(/_/g, ' '))}</b>
        <p>${esc(e.detail)}<br><span style="font-size:12px">${esc(e.at)}</span></p></div>`).join('')
    : '<p class="empty">Nothing recorded yet.</p>');
}

/* ────────────────────────────────────────────────────────────── wiring */

$('#steps').addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-step]');
  if (!b) return;
  goto(b.dataset.step);
  // Explore renders from the schedule, which is fetched separately.
  if (b.dataset.step === 'explore' && !state.schedule && plan()) {
    await loadSchedule();
    render();
  }
});

$('#drawer-body').addEventListener('click', ev => {
  const link = ev.target.closest('[data-explain]');
  if (link) explain(link.dataset.explain);
});
$('#drawer-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeDrawer(); });

$('#btn-help').addEventListener('click', () => openDrawer('About this tool', `
  <p style="font-size:14px; line-height:1.6">Track access only exists between the last train and
  the first. This tool decides which contracted work gets which night, on which stretch of track,
  for a two-line network over a full planning horizon.</p>
  <h4 style="margin:20px 0 8px">How it decides</h4>
  <p style="color:var(--muted); font-size:13.5px; line-height:1.6">A constraint solver places every
  activity so that no safety rule is broken — exclusion zones, live traction closures, how many
  possessions a location can hold, how many nights a contract is granted. Among the valid
  schedules it picks the one with the lowest penalty, where delay to a high-priority contract
  costs a hundred times more than delay to a low-priority one.</p>
  <h4 style="margin:20px 0 8px">How it is checked</h4>
  <p style="color:var(--muted); font-size:13.5px; line-height:1.6">The schedule is written out as
  the three submission files, then read back and re-checked by a separate validator that shares no
  code with the solver. What you see is the second opinion, not the solver marking its own work.</p>`));

$('#view').addEventListener('click', async ev => {
  const t = ev.target;
  const scenario = t.closest('[data-scenario]');
  if (scenario) {
    state.scenario = scenario.dataset.scenario;
    state.schedule = null;
    render();
    await loadSchedule();
    render();
    return;
  }
  const explainEl = t.closest('[data-explain]');
  if (explainEl) return explain(explainEl.dataset.explain);

  const weekEl = t.closest('[data-week]');
  if (weekEl) return openWeek(Number(weekEl.dataset.week));

  const unlock = t.closest('[data-unlock]');
  if (unlock) {
    await api('unlock', { id: unlock.dataset.unlock });
    await refresh(); render(); toast('Override removed.');
    return;
  }
  const dl = t.closest('[data-download]');
  if (dl) { location.href = `/api/export?scenario=${dl.dataset.download}`; return; }

  switch (t.id) {
    case 'btn-pick': $('#files').click(); break;
    case 'btn-reset':
      await api('reset', {});
      state.schedule = state.precheck = null;
      await refresh(); render(); toast('Packaged instance loaded.');
      break;
    case 'btn-precheck':
      state.precheck = await api(`precheck?scenario=${state.scenario}`);
      render(); toast(state.precheck.summary);
      break;
    case 'btn-to-plan': goto('plan'); break;
    case 'btn-solve': solve(); break;
    case 'btn-to-explore':
      goto('explore');
      if (!state.schedule) { await loadSchedule(); render(); }
      break;
    case 'btn-negotiation': showNegotiation(); break;
    case 'btn-override': overrideDialog(); break;
    case 'btn-audit': showAudit(); break;
    case 'btn-download-all': location.href = '/api/export_all'; break;
    case 'btn-clear-disrupt':
      await api('clear_disruptions', {});
      await refresh(); render(); toast('Capacity changes cleared.');
      break;
    case 'btn-disrupt': {
      const text = $('#disrupt').value.trim();
      if (!text) return toast('Describe what happened first.', true);
      const out = await api('disrupt', { text });
      if (!out.understood) return toast(out.problems[0], true);
      await refresh();
      toast(out.echo + ' Re-planning…');
      await solve();
      break;
    }
  }
});

/* file handling — click and drag both land here */
document.addEventListener('change', async ev => {
  if (ev.target.id !== 'files' || !ev.target.files.length) return;
  await upload([...ev.target.files]);
});

async function upload(fileList) {
  const files = {};
  for (const file of fileList) files[file.name] = await file.text();
  try {
    const out = await api('upload', { files, name: `uploaded · ${fileList.length} files` });
    (out.warnings || []).forEach(w => toast(w));
    state.uploadError = null;
    state.schedule = state.precheck = null;
    await refresh(); render();
    const i = out.instance;
    toast(`Loaded ${i.activities} activities, ${i.contracts} contracts, ${i.horizon_weeks} weeks.`);
  } catch (err) {
    // A rejected upload leaves the old instance loaded, which looks like nothing
    // happened. Say so on the page rather than in a toast that disappears.
    state.uploadError = err.message;
    render();
    toast('Upload rejected — see the message on the page.', true);
  }
}

document.addEventListener('dragover', ev => {
  const drop = ev.target.closest?.('#drop');
  if (drop) { ev.preventDefault(); drop.classList.add('over'); }
});
document.addEventListener('dragleave', ev => {
  const drop = ev.target.closest?.('#drop');
  if (drop) drop.classList.remove('over');
});
document.addEventListener('drop', async ev => {
  const drop = ev.target.closest?.('#drop');
  if (!drop) return;
  ev.preventDefault();
  drop.classList.remove('over');
  const files = [...ev.dataTransfer.files].filter(f => f.name.toLowerCase().endsWith('.csv'));
  if (!files.length) return toast('Those did not look like CSV files.', true);
  await upload(files);
});

/* ────────────────────────────────────────────────────────────── start */

(async function start() {
  try {
    await refresh();
    render();
  } catch (err) {
    $('#view').innerHTML = `<div class="card"><h3>Cannot reach the planner</h3>
      <p style="color:var(--muted)">${esc(err.message)}</p></div>`;
  }
})();
