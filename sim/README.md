# `sim/` - the Meshtastic mesh simulator and the SF++ set-reconciliation campaign

Operating manual for the research simulator under `sim/`. This documents a **tool**, not a firmware
feature: the repository's house rule (no design, API or wire-format documents in the tree) still
holds, and anything about what SF++ _is_ belongs in the docs repo. What follows is how to drive the
thing.

`sim/meshtasticator/` is vendored upstream and carries its own README.

---

## 1. What it is, and what it is not

A discrete-event simulator for a Meshtastic mesh, with the SF++ archive protocols running on top as
real traffic. Three layers, and the separation matters when reading a result:

| Layer           | File                                | What it decides                                                                                                           |
| --------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Physics**     | vendored `meshtasticator/lib/`      | who can hear whom, how long a packet holds the channel, whether a marginal link decodes                                   |
| **Transport**   | `sfpp/mesh.py`                      | the firmware's MAC and routing: CAD, TX queue, contention window, duplicate suppression, next-hop routing, hop accounting |
| **Application** | `sfpp/campaign.py`, `sfpp/chain.py` | the archive protocols - set reconciliation, and the chain walk it aims to replace                                         |

**Exactly three things are imported from the vendored tree**: `lib.phy` (path loss, airtime),
`lib.config` (presets, regions), `lib.radio_loss` (the SNR-to-PER curve). All physics. The vendored
tree's own discrete-event simulator - `discrete_event_sim.py`, `mac.py`, `node.py`, `packet.py` - is
**upstream's, models roughly 2.1-era behaviour, and is never called by anything here.** If you are
looking for firmware behaviour, it is in `sfpp/mesh.py` and nowhere else.

**It is not** regulatory evidence, a substitute for hardware, or a model of terrain, clutter or
mobility. Duty cycle is not enforced: airtime figures are what the protocol _asks for_, not what a
region permits. **§10 is the full list of what is simplified, assumed and absent, and every result
from this tool is bounded by it.**

### The one gap worth knowing before you read any result

**There is no client hydration path.** Archives accumulate messages and reconcile with each other, and
nothing models a client asking a server for what it missed. So `held` and `union` are what an archive
_has_, not what a user _gets_. The only measured end-user gain is bystander pickup - a node overhearing
a replayed object and filing it via the replay header.

---

## 2. Quick start

Nothing needs installing. The simulator and its tests are standard library only, as are the three
vendored modules they import. `requirements.txt` lists two optional extras - matplotlib for the
charts, pytest for a shorter test run - and the code degrades rather than fails without either.

```bash
cd sim
python3 -m unittest discover -s sfpp -t . -p 'test_*.py'   # a gate, not a formality
python3 -m pytest sfpp -q                                  # the same suite, shorter output

# one scenario. JSON, statistics report and charts are all written by the run itself
python3 -m sfpp.campaign --hours 24 --seed 1 --protocol sr --out /tmp/r/run.json

# a swept arm, three seeds; same three outputs per block
python3 -m sfpp.sweep --list
python3 -m sfpp.sweep --block Q-protocol --seeds 3 --seed-base 990001 --out /tmp/r

# long work that must survive the shell that started it
./run-blocks.sh /tmp/r 440001 R-oversubscribed R-congestion-input R-srretries
./run-blocks.sh --status /tmp/r
```

A run leaves three things beside each other, and needs no post-processing step to be readable:

```
/tmp/r/run.json            the full report
/tmp/r/reports/run.txt     the per-portnum statistics, text marked and first
/tmp/r/figures/run.png     the charts, footered with commit, seed and duration
```

---

## 3. Operating functions

