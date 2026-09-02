"""A short cycle that proves the bench works, end to end, on stock firmware.

Not a firmware test. Every assertion here is about the bench: that it can flash a node,
put it into a known state, make two radios talk, capture what crossed the air and reach a
verdict from it. If this row is green the machinery is sound, and a red row from a real
scenario afterwards is about the firmware rather than the harness.

Deliberately built on an UPSTREAM RELEASE rather than a local compile. It takes half an
hour to build this firmware, which is far too long to sit between "is the bench working?"
and the answer - and a stock release is also the better control: nothing in it was
produced by the same tree the bench is testing, so a pass cannot be an artefact of a local
change.

The release carries no BENCH_BUILD_TAG, so the settled-state check does not ask for one.
That is the capability gate doing its job in the quiet direction: the bench only demands
evidence an image can actually produce.
"""

from __future__ import annotations

import os

from ..firmware import FirmwareStore
from ..manifest import Bake
from ..provision import NodeSpec
from ..scenario import (
    LogCount,
    PacketCount,
    RoleBake,
    Scenario,
    SettledStateAssertion,
    STIM_RF_PEER,
)

ENV = "nrf52_promicro_diy_tcxo"
REGION = "EU_868"

# Same RF conditions as the LBT table, for the same two reasons: LONG_SLOW puts the bench
# on a different spreading factor from the local LONG_FAST mesh, so ambient traffic is not
# a variable, and 10 dBm is far more than two nodes on one desk need.
PRESET = "LONG_SLOW"
TX_POWER = 10

# The image comes from the bench's own store of known-good releases, not a path written
# here. Commission a bench with:
#
#   python -m bench firmware fetch nrf52_promicro_diy_tcxo           # newest stable
#   python -m bench firmware fetch nrf52_promicro_diy_tcxo --alpha   # newest prerelease
#
# The store re-verifies the digest before handing an image over, so "known-good" stays a
# checked claim rather than a filename. BENCH_SMOKE_VERSION pins a specific release; left
# unset it takes the newest stored image for this board.
BOARD = "NRF52_PROMICRO_DIY"
PINNED = os.environ.get("BENCH_SMOKE_VERSION")
# Stable by default. A smoke test exists to prove the bench, so its firmware should be the
# least surprising thing in the room; BENCH_SMOKE_CHANNEL=alpha runs it against the newest
# prerelease instead.
CHANNEL = os.environ.get("BENCH_SMOKE_CHANNEL", "stable")


def stock_bake() -> Bake:
    """The reference image for this board, taken from the bench's store.

    Resolved when the table is imported, so a bench with an empty store fails at load
    with an actionable message rather than part-way through a run. The store raises
    rather than returning nothing, because a scenario that silently got no image would
    flash whatever was lying around and the row would not mean what it says.
    """
    store = FirmwareStore()
    image = store.get(BOARD, version=PINNED, channel=None if PINNED else CHANNEL)
    return Bake(
        env=ENV,
        label=f"upstream {image.version}",
        prebuilt=str(image.path(store.root)),
    )


# Six messages each way at six-second spacing. Enough to distinguish a working link from a
# lucky packet, and small enough to sit well inside the 10% duty cycle LONG_SLOW spends
# quickly. Delivery between two nodes on one desk should be all six; three is the bar,
# because this row exists to prove the bench rather than to measure the radio.
SENDS = 6
INTERVAL_S = 6.0
DELIVERED_AT_LEAST = 3

STOCK = stock_bake()


def _spec() -> NodeSpec:
    return NodeSpec(
        region=REGION,
        modem_preset=PRESET,
        role="CLIENT",
        debug_log_api=True,
        extra_config={
            "lora.tx_enabled": True,
            "lora.tx_power": TX_POWER,
            "lora.override_duty_cycle": False,
        },
    )


SCENARIOS = [
    Scenario(
        id="S1-two-nodes-talk",
        description=(
            "Flash both nodes with a stock upstream release, provision them onto "
            "LONG_SLOW, and have them exchange messages in both directions."
        ),
        roles={
            "dut": RoleBake("dut", STOCK, _spec()),
            "peer": RoleBake("peer", STOCK, _spec()),
        },
        stimulus=STIM_RF_PEER,
        stimulus_params={
            "sources": ["dut", "peer"],
            "count": SENDS,
            "interval_s": INTERVAL_S,
            "text": "S1-hello",
        },
        senses_channel=False,  # nothing here asks what the radio heard before transmitting
        duration_s=60.0,
        tags=["smoke", "bench-self-test"],
        assertions=[
            # Both nodes are where the scenario says they are. Without this a green row
            # could be two nodes agreeing on the wrong settings.
            SettledStateAssertion(name="dut_settled", role="dut"),
            SettledStateAssertion(name="peer_settled", role="peer"),

            # The link, asserted in both directions separately. One direction working
            # proves one transmitter and one receiver; it does not prove the pair.
            PacketCount(
                "dut_heard_peer",
                observer="dut",
                from_role="peer",
                at_least=DELIVERED_AT_LEAST,
                role="dut",
            ),
            PacketCount(
                "peer_heard_dut",
                observer="peer",
                from_role="dut",
                at_least=DELIVERED_AT_LEAST,
                role="peer",
            ),

            # A duty-cycle abort would stop a node transmitting, and the delivery counts
            # above would then read as a broken link rather than a bench that asked for
            # too much airtime. Name it instead.
            LogCount("no_duty_cycle_abort", [r"Duty cycle limit exceeded"],
                     node="dut", at_most=0, role="dut"),
        ],
    ),
]
