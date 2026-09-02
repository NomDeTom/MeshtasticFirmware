"""Command line entry point: python -m bench <command>.

Commands are deliberately separable, matching the stages, because each is independently
runnable and resumable and a stage-4 failure must never force a stage-1 repeat.

  nodes      what is plugged in right now
  preflight  stage -1, the checks that refuse a run that cannot produce evidence
  build      stage 1 only, so images can be compiled well ahead of hardware time
  run        the whole thing, resuming any rows already recorded
  serve      the read-only status server
  status     the one-line summary for a terminal or a notification
  ledger     render a finished run's packet ledger
  capture    hold the nodes open and record, with no scenario at all
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path

from . import devices, ledger as ledger_mod, observer as observer_mod
from . import platform_probe, preflight, runner, scenario, server, streams

# Run artifacts default to LOCAL storage, deliberately not the repo.
#
# Flashing re-enumerates the USB bus, and a checkout can live on an external USB drive -
# this one does. A run writing its evidence there is recording onto a bus its own
# activity disturbs, which showed up mid-run as WinError 433 and killed the run on a
# single failed write. BENCH_RUNS_ROOT overrides; the repo path is used only if no local
# home directory can be found.
def _default_run_root() -> Path:
    override = os.environ.get("BENCH_RUNS_ROOT")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    try:
        if home.is_dir():
            return home / "bench-runs"
    except OSError:
        pass
    return Path("bench/runs")


DEFAULT_RUN_ROOT = _default_run_root()
DEFAULT_NODES = Path("bench/nodes.json")


def load_nodes(path: Path) -> list[devices.BenchNode]:
    """Node table from JSON. Roles carry their own policy, so this stays declarative."""
    if not path.exists():
        raise SystemExit(
            f"no node table at {path}. Write one, or run `python -m bench nodes --write {path}` "
            "to seed it from what is currently plugged in."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return [devices.BenchNode(**entry) for entry in data["nodes"]]


def load_scenarios(dotted: str) -> list[scenario.Scenario]:
    """Import a scenario module and take its SCENARIOS list."""
    module = importlib.import_module(dotted)
    scenarios = getattr(module, "SCENARIOS", None)
    if not scenarios:
        raise SystemExit(f"{dotted} defines no SCENARIOS")
    return list(scenarios)


# -- commands -------------------------------------------------------------------


def cmd_nodes(args: argparse.Namespace) -> int:
    found = devices.enumerate_devices(all_devices=args.all)
    if not found:
        print("no serial devices found")
        return 1
    print(f"{'port':<8} {'vid':<8} {'pid':<8} {'serial':<20} description")
    for d in found:
        print(
            f"{d.port:<8} {d.vid_hex or '-':<8} {d.pid_hex or '-':<8} "
            f"{d.serial_number or '-':<20} {d.description or ''}"
        )
    if args.write:
        # Seed a table rather than guessing roles: the first two are commandable, and any
        # third defaults to the passive observer, which is the role most easily forgotten
        # and the one whose value is destroyed by a single stray command.
        roles = ["dut", "peer", "observer", "exciter"]
        table = {
            "nodes": [
                {
                    "name": roles[i] if i < len(roles) else f"node{i}",
                    "serial_number": d.serial_number,
                    "role": roles[i] if i < len(roles) else "peer",
                }
                for i, d in enumerate(found)
                if d.serial_number
            ]
        }
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table, indent=2), encoding="utf-8")
        print(f"\nwrote {path} - check the roles before running anything")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    nodes = load_nodes(Path(args.nodes)) if Path(args.nodes).exists() else []
    report = preflight.run_preflight(nodes=nodes, firmware_root=Path(args.firmware_root))
    print(report.summary())
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    return 1 if report.blocked else 0


def cmd_build(args: argparse.Namespace) -> int:
    from . import builder, manifest as manifest_mod

    info = platform_probe.probe()
    if not info.pio:
        raise SystemExit("PlatformIO not found; run `python -m bench preflight`")
    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)

    scenarios = load_scenarios(args.scenarios)
    wanted = [(s.id, role, rb.bake) for s in scenarios for role, rb in s.roles.items()]
    mf = manifest_mod.Manifest(run_dir / "manifest.json")
    b = builder.Builder(
        root=Path(args.firmware_root),
        pio=info.pio,
        manifest=mf,
        on_event=lambda k, d: print(f"[{k}] {json.dumps(d, default=str)[:200]}", flush=True),
    )
    outcome = b.build_all(wanted, force=args.force)
    print(json.dumps(outcome, indent=2))
    return 1 if outcome["failed"] else 0


def cmd_run(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args)
    config = runner.RunConfig(
        run_dir=run_dir,
        firmware_root=Path(args.firmware_root),
        nodes=load_nodes(Path(args.nodes)),
        scenarios=load_scenarios(args.scenarios),
        operator_note=args.note,
        skip_flash=args.skip_flash,
        skip_provision=args.skip_provision,
        only=args.only or [],
    )
    r = runner.Runner(config)
    http = None
    if args.serve:
        # If a daemon is already watching this runs root, use it rather than binding a
        # second one - the durable server is the point, and two of them is a footgun.
        import threading
        import urllib.request

        already = False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{args.port}/runs.json", timeout=2)
            already = True
        except Exception:  # noqa: BLE001 - nothing listening is the normal case
            pass
        if already:
            print(f"status: http://127.0.0.1:{args.port}/?run={run_dir.name}"
                  "  (existing daemon)", flush=True)
        else:
            http = server.serve(run_dir.parent, port=args.port)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            print(f"status: http://127.0.0.1:{args.port}/?run={run_dir.name}"
                  "  (read-only)", flush=True)
    try:
        summary = r.run()
    finally:
        if http is not None:
            http.shutdown()
    print(summary["line"])
    counts = summary["counts"]
    return 1 if (counts.get("FAIL") or counts.get("INVALID")) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    """One long-lived daemon over the runs root.

    Start it once and leave it. Runs are discovered per request, so it can be started
    before anything exists and never needs restarting as builds and runs come and go -
    which is the whole point for headless and unattended work.
    """
    root = Path(args.root) if args.root else (
        Path(args.run_dir) if args.run_dir else DEFAULT_RUN_ROOT
    )
    root.mkdir(parents=True, exist_ok=True)
    http = server.serve(root, port=args.port, host=args.host)
    shown = "0.0.0.0" if args.host == "0.0.0.0" else args.host
    print(f"bench status: http://{shown}:{args.port}/   (read-only, watching {root})")
    for run in server.discover_runs(root):
        print(f"  {run['name']:16} {run['status']:9} {run.get('stage') or ''}")
    if not server.discover_runs(root):
        print("  (no runs yet - they appear here as soon as one starts)")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        http.shutdown()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = server.read_state(_run_dir(args))
    if args.json:
        print(json.dumps(state, indent=2, default=str))
    else:
        print(server.one_line(state))
    return 0 if state["status"] in (server.RUNNING, server.FINISHED) else 1


def cmd_ledger(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args)
    led = (
        ledger_mod.Ledger.for_scenario(run_dir, args.scenario)
        if args.scenario
        else ledger_mod.Ledger.from_run(run_dir)
    )
    print(led.render(limit=args.limit))
    print()
    print(json.dumps(led.summary(), indent=2, default=str))
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Hold every node open and record, with no scenario.

    Useful on its own: it is the only way to watch what the mesh does between runs, and
    it is how the continuous-capture path gets exercised without spending a build.
    """
    run_dir = _run_dir(args)
    nodes = load_nodes(Path(args.nodes))
    rec = streams.Recorder(run_dir)
    obs = observer_mod.Observer(rec, nodes)
    report = obs.start()
    print(json.dumps(report, indent=2))
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(5.0)
            status = obs.status()
            live = {n: f"{v['packets']}p/{v['log_lines']}l" for n, v in status["nodes"].items()}
            print(f"  {int(deadline - time.monotonic()):>5}s left  {live}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        obs.stop()
        rec.close()
    print(json.dumps(rec.status(), indent=2, default=str))
    return 0


def _run_dir(args: argparse.Namespace) -> Path:
    return Path(args.run_dir) if args.run_dir else DEFAULT_RUN_ROOT / args.run


# -- parser ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="scripted hardware test bench")
    p.add_argument("--run", default="latest", help="run name under bench/runs")
    p.add_argument("--run-dir", default=None, help="explicit run directory")
    p.add_argument("--nodes", default=str(DEFAULT_NODES), help="node table JSON")
    p.add_argument("--firmware-root", default=".", help="firmware checkout")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("nodes", help="list connected devices")
    s.add_argument("--all", action="store_true", help="include non-Meshtastic vendors")
    s.add_argument("--write", default=None, help="seed a node table at this path")
    s.set_defaults(func=cmd_nodes)

    s = sub.add_parser("preflight", help="stage -1 checks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("build", help="stage 1: build every distinct bake")
    s.add_argument("--scenarios", default="bench.scenarios.lbt")
    s.add_argument("--force", action="store_true", help="rebuild even if present")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("run", help="the whole run, resuming recorded rows")
    s.add_argument("--scenarios", default="bench.scenarios.lbt")
    s.add_argument("--note", default="", help="operator note recorded into the run")
    s.add_argument("--only", nargs="*", help="run only these scenario ids")
    s.add_argument("--skip-flash", action="store_true")
    s.add_argument("--skip-provision", action="store_true")
    s.add_argument("--serve", action="store_true",
                   help="serve status, reusing a running daemon if there is one")
    s.add_argument("--port", type=int, default=8730)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="read-only status daemon over all runs")
    s.add_argument("--root", default=None,
                   help=f"runs root to watch (default {DEFAULT_RUN_ROOT})")
    s.add_argument("--port", type=int, default=8730)
    s.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to watch an unattended bench from another machine")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="one-line summary")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("ledger", help="render a run's packet ledger")
    s.add_argument("--scenario", default=None)
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(func=cmd_ledger)

    s = sub.add_parser("capture", help="hold nodes open and record, no scenario")
    s.add_argument("--seconds", type=float, default=60.0)
    s.set_defaults(func=cmd_capture)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except platform_probe.UnsupportedPlatform as exc:
        print(f"refusing to run here:\n  {exc}", file=sys.stderr)
        return 2
    except preflight.PreflightFailed as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
