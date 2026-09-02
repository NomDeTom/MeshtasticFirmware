"""Listen Before Talk scenarios - the bench's second consumer.

Run these with the firmware tree checked out at the branch under test:

    git switch listening-now
    python -m bench run --scenarios bench.scenarios.lbt --run lbt-1 --serve

The build stage hashes the tree's git SHA into every image, so the branch you are on IS
part of the scenario's identity and a row can never silently assert against another
branch's firmware.

Why these rows look the way they do. LBT decides NOT to transmit, so the packet lane is
empty by definition when the firmware is correct - the evidence is entirely in the log
lane. And every one of these events happens at symbol timescales, far below the one
second the firmware prints uptime in and far below USB jitter, so nothing here asserts
WHEN anything happened. Each row counts events against trials over many attempts, which
needs no shared clock and says more than any single trace could.

The log strings below are the branch's own, all at DEBUG or WARN so they survive a normal
build:

    CAD arm / CAD busy / CAD free        a scan, and its two outcomes
    CAD>RX started / pkt / empty         the handoff, and whether it delivered
    CAD>RX timeout / CAD>RX void         the two handoff defects the branch fixed
    Can not send yet, busyRx|busyTx      deferral at the queue rather than at CAD

`%d packets in TX queue` is LOG_TRACE, which compiles to nothing on real hardware unless
MESHTASTIC_TRACE_LOGGING is set. L5 deliberately depends on it, so the capability gate is
exercised on every run: without the flag that row is INVALID, which is the truthful
answer, rather than a NOT OBSERVED that looks like a firmware miss.
"""

from __future__ import annotations

from ..manifest import Bake
from ..provision import NodeSpec
from ..scenario import (
    LogCount,
    ObserverSilence,
    NoDecryptFailures,
    PacketCount,
    RateAssertion,
    RoleBake,
    Scenario,
    SettledStateAssertion,
    STIM_RF_EXCITER,
    STIM_RF_PEER,
    STIM_SELF,
)

# The bench's hardware and regulatory domain, read from the nodes themselves rather than
# assumed. A factory reset leaves region UNSET, which disables LoRa TX outright, so every
# spec below must carry it or the row tests nothing.
ENV = "nrf52_promicro_diy_tcxo"
REGION = "EU_868"

# LONG_SLOW, deliberately, and not because it is realistic.
#
# It isolates the bench. The local mesh runs LONG_FAST, so a different spreading factor
# puts these nodes on air the ambient traffic does not occupy - which is the single
# largest uncontrolled variable in every channel-sensing row here. L1 claims the channel
# is idle; on LONG_FAST that claim was at the mercy of whatever the neighbourhood was
# doing.
#
# It also suits CAD: longer symbols mean a longer window in which a scan can notice a
# transmission, so a deferral that should happen has more chance to.
PRESET = "LONG_SLOW"

# 10 dBm rather than the region's 27 dBm ceiling. Two nodes on one desk need nothing
# like full power, and ~16x less radiated power keeps the bench's footprint small while
# the link stays far above the noise floor at this range.
TX_POWER = 10

# EU_868 here is the 869.4-869.65 MHz band: 10% duty cycle, not 1%. That is ~360 seconds
# of airtime an hour, and LONG_SLOW spends it quickly, so the scenarios below are sized
# to fit rather than overriding the limit. override_duty_cycle stays FALSE: a bench that
# tests radio politeness by disabling radio politeness is not testing the same firmware.
DUTY_CYCLE_PCT = 10

# The stock build. No bench-only flags, so rows using it are release-representative.
STOCK = Bake(env=ENV, label="lbt-stock")

# The instrumented build. MESHTASTIC_TRACE_LOGGING unlocks the LOG_TRACE lines that are
# compiled out of a normal image - the noise-floor readings CAD thresholds are judged
# against, the false preamble and header detections, and the TX queue depth. Not
# release-representative, and the manifest records exactly why.
TRACED = Bake(env=ENV, build_flags={"MESHTASTIC_TRACE_LOGGING": 1}, label="lbt-traced")


def _spec(role: str = "CLIENT", tx_enabled: bool = True) -> NodeSpec:
    return NodeSpec(
        region=REGION,
        modem_preset=PRESET,
        role=role,
        debug_log_api=True,
        extra_config={
            "lora.tx_enabled": tx_enabled,
            "lora.tx_power": TX_POWER,
            # Stated rather than assumed. Every one of these is read back off the device
            # in stage 3, so a row cannot run on RF conditions it did not actually get.
            "lora.override_duty_cycle": False,
        },
    )


