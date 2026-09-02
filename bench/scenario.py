"""Scenarios, assertions, and the four verdicts.

A scenario is data, not code: roles, the bakes behind them, a stimulus, and a list of
assertions that each declare what they need. That is what makes the bench general - the
beacon case and the LBT case differ only in the values here, and anything that has to be
special-cased for one of them is a defect in this layer rather than a property of the
test.

Four verdicts, and the last two are the pair that kept being conflated:

  PASS          expected behaviour observed
  FAIL          contrary behaviour observed
  NOT OBSERVED  the evidence would not appear even if the firmware were correct - a
                boot-time-only log the capture started after, an RF the listener is not
                parked on, a half-duplex collision
  INVALID       the bench could not establish preconditions, so the row says nothing
                about firmware at all

An assertion that needs a capability the flashed image does not have is INVALID, never
NOT OBSERVED. That distinction is enforced here rather than left to a reader: a check
keyed on LOG_TRACE against an image built without MESHTASTIC_TRACE_LOGGING would
otherwise report a clean miss forever, against firmware that works perfectly.

Preconditions are encoded, not remembered. The beacon run failed correct firmware
because check_restored is only meaningful where the target's RF differs from home, and
that condition lived in the operator's head instead of in the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import ledger as ledger_mod
from . import manifest

PASS = "PASS"
FAIL = "FAIL"
NOT_OBSERVED = "NOT OBSERVED"
INVALID = "INVALID"

# Stimulus sources. Whether a source puts energy on the air is the property that matters,
# and it is why api injection cannot serve a channel-sensing test.
STIM_SELF = "self"  # the node's own timer or module fires
STIM_API = "api"  # inject_frame - RX pipeline only, NEVER touches the air
STIM_RF_PEER = "rf_peer"  # a real node sends a real frame
STIM_RF_EXCITER = "rf_exciter"  # raw carrier or preamble, not a valid frame

RF_STIMULI = frozenset({STIM_SELF, STIM_RF_PEER, STIM_RF_EXCITER})


@dataclass
class Outcome:
    """One assertion's result, with the evidence line that justifies it."""

    name: str
    verdict: str
    evidence: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "detail": self.detail,
        }


@dataclass
class Context:
    """What an assertion is allowed to know beyond the ledger."""

    scenario_id: str
    nodes: dict[str, Any] = field(default_factory=dict)  # name -> BenchNode
    settled: dict[str, dict] = field(default_factory=dict)  # name -> SettledState dict
    capabilities: dict[str, set[str]] = field(default_factory=dict)  # role -> capabilities
    params: dict[str, Any] = field(default_factory=dict)
    # node name -> "api" | "raw". A raw-captured node yields log lines only: the packet
    # lane is parsed from the protobuf link, which a never-commanded node does not have.
    capture_modes: dict[str, str] = field(default_factory=dict)

    def has_capability(self, role: str, capability: str) -> bool:
        return capability in self.capabilities.get(role, set())

    def produces_packets(self, node_name: str) -> bool:
        """Whether this node can contribute to the packet lane at all.

        Unknown nodes are assumed to, so a context built without capture information does
        not silently invalidate every packet assertion.
        """
        return self.capture_modes.get(node_name, "api") != "raw"


