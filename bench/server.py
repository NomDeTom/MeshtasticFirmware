"""The status server: read-only, and it renders rather than records.

A bench run is hours long, mostly unattended and mostly silent. Across the beacon run,
status was obtained by grepping a background task's stdout, which failed three distinct
ways: a detached run left the orchestrator with no completion signal so it reported
"done" while work continued; prep failures were written only to results.json and never
printed, so the console showed a bare FAIL with no reason; and a stage that hung was
indistinguishable from one that was merely slow.

Four properties follow, and they are constraints rather than features.

  Read-only. No start, stop or abort. Control and observation on one surface is how an
  observer perturbs the experiment it is watching.

  It never becomes a second source of truth. Every value is derived from artifacts the
  run already writes; if server and artifacts disagree, the artifacts win. A crashed
  server cannot corrupt a run, and it rebuilds its whole view from disk on every
  request, so restarting it mid-run loses nothing.

  It distinguishes finished, failed and DIED. The run writes a heartbeat; the server ages
  it. A run whose process vanished shows DIED at its last known position rather than a
  stale RUNNING that looks like progress.

  It reports the wait, not just the work. "Waiting for node B to re-enumerate, 43s" is
  the single most useful line during prep, and it is exactly what a progress bar omits.

Coverage is the whole pipeline, build included - an image compiling for 29 minutes is
the majority of a run's wall clock, and a status page that starts at the first row shows
nothing for the part that takes longest.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import streams

# How stale a heartbeat may be before the run is presumed dead. The runner beats every
# ~10s, and a build blocks nothing, so a minute is generous without being useless.
DEAD_AFTER_S = 60.0

RUNNING = "RUNNING"
FINISHED = "FINISHED"
FAILED = "FAILED"
DIED = "DIED"
# No run has written state here at all - a build-only invocation, or a run that has
# not started. Distinct from DIED, which means a run was here and stopped beating.
NO_RUN = "NO RUN"

# Historical medians, in seconds, for the honest estimate. Measured on the beacon run
# rather than invented: builds were stable at ~29 min and prep at ~3 min, which makes a
# median informative where a synthetic percentage would not be.
STAGE_MEDIANS_S = {
    "0-preflight": 10,
    "1-build": 1740,
    "2-flash": 90,
    "3-provision": 180,
    "4-execute": 150,
    "5-cycle": 30,
}


def read_state(run_dir: Path) -> dict:
    """Rebuild the whole view from artifacts on disk. No in-memory state at all."""
    run_dir = Path(run_dir)
    state = _load_json(run_dir / "state.json") or {}
    results = _load_json(run_dir / "results.json") or {}
    preflight = _load_json(run_dir / "preflight.json") or {}
    manifest = _load_json(run_dir / "manifest.json") or {}

    beats = list(streams.read_stream(run_dir, streams.STATUS))
    last_beat = beats[-1] if beats else None
    beat_age = None if last_beat is None else round(time.time() - last_beat.get("ts", 0), 1)

    status = _liveness(state, results, beat_age)
    return {
        "status": status,
        "run_dir": str(run_dir),
        "identity": {
            "started_at": state.get("started_at"),
            "operator_note": state.get("operator_note"),
            "platform": state.get("platform"),
            # The run records this from its first heartbeat; the manifest is only a
            # fallback for a run that died before writing state.
            "git": state.get("git") or _git_from_manifest(manifest),
            "scenario_table_hash": state.get("scenario_table_hash"),
            "scenarios": state.get("scenarios", []),
        },
        "position": {
            "stage": state.get("stage"),
            "row": state.get("row"),
            "done": state.get("done", len(results)),
            "total": state.get("total"),
            "elapsed_s": state.get("elapsed_s"),
            "expected_stage_s": STAGE_MEDIANS_S.get(state.get("stage") or ""),
        },
        "waiting": {
            "for": state.get("waiting_for"),
            "since": state.get("waiting_since"),
            "seconds": (
                None
                if not state.get("waiting_since")
                else round(time.time() - state["waiting_since"], 1)
            ),
        },
        "counts": state.get("counts", {}),
        "rows": _rows(results),
        "nodes": state.get("nodes", []),
        "observer": state.get("observer"),
        "capture": _capture(state, run_dir),
        "manifest": state.get("manifest", {}),
        "images": _images(manifest),
        "preflight": preflight,
        "heartbeat_age_s": beat_age,
        "generated_at": time.time(),
    }


def _liveness(state: dict, results: dict, beat_age: float | None) -> str:
    """FINISHED, FAILED, DIED or RUNNING - never a stale RUNNING for a vanished run."""
    stage = state.get("stage")
    if stage == "done":
        verdicts = {r.get("verdict") for r in results.values()}
        return FAILED if ("FAIL" in verdicts or "INVALID" in verdicts) else FINISHED
    if not state and beat_age is None:
        return NO_RUN  # nothing has run here; saying DIED would invent a corpse
    if beat_age is None:
        return RUNNING
    return DIED if beat_age > DEAD_AFTER_S else RUNNING


def _rows(results: dict) -> list[dict]:
    out = []
    for scenario_id, row in results.items():
        out.append(
            {
                "id": scenario_id,
                "verdict": row.get("verdict"),
                "error": row.get("error"),
                "release_representative": row.get("release_representative", True),
                "images": row.get("images", {}),
                "duration_s": (
                    round(row["ended_at"] - row["started_at"], 1)
                    if row.get("ended_at") and row.get("started_at")
                    else None
                ),
                # The evidence line per assertion - the thing a bare FAIL never showed.
                "outcomes": [
                    {
                        "name": o.get("name"),
                        "verdict": o.get("verdict"),
                        "evidence": o.get("evidence"),
                    }
                    for o in row.get("outcomes", [])
                ],
            }
        )
    return out


def _capture(state: dict, run_dir: Path) -> dict:
    capture = state.get("capture") or {}
    streams_info = capture.get("streams")
    if not streams_info:
        # The run has not written state yet, or died before it could. Measure the files
        # directly rather than showing nothing.
        streams_info = {}
        for name in streams.STREAM_NAMES:
            path = run_dir / f"{name}.jsonl"
            streams_info[name] = {
                "path": str(path),
                "bytes": path.stat().st_size if path.exists() else 0,
                "rows": None,
                "age_s": None,
            }
    return {"streams": streams_info, "paused": capture.get("paused", False)}


def _images(manifest: dict) -> list[dict]:
    out = []
    for bake_hash, entry in (manifest.get("images") or {}).items():
        out.append(
            {
                "bake_hash": bake_hash,
                "env": entry.get("env"),
                "flash_pct": entry.get("flash_pct"),
                "ram_pct": entry.get("ram_pct"),
                "duration_s": entry.get("duration_s"),
                "release_representative": entry.get("release_representative", True),
                "bench_only_flags": entry.get("bench_only_flags", []),
                "capabilities": entry.get("capabilities", []),
            }
        )
    return sorted(out, key=lambda e: e["bake_hash"])


def _git_from_manifest(manifest: dict) -> dict:
    for entry in (manifest.get("images") or {}).values():
        return {"sha": entry.get("git_sha"), "dirty": entry.get("dirty")}
    return {}


def build_tail(run_dir: Path, limit: int = 25) -> dict:
    """Last lines of the most recently written build log.

    During stage 1 there is nothing in the firmware streams to show, and a build that
    reveals nothing for 29 minutes is indistinguishable from a hung one. This is the only
    thing worth watching while an image compiles.
    """
    builds = Path(run_dir) / "builds"
    logs = sorted(builds.glob("*.log"), key=lambda p: p.stat().st_mtime) if builds.is_dir() else []
    if not logs:
        return {"bake_hash": None, "lines": [], "path": None}
    newest = logs[-1]
    try:
        content = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        content = []
    # Deliberately no age here. On Windows a file's mtime is not updated while a handle
    # is open for writing, so an actively compiling build reported its log as twelve
    # minutes stale - which is exactly the "hung or just slow?" ambiguity this server
    # exists to remove, invented by the server itself. Growth is the honest signal: a
    # viewer polling every few seconds watches total_lines climb.
    return {
        "bake_hash": newest.stem,
        "path": str(newest),
        "lines": content[-limit:],
        "total_lines": len(content),
        "bytes": newest.stat().st_size,
    }


def tail(run_dir: Path, stream: str, limit: int = 40, node: str | None = None) -> list[dict]:
    rows = list(streams.read_stream(Path(run_dir), stream))
    if node:
        rows = [r for r in rows if r.get("node") == node]
    return rows[-limit:]


def one_line(state: dict) -> str:
    """The terse summary, suitable for a terminal or a notification."""
    pos, counts = state["position"], state.get("counts", {})
    cap = state["capture"]["streams"]
    live = sum(1 for s in cap.values() if (s.get("rows") or s.get("bytes")))
    ages = [s["age_s"] for s in cap.values() if s.get("age_s") is not None]
    elapsed = pos.get("elapsed_s") or 0
    started = state["identity"].get("started_at") or time.time()
    waiting = state["waiting"]
    line = (
        f"run {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(started))}  "
        f"stage {pos.get('stage')}  row {pos.get('row') or '-'} "
        f"({pos.get('done')}/{pos.get('total')})  "
        f"elapsed {int(elapsed // 3600)}h{int(elapsed % 3600 // 60):02d}m  [{state['status']}]\n"
        f"  pass {counts.get('PASS', 0)}  fail {counts.get('FAIL', 0)}  "
        f"not-observed {counts.get('NOT OBSERVED', 0)}  invalid {counts.get('INVALID', 0)}   "
        f"capture: {live} streams live, last event "
        f"{'never' if not ages else f'{min(ages):.0f}s ago'}"
    )
    if waiting.get("for"):
        line += f"\n  waiting for {waiting['for']}, {waiting['seconds']:.0f}s"
    return line


class _Handler(BaseHTTPRequestHandler):
    run_dir: Path = Path(".")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/status.json":
                self._json(read_state(self.run_dir))
            elif path == "/status.txt":
                self._text(one_line(read_state(self.run_dir)))
            elif path == "/tail.json":
                self._json({"logs": tail(self.run_dir, streams.LOGS),
                            "events": tail(self.run_dir, streams.EVENTS, 20),
                            "build": build_tail(self.run_dir)})
            elif path in ("/", "/index.html"):
                self._html(PAGE)
            else:
                self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001 - a broken render must not kill the server
            self._json({"error": f"{type(exc).__name__}: {exc}"}, code=500)

    def _json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, indent=1, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # the server's own access log is noise on a bench console


def serve(run_dir: Path, port: int = 871, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start the status server. Returns it; call shutdown() to stop.

    Binds to loopback by default. The page is a convenience; GET /status.json is the
    contract, and it is a plain static document so polling costs nothing.
    """
    handler = type("_BoundHandler", (_Handler,), {"run_dir": Path(run_dir)})
    server = ThreadingHTTPServer((host, port), handler)
    return server


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>bench status</title>
<style>
:root{--bg:#f6f7f9;--fg:#141a22;--mut:#5a6673;--rule:#dce1e7;--card:#fff;
--ok:#1e7a5f;--bad:#a8541e;--warn:#8a6d1f;--accent:#1f5f8b}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--fg:#e4e8ed;--mut:#9aa5b1;
--rule:#242d37;--card:#161c23;--ok:#4fbf97;--bad:#e08a4a;--warn:#d4b352;--accent:#5aa9db}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:1.5rem}
h1{font-size:1.1rem;margin:0 0 .2rem;letter-spacing:.02em}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;color:var(--mut);
margin:1.6rem 0 .5rem;font-weight:600}
.sub{color:var(--mut);margin:0 0 1rem}
.card{background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:.8rem 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:.7rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);border-bottom:1px solid var(--rule);padding:.4rem .5rem}
td{padding:.4rem .5rem;border-bottom:1px solid var(--rule);vertical-align:top}
.pill{display:inline-block;padding:.05em .45em;border-radius:3px;font-size:.72rem;
font-weight:600;border:1px solid currentColor}
.PASS,.RUNNING,.FINISHED{color:var(--ok)}
.FAIL,.DIED,.FAILED{color:var(--bad)}
.INVALID{color:var(--bad)}
[class~="NOT"]{color:var(--warn)}
.warnrow{color:var(--warn)}
pre{margin:0;white-space:pre-wrap;font-size:.8rem;color:var(--mut);max-height:16rem;overflow:auto}
.k{color:var(--mut)}
.big{font-size:1.35rem;font-weight:600}
.wait{border-left:3px solid var(--accent);padding-left:.7rem}
</style>
<div class="wrap">
  <h1>bench status</h1>
  <p class="sub" id="ident">loading&hellip;</p>
  <div class="card wait" id="wait" hidden></div>
  <h2>position</h2>
  <div class="grid" id="pos"></div>
  <h2>rows</h2>
  <div class="card"><table id="rows"><thead><tr><th>row</th><th>verdict</th>
    <th>evidence</th><th>image</th><th>s</th></tr></thead><tbody></tbody></table></div>
  <h2>images</h2>
  <div class="card"><table id="imgs"><thead><tr><th>bake</th><th>env</th><th>flash</th>
    <th>ram</th><th>build s</th><th>release-repr</th></tr></thead><tbody></tbody></table></div>
  <h2>nodes</h2>
  <div class="card"><table id="nodes"><thead><tr><th>node</th><th>role</th><th>port</th>
    <th>mode</th><th>state</th><th>pkts</th><th>logs</th></tr></thead><tbody></tbody></table></div>
  <h2>capture</h2>
  <div class="card"><table id="cap"><thead><tr><th>stream</th><th>rows</th><th>bytes</th>
    <th>last</th></tr></thead><tbody></tbody></table></div>
  <h2 id="tail-head">tail</h2>
  <div class="card"><pre id="tail"></pre></div>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const cell = v => v === null || v === undefined ? "<span class=k>-</span>" : esc(v);
