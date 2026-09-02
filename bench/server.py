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
from typing import Any, Sequence

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
        "rows": _rows(results, state.get("attempt_started_at"),
                      state.get("pending_retry", [])),
        "carried_over": state.get("carried_over", 0),
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


def _rows(
    results: dict,
    attempt_started_at: float | None = None,
    pending_retry: Sequence[str] = (),
) -> list[dict]:
    out = []
    pending = set(pending_retry or ())
    for scenario_id, row in results.items():
        out.append(
            {
                "id": scenario_id,
                # A row queued for a retry shows as planned, not as the verdict its
                # last attempt left behind - that verdict is about to be replaced and
                # was never a statement about the run in front of the reader.
                "verdict": "PLANNED" if scenario_id in pending else row.get("verdict"),
                "pending_retry": scenario_id in pending,
                "previous_verdict": row.get("verdict") if scenario_id in pending else None,
                "error": None if scenario_id in pending else row.get("error"),
                "release_representative": row.get("release_representative", True),
                # True when this verdict was banked by an EARLIER attempt at this run id.
                # Resuming keeps finished rows, so without this a stale verdict reads as
                # something the run in front of you just measured.
                "carried_over": bool(
                    attempt_started_at and (row.get("ended_at") or 0) < attempt_started_at
                ),
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


def tail_sources(run_dir: Path, rows: list[dict]) -> dict:
    """Per-device: how many lines are in the window, and why there are none.

    A device with nothing in the tail looks broken, and on this bench it usually is not:
    it is in DFU, or leased to a flash, and has nothing to say. Silence that cannot
    explain itself is the same defect as an assertion that passes on no evidence - so the
    tail carries each device's last known port state alongside its line count.
    """
    counts: dict[str, int] = {}
    for row in rows:
        name = row.get("node")
        if name:
            counts[name] = counts.get(name, 0) + 1

    # Last port_state event per node is the authoritative account of where a device is.
    # Bounded: this runs on every poll, and a long run's event stream is tens of
    # thousands of rows - rescanning all of it three times a minute to learn where three
    # devices are is not a trade worth making.
    states: dict[str, dict] = {}
    for ev in tail(run_dir, streams.EVENTS, limit=600):
        name = ev.get("node")
        if not name:
            continue
        if ev.get("kind") == "port_state":
            states[name] = {"state": ev.get("now"), "why": ev.get("why"), "ts": ev.get("ts")}
        elif ev.get("kind") in ("flash_start", "enter_dfu", "dfu_via"):
            states.setdefault(name, {})["last_flash_event"] = ev.get("kind")

    out = {}
    for name in sorted(set(counts) | set(states)):
        info = dict(states.get(name) or {})
        info["lines"] = counts.get(name, 0)
        out[name] = info
    return out


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
                self._html(page())
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
                logs = tail(run, streams.LOGS)
                self._json({"run": run.name,
                            "logs": logs,
                            "events": tail(run, streams.EVENTS, 20),
                            "sources": tail_sources(run, logs),
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


# The dashboard lives beside this module as an ordinary .html file rather than inside a
# Python string. Two reasons, both learned the hard way:
#
#   A formatter can reach it. prettier lints the HTML, the CSS and the JS; inside a
#   string they were unlintable, and a stray brace once served a blank page with the
#   error visible only in the browser console.
#
#   Escapes stop being written twice. A backslash in that string had to survive Python
#   before it reached CSS or JS, and one that did not turned a disclosure triangle into
#   the literal text 25B8.
#
# Read per request, keyed on mtime, so editing the page needs no server restart - which
# matters most for a daemon that is deliberately long-lived.
_PAGE_PATH = Path(__file__).with_name("dashboard") / "page.html"
_page_cache: tuple[float, str] | None = None


def page() -> str:
    """The dashboard's HTML, current as of the last time it changed on disk."""
    global _page_cache
    try:
        stamp = _PAGE_PATH.stat().st_mtime
    except OSError:
        return (
            "<!doctype html><title>bench status</title>"
            f"<p>the dashboard page is missing from {_PAGE_PATH}"
        )
    if _page_cache is None or _page_cache[0] != stamp:
        _page_cache = (stamp, _PAGE_PATH.read_text(encoding="utf-8"))
    return _page_cache[1]
