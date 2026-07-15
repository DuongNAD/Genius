"""Genius Control Panel — a local web UI to see the pipeline config at a
glance and operate it.

    python control_panel.py            # serves on GENIUS_PANEL_PORT (default 8090)

What it shows (in-process, no agent servers needed):
- the pipeline workflow: each stage's role -> effective backend -> model ->
  reasoning effort -> fallback, plus that backend's CLI health;
- CLI health for every backend (grok / claude / codex / agy / nlm), reusing
  ``ag_core.diagnostics`` (the same engine as ``serve.py --doctor``);
- a note that distributed worker/client machines carry BOTH the grok and the
  codex CLIs, so they can serve the code stage as well as review.

What it can do:
- Run doctor (refresh CLI health) — in-process, always available.
- Start a pipeline run — drives ``orchestrator.run_pipeline`` in a background
  task and polls status. This DOES need the agent skill servers running
  (start them with ``python serve.py`` first); errors surface in the UI.

Loopback-bound by default. The API/control endpoints are unauthenticated for
the trusted single-user localhost case, but — like the dashboard — accept an
optional shared secret: set ``GENIUS_PANEL_TOKEN`` and every ``/api/*`` call is
then required to present it (``X-Panel-Token`` header or ``?token=``). Override
the host with ``GENIUS_PANEL_HOST`` and the port with ``GENIUS_PANEL_PORT``; if
you expose a non-loopback host, set ``GENIUS_PANEL_TOKEN`` and open
``/?token=<token>``.
"""

import asyncio
import hmac
import os
import sys
import time
import uuid
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.config import load_config  # noqa: E402
from ag_core.diagnostics import run_doctor_async  # noqa: E402
from ag_core.provider_factory import (  # noqa: E402
    chain_source,
    resolve_chain,
    resolve_model,
)

app = FastAPI(title="Genius Control Panel")


def _panel_token() -> str:
    return (os.environ.get("GENIUS_PANEL_TOKEN") or "").strip()


def require_panel_auth(
    x_panel_token: str = Header(default="", alias="X-Panel-Token"),
    token: str = Query(default=""),
) -> None:
    """Guard the panel's data and control endpoints. The panel can start
    pipeline jobs (which shell out to local CLIs and write files) and exposes
    the effective backend/model/CLI wiring, so an off-loopback instance must
    not be open. Enforced only when GENIUS_PANEL_TOKEN is set (the default
    localhost bind keeps it optional for the trusted single-user case), and
    checked in constant time to match the dashboard's auth model."""
    expected = _panel_token()
    if not expected:
        return
    provided = (x_panel_token or token or "").strip()
    if not (provided and hmac.compare_digest(provided, expected)):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Pipeline stage label -> canonical role that runs it. The panel reads the
# LIVE config (env + config.yaml), so it reflects whatever role->backend
# wiring is actually in effect, not a hard-coded ideal.
WORKFLOW = [
    ("Research", "researcher"),
    ("Plan / Design", "claude"),
    ("Code", "codex"),
    ("Test", "tester"),
    ("Security review", "security"),
    ("Deploy", "devops"),
]

# Background pipeline jobs (job_id -> state).
PANEL_JOBS: Dict[str, Dict[str, Any]] = {}
_PANEL_TASKS: set = set()
# Cap the in-memory job registry so a long-lived panel doesn't grow it forever;
# evict the oldest FINISHED jobs first (a running job is never dropped).
_PANEL_JOBS_MAX = max(1, int(os.environ.get("GENIUS_PANEL_MAX_JOBS") or 200))


def _prune_panel_jobs() -> None:
    if len(PANEL_JOBS) < _PANEL_JOBS_MAX:
        return
    finished = [
        (j.get("finished_at") or 0.0, jid)
        for jid, j in PANEL_JOBS.items()
        if j.get("status") != "running"
    ]
    finished.sort()  # oldest-finished first
    while len(PANEL_JOBS) >= _PANEL_JOBS_MAX and finished:
        _ts, jid = finished.pop(0)
        PANEL_JOBS.pop(jid, None)


_ROOT_ARTIFACTS = (
    "research.md",
    "design.md",
    "review.md",
    "audit.md",
    "deploy.md",
    "pitch.md",
    "ai_collaboration_log.md",
)


