"""Full MeshPacket capture, and honest resolution of the last-byte relay fields.

The upstream recorder stores eleven fields. Every one it omits answers a question the
beacon run got wrong by inference, and none of them can be recovered after the fact -
re-acquiring costs hardware hours. So this records the whole packet and truncates only
for display.

Two rules that are not negotiable:

  * Key material is never stored. public_key contributes its presence and length only.
  * relay_node and next_hop are stored as the RAW BYTE plus a separate resolution
    status. The wire carries only the last byte of a 32-bit NodeNum, so the mapping is
    ambiguous on a dense mesh; rendering a bare node name fabricates certainty the wire
    does not carry. Resolution happens at capture time because it is time-dependent -
    the candidate set is scoped to recently-heard neighbours, so the same byte can
    resolve differently minutes apart, and a ledger that resolves later can disagree
    with what the node actually did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# relay_node / next_hop resolution outcomes. Callers must treat AMBIGUOUS and NONE as
# "do not trust it" - the firmware's own resolver refuses to tie-break, and so do we.
UNIQUE = "unique"
AMBIGUOUS = "ambiguous"
NONE = "none"
NOT_SET = "not_set"

# Sentinels from the firmware: NO_RELAY_NODE / NO_NEXT_HOP_PREFERENCE are both 0.
NO_RELAY = 0

# Direction of a ledger row. Both are needed: a row is only meaningful when TX and RX
# are correlated, and inferring one from the absence of the other is exactly the mistake
# the beacon run kept making.
SENT = "SENT"
SEEN = "SEEN"

# Reception status. DECRYPT_FAIL is what separates "wrong key" from "weak signal".
ST_OK = "OK"
ST_DECRYPT_FAIL = "DECRYPT_FAIL"
ST_DUP = "DUP"


def _get(packet: dict, *names: str, default: Any = None) -> Any:
    """First present key among `names`. The lib mixes camelCase and snake_case."""
    for n in names:
        if n in packet and packet[n] is not None:
            return packet[n]
    return default


@dataclass(frozen=True)
class LastByte:
    """A wire-carried last byte plus what, if anything, it resolves to."""

    raw: int | None
    status: str
    node_num: int | None = None

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "raw_hex": None if self.raw is None else f"0x{self.raw:02x}",
            "status": self.status,
            "node_num": self.node_num,
        }

    def render(self) -> str:
        """Display form that never hides the uncertainty."""
        if self.raw is None or self.status == NOT_SET:
            return "direct"
        if self.status == UNIQUE and self.node_num is not None:
            return f"0x{self.raw:02x}(!{self.node_num:08x})"
        return f"0x{self.raw:02x}({self.status})"


def resolve_last_byte(value: Any, node_nums: list[int] | None) -> LastByte:
    """Resolve a last byte against the currently-known node numbers.

    Mirrors NodeDB::resolveLastByte: unique match wins, more than one candidate is
    AMBIGUOUS, none is NONE, and we never tie-break. On a sparse bench a byte often
    resolves uniquely - that is luck, not evidence, and the status field says so.
    """
    if value is None:
        return LastByte(None, NOT_SET)
    try:
        raw = int(value) & 0xFF
    except (TypeError, ValueError):
        return LastByte(None, NOT_SET)
    if raw == NO_RELAY:
        return LastByte(raw, NOT_SET)
    if not node_nums:
        return LastByte(raw, NONE)

    hits = [n for n in node_nums if (n & 0xFF) == raw]
    if len(hits) == 1:
        return LastByte(raw, UNIQUE, hits[0])
    if len(hits) > 1:
        return LastByte(raw, AMBIGUOUS)
    return LastByte(raw, NONE)


def summarize(
    packet: dict,
    *,
    node_nums: list[int] | None = None,
    direction: str = SEEN,
    observer: str | None = None,
    payload_hex_len: int = 64,
) -> dict:
    """Every field the packet exposes, plus derived status and direction.

    `node_nums` is the observing node's current NodeDB, used for last-byte resolution
    at capture time. `observer` names the node that saw it, so rows from several nodes
    can be correlated on packet id later.
    """
    if not isinstance(packet, dict):
        return {"ts": time.time(), "raw_type": type(packet).__name__}

    decoded = packet.get("decoded") or {}
    payload = decoded.get("payload")
    payload_bytes = payload if isinstance(payload, (bytes, bytearray)) else None
    encrypted = packet.get("encrypted")
    encrypted_bytes = encrypted if isinstance(encrypted, (bytes, bytearray)) else None

    body = payload_bytes if payload_bytes is not None else encrypted_bytes
    payload_size = len(body) if body is not None else _get(packet, "payloadSize", "payload_size")

    public_key = _get(packet, "publicKey", "public_key")
    pk_bytes = public_key if isinstance(public_key, (bytes, bytearray)) else None

    relay = resolve_last_byte(_get(packet, "relayNode", "relay_node"), node_nums)
    next_hop = resolve_last_byte(_get(packet, "nextHop", "next_hop"), node_nums)

    hop_limit = _get(packet, "hopLimit", "hop_limit")
    hop_start = _get(packet, "hopStart", "hop_start")

    row = {
        "ts": time.time(),
        "observer": observer,
        "dir": direction,
        # -- identity -----------------------------------------------------------
        "id": packet.get("id"),
        "from_node": _get(packet, "fromId", "from"),
        "to_node": _get(packet, "toId", "to"),
        "from_num": packet.get("from"),
        "to_num": packet.get("to"),
        "channel": packet.get("channel"),
        "portnum": decoded.get("portnum"),
        # -- radio --------------------------------------------------------------
        "rx_time": _get(packet, "rxTime", "rx_time"),
        "rx_rssi": _get(packet, "rxRssi", "rx_rssi"),
        "rx_snr": _get(packet, "rxSnr", "rx_snr"),
        # -- routing ------------------------------------------------------------
        # hop_start - hop_limit is hops actually taken. Without both, a relayed copy is
        # indistinguishable from a direct one.
        "hop_limit": hop_limit,
        "hop_start": hop_start,
        "hops_taken": (
            hop_start - hop_limit
            if isinstance(hop_start, int) and isinstance(hop_limit, int)
            else None
        ),
        "relay_node": relay.to_dict(),
        "next_hop": next_hop.to_dict(),
        # -- transport ----------------------------------------------------------
        # An RF statistic computed over via_mqtt rows is meaningless: they never
        # crossed the air, which is why they carry no RSSI.
        "via_mqtt": _get(packet, "viaMqtt", "via_mqtt", default=False),
        "transport_mechanism": _get(packet, "transportMechanism", "transport_mechanism"),
        # -- scheduling ---------------------------------------------------------
        "want_ack": _get(packet, "wantAck", "want_ack", default=False),
        "priority": packet.get("priority"),
        "delayed": packet.get("delayed"),
        "tx_after": _get(packet, "txAfter", "tx_after"),
        # -- crypto: presence and length only, never the bytes -------------------
        "pki_encrypted": _get(packet, "pkiEncrypted", "pki_encrypted", default=False),
        "public_key_len": len(pk_bytes) if pk_bytes is not None else None,
        "xeddsa_signed": _get(packet, "xeddsaSigned", "xeddsa_signed", default=False),
        # -- payload ------------------------------------------------------------
        "payload_size": payload_size,
        "payload_hex": body[:payload_hex_len].hex() if body else None,
        "encrypted": encrypted_bytes is not None and payload_bytes is None,
    }
    row["status"] = derive_status(row)
    return row


def derive_status(row: dict) -> str:
    """OK / DECRYPT_FAIL, from what the row itself carries.

    A packet that arrived with signal but no decoded portnum failed to decrypt - it was
    addressed to a channel this node holds no key for. That is the distinction between
    "wrong key" and "out of range", and it is the field that would have resolved the
    beacon run's R1 confusion immediately: those packets arrived at -64 dBm with healthy
    SNR and failed only to decrypt.

    DUP is not derivable from a single row; the ledger assigns it when it sees a packet
    id twice on one observer.
    """
    if row.get("portnum") is None and row.get("encrypted"):
        return ST_DECRYPT_FAIL
    return ST_OK


DISPLAY_HEADER = (
    f"{'ts':<12} {'node':<10} {'dir':<4} {'pkt_id':<10} {'portnum':<18} "
    f"{'from->to':<22} {'via':<20} {'ch':>3} {'rssi':>5} {'snr':>6} {'hops':>5} "
    f"{'size':>5}  payload           status"
)


def display_row(row: dict) -> str:
    """Fixed-width, greppable projection. A view of the record, never a substitute."""

    def cell(value, width, right=False):
        text = "" if value is None else str(value)
        text = text[:width]
        return text.rjust(width) if right else text.ljust(width)

    # A ledger row already knows how many hops the copy took; a raw capture row carries
    # the two counters it is derived from and shows both, since start-limit is the
    # evidence and the difference is the conclusion.
    if row.get("hops") is not None:
        hops = str(row["hops"])
    else:
        hop_start, hop_limit = row.get("hop_start"), row.get("hop_limit")
        hops = f"{hop_start}-{hop_limit}" if hop_start is not None else ""

    # A caller that already rendered the path (the ledger, which keeps one row per
    # distinct path) passes it directly; otherwise derive it from the stored bytes.
    via = row.get("via")
    if via is None:
        relay = row.get("relay_node") or {}
        via = LastByte(**_lastbyte_args(relay)).render() if relay else ""

    endpoints = f"{row.get('from_node')}->{row.get('to_node')}"
    ts = time.strftime("%H:%M:%S", time.localtime(row.get("ts") or 0))

    return " ".join(
        [
            cell(ts, 11),
            cell(row.get("observer"), 10),
            cell(row.get("dir"), 4),
            cell(_hexid(row.get("id")), 10),
            cell(row.get("portnum"), 18),
            cell(endpoints, 22),
            cell(via, 20),
            cell(row.get("channel"), 3, right=True),
            cell(row.get("rx_rssi"), 5, right=True),
            cell(row.get("rx_snr"), 6, right=True),
            cell(hops, 5, right=True),
            cell(row.get("payload_size"), 5, right=True),
            cell(row.get("payload_hex"), 16),
            str(row.get("status") or ""),
        ]
    )


def _lastbyte_args(d: dict) -> dict:
    return {"raw": d.get("raw"), "status": d.get("status", NOT_SET), "node_num": d.get("node_num")}


def _hexid(value: Any) -> str | None:
    if isinstance(value, int):
        return f"0x{value:08x}"
    return value