| Module            | Run as                 | Purpose                                                                           |
| ----------------- | ---------------------- | --------------------------------------------------------------------------------- |
| `campaign.py`     | `-m sfpp.campaign`     | one scenario end to end; writes a JSON report                                     |
| `sweep.py`        | `-m sfpp.sweep`        | a named block: one arm, several values, shared seeds                              |
| `report.py`       | `-m sfpp.report`       | per-portnum statistics as distributions, text marked and first                    |
| `analyse.py`      | `-m sfpp.analyse`      | markdown tables from saved JSON, re-tabulated without re-running                  |
| `autochart.py`    | (automatic)            | charts rendered by the run that produced the data                                 |
| `tuning.py`       | `-m sfpp.tuning`       | recommended values with evidence, confidence, and what would overturn each        |
| `figures3.py`     | `-m sfpp.figures3`     | the campaign's set-piece figures (mesh shapes, protocol comparison, coverage gap) |
| `experiment.py`   | `-m sfpp.experiment`   | one-off comparisons that are not worth a named block                             |
| `diagram.py`      | `-m sfpp.diagram`      | draws a mesh's link graph, for checking a topology rather than a result          |
| `check_oracle.py` | `-m sfpp.check_oracle` | compiles `PinSketch.cpp` and diffs it against `pinsketch.py`                      |
| `knowledge.py`    | (library)              | per-node NodeDB state, partitions, stale beliefs                                  |
| `analytic/`       | `-m sfpp.analytic.*`   | pre-transport closed-form and Monte-Carlo models, kept as a cross-check           |
| `run-blocks.sh`   | `./run-blocks.sh`      | detached runner: `setsid`, a lock, a manifest, and a test gate                    |

**Both `campaign.py` and `sweep.py` write their own JSON, statistics report and charts.** That is
deliberate: an unattended or remote run has to leave a complete, readable result behind without
anyone remembering a second command, and its stdout is not always kept. `report.py` and `analyse.py`
remain for re-tabulating saved JSON without re-running it, not for making a run readable in the
first place.

`figures3.py` is the exception - it draws from runs pinned by name (`Q-protocol-hours-48.json`), so
it takes `--runs` and `--out` and skips any figure whose run is not present.

`python3 -m sfpp.sweep --list` prints the 66 named blocks.

---

## 4. Every parameter

### 4.1 Mesh shape and size

| Flag                     | Default          | Meaning                                                                                                               |
| ------------------------ | ---------------- | --------------------------------------------------------------------------------------------------------------------- |
| `--nodes`                | 60               | node count                                                                                                            |
| `--area`                 | 8000             | side of the placement square, metres                                                                                  |
| `--scale-area`           | off              | grow the area as √(n/60) so **density is held constant**. Without it, a size sweep measures density and calls it size |
| `--topology`             | `uniform`        | see §5                                                                                                                |
| `--router-fraction`      | 0.1              | share promoted to ROUTER, chosen by degree                                                                            |
| `--router-late-fraction` | 0.0              | share as ROUTER_LATE                                                                                                  |
| `--client-base-fraction` | 0.0              | share as CLIENT_BASE                                                                                                  |
| `--role-mix`             | `legacy-default` | named role census, e.g. `baymesh-2026-08`                                                                             |
| `--platform-mix`         | `uniform`        | board mix; decides each node's hot-store size. Inert unless `--max-num-nodes` is left unset          |
| `--siting-mix`           | `uniform`        | where nodes physically are, as a per-node gain offset. **Assumed, not measured** - see §10          |
| `--favourite-routers`    | off              | router-like nodes favourite each other, so relays between them keep their hop limit                                   |

### 4.2 Hop limits

