"""Contention A/B: does a firmware's listen-before-talk keep two simultaneous senders apart?

    python -m bench --run ab-1 run --scenarios contention

Two nodes are told to send at the same instant, many times over; passive witnesses count
which of each pair arrived. The same rows run on two prebuilt images - the baseline and the
branch under test - so the only thing that differs between A and B is the firmware.

Unlike the lbt table, nothing here reads a log string. Both images must be judged by the
same evidence, and the baseline does not print the branch's CAD lines, so every figure is
a packet count from the ledger: delivered frames per sender at each witness, and whether
each sender decoded the other's frame (it can only have done so if it was listening rather
than transmitting over it). The assertions only prove the row measured something; the
comparison between images is made from packets.jsonl afterwards.

Images are read from BENCH_AB_DIR (default ~/bench-firmware/ab):
    ab_develop.uf2   baseline
    ab_lbt.uf2       branch under test
    lbt_traced.uf2   branch with MESHTASTIC_TRACE_LOGGING, for identification only

Air: SHORT_FAST on EU_868, and the narrow presets on EU_N_868 slot 3 - slot 1 and LONG_FAST
carry the local mesh. Every node is CLIENT_MUTE, so nothing rebroadcasts and each send is
exactly one frame on air; witnesses additionally have tx_enabled false.
"""

from __future__ import annotations

import os
from pathlib import Path

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
TX_POWER = 10
IMAGE_DIR = Path(os.environ.get("BENCH_AB_DIR", Path.home() / "bench-firmware" / "ab"))


def _image(tag: str, label: str) -> Bake:
    return Bake(env=ENV, label=label, prebuilt=str(IMAGE_DIR / f"{tag}.uf2"))


IMAGES = {
    "dev": _image("ab_develop", "develop 8ef996f5b"),
    "lbt": _image("ab_lbt", "listening-now 631cf3a67"),
}
TRACED = _image("lbt_traced", "listening-now 631cf3a67 traced")

# (short name, region, preset, channel_num, pairs, interval_s). Intervals leave room for
# both frames plus the widest backoff before the next pair, so pairs never overlap each
# other; counts keep every node well inside the 10% duty cycle.
AIR = [
    ("SF", "EU_868", "SHORT_FAST", 0, 80, 2.5),
    ("NF", "EU_N_868", "NARROW_FAST", 3, 60, 4.0),
    ("NS", "EU_N_868", "NARROW_SLOW", 3, 50, 6.0),
]

SENDERS = ("dut", "peer")
WITNESSES = ("witness", "lr")


def _spec(region: str, preset: str, channel_num: int, tx: bool) -> NodeSpec:
    extra = {
        "lora.tx_enabled": tx,
        "lora.tx_power": TX_POWER,
        "lora.override_duty_cycle": False,
    }
    if channel_num:
        extra["lora.channel_num"] = channel_num
    return NodeSpec(
        region=region,
        modem_preset=preset,
        role="CLIENT_MUTE",
        debug_log_api=True,
        extra_config=extra,
    )


def _row(img: str, air: tuple, senders=SENDERS, witnesses=WITNESSES) -> Scenario:
    short, region, preset, ch, pairs, interval = air
    bake = IMAGES[img]
    rid = f"C-{short}-{img}"
    roles = {r: RoleBake(r, bake, _spec(region, preset, ch, True)) for r in senders}
    roles.update({r: RoleBake(r, bake, _spec(region, preset, ch, False)) for r in witnesses})
    heard = [
        PacketCount(
            f"{w}_heard_{s}", observer=w, from_role=s, portnum="TEXT_MESSAGE_APP", at_least=1
        )
        for w in witnesses
        for s in senders
    ]
    return Scenario(
        id=rid,
        description=f"{bake.label}: {pairs} simultaneous pairs on {preset} ({region} slot {ch or 'default'}).",
        roles=roles,
        stimulus=STIM_RF_PEER,
        stimulus_params={
            "sources": list(senders),
            "count": pairs,
            "interval_s": interval,
            "text": rid,
            "concurrent": True,
        },
        senses_channel=True,
        duration_s=pairs * interval + 20.0,
        tags=["contention", img, short],
        assertions=[
            *(SettledStateAssertion(name=f"settled_{r}", role=r) for r in roles),
            *(
                LogCount(f"no_duty_cycle_abort_{s}", [r"Duty cycle limit exceeded"], node=s, at_most=0)
                for s in senders
            ),
            *heard,
        ],
    )


# Identification: every node sends in turn on the traced image, so each SX1262 receiver
# logs the frequency offset it corrected for every other sender. The node without a TCXO is
# the one every other receiver sees far off, and its own readings of the others are too.
IDENTIFY = Scenario(
    id="I1-frequency-offsets",
    description="Traced image, all four send in turn on SHORT_FAST; read Corrected frequency offset per sender.",
    roles={r: RoleBake(r, TRACED, _spec("EU_868", "SHORT_FAST", 0, True)) for r in SENDERS + WITNESSES},
    stimulus=STIM_RF_PEER,
    stimulus_params={"sources": list(SENDERS + WITNESSES), "count": 8, "interval_s": 3.0, "text": "I1"},
    senses_channel=True,
    duration_s=8 * 3.0 + 20.0,
    tags=["identify"],
    assertions=[
        LogCount("offsets_logged", [r"Corrected frequency offset"], node="dut", at_least=3),
    ],
)

# Baseline first, then the branch in reverse preset order, so the boundary between images
# does not also change the air and each image change costs one reprovision, not two.
# Run I1 alone first (--only I1-frequency-offsets) to decide which node is the witness.
SCENARIOS = [
    IDENTIFY,
    *(_row("dev", a) for a in AIR),
    *(_row("lbt", a) for a in reversed(AIR)),
]