def _roles(dut_bake: Bake = TRACED, peer: bool = True, **kw) -> dict[str, RoleBake]:
    """Roles for a row, both on the same bake.

    The peer only has to occupy the channel, so giving it a different image would cost a
    second ~29 minute build and prove nothing. One bake means the whole table
    deduplicates to a single compile.
    """
    roles = {"dut": RoleBake("dut", dut_bake, _spec(**kw))}
    if peer:
        roles["peer"] = RoleBake("peer", dut_bake, _spec())
    return roles


SCENARIOS = [
    # -- L1: the baseline. An idle channel must be found idle. ------------------
    # Without this row a high "CAD busy" rate in L2 proves nothing: a radio that always
    # reports busy would pass L2 and be completely broken.
    Scenario(
        id="L1-idle-channel-is-free",
        description=(
            "With the bench's own peer silenced, CAD scans should mostly find the "
            "channel clear and proceed to transmit."
        ),
        # The peer is provisioned with tx_enabled false rather than left out. A peer that
        # is merely unmentioned is still a live node emitting telemetry and nodeinfo, so
        # the channel this row calls idle would not be - and the baseline would then be
        # measured against traffic the scenario never accounted for. Ambient mesh traffic
        # from nodes outside the bench remains uncontrolled, which is why the threshold
        # here is a majority rather than unanimity.
        roles={
            "dut": RoleBake("dut", TRACED, _spec()),
            "peer": RoleBake("peer", TRACED, _spec(tx_enabled=False)),
        },
        # The DUT is the source, not the peer. CAD only runs when a node wants to
        # transmit, so a DUT left listening arms nothing and the row would score NOT
        # OBSERVED against firmware that is working perfectly.
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["dut"], "count": 10, "interval_s": 5.0, "text": "L1-idle"},
        senses_channel=True,
        duration_s=45.0,
        tags=["lbt", "baseline", "negative-control"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            RateAssertion(
                "cad_finds_channel_free",
                event_patterns=[r"CAD free"],
                trial_patterns=[r"CAD arm"],
                node="dut",
                min_rate=0.5,
                min_trials=5,
            ),
        ],
    ),
    # -- L2: the core LBT claim. A busy channel must defer. ---------------------
    Scenario(
        id="L2-busy-channel-defers",
        description=(
            "With a peer occupying the channel, CAD scans should report busy and the "
            "DUT should defer rather than transmit over the top."
        ),
        roles=_roles(dut_bake=TRACED),
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["peer", "dut"], "count": 10, "interval_s": 5.0, "text": "L2-occupy"},
        senses_channel=True,
        duration_s=45.0,
        tags=["lbt", "core"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            RateAssertion(
                "cad_detects_busy_channel",
                event_patterns=[r"CAD busy", r"Can not send yet, busyRx"],
                trial_patterns=[r"CAD arm"],
                node="dut",
                min_rate=0.15,
                min_trials=5,
            ),
            # The peer's traffic must actually be reaching the DUT, or "busy" would be
            # measuring nothing. The passive observer witnesses the same air.
            PacketCount("peer_traffic_reached_dut", observer="dut", at_least=3),
        ],
    ),
    # -- L3: the handoff delivers what it armed for. ----------------------------
    Scenario(
        id="L3-cad-rx-handoff-delivers",
        description=(
            "A CAD detection that hands off to RX should deliver a packet rather than "
            "returning empty - the defect the branch's handoff commits address."
        ),
        roles=_roles(dut_bake=TRACED),
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["peer", "dut"], "count": 10, "interval_s": 6.0, "text": "L3-handoff"},
        senses_channel=True,
        duration_s=45.0,
        tags=["lbt", "handoff"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            RateAssertion(
                "handoff_delivers_a_packet",
                event_patterns=[r"CAD>RX pkt"],
                trial_patterns=[r"CAD>RX started"],
                node="dut",
                min_rate=0.25,
                min_trials=5,
            ),
        ],
    ),
    # -- L4: the handoff must never wedge the radio. ----------------------------
    # These are the WARN paths the branch added to catch a handoff that delivers nothing
    # and a receiver left un-armed. Any occurrence is a real defect, so the bound is zero.
    Scenario(
        id="L4-handoff-does-not-wedge",
        description=(
            "Across sustained traffic the CAD to RX handoff must never time out, void, "
            "or leave a missed interrupt behind."
        ),
        roles=_roles(dut_bake=TRACED),
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["peer", "dut"], "count": 16, "interval_s": 5.0, "text": "L4-soak"},
        senses_channel=True,
        duration_s=120.0,
        tags=["lbt", "soak", "regression"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            LogCount("no_handoff_timeout", [r"CAD>RX timeout"], node="dut", at_most=0),
            LogCount("no_handoff_void", [r"CAD>RX void"], node="dut", at_most=0),
            LogCount("no_missed_irq", [r"caught missed (RX|TX)_DONE"], node="dut", at_most=0),
            LogCount("no_hardware_failure", [r"Hardware Failure"], node="dut", at_most=0),
            NoDecryptFailures(),
        ],
    ),
    # -- L5: the capability gate, exercised on every run. -----------------------
    # Depends on a LOG_TRACE line. On a stock image this row is INVALID, and that is the
    # correct answer: the evidence cannot exist, so the row says nothing about firmware.
    # Swap STOCK for TRACED to actually run it.
    Scenario(
        id="L5-tx-queue-depth-under-deferral",
        description=(
            "While deferring, the TX queue should hold packets rather than dropping "
            "them. Requires a trace-logging build; INVALID on a stock image by design."
        ),
        roles=_roles(dut_bake=TRACED),
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["peer", "dut"], "count": 10, "interval_s": 5.0, "text": "L5-queue"},
        senses_channel=True,
        duration_s=45.0,
        tags=["lbt", "capability-gate"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            LogCount(
                "tx_queue_reported",
                [r"packets in TX queue"],
                node="dut",
                at_least=1,
                requires=["log.TRACE"],
            ),
        ],
    ),
    # -- L6: the negative control for the whole apparatus. ----------------------
    # A DUT with tx_enabled false must produce no transmissions at all. If this row ever
    # shows traffic from the DUT, every "the DUT transmitted" claim in the other rows is
    # measuring something else.
    Scenario(
        id="L6-tx-disabled-emits-nothing",
        description=(
            "With lora.tx_enabled false the DUT must never transmit, however busy or "
            "idle the channel is. The control that keeps the other rows honest."
        ),
        roles=_roles(dut_bake=TRACED, tx_enabled=False),
        stimulus=STIM_RF_PEER,
        stimulus_params={"sources": ["peer", "dut"], "count": 8, "interval_s": 5.0, "text": "L6-control"},
        senses_channel=True,
        duration_s=45.0,
        tags=["lbt", "negative-control"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            LogCount(
                "tx_refused",
                [r"Drop Tx packet: LoRa Tx disabled", r"send - !config\.lora\.tx_enabled"],
                node="dut",
                at_least=1,
            ),
            # The passive observer never has a client attached, so what it hears is the
            # air itself rather than anything the bench provoked. It is captured on raw
            # serial and therefore has no packet lane - the evidence is its console text,
            # matched against every wording the firmware uses for a reception.
            ObserverSilence(
                "observer_hears_nothing_from_dut",
                observer_node="observer",
                from_role="dut",
                role="dut",
            ),
        ],
    ),
]

# Rows that need the cut-down exciter image. Kept separate and NOT in the default set:
# a stock peer in a send loop occupies the channel well enough for everything above, and
# pure carrier is only needed for threshold calibration and the false-preamble path.
# Build the exciter env before running these.
EXCITER_SCENARIOS = [
    Scenario(
        id="X1-carrier-forces-deferral",
        description=(
            "Continuous carrier from the exciter, which is not a valid frame, must still "
            "be detected as channel activity and defer the DUT."
        ),
        roles=_roles(peer=False),
        stimulus=STIM_RF_EXCITER,
        stimulus_params={"source": "exciter", "mode": "carrier", "dwell_ms": 500, "count": 40},
        senses_channel=True,
        duration_s=120.0,
        tags=["lbt", "exciter", "calibration"],
        assertions=[
            SettledStateAssertion(),
            LogCount(
                "no_duty_cycle_abort",
                [r"Duty cycle limit exceeded"],
                node="dut",
                at_most=0,
            ),
            RateAssertion(
                "carrier_defers_dut",
                event_patterns=[r"CAD busy"],
                trial_patterns=[r"CAD arm"],
                node="dut",
                min_rate=0.5,
                min_trials=10,
            ),
        ],
    ),
]