| Flag                               | Default      | Meaning                                                                                                                                                 |
| ---------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--hop-limit`                      | 3            | one limit for every node                                                                                                                                |
| `--hop-spread` / `--no-hop-spread` | **on**       | per-node limits 3-7                                                                                                                                     |
| `--hop-assign`                     | `centrality` | `centrality` is realistic (edge nodes raise theirs) but confounds hop limit with position; `random` is the control that isolates the limit's own effect |

### 4.3 Radio and firmware behaviour

| Flag                        | Default      | Meaning                                                                                          |
| --------------------------- | ------------ | -------------------------------------------------------------------------------------------------- |
| `--preset`                  | `LONG_FAST`  | modem preset. **Changes reception, not just airtime** - see §6                                   |
| `--profile`                 | `2.8`        | which release series' rules to obey: `2.4` … `2.8`, or `legacy`. See §9.1                        |
| `--old-profile`             | `legacy`     | the rules the `--legacy-fraction` share runs instead. Inert at `--legacy-fraction 0`             |
| `--legacy-fraction`         | 0.0          | share of nodes on `--old-profile`, drawn at random not by degree                                 |
| `--profile-flag NAME=VALUE` | -            | override one rule, repeatable. A specific pathology lives here rather than as a fake version     |
| `--rebroadcast-mode`        | `ALL`        | `ALL`, `ALL_SKIP_DECODING`, `LOCAL_ONLY`, `KNOWN_ONLY`, `CORE_PORTNUMS_ONLY`, `NONE`             |
| `--max-num-nodes`           | 120          | modelled `MAX_NUM_NODES`. Sizes the hot store **and** bounds the congestion input                |
| `--warm-num-nodes`          | from board   | `WARM_NODE_COUNT`: identities kept for peers evicted from hot, so a DM still encrypts. 0 disables |
| `--signature-policy`        | `COMPATIBLE` | `config.security.packet_signature_policy` on receive: `COMPATIBLE`, `BALANCED`, `STRICT`         |
| `--traceroute-per-hour`     | 0.0          | route discoveries per node per hour - what seeding next-hop routing costs                        |
| `--trace-interval-s`        | 0            | sample every adaptive quantity per node this often and keep the series; 0 disables               |
| `--no-adopt-hop-recommendation` | off      | compute the hop recommendation but do not apply it, as the control                               |
| `--dm-transport`            | `hop-by-hop` | `transport` routes an addressed SR message through next-hop routing and its retry ladder         |
| `--dm-mode`                 | `directed-with-late-flood` | how a DM escalates. Inert unless `--dm-transport transport`                         |
| `--coding-rate-ladder`      | off          | raise the coding rate on each retransmission. Not in any release                                 |
| `--extra-repeats`           | off          | tolerate a second heard copy before cancelling our own rebroadcast. Not in any release           |
| `--congestion-mode`         | `adaptive`   | recompute the broadcast throttle per node from its own online count, or one mesh-wide value      |
| `--no-phy-loss`             | off          | disable the empirical SNR-to-PER curve                                                           |

**On `--profile`.** Each value is a **release series taken at its final release**, dated by walking
the firmware's own tags. `2.8` is this tree, read line by line; `2.4` through `2.7` turn off the
behaviours that arrived after that series. `legacy` is **not a firmware version** - it is this
simulator's own pre-fold-in transport, kept so a result can be attributed to a rule change rather
than to the rewrite around it. §9.1 has the full register and §10.2 what each profile is confident
about.

Useful individual flags: `clamp_cw=true` restores the unclamped Arduino `map()` contention window,
`router_cw_floor=true` the old router-pinned window, `max_backoffs=400` the defect that discarded two
thirds of rebroadcast attempts.

### 4.4 Offered load

| Flag                                          | Default       | Meaning                                                                                                                                      |
| --------------------------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `--broadcast-interval-s`                      | per-class mix | one interval for every device class. **This is the denominator every airtime share is quoted against**                                       |
| `--diurnal`                                   | `commuter`    | `flat`, `sinusoid`, `commuter` (17:1 peak-to-trough). Applies to text and position only - a device reports on a timer regardless of the hour |
| `--start-hour`                                | 8.0           | so a run does not always begin in the quietest part of the day                                                                               |
| `--congestion-input`                          | `hotstore`    | what drives the throttle: `hotstore` (what the firmware does, and saturates), `truesize` (the unbounded ideal), `utilisation`                |
| `--no-congestion-scaling`                     | off           | disable the firmware's node-count interval scaling entirely                                                                                  |
| `--position-throttle`, `--telemetry-throttle` | 1             | region-profile integer multipliers                                                                                                           |
| `--catch-up-hours`                            | -             | defer reconciliation to the quiet hours, e.g. `02-06`. Empty reconciles any time                                                            |

### 4.5 Degradation

| Flag           | Default | Meaning                                                                                                               |
| -------------- | ------- | --------------------------------------------------------------------------------------------------------------------- |
| `--extra-loss` | 0.0     | flat loss floor on every reception                                                                                    |
| `--burst-loss` | 0.0     | chance a node is deaf for a whole window                                                                              |
| `--burst-ms`   | 60000   | length of that window. A 60 s outage is nothing to a bucket that takes an hour to fill; 1800000 is the one that bites |

`mesh.break_mesh(mode, count)` offers `bridge`/`routers`/`degree`/`random`/`split`, and `take_down`
deliberately does **not** remove the node from anyone else's NodeDB - failure is not broadcast, so the
rest of the mesh keeps believing routes through a node that has gone. Not yet exposed as a flag.

### 4.6 The archive

| Flag                  | Default     | Meaning                                                                                                                |
| --------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------- |
| `--protocol`          | `sr`        | `none` (paired baseline, servers still _sited_ and instrumented), `chain` (today's SF++), `sr` (the sketch)            |
| `--baseline`          | off         | no servers **and no observers** - a plain mesh. `--protocol none` is the paired control; this is the unpaired one     |
| `--servers`           | 3           | archive count                                                                                                          |
| `--place`             | `spread`    | see §5.2                                                                                                               |
| `--hops-apart`        | 3           | target separation for `hops-apart`                                                                                     |
| `--bucket-mode`       | `local`     | `local` is what the firmware does; `global` is a labelled fiction; `time` and `window` need no agreement               |
| `--capacity`          | 32          | sketch capacity                                                                                                        |
| `--window-size`       | 32          | objects in the sliding window                                                                                          |
| `--time-bucket-s`     | 1800        | window width for `time`                                                                                                |
| `--short-id-bits`     | 32          | sketch member width                                                                                                    |
| `--signed`            | off         | sign the advert (66 bytes)                                                                                             |
| `--trigger`           | `bucket`    | `bucket`, `interval`, `aimd`, `bucket+interval`                                                                        |
| `--resolve`           | `hybrid`    | `sketch`, `enum`, `hybrid`                                                                                             |
| `--advert-interval-s` | 300         | interval-trigger period, and the AIMD floor                                                                            |
| `--advert-max-interval-s` | 3600    | AIMD ceiling. Only read by `--trigger aimd`                                                                            |
| `--advert-jitter-s`   | 30          | spread on bucket-close. **A bucket seals on a global counter, so every archive fires at once - seconds is too little** |
| `--advert-transport`  | `broadcast` | or `dm` to each known peer                                                                                             |
| `--provide-transport` | `dm`        | or `broadcast`, so bystanders can file replays                                                                         |
| `--replay-ordering`   | `tip`       | `heard` files a replay by its `heard_ago` into the receiver's own stream                                               |
| `--sr-retries`        | 2           | retries per addressed hop                                                                                              |
| `--chain-walk-cap`    | 4.0         | abandon a chain walk after this many round trips per object                                                            |

### 4.7 Run control

| Flag          | Default | Meaning                                            |
| ------------- | ------- | -------------------------------------------------- |
| `--hours`     | 72      | simulated duration                                 |
| `--seed`      | random  | omit to draw and record one                        |
| `--repeats`   | 1       | **seed** repeats, not packet retries               |
| `--observers` | 6       | ordinary nodes instrumented for the bystander view |
| `--out`       | -       | JSON path. The report lands in `reports/` and the charts in `figures/` beside it |
| `--label`     | -       | free text copied into the report, for telling two runs apart afterwards |
| `--no-charts` | off     | skip chart rendering. The JSON and the text report are written either way |

---

## 5. Topologies and placements

### 5.1 Mesh shapes - `--topology`

At 60 nodes, seed 990001, 8 km:

| Value       | Shape                          | Degree     | Diameter | Why it is a different question                                                    |
| ----------- | ------------------------------ | ---------- | -------- | --------------------------------------------------------------------------------- |
| `uniform`   | Poisson-disc across the square | 8.7         | 7        | the control, and the only shape rounds one and two ever ran                       |
| `clustered` | _k_ towns, sparse between      | 20.4        | 5        | what most regional meshes look like; between-town links are the bottleneck        |
| `corridor`  | long and thin, aspect 6:1      | 8.4         | 12       | a valley or coast road; hop limit binds hard, placement is nearly one-dimensional |
| `hub`       | dense core plus radial spokes  | 18.2        | 5        | the core hears everything, the spoke ends almost nothing                          |
| `chain`     | towns strung in a line         | 10.6 @16 km | **11**   | **the way to build a mesh wider than any hop limit that stays connected**         |
| `mixed`     | drawn from the seed            | -          | -        | a sweep samples across _shapes_ rather than draws of one shape                    |

**Use `chain`, not a stretched `uniform`, for wide meshes.** Stretching a uniform field far enough to
exceed seven hops fragments it - at 16 km with 60 nodes it splits into components, and a diameter
measured across a fragmented graph is the diameter of whichever fragment the walk started in.
`link_stats()` reports `components`, `largest_component` and `connected`, and `diameter()` returns
`None` rather than a misleading number when the mesh is not connected.

### 5.2 Archive placement - `--place`

| Value               | Where the archives go                                                   |
| ------------------- | ----------------------------------------------------------------------- |
| `spread`            | farthest-point across the area                                          |
| `routers`           | on the highest-degree routers                                           |
| `alternate-routers` | every other router by degree                                            |
| `beside-router`     | a plain client one hop from each router                                 |
| `random-clients`    | ordinary nodes at random - the control for every deliberate arrangement |
| `hops-apart`        | targeting `--hops-apart` pairwise separation                            |

**Known limitation:** `hops-apart` picks greedily from a high-degree start, which on a `chain` walks
only a short way along it. On a 24 km chain with 8 archives it clusters them in the left third and
strands 25 of 112 nodes with no archive in reach. A chain-aware placement that spreads along the
principal axis does not exist yet, and testing sync quality on such a mesh would measure the placement
instead.

---

## 6. Presets change reception, not just airtime

Same 60 nodes, same positions, only `--preset` changed:

| Preset           | Sensitivity | Links | Degree | Diameter       | Isolated | Airtime (53 B) |
| ---------------- | ----------- | ----- | ------ | -------------- | -------- | -------------- |
| `SHORT_FAST`     | −121.5      | 105   | 3.5    | **fragmented** | **3**    | 111 ms         |
| `MEDIUM_FAST`    | −126.5      | 160   | 5.3    | 10             | 1        | 353 ms         |
| `LONG_FAST`      | −131.5      | 259   | 8.7    | 7              | 0        | 1190 ms        |
| `LONG_MODERATE`  | −134.5      | 355   | 11.8   | 5              | 0        | 3609 ms        |
| `LONG_SLOW`      | −137.0      | 449   | 15.0   | 5              | 0        | 6431 ms        |
| `VERY_LONG_SLOW` | −140.0      | 576   | 19.2   | 4              | 0        | 11289 ms       |

Preset feeds four paths: **sensitivity → the link graph** (18.5 dB across the range, so 105 links
against 576 on identical geometry - the dominant effect), airtime → contention → collisions, coding
rate → PER, and SF/bandwidth → CSMA slot time. A preset change is a different _mesh_, not just a
different clock. At `SHORT_FAST` this geometry does not stay connected at all, which is why the
diameter column reads fragmented rather than a number.

---

## 7. Outputs

### 7.1 The JSON report

| Section        | Contains                                                                                                                                                                                                              |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mesh`         | nodes, area, degree, `diameter` (`None` if fragmented), components, routers, topology                                                                                                                                 |
| `traffic`      | originated per class, channel utilisation, transmissions, **`queue_drops`**, `dropped_to_backoff_cap`, receptions, collision and half-duplex losses, airtime by kind, congestion coefficient                          |
| `by_class`     | per portnum: sent, received, **per-node reception distribution**, `nodes_receiving_none`, airtime share, `archived`                                                                                                   |
| `by_hop_limit` | reception and hops traversed, split by the node's own limit                                                                                                                                                           |
| `baseline`     | text reach min/median/mean/max, routing ceiling, and the loss split into beyond-hop-limit against lost-within-reach                                                                                                   |
| `designated`   | the archive-sited nodes' own reception, with the archive off or on, plus held and the reconciled gain                                                                                                                 |
| `observers`    | per-observer direct against overheard, and replay placement error                                                                                                                                                     |
| `sfpp`         | held, union, adverts, objects moved, bytes and airtime by message type, decode failures, misdecodes, escalations, bystander pickups, **`silent_losses`**, the at-rest audit, drift telemetry, and the stretch metrics |
| `hops_away`    | how far away each node's NodeDB believes its peers are, against the topology's own answer - the belief and the truth side by side                                                                                    |
| `hop_scaling`  | the firmware's hop histogram: truth, what a node observed, and what its estimator inferred per hop, plus the recommendation it would make                                                                            |
| `adaptive`     | the per-node time series `--trace-interval-s` collects. Empty unless that flag is set                                                                                                                                |
| `opts`         | every resolved option, so a report can be replayed without the command line that made it                                                                                                                            |

