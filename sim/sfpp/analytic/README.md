# Analytic and Monte-Carlo tools - the layer beneath the transport simulator

Brought across from a research vault on 2026-08-17. These predate `sim/sfpp/mesh.py` and model the
same questions **without a radio**: closed-form and Monte-Carlo cost of a catch-up strategy, and how
often advertising is worth it. They are retained rather than retired because they are an _independent_
implementation, and independence is what has caught the real bugs in this work.

| File                 | What it does                                                                       |
| -------------------- | ---------------------------------------------------------------------------------- |
| `model.py`           | Closed-form expected cost of each catch-up strategy, from the wire constants       |
| `simulate.py`        | Monte-Carlo over the same question - **written to disagree with `model.py`**       |
| `advertising.py`     | How often advertising pays, and whether a cheap probe then escalate beats a sketch |
| `radio.py`           | Airtime, computed twice by different routes so the two can be compared             |
| `validate.py`        | Cross-checks between the above, so no single implementation is trusted alone       |
| `parse_packetlog.py` | Per-packet facts out of a `meshtastic-compression-test` capture                    |

**Status: superseded as the primary simulator, retained as a cross-check.** They model an abstract
transport - independent per-node loss, no contention, no queue - which is exactly the simplification
`mesh.py` exists to remove. Any cost figure from here is a lower bound on a real mesh.

**Why they are kept.** Every serious error in this campaign was caught by two implementations
disagreeing, not by inspection: the transport's backoff cap surfaced when the 2.8 fold-in disagreed
with `mesh.py`, and `pinsketch.py` is checked against compiled C++ by `check_oracle.py` for the same
reason. A closed-form model that disagrees with a Monte-Carlo one is the same tactic applied a layer
lower down.

Run from `sim/`:

```
python3 -m sfpp.analytic.model
python3 -m sfpp.analytic.simulate
python3 -m sfpp.analytic.validate
```