def _model_for(backend: str, role: str, config) -> str:
    """What the runtime will actually run: provider_factory.resolve_model is
    the SAME function build_backend uses, so per-role pins (and the foreign-
    family veto) show up here instead of the panel re-deriving its own — the
    panel used to report the per-backend model while the runtime honored a
    per-role override."""
    model = resolve_model(backend, config, role=role)
    return model or "(CLI default)"


def _effort_for(backend: str, role: str) -> str:
    if backend == "claude":
        return (
            os.getenv(f"GENIUS_CLAUDE_EFFORT_{role.upper()}", "")
            or os.getenv("GENIUS_CLAUDE_EFFORT", "")
            or ""
        )
    if backend == "codex":
        return os.getenv("GENIUS_CODEX_EFFORT", "") or ""
    return ""


async def _build_status() -> Dict[str, Any]:
    config = load_config()
    clis = await run_doctor_async()
    cli_by_name = {c["cli"]: c for c in clis}

    stages = []
    for label, role in WORKFLOW:
        try:
            chain = resolve_chain(role)
        except Exception as exc:  # noqa: BLE001
            stages.append({"stage": label, "role": role, "error": str(exc)})
            continue
        primary = chain[0]
        cli = cli_by_name.get(primary, {})
        stages.append(
            {
                "stage": label,
                "role": role,
                "backend": primary,
                "model": _model_for(primary, role, config),
                "effort": _effort_for(primary, role),
                "fallback": (
                    os.getenv("GENIUS_CLAUDE_FALLBACK_MODEL", "")
                    if primary == "claude"
                    else ""
                ),
                "chain": chain,
                "chain_source": chain_source(role) or "default",
                "cli_status": cli.get("status", "?"),
            }
        )
    return {"stages": stages, "clis": clis, "generated_at": time.time()}


@app.get("/api/status", dependencies=[Depends(require_panel_auth)])
async def api_status() -> JSONResponse:
    return JSONResponse(await _build_status())


@app.get("/api/doctor", dependencies=[Depends(require_panel_auth)])
async def api_doctor() -> JSONResponse:
    return JSONResponse({"clis": await run_doctor_async()})


def _list_artifacts(workspace: str) -> list:
    present = []
    for name in _ROOT_ARTIFACTS:
        if os.path.exists(os.path.join(workspace, name)):
            present.append(name)
    return present


async def _run_job(job_id: str, prompt: str, pipeline: str, workspace: str) -> None:
    from orchestrator import run_e2e_pipeline, run_pipeline

    job = PANEL_JOBS[job_id]
    try:
        if pipeline == "e2e":
            await run_e2e_pipeline(prompt, workspace=workspace)
        else:
            await run_pipeline(prompt, workspace=workspace)
        job["status"] = "completed"
        job["artifacts"] = _list_artifacts(workspace)
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        job["finished_at"] = time.time()


@app.post("/api/orchestrate", dependencies=[Depends(require_panel_auth)])
async def api_orchestrate(body: Dict[str, Any]) -> JSONResponse:
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    pipeline = body.get("pipeline") or "sequential"
    if pipeline not in ("sequential", "e2e"):
        return JSONResponse({"error": "pipeline must be sequential|e2e"}, 400)

    _prune_panel_jobs()
    job_id = uuid.uuid4().hex
    workspace = os.path.join(root_dir, "projects", f"panel-{job_id}")
    os.makedirs(workspace, exist_ok=True)
    PANEL_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "pipeline": pipeline,
        "prompt": prompt,
        "workspace": workspace,
        "error": None,
        "artifacts": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    task = asyncio.create_task(_run_job(job_id, prompt, pipeline, workspace))
    _PANEL_TASKS.add(task)
    task.add_done_callback(_PANEL_TASKS.discard)
    return JSONResponse({"job_id": job_id, "status": "running"})


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_panel_auth)])
async def api_job(job_id: str) -> JSONResponse:
    job = PANEL_JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    view = {k: job[k] for k in ("job_id", "status", "pipeline", "error", "artifacts")}
    started = job.get("started_at")
    end = job.get("finished_at") or time.time()
    view["elapsed_seconds"] = round(end - started, 1) if started else None
    return JSONResponse(view)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Genius — Control Panel</title>