`seed`, `label` and `wall_seconds` sit at the top level beside these.

**Stretch metrics** - the ones that answer "was this worth it on a wide mesh":

- `structurally_unreachable` - no path within the _sender's_ hop limit exists, so nothing would ever
  have delivered it
- `recoverable_from_reachable_archive` - unreachable, but held by an archive the node can reach
- `delivered_though_unreachable` - **unreachable and the node has it anyway.** Proof of
  archive-delivered coverage, not an inference from what a server holds
- `per_node_share_of_unreachable_delivered` and `nodes_with_zero_delivered` - the tail, because the
  mean is dragged up by nodes that had little to recover

### 7.2 Reading it

Every per-node quantity is `min / p10 / median / mean / p90 / max`. **Prefer the worst node to the
mean**: on a stretched mesh the result is bimodal - nodes near an archive gain a great deal, nodes past
the last archive gain nothing - and a mean describes neither.

### 7.3 Charts

Rendered automatically beside the JSON, footered with the transport commit, seed and duration so a
figure cannot be read against the wrong code. Per-class reception spread with the worst node marked,
airtime by class, and the stretch metrics where present.

---

## 8. Two checks that would have caught real bugs on day one

Both of these are in the JSON and both were ignored for three rounds:

- **`queue_drops` against `transmissions`.** A backoff cap was discarding about two thirds of all
  rebroadcast attempts, including the archive's own packets, and every airtime figure from rounds one
  to three was measured through it.
