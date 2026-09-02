"""Which board an image targets, and which board a node actually is.

This module exists because of a near-miss on this bench. The node table named three
nodes, two of which were NRF52_PROMICRO_DIY and one a HELTEC_MESH_POCKET, and nothing
anywhere would have stopped the promicro image being written to the Heltec. That is not
a failed row; it is a destroyed node, and it would have looked like a routine flash right
up until the board never came back.

Every other check in this bench guards against a wrong ANSWER. This one guards against
losing the instrument, so it blocks rather than warns, and it compares what the device
says it is against what the image was built for - never against what the node table
claims, since the table is the thing most likely to be wrong.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SLUG_RE = re.compile(r"^\s*custom_meshtastic_hw_model_slug\s*=\s*(?P<slug>\S+)", re.M)
ENV_RE = re.compile(r"^\s*\[env:(?P<env>[^\]]+)\]", re.M)


class HardwareMismatch(RuntimeError):
    """The image was built for a different board than the node actually is."""


def hw_model_for_env(root: Path, env: str) -> str | None:
    """The hardware model slug a PlatformIO env targets.

    Read from the variant's own platformio.ini, which is where the mapping already
    lives - inventing a second table here would be one more thing to drift.
    """
    for ini in Path(root).glob("variants/**/platformio.ini"):
        try:
            text = ini.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        envs = [m.group("env").strip() for m in ENV_RE.finditer(text)]
        if env not in envs:
            continue
        slug = SLUG_RE.search(text)
        if slug:
            return slug.group("slug").strip()
    return None


def model_from_interface(iface: Any) -> str | None:
    """The hardware model from an interface someone already holds open.

    The serial port is exclusive, so a node held by the observer cannot also be opened
    by this check - the second connect simply fails and the model comes back unknown,
    which the flasher then correctly refuses to act on. Asking the held interface avoids
    the conflict entirely, and it is the same question either way.
    """
    try:
        info = iface.getMyNodeInfo() or {}
        return (info.get("user") or {}).get("hwModel")
    except Exception:  # noqa: BLE001
        return None


def read_hw_model(owner: Any, budget_s: float = 30.0) -> str | None:
    """The model the DEVICE reports, via the node's port owner.

    Takes an owner rather than a port: opening the port here would be a second opener on
    an exclusive device, and an open that times out abandons a thread still holding it.
    Asks the node rather than trusting the node table, because the table is hand-written
    and is exactly what gets a board wrong.
    """
    try:
        with owner.lease("hw_model", budget_s=budget_s) as iface:
            return model_from_interface(iface)
    except Exception:  # noqa: BLE001 - busy or absent both mean "could not tell"
        return None


def normalize(slug: Any) -> str:
    return str(slug or "").strip().upper().replace("-", "_")


def assert_compatible(node_name: str, device_model: str | None, image_model: str | None) -> None:
    """Refuse a flash whose image targets a different board than the node is.

    An unknown model on either side is also refused. "I could not tell" is not a licence
    to write flash to a board - the cost of being wrong here is the node, and the cost of
    stopping is one line in the node table.
    """
    if image_model is None:
        raise HardwareMismatch(
            f"{node_name}: the image does not record which board it targets, so it "
            "cannot be shown to be safe for this node"
        )
    if device_model is None:
        raise HardwareMismatch(
            f"{node_name}: the node did not report a hardware model, so it cannot be "
            "shown to match an image built for {0}".format(image_model)
        )
    if normalize(device_model) != normalize(image_model):
        raise HardwareMismatch(
            f"{node_name} is a {device_model} but the image was built for {image_model}. "
            "Writing it would very likely destroy the node. Correct the node table, or "
            "add a scenario role that targets this board."
        )