const secs = v => v === null || v === undefined ? "-" : Math.round(v) + "s";

function rows(id, data, cols) {
  const body = $(id).querySelector("tbody");
  body.innerHTML = data.map(d => "<tr>" + cols.map(c => "<td>" + c(d) + "</td>").join("") + "</tr>").join("")
    || "<tr><td colspan=9 class=k>nothing yet</td></tr>";
}

async function refresh() {
  let s;
  try { s = await (await fetch("status.json", {cache:"no-store"})).json(); }
  catch (e) { $("#ident").textContent = "status unavailable - is the run directory there?"; return; }

  const id = s.identity || {}, p = s.position || {}, c = s.counts || {};
  $("#ident").innerHTML =
    `<span class="pill ${esc(s.status)}">${esc(s.status)}</span> ` +
    `${cell(id.git && id.git.sha)}${id.git && id.git.dirty ? " <span class=warnrow>(dirty)</span>" : ""} &middot; ` +
    `${cell((id.platform||{}).os)} &middot; ${cell(id.operator_note || "no note")}`;

  const w = s.waiting || {};
  $("#wait").hidden = !w.for;
  if (w.for) $("#wait").innerHTML = `waiting for <b>${esc(w.for)}</b>, ${secs(w.seconds)}`;

  $("#pos").innerHTML = [
    ["stage", p.stage, ""],
    ["row", p.row || "-", `${p.done ?? 0} / ${p.total ?? 0} done`],
    ["elapsed", secs(p.elapsed_s), p.expected_stage_s ? "stage median " + secs(p.expected_stage_s) : ""],
    ["pass / fail", `${c.PASS||0} / ${c.FAIL||0}`, `${c["NOT OBSERVED"]||0} not-observed, ${c.INVALID||0} invalid`],
    ["heartbeat", secs(s.heartbeat_age_s) + " ago", ""],
  ].map(([k,v,sub]) => `<div class="card"><div class=k>${k}</div><div class=big>${cell(v)}</div>
     <div class=k>${esc(sub)}</div></div>`).join("");

  rows("#rows", s.rows || [], [
    d => esc(d.id),
    d => `<span class="pill ${esc((d.verdict||"").split(" ")[0])}">${esc(d.verdict)}</span>` +
         (d.release_representative === false ? " <span class=warnrow>bench-only</span>" : ""),
    d => d.error ? `<span class=FAIL>${esc(d.error)}</span>`
                 : (d.outcomes||[]).map(o => `${esc(o.name)}: ${esc(o.evidence)}`).join("<br>") || "<span class=k>-</span>",
    d => Object.values(d.images||{}).map(esc).join(" ") || "-",
    d => secs(d.duration_s)]);

  rows("#imgs", s.images || [], [
    d => esc(d.bake_hash), d => esc(d.env),
    d => d.flash_pct != null ? d.flash_pct + "%" : "-",
    d => d.ram_pct != null ? d.ram_pct + "%" : "-",
    d => secs(d.duration_s),
    d => d.release_representative ? "yes" : `<span class=warnrow>no (${esc((d.bench_only_flags||[]).join(","))})</span>`]);

  const obs = (s.observer || {}).nodes || {};
  rows("#nodes", s.nodes || [], [
    d => esc(d.name), d => esc(d.role), d => cell(d.port),
    d => esc((obs[d.name]||{}).mode || (d.never_command ? "raw" : "api")),
    d => { const o = obs[d.name] || {};
           if (o.connected) return "<span class=PASS>connected</span>";
           if (o.dropped_for_s != null) return `<span class=FAIL>dropped ${secs(o.dropped_for_s)}</span>`;
           return d.port ? "<span class=k>present</span>" : "<span class=FAIL>absent</span>"; },
    d => cell((obs[d.name]||{}).packets), d => cell((obs[d.name]||{}).log_lines)]);

  const cap = (s.capture||{}).streams || {};
  rows("#cap", Object.entries(cap).map(([k,v]) => ({name:k, ...v})), [
    d => esc(d.name), d => cell(d.rows), d => cell(d.bytes),
    d => d.age_s == null ? "<span class=k>never</span>"
         : (d.age_s > 120 ? `<span class=FAIL>${secs(d.age_s)} ago</span>` : secs(d.age_s) + " ago")]);

  try {
    const t = await (await fetch("tail.json", {cache:"no-store"})).json();
    $("#tail").textContent = (t.logs||[]).slice(-25)
      .map(r => `${(r.node||"-").padEnd(8)} ${(r.line||r.msg||"").slice(0,150)}`).join("\\n") || "no lines yet";
  } catch (e) { $("#tail").textContent = "tail unavailable"; }
}
refresh(); setInterval(refresh, 3000);
</script>
"""
