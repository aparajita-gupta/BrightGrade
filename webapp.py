"""
webapp.py

Local web dashboard for the assignment pipeline -- an alternative to
running pipeline.py / todoist_sync.py / syllabus_parser.py by hand from
the terminal every time.

Run:
    python3 webapp.py

Opens http://127.0.0.1:5055 in your default browser. Runs entirely on
your own Mac (Flask's built-in dev server, not exposed to the internet)
since the pipeline itself needs local browser automation (Playwright)
and AppleScript (Calendar.app) that only make sense running here.

Notes / limitations (read before relying on this for everything):
  - "Run Pipeline" always runs unattended (--unattended), same as the
    8PM launchd job -- if a Brightspace/Gradescope session has actually
    expired, this can't pop an interactive login window from a web
    request. You'll get the same desktop notification either way; when
    that happens, log in the normal terminal way:
        python3 pipeline.py
  - Syllabus parsing here skips the multi-section interactive picker in
    favor of a dropdown, and defaults exam delivery to a simple
    Calendar/Todoist radio choice instead of a y/n prompt -- otherwise
    it's the same extract -> review -> merge flow as syllabus_parser.py.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from matching import is_exam_like, normalize_course_name
from pipeline import load_store, save_store

app = Flask(__name__)
PROJECT_DIR = Path(__file__).parent
PYTHON = sys.executable
PORT = 5055


# ---------------------------------------------------------------- data --

def _synced_keys() -> set[str]:
    import json
    path = PROJECT_DIR / "todoist_synced.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("synced_keys", {}).keys())
    except Exception:
        return set()


def _assignments_payload() -> list[dict]:
    store = load_store()
    synced = _synced_keys()
    rows = []
    for key, item in store["assignments"].items():
        rows.append(
            {
                "key": key,
                "title": item.get("title", key),
                "course": item.get("course") or (key.split("::")[0] if "::" in key else None),
                "due_date": item.get("due_date"),
                "location": item.get("location"),
                "source": item.get("source"),
                "tbd": bool(item.get("tbd")),
                "delivery": item.get("delivery"),
                "missing_fields": item.get("missing_fields", []),
                "synced": key in synced,
            }
        )
    # Undated first (need attention), then sort by due_date string.
    rows.sort(key=lambda r: (r["due_date"] is not None, r["due_date"] or ""))
    return rows


# -------------------------------------------------------------- routes --

@app.route("/")
def dashboard():
    return render_template_string(PAGE)


@app.route("/api/assignments")
def api_assignments():
    return jsonify(_assignments_payload())


@app.route("/api/run-pipeline", methods=["POST"])
def api_run_pipeline():
    try:
        result = subprocess.run(
            [PYTHON, "pipeline.py", "--unattended"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=240,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return jsonify({"ok": result.returncode == 0, "output": output.strip()[-4000:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "Timed out after 240s -- check pipeline.log."}), 504


@app.route("/api/sync-todoist", methods=["POST"])
def api_sync_todoist():
    try:
        result = subprocess.run(
            [PYTHON, "todoist_sync.py", "--ai-titles"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return jsonify({"ok": result.returncode == 0, "output": output.strip()[-4000:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "Timed out after 180s."}), 504


@app.route("/api/syllabus/parse", methods=["POST"])
def api_syllabus_parse():
    from syllabus_parser import extract_syllabus_data, resolve_due_date

    upload = request.files.get("pdf")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "No file uploaded."}), 400

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / upload.filename
        upload.save(pdf_path)
        data = extract_syllabus_data(pdf_path)

    if data is None:
        return jsonify({"ok": False, "error": "Gemini extraction failed -- check GEMINI_API_KEY in config.py, or the PDF itself."}), 502

    course_key = normalize_course_name(data["course"])
    items = data.get("items", [])
    sections = sorted({i["section"] for i in items if i.get("section")})
    for item in items:
        item["is_exam"] = is_exam_like(item["title"])
        item["resolved_preview"] = resolve_due_date(item)

    return jsonify(
        {
            "ok": True,
            "course": data["course"],
            "course_key": course_key,
            "items": items,
            "sections": sections,
        }
    )


@app.route("/api/syllabus/merge", methods=["POST"])
def api_syllabus_merge():
    from syllabus_parser import get_calendar_names, find_matching_calendar, merge_into_store

    body = request.get_json(force=True)
    course_key = body["course_key"]
    items = body["items"]
    exam_delivery = body.get("exam_delivery", "todoist")  # "calendar" | "todoist"

    calendar_titles: set[str] = set()
    calendar_name = None
    if exam_delivery == "calendar":
        exam_items = [i for i in items if i.get("is_exam") and not i.get("tbd") and i.get("date")]
        if exam_items:
            calendar_name = find_matching_calendar(course_key, get_calendar_names())
            if calendar_name:
                calendar_titles = {i["title"] for i in exam_items}

    store = load_store()
    stats = merge_into_store(
        store, course_key, items, calendar_titles=calendar_titles, calendar_name=calendar_name
    )
    save_store(store)

    stats["calendar_name"] = calendar_name
    stats["calendar_fallback"] = exam_delivery == "calendar" and calendar_name is None and bool(calendar_titles) is False and any(
        i.get("is_exam") and not i.get("tbd") and i.get("date") for i in items
    )
    return jsonify({"ok": True, "stats": stats})


# ---------------------------------------------------------------- page --

PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Assignment Pipeline</title>
<style>
  :root { --ink:#1f2430; --muted:#5b6472; --line:#dde1e7; --bg:#f7f8fa; --accent:#4a7fd6; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,"Helvetica Neue",Arial,sans-serif; background:var(--bg); color:var(--ink); margin:0; }
  header { padding:20px 28px; border-bottom:1px solid var(--line); background:#fff; display:flex; align-items:center; justify-content:space-between; }
  h1 { font-size:18px; margin:0; }
  main { max-width:1000px; margin:0 auto; padding:24px 28px 60px; }
  .actions { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
  button { font:inherit; font-weight:600; border:none; border-radius:7px; padding:9px 16px; cursor:pointer; background:var(--accent); color:#fff; }
  button.secondary { background:#fff; color:var(--ink); border:1px solid var(--line); }
  button:disabled { opacity:0.5; cursor:default; }
  .stats { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
  .stat { background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 16px; min-width:100px; }
  .stat b { display:block; font-size:20px; }
  .stat span { color:var(--muted); font-size:11px; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
  th { background:#fafbfc; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.03em; }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; font-size:10px; font-weight:700; padding:2px 7px; border-radius:999px; }
  .badge.synced { background:#e6f4ea; color:#1e7b34; }
  .badge.unsynced { background:#fdecec; color:#c23a3a; }
  .badge.tbd { background:#fff4de; color:#a5690c; margin-left:4px; }
  .badge.cal { background:#eef4ff; color:#3a5fa8; margin-left:4px; }
  .muted { color:var(--muted); }
  #log { white-space:pre-wrap; font:11px ui-monospace,Menlo,monospace; background:#161a22; color:#d7dbe2; padding:12px; border-radius:8px; max-height:220px; overflow:auto; margin-top:12px; display:none; }
  section.card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-top:24px; }
  section.card h2 { font-size:14px; margin:0 0 12px; }
  input[type=file] { font-size:13px; }
  select { font:inherit; padding:5px 8px; border-radius:6px; border:1px solid var(--line); }
  #syllabusReview { margin-top:16px; display:none; }
  .item-row { padding:6px 0; border-bottom:1px solid var(--line); font-size:13px; }
  .radio-row { margin:10px 0; font-size:13px; }
  .spinner { display:inline-block; width:12px; height:12px; border:2px solid #ffffff77; border-top-color:#fff; border-radius:50%; animation:spin .7s linear infinite; margin-right:6px; vertical-align:-1px; }
  @keyframes spin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>📋 Assignment Pipeline</h1>
  <div class="muted" id="lastUpdated"></div>
</header>
<main>
  <div class="actions">
    <button id="btnPipeline" onclick="runPipeline()">Run Pipeline</button>
    <button id="btnSync" onclick="runSync()">Push to Todoist</button>
    <button class="secondary" onclick="loadAssignments()">Refresh</button>
  </div>
  <div class="stats" id="stats"></div>
  <table id="table">
    <thead><tr><th>Course</th><th>Title</th><th>Due</th><th>Location</th><th>Source</th><th>Status</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="log"></div>

  <section class="card">
    <h2>Parse a syllabus</h2>
    <input type="file" id="pdfInput" accept="application/pdf">
    <button onclick="parseSyllabus()">Parse</button>
    <div id="syllabusReview"></div>
  </section>
</main>

<script>
function fmtDue(d) {
  if (!d) return '<span class="muted">&mdash;</span>';
  return d;
}

async function loadAssignments() {
  const res = await fetch('/api/assignments');
  const rows = await res.json();
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.course || '<span class="muted">&mdash;</span>'}</td>
      <td>${r.title}</td>
      <td>${fmtDue(r.due_date)}</td>
      <td>${r.location || ''}</td>
      <td class="muted">${r.source || ''}</td>
      <td>
        ${r.due_date ? (r.synced ? '<span class="badge synced">SYNCED</span>' : '<span class="badge unsynced">NOT SYNCED</span>') : ''}
        ${r.tbd ? '<span class="badge tbd">TBD</span>' : ''}
        ${r.delivery === 'calendar' ? '<span class="badge cal">CALENDAR</span>' : ''}
      </td>
    </tr>
  `).join('');

  const total = rows.length;
  const dated = rows.filter(r => r.due_date).length;
  const undated = total - dated;
  const synced = rows.filter(r => r.synced).length;
  const calEvents = rows.filter(r => r.delivery === 'calendar').length;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><b>${total}</b><span>Total tracked</span></div>
    <div class="stat"><b>${dated}</b><span>Dated</span></div>
    <div class="stat"><b>${undated}</b><span>Undated</span></div>
    <div class="stat"><b>${synced}</b><span>Synced to Todoist</span></div>
    <div class="stat"><b>${calEvents}</b><span>Calendar events</span></div>
  `;
  document.getElementById('lastUpdated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function runPipeline() {
  const btn = document.getElementById('btnPipeline');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Running…';
  const log = document.getElementById('log');
  log.style.display = 'block';
  log.textContent = 'Scraping Brightspace + Gradescope, this can take a minute…';
  try {
    const res = await fetch('/api/run-pipeline', { method: 'POST' });
    const data = await res.json();
    log.textContent = data.output || '(no output)';
  } catch (e) {
    log.textContent = 'Request failed: ' + e;
  }
  btn.disabled = false;
  btn.textContent = 'Run Pipeline';
  loadAssignments();
}

async function runSync() {
  const btn = document.getElementById('btnSync');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Pushing…';
  const log = document.getElementById('log');
  log.style.display = 'block';
  log.textContent = 'Pushing to Todoist…';
  try {
    const res = await fetch('/api/sync-todoist', { method: 'POST' });
    const data = await res.json();
    log.textContent = data.output || '(no output)';
  } catch (e) {
    log.textContent = 'Request failed: ' + e;
  }
  btn.disabled = false;
  btn.textContent = 'Push to Todoist';
  loadAssignments();
}

let parsedData = null;

async function parseSyllabus() {
  const input = document.getElementById('pdfInput');
  if (!input.files.length) { alert('Choose a PDF first.'); return; }
  const fd = new FormData();
  fd.append('pdf', input.files[0]);
  const review = document.getElementById('syllabusReview');
  review.style.display = 'block';
  review.innerHTML = '<span class="spinner" style="border-top-color:#4a7fd6;border-color:#4a7fd633"></span>Reading with Gemini…';
  const res = await fetch('/api/syllabus/parse', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { review.innerHTML = '<span style="color:#c23a3a">' + data.error + '</span>'; return; }
  parsedData = data;
  renderReview();
}

function renderReview() {
  const review = document.getElementById('syllabusReview');
  const hasExams = parsedData.items.some(i => i.is_exam && !i.tbd && i.date);
  let sectionPicker = '';
  if (parsedData.sections.length > 1) {
    sectionPicker = `<div class="radio-row">Section:
      <select id="sectionPick" onchange="renderReview()">
        ${parsedData.sections.map(s => `<option value="${s}">${s}</option>`).join('')}
      </select></div>`;
  }
  const chosenSection = parsedData.sections.length > 1
    ? (document.getElementById('sectionPick') ? document.getElementById('sectionPick').value : parsedData.sections[0])
    : null;
  const visible = parsedData.items.filter(i => !i.section || i.section === chosenSection);

  review.innerHTML = `
    <p><b>${parsedData.course}</b> &rarr; <code>${parsedData.course_key}</code>, ${visible.length} item(s)</p>
    ${sectionPicker}
    ${visible.map(i => `<div class="item-row">${i.title}: ${i.tbd ? '<i>TBD</i>' : (i.resolved_preview || '(no date)')}${i.location ? ' @ ' + i.location : ''}${i.is_exam ? ' <span class="badge cal">exam</span>' : ''}</div>`).join('')}
    ${hasExams ? `
      <div class="radio-row">
        <label><input type="radio" name="examDelivery" value="calendar" checked> Add exams as Calendar events</label><br>
        <label><input type="radio" name="examDelivery" value="todoist"> Add exams as Todoist tasks</label>
      </div>` : ''}
    <button onclick="mergeSyllabus('${chosenSection || ''}')">Merge into assignments.json</button>
  `;
}

async function mergeSyllabus(chosenSection) {
  const items = parsedData.items.filter(i => !i.section || i.section === chosenSection);
  const examDelivery = (document.querySelector('input[name=examDelivery]:checked') || {}).value || 'todoist';
  const res = await fetch('/api/syllabus/merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ course_key: parsedData.course_key, items, exam_delivery: examDelivery }),
  });
  const data = await res.json();
  const review = document.getElementById('syllabusReview');
  if (!data.ok) { review.innerHTML = '<span style="color:#c23a3a">Merge failed.</span>'; return; }
  const s = data.stats;
  review.innerHTML = `<p>Merged: ${s.added} added, ${s.filled_in} filled in, ${s.skipped_has_date} already dated, ${s.tbd} flagged TBD, ${s.calendar_events} added as calendar events.</p>`;
  loadAssignments();
}

loadAssignments();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
