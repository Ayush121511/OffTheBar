"""
Build evals/reports/dashboard.html from every report in evals/reports/.

    python evals/dashboard.py          # rebuild (run_evals.py does this after every run)

The page is self-contained (no network): the run data is embedded as JSON and
rendered with plain JS + SVG. Old reports are re-scored with evals/scoring.py,
so every run on the trend chart is scored the same way.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Rupees per US dollar for the INR cost figures (~95 in late October 2026).
# Update it when the rupee moves.
USD_TO_INR = 95


# Column headers in the cases table, where the full labels don't fit.
SHORT_LABELS = {"understanding": "Underst.", "retrieval": "Retrieval", "routing": "Routing", "accuracy": "Accuracy",
                "quality": "Quality", "safety": "Safety", "performance": "Perf."}


def _run_payload(path: Path, domain_of: dict) -> dict:
    report = json.loads(path.read_text())
    results = report.get("results", [])
    calls = report.get("calls", [])
    return {
        "file": path.name,
        "ran_at": report.get("ran_at", ""),
        "as_of": report.get("as_of"),
        "answers": bool(report.get("answers")),
        "filtered": bool(report.get("filtered")),
        "cost": report.get("cost_usd", 0.0),
        "calls": len(calls),
        "free": sum(1 for c in calls if c.get("tier") == "free"),
        "paid": sum(1 for c in calls if c.get("tier") == "paid"),
        "duration": report.get("duration_s"),
        "scores": scoring.score_run(results, domain_of),
        "cases": [
            {
                "id": r["id"],
                "domain": r.get("domain") or domain_of.get(r["id"], "other"),
                "question": r.get("question", ""),
                "info": r.get("info", {}),
                "cost": r.get("cost", 0.0),
                "checks": [dict(c, section=scoring.section_for(c), score=scoring.check_score(c)) for c in r["checks"]],
            }
            for r in results
        ],
    }


def build(reports_dir: Path = REPO / "evals" / "reports", cases_path: Path = REPO / "evals" / "cases.json") -> Path:
    spec = json.loads(cases_path.read_text())
    domain_of = {c["id"]: c["domain"] for c in spec["cases"]}
    runs = [_run_payload(p, domain_of) for p in sorted(reports_dir.glob("*.json"))]
    data = {
        "sections": [{"id": s[0], "label": s[1], "short": SHORT_LABELS[s[0]], "weight": s[2], "about": s[3]}
                     for s in scoring.SECTIONS],
        "domains": spec["domains"],
        "usd_to_inr": USD_TO_INR,
        "runs": runs,
    }
    page = reports_dir / "dashboard.html"
    page.write_text(TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    return page


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Off The Bar Evals</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834;
  --good: #0ca30c; --good-text: #006300; --critical: #d03b3b; --track: #e8eef8;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926;
    --good: #0ca30c; --good-text: #0ca30c; --critical: #d03b3b; --track: #1e2b3d;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926;
  --good: #0ca30c; --good-text: #0ca30c; --critical: #d03b3b; --track: #1e2b3d;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 16px; margin-bottom: 16px; }
h1 { font-size: 20px; margin: 0; font-weight: 650; }
h2 { font-size: 14px; margin: 0; font-weight: 650; }
.sub { color: var(--ink-2); font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 16px; }
select, button { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--border);
                 border-radius: 8px; padding: 6px 10px; }
select { max-width: 100%; }
button { cursor: pointer; }
button:hover, select:hover { border-color: var(--axis); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 12px; }
.card-head .sub { font-size: 12px; }
.grid { display: grid; gap: 16px; }
.top { grid-template-columns: minmax(200px, 1.1fr) repeat(5, minmax(130px, 1fr)); }
.two { grid-template-columns: 1fr 1fr; }
@media (max-width: 1100px) { .top { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 900px) { .top { grid-template-columns: 1fr 1fr; } .two { grid-template-columns: 1fr; } }
@media (max-width: 520px) { .top { grid-template-columns: 1fr; } }
.hero-value { font-size: 56px; font-weight: 650; line-height: 1; margin: 6px 0 8px; }
.tile-label { color: var(--ink-2); font-size: 13px; }
.tile-value { font-size: 28px; font-weight: 600; margin-top: 6px; white-space: nowrap; }
.delta-up { color: var(--good-text); } .delta-down { color: var(--critical); }
.note { color: var(--muted); font-size: 12px; margin-top: 4px; }
.bars .row { display: grid; grid-template-columns: 140px 1fr 60px; align-items: center; gap: 10px; padding: 5px 0; }
.bars .label { color: var(--ink-2); font-size: 13px; }
.bars .track { position: relative; height: 12px; }
.bars .fill { position: absolute; left: 0; top: 0; bottom: 0; background: var(--series-1); border-radius: 0 4px 4px 0; }
.bars .gridline { position: absolute; top: -3px; bottom: -3px; width: 1px; background: var(--grid); }
.bars .value { font-variant-numeric: tabular-nums; text-align: right; font-size: 13px; }
.bars .none { color: var(--muted); font-size: 12px; }
.heat { width: 100%; border-collapse: separate; border-spacing: 2px; }
.heat th { font-weight: 500; color: var(--ink-2); font-size: 12px; text-align: left; padding: 4px 6px; }
.heat th.col { text-align: center; }
.heat td { text-align: center; border-radius: 4px; padding: 8px 4px; font-variant-numeric: tabular-nums; font-size: 12px; min-width: 52px; }
.heat td.empty { color: var(--muted); background: transparent; }
.heat td:focus { outline: 2px solid var(--ink); outline-offset: 1px; }
.scale { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; margin-top: 10px; }
.scale span.ramp { display: inline-block; width: 140px; height: 8px; border-radius: 2px; }
svg text { fill: var(--muted); font-size: 11px; }
.legend { display: flex; gap: 14px; color: var(--ink-2); font-size: 12px; }
.legend i { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; }
.tooltip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--border);
           border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.12); display: none; z-index: 10; }
.tooltip strong { font-size: 14px; display: block; }
table.data { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data th { text-align: left; font-weight: 500; color: var(--ink-2); border-bottom: 1px solid var(--grid); padding: 6px 8px; }
table.data td { border-bottom: 1px solid var(--grid); padding: 6px 8px; vertical-align: top; }
table.data td.num { font-variant-numeric: tabular-nums; text-align: right; }
.status { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; }
.status.pass { color: var(--good-text); } .status.fail { color: var(--critical); }
tr.case-row { cursor: pointer; }
tr.case-row:hover td { background: color-mix(in srgb, var(--ink) 3%, transparent); }
tr.detail td { background: color-mix(in srgb, var(--ink) 2%, transparent); }
.detail-grid { display: grid; grid-template-columns: 120px 1fr; gap: 4px 12px; font-size: 12.5px; }
.detail-grid .k { color: var(--muted); }
pre.answer { white-space: pre-wrap; font: inherit; margin: 0; max-height: 320px; overflow: auto; }
.chip { font-variant-numeric: tabular-nums; font-size: 12px; }
.hidden { display: none !important; }
.empty-state { color: var(--muted); padding: 24px 0; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Off The Bar · pipeline evals</h1>
      <div class="sub" id="runMeta"></div>
    </div>
    <button id="theme" title="Switch light / dark">Theme</button>
  </header>

  <div class="filters">
    <label class="sub" for="run">Run</label> <select id="run"></select>
    <label class="sub" for="domain">Domain</label> <select id="domain"></select>
  </div>

  <div class="grid top">
    <div class="card">
      <div class="tile-label">Overall score</div>
      <div class="hero-value" id="overall">-</div>
      <div class="sub" id="overallDelta"></div>
      <div class="note" id="overallNote"></div>
    </div>
    <div class="card"><div class="tile-label">Cases passed</div><div class="tile-value" id="cases">-</div><div class="note" id="casesNote"></div></div>
    <div class="card"><div class="tile-label">Checks passed</div><div class="tile-value" id="checks">-</div><div class="note" id="checksNote"></div></div>
    <div class="card"><div class="tile-label">Run cost</div><div class="tile-value" id="cost">-</div><div class="note" id="costNote"></div></div>
    <div class="card" title="Each eval answer also pays for 3 flash-lite calls a real chat doesn't make (the routing checks and the rubric judge), so a real question costs slightly less.">
      <div class="tile-label">Cost per question</div><div class="tile-value" id="perQuestion">-</div><div class="note" id="perQuestionNote"></div></div>
    <div class="card"><div class="tile-label">Median time to first words</div><div class="tile-value" id="latency">-</div><div class="note" id="latencyNote"></div></div>
  </div>

  <div class="grid two" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><h2>Score by section</h2><button data-table="sections">Table</button></div>
      <div id="sectionBars" class="bars"></div>
      <table class="data hidden" id="sectionsTable"></table>
    </div>
    <div class="card">
      <div class="card-head"><h2>Score trend</h2><button data-table="trend">Table</button></div>
      <div class="legend"><span><i style="background:var(--series-1)"></i>With answers</span><span><i style="background:var(--series-2)"></i>Routing only</span></div>
      <div id="trend"></div>
      <table class="data hidden" id="trendTable"></table>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>Domain × section</h2><span class="sub">score 0-100 · hover for checks</span></div>
    <div style="overflow-x:auto"><table class="heat" id="heat"></table></div>
    <div class="scale"><span>≤50</span><span class="ramp" id="ramp"></span><span>100</span><span style="margin-left:8px">- not measured</span></div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>Failed checks</h2><span class="sub" id="failCount"></span></div>
    <div style="overflow-x:auto"><table class="data" id="failures"></table></div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>Cases</h2><span class="sub">click a row for the question, query, articles and answer</span></div>
    <div style="overflow-x:auto"><table class="data" id="casesTable"></table></div>
  </div>
</div>
<div class="tooltip" id="tip"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const SECTIONS = DATA.sections;
const RAMP = ['#cde2fb','#b7d3f6','#9ec5f4','#86b6ef','#6da7ec','#5598e7','#3987e5','#2a78d6','#256abf','#1c5cab','#184f95','#104281','#0d366b'];
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v; else if (k === 'style') n.style.cssText = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const kid of kids) n.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ''));
  return n;
};
const pct = (v) => v == null ? '-' : (v * 100).toFixed(1);
const fmtDate = (s) => s ? s.replace('T', ' ').slice(0, 16) : '';
const inr = (usd) => '₹' + (usd * DATA.usd_to_inr).toFixed(2);
const usd = (v) => '$' + v.toFixed(4);
const median = (sorted) => sorted.length % 2 ? sorted[(sorted.length - 1) / 2]
  : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
const isDark = () => document.documentElement.dataset.theme === 'dark' ||
  (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Scores cluster in the 90s, so the colour scale spans 50-100 (anything lower
// takes the lightest step) - on 0-100, a 92 and a 100 looked identical.
const RAMP_FLOOR = 0.5;
function rampColor(score) {
  const ramp = isDark() ? [...RAMP].reverse() : RAMP;   // low scores recede toward the surface in both modes
  const t = Math.min(1, Math.max(0, (score - RAMP_FLOOR) / (1 - RAMP_FLOOR)));
  return ramp[Math.round(t * (ramp.length - 1))];
}
function inkOn(hex) {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map(c => c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.3 ? '#0b0b0b' : '#ffffff';
}

const tip = $('tip');
function showTip(evt, value, label, extra) {
  tip.replaceChildren(el('strong', {}, value), el('div', {}, label), extra ? el('div', { style: 'color:var(--muted)' }, extra) : '');
  tip.style.display = 'block';
  const x = evt.clientX ?? (evt.target.getBoundingClientRect().right);
  const y = evt.clientY ?? (evt.target.getBoundingClientRect().top);
  tip.style.left = Math.min(x + 14, innerWidth - tip.offsetWidth - 8) + 'px';
  tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + 'px';
}
const hideTip = () => { tip.style.display = 'none'; };

// ---- run + domain selection ----
const runs = DATA.runs;
const runSel = $('run'), domSel = $('domain');
runs.forEach((r, i) => runSel.append(el('option', { value: i },
  `${fmtDate(r.ran_at)} · ${r.answers ? 'with answers' : 'routing only'} · ${r.cases.length} cases${r.filtered ? ' (filtered)' : ''}`)));
domSel.append(el('option', { value: '' }, 'All domains'));
Object.keys(DATA.domains).forEach(d => domSel.append(el('option', { value: d }, d)));
// Open on the latest complete run - a partial rerun (--only / --domain) isn't the headline.
const latestFull = runs.map((r, i) => [r, i]).filter(([r]) => !r.filtered).pop();
runSel.value = String(latestFull ? latestFull[1] : runs.length - 1);

function current() {
  const run = runs[+runSel.value];
  const dom = domSel.value;
  const scores = dom ? (run.scores.domains[dom] || { overall: null, sections: {}, cases: 0, cases_passed: 0, checks: 0, checks_passed: 0 }) : run.scores.all;
  const cases = run.cases.filter(c => !dom || c.domain === dom);
  return { run, dom, scores, cases };
}

function previousComparable(run) {
  const idx = runs.indexOf(run);
  for (let i = idx - 1; i >= 0; i--) if (runs[i].answers === run.answers && !runs[i].filtered) return runs[i];
  return null;
}

// ---- tiles ----
function renderTiles({ run, dom, scores, cases }) {
  $('runMeta').textContent = `${fmtDate(run.ran_at)} · cases as of ${run.as_of || '?'} · ${run.file}`;
  $('overall').textContent = pct(scores.overall);
  const prev = previousComparable(run);
  const prevScore = prev && (dom ? prev.scores.domains[dom]?.overall : prev.scores.all.overall);
  const delta = $('overallDelta');
  delta.className = 'sub'; delta.textContent = '';
  if (run.filtered) {
    delta.textContent = 'Partial rerun - not compared';
  } else if (prevScore != null && scores.overall != null) {
    const d = (scores.overall - prevScore) * 100;
    delta.className = 'sub ' + (d > 0.05 ? 'delta-up' : d < -0.05 ? 'delta-down' : '');
    delta.textContent = `${d >= 0 ? '+' : ''}${d.toFixed(1)} vs previous ${run.answers ? 'answer' : 'routing'} run`;
  }
  $('overallNote').textContent = run.answers ? 'Weighted across all seven sections'
    : 'Routing only: understanding, retrieval and routing sections';
  $('cases').textContent = `${scores.cases_passed} / ${scores.cases}`;
  $('casesNote').textContent = scores.cases ? `${Math.round(100 * scores.cases_passed / scores.cases)}% of cases pass every check` : '';
  $('checks').textContent = `${scores.checks_passed} / ${scores.checks}`;
  $('checksNote').textContent = dom ? `in ${dom}` : 'all sections';
  const cost = dom ? cases.reduce((a, c) => a + (c.cost || 0), 0) : run.cost;
  $('cost').textContent = inr(cost);
  $('costNote').textContent = `${usd(cost)} at ₹${DATA.usd_to_inr}/$ · ` +
    (dom ? 'cases in this domain' : `${run.calls} Gemini calls · ${run.free} free key, ${run.paid} paid`);
  // Per question: the answered cases when there are any, else the routing checks alone.
  const answered = cases.filter(c => c.info?.answer);
  const costs = (answered.length ? answered : cases).map(c => c.cost || 0).sort((a, b) => a - b);
  $('perQuestion').textContent = costs.length ? inr(median(costs)) : '-';
  $('perQuestionNote').textContent = !costs.length ? ''
    : `median of ${costs.length} ${answered.length ? 'answered questions' : 'routing checks, no answers'} · ${inr(costs[0])}-${inr(costs[costs.length - 1])}`;
  const times = cases.map(c => c.info?.answer?.first_words_s).filter(v => v != null).sort((a, b) => a - b);
  $('latency').textContent = times.length ? median(times).toFixed(1) + 's' : '-';
  $('latencyNote').textContent = times.length
    ? `${times.length} of ${cases.length} questions answered · ${times[0].toFixed(1)}-${times[times.length - 1].toFixed(1)}s` : 'run with --answers';
}

// ---- section bars ----
function renderSections({ scores }) {
  const box = $('sectionBars'); box.replaceChildren();
  const table = $('sectionsTable');
  table.replaceChildren(el('tr', {}, el('th', {}, 'Section'), el('th', {}, 'Weight'), el('th', {}, 'Checks'), el('th', {}, 'Score')));
  for (const s of SECTIONS) {
    const v = scores.sections[s.id];
    const track = el('div', { class: 'track' });
    [0.25, 0.5, 0.75, 1].forEach(t => track.append(el('div', { class: 'gridline', style: `left:${t * 100}%` })));
    if (v) {
      const fill = el('div', { class: 'fill', style: `width:${Math.max(v.score * 100, 0.5)}%`, tabindex: 0 });
      const show = (e) => showTip(e, pct(v.score), s.label, `${v.checks} checks · weight ${s.weight} · ${s.about}`);
      fill.addEventListener('pointermove', show); fill.addEventListener('focus', show);
      fill.addEventListener('pointerleave', hideTip); fill.addEventListener('blur', hideTip);
      track.append(fill);
    }
    box.append(el('div', { class: 'row' }, el('div', { class: 'label', title: s.about }, s.label), track,
      v ? el('div', { class: 'value' }, pct(v.score)) : el('div', { class: 'value none' }, 'not run')));
    table.append(el('tr', {}, el('td', {}, s.label), el('td', { class: 'num' }, s.weight),
      el('td', { class: 'num' }, v ? v.checks : '-'), el('td', { class: 'num' }, v ? pct(v.score) : 'not run')));
  }
}

// ---- trend ----
function renderTrend({ dom }) {
  const box = $('trend'); box.replaceChildren();
  const pts = runs.map((r, i) => ({ r, i, v: dom ? r.scores.domains[dom]?.overall : r.scores.all.overall }))
    .filter(p => !p.r.filtered && p.v != null);
  const table = $('trendTable');
  table.replaceChildren(el('tr', {}, el('th', {}, 'Run'), el('th', {}, 'Kind'), el('th', {}, 'Cases'), el('th', {}, 'Score')));
  pts.forEach(p => table.append(el('tr', {}, el('td', {}, fmtDate(p.r.ran_at)), el('td', {}, p.r.answers ? 'with answers' : 'routing only'),
    el('td', { class: 'num' }, p.r.cases.length), el('td', { class: 'num' }, pct(p.v)))));
  if (!pts.length) { box.append(el('div', { class: 'empty-state' }, 'No complete runs yet')); return; }

  const W = Math.max(320, box.clientWidth || 520), H = 220, m = { l: 34, r: 16, t: 14, b: 28 };
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${dom || 'Overall'} score per run`);
  const s = (tag, attrs) => { const n = document.createElementNS(ns, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); svg.append(n); return n; };
  const lo = Math.max(0, Math.floor(Math.min(...pts.map(p => p.v)) * 10) / 10 - 0.1);
  const x = (k) => m.l + (pts.length === 1 ? (W - m.l - m.r) / 2 : k * (W - m.l - m.r) / (pts.length - 1));
  const y = (v) => m.t + (1 - (v - lo) / (1 - lo)) * (H - m.t - m.b);
  for (let t = lo; t <= 1.0001; t += (1 - lo) / 4) {
    s('line', { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: css('--grid'), 'stroke-width': 1 });
    const label = s('text', { x: m.l - 6, y: y(t) + 4, 'text-anchor': 'end' }); label.textContent = Math.round(t * 100);
  }
  s('line', { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: css('--axis'), 'stroke-width': 1 });
  [0, pts.length - 1].filter((k, i, a) => a.indexOf(k) === i).forEach(k => {
    const t = s('text', { x: x(k), y: H - 8, 'text-anchor': k === 0 && pts.length > 1 ? 'start' : 'end' });
    t.textContent = fmtDate(pts[k].r.ran_at).slice(5);
  });
  for (const kind of [true, false]) {
    const series = pts.map((p, k) => ({ ...p, k })).filter(p => p.r.answers === kind);
    const color = css(kind ? '--series-1' : '--series-2');
    if (series.length > 1) s('polyline', { points: series.map(p => `${x(p.k)},${y(p.v)}`).join(' '), fill: 'none', stroke: color,
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
    series.forEach(p => s('circle', { cx: x(p.k), cy: y(p.v), r: 4.5, fill: color, stroke: css('--surface'), 'stroke-width': 2 }));
  }
  const cross = s('line', { y1: m.t, y2: H - m.b, stroke: css('--axis'), 'stroke-width': 1, visibility: 'hidden' });
  const hit = s('rect', { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: 'transparent' });
  hit.addEventListener('pointermove', (e) => {
    const bx = svg.getBoundingClientRect().left;
    let best = 0; pts.forEach((p, k) => { if (Math.abs(x(k) - (e.clientX - bx)) < Math.abs(x(best) - (e.clientX - bx))) best = k; });
    cross.setAttribute('x1', x(best)); cross.setAttribute('x2', x(best)); cross.setAttribute('visibility', 'visible');
    const p = pts[best];
    showTip(e, pct(p.v), `${p.r.answers ? 'With answers' : 'Routing only'} · ${p.r.cases.length} cases`, fmtDate(p.r.ran_at));
  });
  hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
  hit.addEventListener('click', (e) => {
    const bx = svg.getBoundingClientRect().left;
    let best = 0; pts.forEach((p, k) => { if (Math.abs(x(k) - (e.clientX - bx)) < Math.abs(x(best) - (e.clientX - bx))) best = k; });
    runSel.value = String(pts[best].i); render();
  });
  box.append(svg);
}

// ---- heatmap ----
function renderHeat({ run, dom }) {
  const table = $('heat'); table.replaceChildren();
  table.append(el('tr', {}, el('th', {}, ''), ...SECTIONS.map(s => el('th', { class: 'col', title: s.about }, s.label))));
  const domains = Object.keys(DATA.domains).filter(d => run.scores.domains[d] && (!dom || d === dom));
  for (const d of domains) {
    const row = el('tr', {}, el('th', { title: DATA.domains[d] }, d));
    for (const s of SECTIONS) {
      const v = run.scores.domains[d].sections[s.id];
      if (!v) { row.append(el('td', { class: 'empty' }, '-')); continue; }
      const bg = rampColor(v.score);
      const td = el('td', { tabindex: 0, style: `background:${bg};color:${inkOn(bg)}` }, Math.round(v.score * 100));
      const show = (e) => showTip(e, pct(v.score), `${d} · ${s.label}`, `${v.checks} checks`);
      td.addEventListener('pointermove', show); td.addEventListener('focus', show);
      td.addEventListener('pointerleave', hideTip); td.addEventListener('blur', hideTip);
      row.append(td);
    }
    table.append(row);
  }
  const ramp = isDark() ? [...RAMP].reverse() : RAMP;
  $('ramp').style.background = `linear-gradient(90deg, ${ramp.join(',')})`;
}

// ---- failures ----
function renderFailures({ cases }) {
  const table = $('failures'); table.replaceChildren();
  const rows = cases.flatMap(c => c.checks.filter(k => !k.ok).map(k => ({ c, k })));
  $('failCount').textContent = `${rows.length} failed`;
  if (!rows.length) { table.append(el('tr', {}, el('td', { class: 'empty-state' }, 'Nothing failed in this selection'))); return; }
  table.append(el('tr', {}, el('th', {}, 'Case'), el('th', {}, 'Domain'), el('th', {}, 'Section'), el('th', {}, 'Check'), el('th', {}, 'Detail')));
  const label = Object.fromEntries(SECTIONS.map(s => [s.id, s.label]));
  rows.forEach(({ c, k }) => table.append(el('tr', {}, el('td', {}, c.id), el('td', {}, c.domain), el('td', {}, label[k.section] || k.section),
    el('td', {}, k.check), el('td', {}, k.detail || ''))));
}

// ---- cases ----
function renderCases({ cases }) {
  const table = $('casesTable'); table.replaceChildren();
  table.append(el('tr', {}, el('th', {}, 'Case'), el('th', {}, 'Domain'), el('th', {}, 'Path'), el('th', {}, 'Coverage'),
    ...SECTIONS.map(s => el('th', { title: `${s.label}: ${s.about}`, style: 'text-align:right' }, s.short)), el('th', {}, 'Result')));
  for (const c of cases) {
    const bySection = {};
    c.checks.forEach(k => (bySection[k.section] ||= []).push(k.score));
    const passed = c.checks.every(k => k.ok);
    const row = el('tr', { class: 'case-row', tabindex: 0 },
      el('td', {}, c.id), el('td', {}, c.domain), el('td', {}, c.info?.answer?.path || c.info?.path || '-'),
      el('td', {}, c.info?.coverage || '-'),
      ...SECTIONS.map(s => { const v = bySection[s.id]; return el('td', { class: 'num chip' }, v ? Math.round(100 * v.reduce((a, b) => a + b, 0) / v.length) : '-'); }),
      el('td', {}, el('span', { class: 'status ' + (passed ? 'pass' : 'fail') }, passed ? '✓ pass' : '✕ fail')));
    const detail = el('tr', { class: 'detail hidden' }, el('td', { colspan: 5 + SECTIONS.length }, caseDetail(c)));
    const toggle = () => detail.classList.toggle('hidden');
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    table.append(row, detail);
  }
}

function caseDetail(c) {
  const g = el('div', { class: 'detail-grid' });
  const add = (k, v) => g.append(el('div', { class: 'k' }, k), v instanceof Node ? v : el('div', {}, v ?? '-'));
  add('Question', c.question);
  add('Search', c.info?.search_required == null ? '-' : `${c.info.search_required ? 'yes' : 'no'}${c.info.latest_news ? ' · latest news' : ''}`);
  add('Search query', c.info?.query || '(none)');
  add('Run of Play', (c.info?.titles || []).join('  ·  ') || '(none)');
  if (c.info?.web_query != null) {
    add('Web query', c.info.web_query || '(none)');
    add('Web results', (c.info.web_titles || []).join('  ·  ') || '(none)');
  }
  const a = c.info?.answer;
  if (a) {
    add('Answer', el('pre', { class: 'answer' }, a.answer || ''));
    add('Timing', `first words ${a.first_words_s ?? '-'}s · done ${a.total_s ?? '-'}s · ${a.path}`);
    if (a.judge_note) add('Judge', a.judge_note);
  }
  add('Checks', el('div', {}, ...c.checks.map(k => el('div', {},
    el('span', { class: 'status ' + (k.ok ? 'pass' : 'fail') }, k.ok ? '✓' : '✕'), ` ${k.section} / ${k.check}`,
    k.detail ? el('span', { style: 'color:var(--muted)' }, ` - ${k.detail}`) : ''))));
  add('Cost', `${inr(c.cost || 0)} (${usd(c.cost || 0)})`);
  return g;
}

function render() {
  const view = current();
  renderTiles(view); renderSections(view); renderTrend(view); renderHeat(view); renderFailures(view); renderCases(view);
}

document.querySelectorAll('button[data-table]').forEach(b => b.addEventListener('click', () => {
  const which = b.dataset.table;
  const [chart, table] = which === 'sections' ? [$('sectionBars'), $('sectionsTable')] : [$('trend'), $('trendTable')];
  chart.classList.toggle('hidden'); table.classList.toggle('hidden');
  b.textContent = table.classList.contains('hidden') ? 'Table' : 'Chart';
}));
$('theme').addEventListener('click', () => {
  document.documentElement.dataset.theme = isDark() ? 'light' : 'dark';
  render();
});
runSel.addEventListener('change', render);
domSel.addEventListener('change', render);
addEventListener('resize', () => renderTrend(current()));
if (!runs.length) document.querySelector('.wrap').append(el('div', { class: 'empty-state' }, 'No reports yet - run python evals/run_evals.py'));
else render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    page = build()
    print(f"Dashboard: {page.relative_to(REPO)}")