<style>
:root{
  --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d21; --muted:#5b6570; --line:#e4e7eb;
  --accent:#3b6ef5; --ok:#1a9d5a; --warn:#c9820a; --miss:#d23b3b;
  --chip:#eef1f6;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0f1216; --card:#171b21; --ink:#e8ebef; --muted:#9aa4b0;
    --line:#262c34; --accent:#5b8cff; --chip:#1f242c; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:22px;margin:0 0 2px} .sub{color:var(--muted);margin:0 0 20px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);margin:26px 0 10px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px}
.stage-h{display:flex;align-items:center;justify-content:space-between;gap:8px}
.stage-h b{font-size:15px}
.role{color:var(--muted);font-size:12px}
.row{display:flex;justify-content:space-between;gap:8px;margin-top:8px;
  font-size:13px}
.row .k{color:var(--muted)} .row .v{font-weight:600;text-align:right;
  word-break:break-word}
.chip{display:inline-block;background:var(--chip);border:1px solid var(--line);
  border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600}
.eff{color:#fff;border:0}
.eff.max{background:#b3402e}.eff.xhigh{background:#c9820a}
.eff.high{background:#1a9d5a}.eff.medium{background:#3b6ef5}
.eff.low{background:#6b7684}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.OK{background:var(--ok)}.WARN{background:var(--warn)}
.MISSING{background:var(--miss)}
.cli{display:flex;align-items:flex-start;gap:8px;padding:8px 0;
  border-bottom:1px solid var(--line);font-size:13px}
.cli:last-child{border-bottom:0}
.cli .d{color:var(--muted);font-size:12px;word-break:break-word}
.note{background:var(--card);border:1px dashed var(--line);border-radius:12px;
  padding:12px 16px;color:var(--muted);font-size:13px}
button{background:var(--accent);color:#fff;border:0;border-radius:9px;
  padding:9px 16px;font-weight:600;cursor:pointer;font-size:13px}
button.ghost{background:transparent;color:var(--accent);
  border:1px solid var(--accent)}
button:disabled{opacity:.5;cursor:default}
textarea,select{width:100%;background:var(--bg);color:var(--ink);
  border:1px solid var(--line);border-radius:9px;padding:9px;font:inherit}
textarea{min-height:64px;resize:vertical}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
.run-out{margin-top:10px;font-size:13px;color:var(--muted);white-space:pre-wrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Genius — Control Panel</h1>
  <p class="sub">Live pipeline config: who runs each stage, on which model &amp;
    reasoning effort, and CLI health. <span id="ts"></span></p>

  <h2>Pipeline workflow</h2>
  <div class="grid" id="stages"></div>

  <h2>CLI health</h2>
  <div class="card" id="clis"></div>

  <div class="note" id="distnote" style="margin-top:12px">
    Distributed worker/client machines carry <b>both</b> the <b>grok</b> and
    the <b>codex</b> CLIs — so a worker can serve the <b>code</b> stage
    (codex) as well as review (grok), and codex-heavy load spreads across the
    GPT accounts on those machines.
  </div>

  <h2>Run pipeline</h2>
  <div class="card">
    <textarea id="prompt" placeholder="Describe what to build, e.g. build a TODO API"></textarea>
    <div class="bar">
      <select id="pipeline" style="max-width:200px">
        <option value="sequential">sequential</option>
        <option value="e2e">e2e</option>
      </select>
      <button id="run">Start pipeline</button>
      <button class="ghost" id="doctor">Run doctor</button>
      <button class="ghost" id="refresh">Refresh</button>
    </div>
    <div class="run-out" id="runout">Needs the agent servers running
      (<span class="mono">python serve.py</span>). Status appears here.</div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
// When the panel is token-protected (GENIUS_PANEL_TOKEN set), open
// /?token=<token>; the page forwards it as X-Panel-Token on every call.
const PANEL_TOKEN = new URLSearchParams(window.location.search).get('token') || '';
function authHeaders(extra){
  const h = extra ? Object.assign({}, extra) : {};
  if(PANEL_TOKEN) h['X-Panel-Token'] = PANEL_TOKEN;
  return h;
}
function effChip(e){
  if(!e) return '<span class="chip">—</span>';
  return `<span class="chip eff ${e}">${e}</span>`;
}
function render(d){
  $('#ts').textContent = 'updated ' + new Date().toLocaleTimeString();
  $('#stages').innerHTML = d.stages.map(s => {
    if(s.error) return `<div class="card"><div class="stage-h"><b>${s.stage}</b>
      </div><div class="row"><span class="k">error</span>
      <span class="v">${s.error}</span></div></div>`;
    const fb = s.fallback ? `<div class="row"><span class="k">fallback</span>
      <span class="v">${s.fallback}</span></div>` : '';
    return `<div class="card">
      <div class="stage-h"><b>${s.stage}</b>
        <span><span class="dot ${s.cli_status}"></span>
        <span class="chip">${s.backend}</span></span></div>
      <div class="role">role: ${s.role} · chain: ${s.chain.join(' → ')}
        ${s.chain_source!=='default' ? '('+s.chain_source+')':''}</div>
      <div class="row"><span class="k">model</span>
        <span class="v">${s.model}</span></div>
      <div class="row"><span class="k">effort</span>
        <span class="v">${effChip(s.effort)}</span></div>
      ${fb}
    </div>`;
  }).join('');
  $('#clis').innerHTML = d.clis.map(c => `<div class="cli">
      <span class="dot ${c.status}" style="margin-top:5px"></span>
      <div><b>${c.cli}</b> <span class="chip">${c.status}</span>
        <div class="d">${c.detail||''}</div>
        <div class="d">used by: ${(c.dependents||'')}</div></div>
    </div>`).join('');
}
async function load(){
  try{ render(await (await fetch('/api/status', {headers: authHeaders()})).json()); }
  catch(e){ $('#ts').textContent = 'status error: '+e; }
}
$('#refresh').onclick = load;
$('#doctor').onclick = async () => {
  $('#doctor').disabled = true; $('#doctor').textContent='Running…';
  try{ await fetch('/api/doctor', {headers: authHeaders()}); await load(); }
  finally{ $('#doctor').disabled=false; $('#doctor').textContent='Run doctor'; }
};
$('#run').onclick = async () => {
  const prompt = $('#prompt').value.trim();
  if(!prompt){ $('#runout').textContent='Enter a prompt first.'; return; }
  $('#run').disabled = true;
  $('#runout').textContent = 'Starting…';
  try{
    const r = await (await fetch('/api/orchestrate',{method:'POST',
      headers:authHeaders({'content-type':'application/json'}),
      body:JSON.stringify({prompt, pipeline:$('#pipeline').value})})).json();
    if(r.error){ $('#runout').textContent='Error: '+r.error; return; }
    poll(r.job_id);
  }catch(e){ $('#runout').textContent='Error: '+e; $('#run').disabled=false; }
};
async function poll(id){
  try{
    const j = await (await fetch('/api/jobs/'+id, {headers: authHeaders()})).json();
    $('#runout').textContent = `job ${id.slice(0,8)} · ${j.status} · `
      + `${j.elapsed_seconds||0}s`
      + (j.error ? `\n${j.error}` : '')
      + (j.artifacts ? `\nartifacts: ${j.artifacts.join(', ')}` : '');
    if(j.status === 'running'){ setTimeout(()=>poll(id), 1500); }
    else { $('#run').disabled = false; }
  }catch(e){ $('#runout').textContent='poll error: '+e; $('#run').disabled=false; }
}
load();
</script>
</body>
</html>
"""


def main() -> None:
    import uvicorn

    host = os.getenv("GENIUS_PANEL_HOST") or "127.0.0.1"
    port = int(os.getenv("GENIUS_PANEL_PORT") or "8090")
    # Fail closed (same policy as dashboard.py and the MCP HTTP server): the
    # panel can START pipeline jobs and read the config, and
    # require_panel_auth only enforces the token when one is set, so a
    # missing token on a public bind must be refused at startup rather than
    # just warned about.
    if host.strip() not in ("127.0.0.1", "localhost", "::1", "") and not _panel_token():
        sys.exit(
            f"Refusing to start: GENIUS_PANEL_HOST={host!r} exposes the "
            "control panel beyond loopback, but GENIUS_PANEL_TOKEN is not "
            "set. Anyone who can reach this port could start pipeline jobs "
            "and read your config. Set GENIUS_PANEL_TOKEN=<secret> and open "
            "/?token=<token>, or bind 127.0.0.1."
        )
    print(f"Genius Control Panel -> http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
