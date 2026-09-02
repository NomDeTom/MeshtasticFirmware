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

It is one long-lived daemon over a runs ROOT, not a view of a single run. Start it once
and leave it: runs appear and disappear underneath it, a build-only invocation shows up
the moment it writes its first log line, and nothing needs restarting when the next run
begins. That is what makes it usable for headless and unattended work - the thing you
check on is already running before the thing you are checking on starts.
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

# When this daemon started. The title asks about the bench process, which outlives any
# individual run and is the thing an unattended watcher wants to know is still up.
SERVER_STARTED_AT = time.time()

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



# Files that mark a directory as a bench run. A build-only invocation writes only a
# manifest and a build log, and must still be visible - it is the longest stage.
RUN_MARKERS = ("state.json", "results.json", "manifest.json", "events.jsonl", "builds")


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and any((path / m).exists() for m in RUN_MARKERS)


def discover_runs(root: Path) -> list[dict]:
    """Every run under `root`, newest activity first.

    Rebuilt on each request, so a run created after the server started is picked up with
    no restart. That is the property headless use depends on.
    """
    root = Path(root)
    if is_run_dir(root):
        return [_run_row(root)]  # pointed straight at one run
    if not root.is_dir():
        return []
    rows = [_run_row(p) for p in sorted(root.iterdir()) if is_run_dir(p)]
    rows.sort(key=lambda r: r["last_activity"] or 0, reverse=True)
    return rows


def _run_row(path: Path) -> dict:
    """A cheap summary for the run list - no stream parsing, so listing stays fast."""
    state = _load_json(path / "state.json") or {}
    results = _load_json(path / "results.json") or {}
    beats = list(streams.read_stream(path, streams.STATUS))
    beat_age = None
    if beats:
        beat_age = round(time.time() - (beats[-1].get("ts") or 0), 1)

    counts = state.get("counts") or {}
    for row in results.values():
        if not counts:
            break
    if not counts and results:
        counts = {}
        for row in results.values():
            v = row.get("verdict", "INVALID")
            counts[v] = counts.get(v, 0) + 1

    return {
        "name": path.name,
        "path": str(path),
        "status": _liveness(state, results, beat_age),
        "stage": state.get("stage"),
        "row": state.get("row"),
        "done": state.get("done", len(results)),
        "total": state.get("total"),
        "counts": counts,
        "note": state.get("operator_note"),
        "started_at": state.get("started_at"),
        "last_activity": _last_activity(path),
        "heartbeat_age_s": beat_age,
    }


def _last_activity(path: Path) -> float | None:
    """Newest mtime among the run's artifacts.

    Only used for ordering the list. Deliberately NOT used as a freshness claim: on
    Windows a file being written keeps a stale mtime, which is why the build log reports
    growth instead.
    """
    newest = None
    for name in ("state.json", "results.json", "events.jsonl", "manifest.json"):
        f = path / name
        try:
            if f.exists():
                newest = max(newest or 0, f.stat().st_mtime)
        except OSError:
            continue
    try:
        for log in (path / "builds").glob("*.log"):
            newest = max(newest or 0, log.stat().st_mtime)
    except OSError:
        pass
    return newest


def resolve_run(root: Path, name: str | None) -> Path | None:
    """Pick the run to show: the one asked for, else the most recently active."""
    root = Path(root)
    if is_run_dir(root):
        return root
    if name:
        candidate = root / name
        # Refuse anything that escapes the root - the name arrives from a URL.
        if is_run_dir(candidate) and root.resolve() in candidate.resolve().parents:
            return candidate
        return None
    rows = discover_runs(root)
    return Path(rows[0]["path"]) if rows else None



