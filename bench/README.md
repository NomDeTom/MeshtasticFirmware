# Hardware Test Bench - Operating Guide

This directory contains a scripted bench that flashes real nodes, puts them into a known
state, exercises them over the air, and reaches a verdict from what was captured. It is a
**counting instrument, not a timing one**: firmware uptime is printed in whole seconds and
two nodes share no clock, so every assertion is expressed as a count or a rate over
trials. Anything that would need sub-second correlation between devices is not assertable
here and must not be written as though it were.

It is Python, runs on the host, and is independent of the native C++ suite in
[`test/`](../test/README.md).

## Running

```bash
python -m bench nodes                        # what is plugged in
python -m bench nodes --write bench/nodes.json   # seed a node table from it
python -m bench preflight                    # stage -1 only; prints the bus it found
python -m bench --run smoke run --scenarios smoke   # a full run
python -m bench serve --port 8730 --host 0.0.0.0    # status daemon over every run
python -m bench status                       # one line, for a terminal or a notification
```

`--scenarios` takes a bare table name from [`scenarios/`](scenarios/) (`smoke`, `lbt`) or a
full dotted path to one kept elsewhere. `--only S1 S2` restricts a run to named rows;
`--skip-flash` and `--skip-provision` skip prep wholesale.

Runs are written to `~/bench-runs/<id>` (`BENCH_RUNS_ROOT` overrides). Known-good firmware
lives in `~/bench-firmware` (`BENCH_FIRMWARE_ROOT`). Neither is in the checkout: images are
large, they are not source, and a bench whose checkout sits on removable storage should
not lose its reference firmware with the drive.

**Commission a bench before its first run.** The smoke row builds on an upstream release
rather than a local compile, and the store starts empty:

```bash
python -m bench firmware fetch nrf52_promicro_diy_tcxo           # newest stable
python -m bench firmware fetch nrf52_promicro_diy_tcxo --alpha   # newest prerelease
python -m bench firmware list
```

**Start with the smoke row.** `S1-two-nodes-talk` asserts nothing about the firmware -
every check in it is about the bench. If it is green the machinery is sound, and a red row
from a real scenario afterwards is about the firmware rather than the harness.

## Four verdicts, not two

|                | meaning                                                              |
| -------------- | -------------------------------------------------------------------- |
| `PASS`         | the assertion held                                                   |
| `FAIL`         | it did not                                                           |
| `NOT OBSERVED` | nothing was seen, and nothing rules out the instrument as the reason |
| `INVALID`      | the bench failed to take a measurement at all                        |

**`INVALID` is not a failure of the firmware.** It is the bench reporting that it cannot
speak to the question - a flash that did not land, a capture that stalled, an assertion
that needs a capability the flashed image does not have. It never satisfies a resume, and
a row carrying it is re-run rather than reported.

**Silence is not evidence.** An `at_most` check against a node that produced no log lines
at all returns `INVALID`, not `PASS`: "I saw none" and "I saw nothing" are different
claims, and one of them is the instrument being deaf.

## Rules the code enforces

**Only `ports.py` may open a device.** A serial port is exclusive and the client library's
`connect()` can block indefinitely, so every bounded open abandons a thread that still
holds the handle. Two openers on one device is a race with no winner - it produced four
separate failures that all looked like hardware. A test greps for this; do not work around
it.

**Only whoever opened a connection may close it.** Closing someone else's leaves a handle
in a thread they know nothing about, and the port then locks out the operation that
legitimately owns the device. Callers name themselves to `release()`. The one case that
must close a connection it did not open - a config read-back, because the client answers
reads from its own cache - is `drop_cached_connection()`, named rather than quietly
defeating the check.

**Nothing may take a node that was deliberately sent away.** A flash gives up its lease the
moment it commands DFU, because the handle must be abandoned rather than closed on a
device that is already leaving. The device stays off limits for a caller-sized window
afterwards; only the operation waiting for it may bring it back.

**Never touch a node that is not answering.** A node in its bootloader cannot respond, and
repeatedly touching it is the most likely way to lose it for good. A node already in DFU
is finished through its standing volume, never commanded into one it is already in.

**Whatever puts a node into DFU is responsible for getting it out.** A flash that gives up
part way leaves hardware stranded, and every later row then correctly refuses to touch it -
so one bad flash costs the whole matrix.

**Every operation has a budget and an exit state.** `OK`, `TIMED_OUT`, `ABSENT`, `BUSY`,
`REFUSED`, `FAILED`. The run schedule is the sum of them, so an unattended bench has a
knowable end rather than an open-ended one. A step running past its budget reports
`overdue`, which is not yet failed but is no longer on schedule.

**A run id names a workspace, not an attempt.** Re-running one resumes it: rows with a real
verdict are kept, and what each node was left in is carried forward so a retry confirms
rather than reapplies. Confirmation is seconds; prep is about a minute a node to flash and
three to provision.

**Each row records entry and exit conditions.** Entry says what had to be true and how it
came about - `satisfied` (found already true) or `established` (the bench did the work).
Exit asks whether the instrument survived the measurement: whether every node was still
being captured when the window closed. A row whose assertions pass but whose exit
conditions do not is `INVALID` - it did not measure the firmware badly, it failed to
measure it.

## Layout

The stage sequence lives entirely in `runner.py`; each stage delegates to the module that
owns that kind of work. A stage never reaches into another stage's module.

| stage         | module                                   |
| ------------- | ---------------------------------------- |
| `0-preflight` | `preflight.py`                           |
| `1-build`     | `builder.py`, `manifest.py`              |
| `2-flash`     | `flasher.py`, `firmware.py`              |
| `3-provision` | `provision.py`                           |
| `4-execute`   | `scenario.py`, `ledger.py`, `packets.py` |

Used by every stage, and owned by none: `ports.py` (the sole port opener, leases, the run
schedule), `observer.py` (continuous capture, spanning stages rather than starting and
stopping with them), `devices.py`, `hardware.py`, `platform_probe.py`, `streams.py`,
`server.py`, `cli.py`.

Scenario tables are in `scenarios/`. The dashboard is `dashboard/page.html` - an ordinary
HTML file so a formatter can lint it, read per request so editing it needs no restart of
the daemon.

**Phase names are defined by the component that does the work.** `flasher.PHASES`,
`provision.PHASES`, `preflight.PHASES`; the planner reads them from there. When those names
lived in two places they drifted, and a schedule step addressed by a name that no longer
matched could never be marked - every sub-step read "planned" for the whole run.

## Tests

```bash
python -m unittest discover -s bench/tests -t .
```

No hardware required. Most of these tests exist because their absence produced a
misleading result on real hardware, and each names the failure it prevents.

## Host notes

**Windows and native Linux are supported; WSL is refused.** Preflight blocks on it rather
than degrading quietly - USB passthrough there is not the same instrument.

**`trunk fmt` does not run on a checkout on exFAT.** It fails creating a symlink at
`.trunk/out` and does nothing. Use `npx prettier@3.9.6 --write bench/dashboard/page.html`
for the page.

**`uhubctl` is optional and worth having.** It is the only rung that recovers a node
answering nothing without someone walking to the bench. Preflight warns when it is absent.

**A nice!nano keeps the same USB PID in its bootloader as in its application**, so a PID
cannot tell the two apart. A bootloader exposes mass storage beside its serial interface
and an application does not - a device presenting both is in DFU, which is what the
preflight bus inventory reports.