class Assertion:
    """Base class. Subclasses implement `check`; the framework handles gating.

    `requires` names image capabilities. `precondition` is a callable over the context
    that says whether this check is meaningful for this row at all - returning False
    yields NOT OBSERVED with the reason, rather than a FAIL against correct firmware.
    """

    def __init__(
        self,
        name: str,
        requires: Sequence[str] = (),
        role: str = "dut",
        precondition: Callable[[Context], bool] | None = None,
        precondition_reason: str = "precondition not met for this row",
    ) -> None:
        self.name = name
        self.requires = list(requires)
        self.role = role
        self.precondition = precondition
        self.precondition_reason = precondition_reason

    def evaluate(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        missing = [c for c in self.requires if not ctx.has_capability(self.role, c)]
        if missing:
            return Outcome(
                self.name,
                INVALID,
                f"image for role {self.role!r} lacks {', '.join(missing)} - this check "
                "cannot produce evidence either way",
                {"missing_capabilities": missing},
            )
        if self.precondition is not None and not self.precondition(ctx):
            return Outcome(self.name, NOT_OBSERVED, self.precondition_reason)
        return self.check(led, ctx)

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:  # pragma: no cover
        raise NotImplementedError


class PacketCount(Assertion):
    """At least (or at most) N distinct packets matching a filter.

    The counting primitive for anything whose evidence is traffic. rf_only excludes
    packets that never crossed the air, so a node's own local telemetry cannot inflate a
    delivery claim.
    """

    def __init__(
        self,
        name: str,
        observer: str,
        at_least: int | None = None,
        at_most: int | None = None,
        portnum: str | None = None,
        from_node: str | None = None,
        from_role: str | None = None,
        status: str | None = None,
        rf_only: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.observer = observer
        self.at_least = at_least
        self.at_most = at_most
        self.portnum = portnum
        self.from_node = from_node
        # A role name is not a node id. The ledger records "!77e4f0dc", so filtering by
        # the string "dut" silently matches nothing - and an at_most=0 assertion would
        # then pass no matter what the node transmitted. from_role resolves the role to
        # the id the device actually reported during provisioning.
        self.from_role = from_role
        self.status = status
        self.rf_only = rf_only

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        # A raw-captured node emits log text, never parsed packets, so counting its
        # packet lane always returns zero. An at_least bound would read as a firmware
        # miss and an at_most bound would pass no matter what happened on the air - the
        # second being the more dangerous, because it is a control that cannot fail.
        if not ctx.produces_packets(self.observer):
            return Outcome(
                self.name,
                INVALID,
                f"{self.observer} is captured on raw serial, which yields log lines and "
                "not packets - this check cannot see its own subject. Assert on the log "
                "lane for a never-commanded node.",
                {"capture_mode": "raw"},
            )

        from_node = self.from_node
        if self.from_role is not None:
            settled = ctx.settled.get(self.from_role) or {}
            from_node = settled.get("node_id")
            if not from_node:
                # Without the id this check cannot address its subject, so an at_most
                # bound would pass vacuously. Say so instead.
                return Outcome(
                    self.name,
                    INVALID,
                    f"cannot resolve role {self.from_role!r} to a node id - it was not "
                    "provisioned, so this check has no subject to count",
                )

        n = led.packets.count(
            observer=self.observer,
            portnum=self.portnum,
            from_node=from_node,
            status=self.status,
            rf_only=self.rf_only,
        )
        what = f"{self.observer} saw {n} packets"
        if from_node:
            what += f" from {from_node}"
        if self.portnum:
            what += f" on {self.portnum}"
        if self.at_least is not None and n < self.at_least:
            # Zero with a demonstrably live capture is a real negative; zero with a dead
            # stream is not, and the runner has already asserted liveness by here.
            verdict = NOT_OBSERVED if n == 0 else FAIL
            return Outcome(self.name, verdict, f"{what}, expected at least {self.at_least}",
                           {"count": n})
        if self.at_most is not None and n > self.at_most:
            return Outcome(self.name, FAIL, f"{what}, expected at most {self.at_most}",
                           {"count": n})
        return Outcome(self.name, PASS, what, {"count": n})


class LogCount(Assertion):
    """At least (or at most) N matching firmware log lines.

    Patterns are always a list of alternatives. Log wording is not a stable contract: it
    varies with firmware version, build flags, and whether a client is attached, since an
    attached client makes the node log a reception as "phone downloaded packet" rather
    than "Received text msg". A check that matched only one wording scored a working link
    as zero, twice.
    """

    def __init__(
        self,
        name: str,
        patterns: Sequence[str],
        node: str | None = None,
        at_least: int | None = None,
        at_most: int | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.patterns = list(patterns)
        self.node = node
        self.at_least = at_least
        self.at_most = at_most

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        n = led.logs.count(self.patterns, node=self.node)
        what = f"{n} log lines matched {self.patterns!r}"
        if self.node:
            what += f" on {self.node}"
        if self.at_least is not None and n < self.at_least:
            verdict = NOT_OBSERVED if n == 0 else FAIL
            return Outcome(self.name, verdict, f"{what}, expected at least {self.at_least}",
                           {"count": n})
        if self.at_most is not None:
            if n > self.at_most:
                return Outcome(self.name, FAIL, f"{what}, expected at most {self.at_most}",
                               {"count": n})
            # "I saw none" and "I saw nothing" are different claims. An at_most check on
            # a node that produced no log lines at all is the instrument being deaf, not
            # the firmware being quiet, and passing it green is the worst kind of wrong:
            # the row reports evidence it never had. Seen on a node that was in its
            # bootloader for the whole window.
            heard = led.logs.by_node().get(self.node, 0) if self.node else len(led.logs.rows)
            if heard == 0:
                return Outcome(
                    self.name, INVALID,
                    f"no log lines at all from {self.node or 'any node'} during the window, "
                    "so 'at most' cannot be judged",
                    {"count": n, "lines_from_node": 0},
                )
        return Outcome(self.name, PASS, what, {"count": n})


class RateAssertion(Assertion):
    """A rate over trials: matched events divided by attempts.

    The shape every radio-timescale check takes. "The DUT deferred on 100 of 100
    attempts" needs no shared clock and no sub-second resolution, which is what makes it
    assertable at all - firmware uptime is printed in whole seconds and two nodes share
    no time base.
    """

    def __init__(
        self,
        name: str,
        event_patterns: Sequence[str],
        trial_patterns: Sequence[str],
        node: str | None = None,
        min_rate: float = 1.0,
        max_rate: float | None = None,
        min_trials: int = 1,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.event_patterns = list(event_patterns)
        self.trial_patterns = list(trial_patterns)
        self.node = node
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.min_trials = min_trials

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        trials = led.logs.count(self.trial_patterns, node=self.node)
        events = led.logs.count(self.event_patterns, node=self.node)
        if trials < self.min_trials:
            # Too few attempts is not a failure of the firmware; it is a failure to
            # stimulate it, and saying otherwise would be exactly the false signal the
            # verdict taxonomy exists to prevent.
            return Outcome(
                self.name,
                NOT_OBSERVED,
                f"only {trials} trials observed (needed {self.min_trials}) - the "
                "stimulus did not run often enough to say anything",
                {"trials": trials, "events": events},
            )
        rate = events / trials
        what = f"{events}/{trials} = {rate:.2%}"
        detail = {"trials": trials, "events": events, "rate": round(rate, 4)}
        if rate < self.min_rate:
            return Outcome(self.name, FAIL, f"{what}, expected at least {self.min_rate:.0%}", detail)
        if self.max_rate is not None and rate > self.max_rate:
            return Outcome(self.name, FAIL, f"{what}, expected at most {self.max_rate:.0%}", detail)
        return Outcome(self.name, PASS, what, detail)


class ObserverSilence(Assertion):
    """A never-commanded observer heard nothing from a given role.

    The log-lane counterpart to a PacketCount at_most=0, for the one node that has no
    packet lane. It matches on the node's 8-hex id, which every reception wording carries
    somewhere - "Received text msg from=0x77e4f0dc", "phone downloaded packet (id=...
    fr=0x77e4f0dc ...)" and the rx-error line all include it - so the check does not
    depend on which phrasing the firmware happened to use.

    Refuses to run without a resolved id, because matching nothing would otherwise look
    exactly like silence.
    """

    def __init__(self, name: str, observer_node: str, from_role: str, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.observer_node = observer_node
        self.from_role = from_role

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        settled = ctx.settled.get(self.from_role) or {}
        node_id = settled.get("node_id")
        node_num = settled.get("node_num")
        if not node_id and node_num is None:
            return Outcome(
                self.name,
                INVALID,
                f"cannot resolve {self.from_role!r} to a node id, so silence from it is "
                "indistinguishable from a filter that matches nothing",
            )

        patterns = []
        if node_id:
            patterns.append(re.escape(str(node_id).lstrip("!")))
        if isinstance(node_num, int):
            patterns.append(f"0x0*{node_num:x}")
        hits = led.logs.matching(patterns, node=self.observer_node)
        if hits:
            sample = (hits[0].get("line") or "")[:120]
            return Outcome(
                self.name,
                FAIL,
                f"{self.observer_node} heard {len(hits)} lines mentioning "
                f"{node_id or node_num}: {sample!r}",
                {"count": len(hits)},
            )
        # Silence only means something if the observer was demonstrably listening.
        total = led.logs.count([r".+"], node=self.observer_node)
        if total == 0:
            return Outcome(
                self.name,
                NOT_OBSERVED,
                f"{self.observer_node} logged nothing at all during this row, so its "
                "silence about the DUT is inattention rather than evidence",
                {"observer_lines": 0},
            )
        return Outcome(
            self.name,
            PASS,
            f"{self.observer_node} logged {total} lines and none mentioned "
            f"{node_id or node_num}",
            {"observer_lines": total},
        )


class SettledStateAssertion(Assertion):
    """The node was in the state the scenario asked for.

    Cheap, and it is the check that would have caught the beacon run's hollow pass: a
    node with psk_len 1 and an empty channel name beacons happily and passes everything
    else.
    """

    def __init__(self, name: str = "settled_state", role: str = "dut", **kw: Any) -> None:
        super().__init__(name, role=role, **kw)

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        state = ctx.settled.get(self.role)
        if state is None:
            return Outcome(self.name, INVALID, f"no settled state recorded for {self.role!r}")
        errors = state.get("errors") or []
        if errors:
            return Outcome(self.name, INVALID, "; ".join(errors), {"state": state})

        # An image that claims it can prove its identity, running on a node that never
        # said so, is the failure the build tag exists to catch. Most likely the -D never
        # reached the compiler, in which case every row is asserting against firmware
        # nobody can identify - which is exactly the state the beacon run was in.
        if ctx.has_capability(self.role, manifest.BUILD_TAG) and not state.get("build_tag"):
            return Outcome(
                self.name,
                INVALID,
                f"the image for {self.role!r} was built with BENCH_BUILD_TAG but the node "
                "never echoed one at boot - its identity is unproven, so this row cannot "
                "say which firmware it tested",
                {"state": state},
            )
        return Outcome(
            self.name,
            PASS,
            f"{state.get('node_id')} on {state.get('region')}/{state.get('modem_preset')}, "
            f"build tag {state.get('build_tag')}, "
            f"{len(state.get('channels') or [])} channels",
            {"state": state},
        )


class NoDecryptFailures(Assertion):
    """No packets failed to decrypt from the nodes under test.

    Separates "wrong key" from "out of range" - the confusion that consumed hours when
    packets arriving at -64 dBm with healthy SNR were read as a range problem.
    """

    def __init__(self, name: str = "no_decrypt_failures", sources: Sequence[str] = (), **kw: Any):
        super().__init__(name, **kw)
        self.sources = list(sources)

    def check(self, led: ledger_mod.Ledger, ctx: Context) -> Outcome:
        failures = led.packets.decrypt_failures_by_source()
        relevant = (
            {k: v for k, v in failures.items() if k in self.sources} if self.sources else failures
        )
        total = sum(relevant.values())
        if total:
            return Outcome(
                self.name,
                FAIL,
                f"{total} decrypt failures by source: {relevant} - these arrived with "
                "signal and failed only to decrypt, which is a key mismatch not range",
                {"by_source": relevant},
            )
        return Outcome(self.name, PASS, "no decrypt failures", {"by_source": {}})


@dataclass
class RoleBake:
    """What a role runs in a scenario."""

    role: str
    bake: manifest.Bake
    spec: Any = None  # provision.NodeSpec


@dataclass
class Scenario:
    """One row of the matrix.

    `stimulus` is declared rather than implied, because whether the source puts energy on
    the air decides what the row can prove. An api stimulus injects into the receive
    pipeline and never keys the transmitter, so a scenario that senses the channel and
    declares STIM_API is incoherent - validate() refuses it rather than letting it
    produce a confident wrong answer.
    """

    id: str
    description: str
    roles: dict[str, RoleBake] = field(default_factory=dict)
    stimulus: str = STIM_SELF
    stimulus_params: dict[str, Any] = field(default_factory=dict)
    assertions: list[Assertion] = field(default_factory=list)
    duration_s: float = 60.0
    senses_channel: bool = False
    tags: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        problems = []
        if not self.roles:
            problems.append(f"{self.id}: no roles")
        if not self.assertions:
            problems.append(f"{self.id}: no assertions - the row could not fail")
        if self.senses_channel and self.stimulus not in RF_STIMULI:
            problems.append(
                f"{self.id}: senses_channel is set but stimulus is {self.stimulus!r}, "
                "which puts no energy on the air - this row cannot test what it claims"
            )
        for assertion in self.assertions:
            if assertion.role not in self.roles:
                problems.append(
                    f"{self.id}: assertion {assertion.name!r} targets role "
                    f"{assertion.role!r}, which the scenario does not define"
                )
        return problems

    def required_capabilities(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for assertion in self.assertions:
            out.setdefault(assertion.role, set()).update(assertion.requires)
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "stimulus": self.stimulus,
            "stimulus_params": dict(self.stimulus_params),
            "senses_channel": self.senses_channel,
            "duration_s": self.duration_s,
            "tags": list(self.tags),
            "roles": {
                r: {"bake": rb.bake.fingerprint(), "spec": rb.spec.to_dict() if rb.spec else None}
                for r, rb in self.roles.items()
            },
            "assertions": [
                {"name": a.name, "role": a.role, "requires": a.requires} for a in self.assertions
            ],
        }


@dataclass
class Condition:
    """One thing that had to be true before a row ran, or after it finished.

    Entry conditions say what state the row needed and how it got there: `satisfied`
    means the bench arrived and found it already true, which is what makes a resumed run
    cheap; `established` means the bench had to do the work. Either is fine, and the
    difference is exactly what a reader wants when a retry takes four minutes instead of
    fifteen.

    Exit conditions are the ones with teeth. They ask whether the instrument survived the
    measurement - whether every node was still answering and still in its required state
    when the window closed. A row whose assertions passed on a node that died half way
    through has not measured what it claims to, and that is INVALID rather than PASS.
    """

    name: str
    met: bool
    detail: str = ""
    how: str = ""  # entry only: "satisfied" (found true) or "established" (made true)

    def to_dict(self) -> dict:
        return {"name": self.name, "met": self.met, "detail": self.detail, "how": self.how}


@dataclass
class RowResult:
    """One scenario's verdict, and everything that justifies it."""

    scenario_id: str
    verdict: str
    outcomes: list[Outcome] = field(default_factory=list)
    settled: dict[str, dict] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    release_representative: bool = True
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None
    entry: list[Condition] = field(default_factory=list)
    exit: list[Condition] = field(default_factory=list)

    @property
    def entry_met(self) -> bool:
        return all(c.met for c in self.entry)

    @property
    def exit_met(self) -> bool:
        return all(c.met for c in self.exit)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "verdict": self.verdict,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "settled": self.settled,
            "images": self.images,
            "release_representative": self.release_representative,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "entry": [c.to_dict() for c in self.entry],
            "exit": [c.to_dict() for c in self.exit],
        }


def roll_up(outcomes: Sequence[Outcome]) -> str:
    """The row's verdict from its assertions.

    Order matters and is deliberate. INVALID dominates everything: if the bench could not
    establish preconditions, the row says nothing about firmware and must not be reported
    as though it did. FAIL beats NOT OBSERVED, because contrary evidence outranks absent
    evidence. A row with no assertions cannot pass.
    """
    if not outcomes:
        return INVALID
    verdicts = {o.verdict for o in outcomes}
    if INVALID in verdicts:
        return INVALID
    if FAIL in verdicts:
        return FAIL
    if NOT_OBSERVED in verdicts:
        return NOT_OBSERVED
    return PASS