def devices_view(state: dict, run_status: str, beat_age: float | None) -> list[dict]:
    """One row per device: what is true NOW, and what the run last recorded.

    Those are different claims and the page must not blur them. USB presence is checked
    at request time - enumeration opens nothing, so it is safe for a read-only observer
    to ask - while port state, reconnect counts and the observed model can only come from
    the run that held the device, and go stale the moment that run stops.

    Showing a finished run's last port state as though it were current is how a healthy
    bench came to look broken: "gave_up" and "absent" were both true twenty minutes ago
    and neither was true any more.
    """
    from . import devices as devices_mod

    try:
        live = {
            (d.serial_number or "").upper(): d
            for d in devices_mod.enumerate_devices(all_devices=True)
        }
    except Exception:  # noqa: BLE001 - a listing failure must not blank the page
        live = {}

    ports_state = state.get("ports") or {}
    stale = run_status != RUNNING
    out = []
    for node in state.get("nodes") or []:
        name = node.get("name")
        recorded = dict(ports_state.get(name) or {})
        serial = (node.get("serial_number") or "").upper()
        seen = live.get(serial)

        row = {
            "node": name,
            # Identity comes from the node table, so it is present even before any run.
            "role": recorded.get("role") or node.get("role"),
            "serial_number": node.get("serial_number"),
            "declared_board": recorded.get("declared_board") or node.get("board"),
            "observed_model": recorded.get("observed_model"),
            "node_id": recorded.get("node_id"),
            "firmware": recorded.get("firmware"),
            "never_command": node.get("never_command"),
            "never_flash": node.get("never_flash"),
            "capture": recorded.get("capture") or (
                "raw serial" if node.get("never_command") else "protobuf api"
            ),
            # Live, and labelled as such.
            "present": seen is not None,
            "port": seen.port if seen else None,
            # Remembered, and labelled as such.
            "recorded_state": recorded.get("state"),
            "recorded_port": recorded.get("port"),
            "reconnects": recorded.get("reconnects"),
            "last_error": recorded.get("last_error"),
            "stale": stale,
            "as_of_s": beat_age if stale else None,
        }
        declared, observed = row["declared_board"], row["observed_model"]
        row["board_matches"] = (
            None if not (declared and observed)
            else declared.strip().upper() == observed.strip().upper()
        )
        out.append(row)
    return out


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
            # Planned against actual. The schedule is a worst case computed from the
            # budget on every device operation, so a run past it has something wrong
            # rather than something slow - which a percentage bar could never say.
            "planned_total_s": (state.get("schedule") or {}).get("total_s"),
            "over_plan": (
                bool(state.get("elapsed_s", 0) > (state.get("schedule") or {}).get("total_s", 1e9))
            ),
        },
        "schedule": state.get("schedule"),
        "ports": state.get("ports"),
        "devices": devices_view(state, status, beat_age),
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
        "server_started_at": SERVER_STARTED_AT,
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
    root: Path = Path(".")

    def _run(self) -> Path | None:
        from urllib.parse import parse_qs, urlparse

        wanted = parse_qs(urlparse(self.path).query).get("run", [None])[0]
        return resolve_run(self.root, wanted)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/runs.json":
                self._json({"root": str(self.root), "runs": discover_runs(self.root)})
                return
            if path in ("/", "/index.html"):
                self._html(PAGE)
                return

            run = self._run()
            if run is None:
                # No run yet is a normal state for a daemon started before any work.
                if path == "/status.txt":
                    self._text("no runs yet under " + str(self.root))
                else:
                    self._json({"status": NO_RUN, "root": str(self.root),
                                "server_started_at": SERVER_STARTED_AT,
                                "runs": discover_runs(self.root)})
                return

            if path == "/status.json":
                payload = read_state(run)
                payload["runs"] = discover_runs(self.root)
                self._json(payload)
            elif path == "/status.txt":
                self._text(one_line(read_state(run)))
            elif path == "/tail.json":
                self._json({"run": run.name,
                            "logs": tail(run, streams.LOGS),
                            "events": tail(run, streams.EVENTS, 20),
                            "build": build_tail(run)})
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