- **Identical rows across a swept arm.** Two arms have been silently inert - accepted on the command
  line, stored, never read - and both produced well-formed tables supporting the opposite of the truth.

And the standing one: **`silent_losses` must be zero.** A checksum that closes over two unequal sets
would falsify the design. Across roughly 280 runs, two bucket regimes and both protocols, it never has.

---

## 9. Register: what this iteration is built from

Four separate bodies of work, none of them ours alone. Recorded here so credit is attributable and a
re-sync is a diff rather than an archaeology exercise.

| Layer                                                              | Drawn from                                                               | Version / commit                                   |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------ | -------------------------------------------------- |
| Radio physics, topology, collision model                           | **Meshtasticator**, upstream `master`                                    | `17ceb82`                                          |
| Terrain, clutter, capture-aware RF, dynamic CR/TX-power            | **Komzpa**, `codex/pr33-remaining-optimizations` (Meshtasticator PR #77) | `ec0a51e`                                          |
| Firmware-preset sync (stacked under the above)                     | **powersjcb**, Meshtasticator PR #33                                     | in `ec0a51e`                                       |
| MAC and routing rules, per-node NodeDB, board/role census          | this repo's 2.8 fold-in                                                  | `95c387bc6`, `95b7651b9`, `8c2b17145`, `6de4495d4` |
| SF++ set reconciliation, the chain incumbent, sweeps and reporting | `sim/sfpp/`, written here                                                | `7dcae53d5` onward                                 |

**Komzpa's stack is the reason there is a credible radio model at all.** Upstream `master` still carries
2.1-era physics; the SRTM terrain, OSM land-cover clutter, capture-aware physics with a real collision
model, and the dynamic coding-rate and TX-power policies are all PR #77, which itself stacks powersjcb's
preset sync from PR #33. None of it is merged upstream, so vendoring it was a fork-and-own decision - see
`sim/meshtasticator/UPSTREAM` for the exact merge, the one conflict resolved (`batchSim.py`, upstream #83's
keyword-argument form kept), and the re-sync recipe. PR #78's Burning Man scenario is **not** included.

`sim/sfpp/analytic/` is a fifth, independent line: a closed-form model kept deliberately separate as a
cross-check on the event simulator rather than folded into it.

### 9.1 Firmware versions the transport can imitate

`--profile` selects which firmware's rules to obey - a **release series**, taken at the final release
of that series:

| Profile  | What arrives in that series                                                                                                                                                                   |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2.4`    | the floor. CW 2-8, SNR range to 15 dB, router offset, quantised slots, utilisation backoff, reliable retransmission at 3 attempts, a flat 100-entry NodeDB and a flat congestion coefficient  |
| `2.5`    | the late-rebroadcast window and the queue ordering that goes with it (late first, relayed preferred), `ROUTER_LATE`, `CORE_PORTNUMS_ONLY`, CW 2-7, congestion scaled per preset                |
| `2.6`    | next-hop routing, CW floor to 3, SNR range narrowed to 10 dB, per-board NodeDB sizing                                                                                                          |
| `2.7`    | next-hop and traceroute **learning**, role-aware cancellation, `CLIENT_BASE`, favourite-and-base early rebroadcast, hop preservation and hop upgrade, congestion scaled on SF and bandwidth    |
| `2.8`    | this tree. Traceroute corroboration, the overflow route cache, last-byte ambiguity resolution, RouteHealth, the warm store, packet signing, the hop-scaling histogram and its recommendation, opaque relay, congestion clamp, 5 unicast attempts |
| `legacy` | this transport's own pre-fold-in model - **not a firmware version.** Four of its deviations were never any firmware's behaviour (no router offset, a continuous slot draw, a clamped contention window, a 400-backoff discard), so it must not be read as "2.7 and earlier" |

Each row is **cumulative**: a profile carries everything from the rows above it. A version was dated
by walking the firmware's own release tags for the commit that introduced the behaviour, so the date
is evidence; the claim that nothing else in that series matters is the assumption (§10.2).

`--old-profile` and `--legacy-fraction` run a share of the nodes on a different series, for a
mixed-version mesh. `--profile-flag NAME=VALUE` overrides a single rule, which is where a specific
pathology belongs rather than as a profile of its own.

Three mechanisms are **not in any release** and are switched on explicitly: `--extra-repeats`
(branch `extra-repeats`), `--coding-rate-ladder` (branch `CRCRRCRRR`), and `--dm-mode m4-early-flood`
(written and compiled out at `NEXTHOP_EARLY_FLOOD_ON_UNVERIFIED 0`).

The vendored Meshtasticator's own 2.1-era physics remains reachable in `sim/meshtasticator/` for
comparison, but the SF++ transport does not model behaviour older than `2.4`.

---

## 10. What is simplified, assumed, or not there at all

Every result from this tool is bounded by this section. Three categories, and the difference matters:
**simplified** means the mechanism is present but coarser than the firmware; **assumed** means a
number was chosen rather than measured, and choosing differently would move results; **absent** means
the mechanism is not modelled at all and any question about it has no answer here.

### 10.1 Simplified

| Thing                     | What the firmware does                                           | What this does                                                                                                                        |
| ------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Cryptography              | real X25519/AES-CCM, real XEdDSA over the encoded payload        | key **possession** and byte **cost** only. Nothing is enciphered and no signature is computed; a node holding a peer's key verifies, one without it does not. `--signed` buys the 66-byte field, `signedDataFits()` is applied for real |
| Packet encoding           | protobuf, with field-by-field sizes                              | payload lengths are computed from the wire layout, but nothing is serialised. Length is right; encoding cost is not modelled          |
| CAD and channel sensing   | per-symbol CAD against a threshold                               | a slot-time model - `computeSlotTimeMsec` with the region's `wideLora` flag - plus a channel-busy test at the moment of transmit      |
| Collisions                | analogue capture at the receiver                                 | overlap in time on a shared channel, with the vendored capture-aware check. No partial-packet recovery                                |
| Time                      | free-running per-device clocks, drift and NTP-less skew          | one global millisecond clock. Every node agrees on the time exactly                                                                   |
| Reboots and config        | nodes restart, lose state, get reconfigured mid-flight           | a node's role, profile, board and hop limit are fixed for the run. `break_mesh` / `take_down` remove a node without a NodeDB update    |
| Retries                   | full reliable-delivery state machine with per-packet timers      | a retry ladder with the firmware's counts and escalation points, on the simulator's own timer                                         |
| Traceroute                | full `RouteDiscovery` with SNR arrays both ways                  | the route array and the `relay_node` corroboration guard. SNR entries are not carried                                                 |
| NodeDB persistence        | flash-backed, survives reboot, written on a schedule             | in-memory only, with the real hot/warm/cold tier sizes and eviction order                                                             |

### 10.2 Assumed

| Assumption                     | Value                                            | Why it matters                                                                                                                     |
| ------------------------------ | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| **Siting gain** (`--siting-mix`) | roof +6, desk 0, pocket −10, basement −20 dB   | **Not from the firmware and not measured.** The firmware has no concept of siting at all. 26 dB between roof and basement is wide enough to move any result. The default `uniform` is all-desk, i.e. 0 dB, so a run that does not set this flag is unaffected |
| Link asymmetry                 | Gaussian, mean 0, σ 2 dB, one draw per pair      | vendored default. Real asymmetry comes from antennas and height, which are not modelled                                            |
| Path loss                      | 3GPP Suburban Macro (`MODEL = 5`)                | one propagation environment for every run. No terrain, no clutter, no per-link environment                                         |
| Diurnal shape                  | `commuter`, 17:1 peak-to-trough                  | invented, not measured. It sets when the mesh is busy, which the whole congestion story rests on                                    |
| Role and board census          | `baymesh-2026-08`, 1769 real nodes               | measured, but from one metro mesh on one day. Not a global distribution                                                            |
| Profiles `2.4`–`2.7`           | dated by walking the firmware's release tags     | the *date* a behaviour first appeared is evidence; the claim that nothing else changed in that series is not. A profile is a floor - the named behaviours are off, everything unnamed is left at 2.8 |
| Hop-scaling estimator          | firmware arithmetic, exhaustive count as control | the estimator is ported exactly; what a real mesh's hop histogram looks like is the assumption                                      |

### 10.3 Not included

Nothing below is modelled. A question about any of it has no answer here, and a result that would
depend on it is not evidence.

- **No client hydration path.** Archives reconcile with each other; nothing models a client asking a
  server for what it missed, so `held` and `union` are what an archive _has_, not what a user _gets_.
  The only measured end-user gain is bystander pickup.
- **No duty cycle enforcement.** Airtime figures are what the protocol asks for, not what a region
  permits. A run can and does exceed what is legal to transmit.
- **No MQTT, no internet-connected nodes**, and so no packets arriving without RF provenance beyond
  the one place the traceroute guard tests for them.
- **No terrain and no clutter**, despite the vendored tree carrying Komzpa's SRTM and OSM land-cover
  code (§9). It is present in `sim/meshtasticator/` and this simulator does not call it.
- **No mobility.** Positions are drawn once and never change.
- **No power model**: no sleep, no battery, no duty-cycled receivers. Every node is listening at all
  times, which overstates reception on any mesh with sleeping clients.
- **No dynamic TX power or dynamic coding rate** as shipped policies - `--coding-rate-ladder` is an
  unreleased branch's behaviour, not the vendored dynamic-CR code, which is also not called.
- **No admin messages, no channel or PSK model, no position precision, no NeighborInfo module.**
- **No firmware-side store and forward**: the SF++ archive here is the campaign's own protocol, not
  the shipped StoreForward module.
- **No regulatory regions beyond the vendored preset table**, and no per-region duty or power policy
  beyond `power_limit`.

### 10.4 Flags that do nothing on their own

Each of these is live, but only once the flag that enables it is set. A sweep over one of them
without its enabling flag produces well-formed identical rows - which is exactly the failure mode
§8 warns about.

| Flag                            | Does nothing unless                                       |
| ------------------------------- | --------------------------------------------------------- |
| `--old-profile`                 | `--legacy-fraction` is above 0                            |
| `--dm-mode`                     | `--dm-transport transport`                                |
| `--coding-rate-ladder`          | there are addressed messages to retransmit, so in practice `--dm-transport transport` with `--traceroute-per-hour` above 0 |
| `--no-adopt-hop-recommendation` | the run is long enough and large enough for a node to reach a recommendation at all |
| `--platform-mix`                | `--max-num-nodes` is left unset - an explicit value overrides every board's own size |
| `--advert-max-interval-s`       | `--trigger aimd`                                          |
| `--window-size`                 | `--bucket-mode window`                                    |
| `--time-bucket-s`               | `--bucket-mode time`                                      |
| `--hops-apart`                  | `--place hops-apart`                                      |
| `--chain-walk-cap`              | `--protocol chain`                                        |
| the `adaptive` JSON section     | `--trace-interval-s` is above 0                           |
| `--hop-spread`                  | nothing - it is already the default. `--no-hop-spread` is the control |