def serve(root: Path, port: int = 871, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start the status daemon over a runs ROOT. Returns it; call shutdown() to stop.

    `root` is normally `bench/runs`, holding many runs, but a single run directory works
    too. Runs are discovered per request, so this can be started before anything exists
    and never needs restarting as work comes and goes.

    Binds to loopback by default; pass host="0.0.0.0" to watch an unattended bench from
    another machine. The page is a convenience - GET /status.json is the contract, and it
    is a plain static document so polling costs nothing.
    """
    handler = type("_BoundHandler", (_Handler,), {"root": Path(root)})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
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
/* Three states, not two: an explicit choice stamps data-theme on the root, and the
   default "system" setting stamps nothing - so the media query has to be guarded, or a
   reader who picks light on a dark OS gets dark anyway. */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1419;
--fg:#e4e8ed;--mut:#9aa5b1;--rule:#242d37;--card:#161c23;--ok:#4fbf97;--bad:#e08a4a;
--warn:#d4b352;--accent:#5aa9db}}
:root[data-theme="dark"]{--bg:#0f1419;--fg:#e4e8ed;--mut:#9aa5b1;--rule:#242d37;
--card:#161c23;--ok:#4fbf97;--bad:#e08a4a;--warn:#d4b352;--accent:#5aa9db}
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
.runs{display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 .7rem}
.tabs{display:flex;gap:.3rem;margin:.2rem 0 .4rem;border-bottom:1px solid var(--rule)}
.tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--mut);
cursor:pointer;font:inherit;font-size:.85rem;padding:.4rem .8rem}
.tab:hover{color:var(--fg)}
.tab[aria-current="true"]{color:var(--fg);border-bottom-color:var(--accent)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
[data-panel][hidden]{display:none}
.bar{height:6px;background:var(--rule);border-radius:3px;overflow:hidden;margin-top:.3rem}
.bar i{display:block;height:100%;background:var(--accent)}
.bar i.over{background:var(--bad)}
.step{border-bottom:1px solid var(--rule);padding:.25rem 0}
.step:last-child{border-bottom:0}
.step>summary{cursor:pointer;display:flex;gap:.55rem;align-items:baseline;
list-style:none;padding:.15rem 0}
.step>summary::-webkit-details-marker{display:none}
.step>summary::before{content:"▸";color:var(--mut);width:.8em;flex:none;
transition:transform .12s}
.step[open]>summary::before{transform:rotate(90deg)}
.step.leaf>summary::before{content:"";}
.step .nm{flex:1;min-width:0}
.step .bud{color:var(--mut);font-variant-numeric:tabular-nums}
.kids{margin:.15rem 0 .35rem 1.6rem;border-left:1px solid var(--rule);padding-left:.7rem}
.kid{display:flex;gap:.55rem;align-items:baseline;padding:.12rem 0;font-size:.82rem}
.mark{width:5.4rem;flex:none;font-size:.68rem;font-weight:600;letter-spacing:.05em;
text-transform:uppercase}
.m-planned{color:var(--mut)} .m-running{color:var(--accent)}
.m-done{color:var(--ok)} .m-skipped{color:var(--mut);opacity:.7}
.m-failed{color:var(--bad)}
.chips{display:flex;flex-wrap:wrap;gap:.3rem;margin:0 0 .5rem}
.chip{background:var(--card);border:1px solid var(--rule);border-radius:999px;
color:var(--mut);cursor:pointer;font:inherit;font-size:.76rem;padding:.15rem .6rem}
.chip[aria-pressed="true"]{border-color:var(--accent);color:var(--fg)}
.chip:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.top{display:flex;align-items:flex-start;gap:1rem}
.top>div{flex:1;min-width:0}
#theme{background:var(--card);border:1px solid var(--rule);border-radius:4px;
color:var(--mut);cursor:pointer;font:inherit;font-size:.78rem;padding:.25rem .6rem;
flex:none;white-space:nowrap}
#theme:hover{color:var(--fg);border-color:var(--rule-strong,var(--mut))}
#theme:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.started{color:var(--mut);font-size:.78rem;font-weight:400;margin-left:.6rem}
.run{background:var(--card);border:1px solid var(--rule);border-radius:4px;
padding:.3rem .6rem;cursor:pointer;font-size:.8rem;color:inherit;display:flex;gap:.5rem;align-items:baseline}
.run:hover{border-color:var(--rule-strong)}
.run[aria-current="true"]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.run small{color:var(--mut)}
.run:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
<div class="wrap">
  <div class=top>
    <div><h1>bench status<span class=started id=started></span></h1></div>
    <button id=theme type=button title="cycle: system, light, dark">theme</button>
  </div>
  <div id="runs" class="runs"></div>
  <p class="sub" id="ident">loading&hellip;</p>
  <nav class="tabs" id="tabs">
    <button class="tab" data-tab="overview" aria-current="true">overview</button>
    <button class="tab" data-tab="schedule">schedule</button>
    <button class="tab" data-tab="devices">devices</button>
  </nav>
  <div class="card wait" id="wait" hidden></div>
  <div data-panel="overview">
  <h2>position</h2>
  <div class="grid" id="pos"></div>
  <h2>rows</h2>
  <div class="card"><table id="rows"><thead><tr><th>row</th><th>verdict</th>
    <th>evidence</th><th>image</th><th>s</th></tr></thead><tbody></tbody></table></div>
  </div>

  <div data-panel="schedule" hidden>
  <h2>planned schedule</h2>
  <p class="sub" id="planline">no schedule yet</p>
  <div class="card" id="plan"></div>
  </div>

  <div data-panel="devices" hidden>
  <h2>devices</h2>
  <p class="sub" id="devnote"></p>
  <div class="card" id="ports"></div>
  <h2>images</h2>
  <div class="card"><table id="imgs"><thead><tr><th>bake</th><th>env</th><th>flash</th>
    <th>ram</th><th>build s</th><th>release-repr</th></tr></thead><tbody></tbody></table></div>
  <h2>nodes</h2>
  <div class="card"><table id="nodes"><thead><tr><th>node</th><th>role</th><th>port</th>
    <th>mode</th><th>state</th><th>pkts</th><th>logs</th></tr></thead><tbody></tbody></table></div>
  </div>

  <div data-panel="overview">
  <h2>capture</h2>
  <div class="card"><table id="cap"><thead><tr><th>stream</th><th>rows</th><th>bytes</th>
    <th>last</th></tr></thead><tbody></tbody></table></div>
  <h2 id="tail-head">tail</h2>
  <div class="chips" id="tailchips"></div>
  <div class="card"><pre id="tail"></pre></div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);

// Theme cycles system -> light -> dark. "system" stamps nothing, which is what lets
// prefers-color-scheme still apply; the other two stamp the root so an explicit choice
// beats the OS in both directions. Kept per browser, and applied before the first paint
// so the page does not flash the wrong ground.
const THEMES = ["system", "light", "dark"];
function applyTheme(name) {
  try { localStorage.setItem("bench-theme", name); } catch (e) { /* private window */ }
  if (name === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", name);
  const btn = $("#theme");
  if (btn) btn.textContent = name === "system" ? "theme: auto" : "theme: " + name;
}
let THEME = "system";
try { THEME = localStorage.getItem("bench-theme") || "system"; } catch (e) { /* ignore */ }
applyTheme(THEME);
$("#theme").onclick = () => {
  THEME = THEMES[(THEMES.indexOf(THEME) + 1) % THEMES.length];
  applyTheme(THEME);
};
// Which run is being shown. null = whichever is most recently active, which is what an
// unattended watcher wants by default: the thing happening now.
let RUN = new URLSearchParams(location.search).get("run");
const q = p => RUN ? `${p}?run=${encodeURIComponent(RUN)}` : p;
let TAB = new URLSearchParams(location.search).get("tab") || "overview";
// Which nodes the tail shows. null means "all", an empty Set means "none" - the two are
// different questions and both are worth being able to ask.
let TAILNODES = null;
// Which <details> the reader has opened, by id. The page redraws on a timer and would
// otherwise collapse a section a second or two after it was opened.
const OPEN = new Set();

function keepOpenState(root) {
  root.querySelectorAll("details[data-id]").forEach(d => {
    if (OPEN.has(d.dataset.id)) d.open = true;
    d.addEventListener("toggle", () => {
      d.open ? OPEN.add(d.dataset.id) : OPEN.delete(d.dataset.id);
    });
  });
}

function showTab(name) {
  TAB = name;
  document.querySelectorAll("[data-panel]").forEach(el => {
    el.hidden = el.dataset.panel !== name;
  });
  document.querySelectorAll(".tab").forEach(b => {
    b.setAttribute("aria-current", String(b.dataset.tab === name));
  });
  const u = new URLSearchParams(location.search);
  u.set("tab", name);
  if (RUN) u.set("run", RUN);
  history.replaceState(null, "", "?" + u.toString());
}
document.querySelectorAll(".tab").forEach(b => {
  b.onclick = () => showTab(b.dataset.tab);
});
showTab(TAB);
const esc = v => String(v ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const cell = v => v === null || v === undefined ? "<span class=k>-</span>" : esc(v);
const secs = v => v === null || v === undefined ? "-" : Math.round(v) + "s";

function rows(id, data, cols) {
  const body = $(id).querySelector("tbody");
  body.innerHTML = data.map(d => "<tr>" + cols.map(c => "<td>" + c(d) + "</td>").join("") + "</tr>").join("")
    || "<tr><td colspan=9 class=k>nothing yet</td></tr>";
}

function drawRuns(runs) {
  const box = $("#runs");
  if (!runs || !runs.length) { box.innerHTML = "<span class=k>no runs yet</span>"; return; }
  box.innerHTML = runs.map(r => {
    const cur = (RUN ? r.name === RUN : r === runs[0]);
    const pos = r.total ? `${r.done}/${r.total}` : (r.stage || "");
    return `<button class=run role=link aria-current="${cur}" data-run="${esc(r.name)}">
      <b>${esc(r.name)}</b>
      <span class="pill ${esc((r.status||"").split(" ")[0])}">${esc(r.status)}</span>
      <small>${esc(pos)}</small></button>`;
  }).join("");
  box.querySelectorAll(".run").forEach(b => b.onclick = () => {
    RUN = b.dataset.run;
    history.replaceState(null, "", `?run=${encodeURIComponent(RUN)}`);
    refresh();
  });
}

async function refresh() {
  let s;
  try { s = await (await fetch(q("status.json"), {cache:"no-store"})).json(); }
  catch (e) { $("#ident").textContent = "status unavailable - is the server still up?"; return; }
  drawRuns(s.runs);
  if (s.runs && !s.runs.length) { $("#ident").textContent = "waiting for a run to appear under " + esc(s.root||""); return; }

  const id = s.identity || {}, p = s.position || {}, c = s.counts || {};
  if (s.server_started_at) {
    const t = new Date(s.server_started_at * 1000);
    const up = Math.max(0, Math.round((Date.now() / 1000) - s.server_started_at));
    const h = Math.floor(up / 3600), m = Math.floor((up % 3600) / 60);
    $("#started").textContent =
      ` · bench process started ${t.toLocaleString()} (up ${h}h${String(m).padStart(2, "0")}m)`;
  }
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
    ["planned", secs(p.planned_total_s), p.over_plan ? "OVER PLAN" : "worst case for this table"],
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

  // --- schedule tab ---------------------------------------------------------
  const plan = s.schedule;
  if (plan && plan.steps) {
    const done = p.elapsed_s || 0, total = plan.total_s || 0;
    const pct = total ? Math.min(100, 100 * done / total) : 0;
    const c = plan.counts || {};
    $("#planline").innerHTML =
      `worst case <b>${secs(total)}</b> (${Math.round(total/60)} min) &middot; ` +
      `elapsed ${secs(done)} &middot; ` +
      `<span class=m-done>${c.done||0} done</span> &middot; ` +
      `<span class=m-skipped>${c.skipped||0} skipped</span> &middot; ` +
      `<span class=m-planned>${c.planned||0} planned</span>` +
      (c.failed ? ` &middot; <span class=m-failed>${c.failed} failed</span>` : "") +
      (p.over_plan ? ' &middot; <span class=FAIL>OVER PLAN</span>' : '') +
      `<div class=bar><i class="${p.over_plan ? 'over' : ''}" style="width:${pct}%"></i></div>`;

    // A skipped step spent none of its budget, so the total is a ceiling. Showing
    // elapsed against budget per step is where that difference becomes readable.
    const timing = st => {
      if (st.elapsed_s == null) return `<span class=bud>${secs(st.budget_s)}</span>`;
      const over = st.overran ? " FAIL" : "";
      return `<span class="bud${over}">${secs(st.elapsed_s)} / ${secs(st.budget_s)}</span>`;
    };
    const mark = st => `<span class="mark m-${esc(st.status)}">${esc(st.status)}</span>`;

    $("#plan").innerHTML = plan.steps.map(st => {
      const kids = (st.children || []);
      const openNow = st.status === "running";
      const body = kids.map(k =>
        `<div class=kid>${mark(k)}<span class=nm>${esc(k.name)}</span>${timing(k)}
         <span class=k>${esc(k.outcome || k.detail || "")}</span></div>`).join("");
      if (openNow) OPEN.add(st.id);
      return `<details class="step${kids.length ? "" : " leaf"}" data-id="${esc(st.id)}"${OPEN.has(st.id) ? " open" : ""}>
        <summary>${mark(st)}<span class=nm>${esc(st.name)}</span>${timing(st)}
          <span class=k>${esc(st.outcome || st.detail || "")}</span></summary>
        ${kids.length ? `<div class=kids>${body}</div>` : ""}
      </details>`;
    }).join("");
    keepOpenState($("#plan"));
  } else {
    $("#planline").textContent = "no schedule recorded for this run";
    $("#plan").innerHTML = "";
  }

  // --- devices tab ----------------------------------------------------------
  const pstate = s.ports || {};
  const anyStale = (s.devices || []).some(d => d.stale);
  $("#devnote").innerHTML = anyStale
    ? "USB presence is live. Port state, reconnects and firmware are what the last run "
      + "recorded and are <b>not current</b> - no run is holding these devices now."
    : "Live, from the run currently holding these devices.";

  $("#ports").innerHTML = (s.devices || []).map(d => {
    // Live presence and remembered port state are different claims, so they are shown
    // as different things. A finished run's last port state is history, not status.
    const presence = d.present
      ? `<span class="mark m-done">present</span>`
      : `<span class="mark m-failed">absent</span>`;
    const recorded = d.recorded_state
      ? (d.stale
          ? `<span class=k>was ${esc(d.recorded_state)}${d.as_of_s ? " " + secs(d.as_of_s) + " ago" : ""}</span>`
          : `<span class="${["gave_up","lost","absent"].includes(d.recorded_state) ? "FAIL" : "PASS"}">${esc(d.recorded_state)}</span>`)
      : `<span class=k>no run has held this device</span>`;

    const board = d.observed_model
      ? (d.board_matches === false
          ? `<span class=FAIL>${esc(d.observed_model)} &mdash; table says ${esc(d.declared_board)}</span>`
          : esc(d.observed_model))
      : `<span class=k>${esc(d.declared_board || "unknown")} (declared; not yet read from the device)</span>`;

    const facts = [
      ["hardware", board],
      ["node id", cell(d.node_id)],
      ["firmware", cell(d.firmware)],
      ["usb serial", cell(d.serial_number)],
      ["port now", cell(d.port)],
      ["role", cell(d.role)],
      ["capture", cell(d.capture)],
      ["policy", (d.never_command ? "never commanded" : "commanded") + ", " +
                 (d.never_flash ? "never flashed" : "flashable")],
      ["port state", recorded],
      ["reconnects", cell(d.reconnects)],
      ["last error", d.last_error ? `<span class=warnrow>${esc(d.last_error)}</span>` : "-"],
    ];
    return `<details class=step data-id="dev:${esc(d.node)}"${OPEN.has("dev:" + d.node) ? " open" : ""}>
      <summary>${presence}<span class=nm><b>${esc(d.node)}</b></span>
        <span class=bud>${esc(d.port || "-")}</span>
        <span class=k>${esc(d.observed_model || d.declared_board || "")}</span></summary>
      <div class=kids>${facts.map(([k, v]) =>
        `<div class=kid><span class="mark m-planned">${k}</span><span class=nm>${v}</span></div>`
      ).join("")}</div></details>`;
  }).join("") || "<span class=k>no devices</span>";
  keepOpenState($("#ports"));

  const cap = (s.capture||{}).streams || {};
  rows("#cap", Object.entries(cap).map(([k,v]) => ({name:k, ...v})), [
    d => esc(d.name), d => cell(d.rows), d => cell(d.bytes),
    d => d.age_s == null ? "<span class=k>never</span>"
         : (d.age_s > 120 ? `<span class=FAIL>${secs(d.age_s)} ago</span>` : secs(d.age_s) + " ago")]);

  try {
    const t = await (await fetch(q("tail.json"), {cache:"no-store"})).json();
    const b = t.build || {};
    // While an image is compiling there is nothing in the firmware streams; the build
    // log is the only thing that shows the run is still moving.
    if (p.stage === "1-build" && (b.lines||[]).length) {
      $("#tail-head").textContent = `build log · ${b.bake_hash} · ${b.total_lines} lines · ${b.bytes} bytes`;
      $("#tailchips").innerHTML = "";
      $("#tail").textContent = b.lines.join("\\n");
    } else {
      $("#tail-head").textContent = "tail";
      const logs = t.logs || [];
      const nodes = [...new Set(logs.map(r => r.node).filter(Boolean))].sort();
      const on = n => TAILNODES === null || TAILNODES.has(n);
      $("#tailchips").innerHTML =
        `<button class=chip data-pick="all" aria-pressed="${TAILNODES===null}">all</button>` +
        `<button class=chip data-pick="none" aria-pressed="${TAILNODES!==null && TAILNODES.size===0}">none</button>` +
        nodes.map(n => `<button class=chip data-pick="${esc(n)}" aria-pressed="${on(n)}">${esc(n)}</button>`).join("");
      $("#tailchips").querySelectorAll(".chip").forEach(btn => btn.onclick = () => {
        const pick = btn.dataset.pick;
        if (pick === "all") TAILNODES = null;
        else if (pick === "none") TAILNODES = new Set();
        else {
          if (TAILNODES === null) TAILNODES = new Set(nodes);
          TAILNODES.has(pick) ? TAILNODES.delete(pick) : TAILNODES.add(pick);
        }
        refresh();
      });
      const shown = logs.filter(r => on(r.node));
      $("#tail").textContent = shown.slice(-25)
        .map(r => `${(r.node||"-").padEnd(9)} ${(r.line||r.msg||"").slice(0,150)}`).join("\\n")
        || (TAILNODES && TAILNODES.size === 0 ? "no devices selected" : "no lines yet");
    }
  } catch (e) { $("#tail").textContent = "tail unavailable"; }
}
refresh(); setInterval(refresh, 3000);
</script>
"""
