"""The ledger: the interpreting half of stage 0, and the surface assertions run against.

Two lanes, because the two consumers need different primary evidence.

  Packet lane. One row per packet per observing node, deduplicated by id. Compression is
  dramatic and necessary - a 4-minute bidirectional sounding produced 249 RF log lines
  for 8 transmitted messages, because mesh rebroadcast at hop_limit 3 emits each packet
  many times. Raw volume is ~30x the information content.

  Log-event lane. Counts of matched firmware log lines. The beacon case treats these as
  corroboration, but for anything that tests a decision NOT to transmit they are the
  only evidence there is: the packet lane is empty by definition when the radio
  correctly defers.

Both lanes reduce to COUNTS. That is deliberate and it is the central design decision of
this bench. There is no clock here fine enough to time a radio-layer event - the
firmware prints uptime in whole seconds, host timestamps carry USB buffering jitter, and
two nodes share no time base at all. So the bench never asserts on when one event
happened; it asserts on how often something happened across many trials, which needs no
shared clock and is a stronger claim than any single trace.

Two aggregation rules that exist because averaging destroyed real information:

  * Keep every distinct (relay_node, rssi) pair. Do not average across paths - a -54 dBm
    mean over a -38 direct and a -73 relayed path describes no real path, only the
    midpoint of two.
  * Report median and spread, never mean alone.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import packets, streams


@dataclass
class Sighting:
    """One observer's view of one packet id, with every distinct path it arrived by."""

    packet_id: int | None
    observer: str | None
    direction: str
    portnum: str | None
    from_node: str | None
    to_node: str | None
    channel: int | None
    status: str
    first_ts: float
    last_ts: float
    rebroadcast_count: int = 1
    # Every distinct (relay rendering, rssi, snr, hops_taken) this id arrived by. A set,
    # because collapsing them is what erases the direct-vs-relayed distinction.
    paths: list[tuple[str, float | None, float | None, int | None]] = field(default_factory=list)
    via_mqtt: bool = False
    payload_size: int | None = None

    @property
    def rssis(self) -> list[float]:
        return [p[1] for p in self.paths if p[1] is not None]

    @property
    def snrs(self) -> list[float]:
        return [p[2] for p in self.paths if p[2] is not None]

    def to_dict(self) -> dict:
        return {
            "packet_id": self.packet_id,
            "observer": self.observer,
            "dir": self.direction,
            "portnum": self.portnum,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "channel": self.channel,
            "status": self.status,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "rebroadcast_count": self.rebroadcast_count,
            "paths": [
                {"via": v, "rssi": r, "snr": s, "hops_taken": h} for v, r, s, h in self.paths
            ],
            "via_mqtt": self.via_mqtt,
            "payload_size": self.payload_size,
        }


class PacketLane:
    """Deduplicated packet sightings, and the cross-node correlation over them."""

    def __init__(self, rows: Iterable[dict]) -> None:
        self._sightings: dict[tuple[str | None, int | None], Sighting] = {}
        for row in rows:
            self._add(row)

    def _add(self, row: dict) -> None:
        pid = row.get("id")
        obs = row.get("observer")
        key = (obs, pid)
        relay = row.get("relay_node") or {}
        via = packets.LastByte(
            raw=relay.get("raw"),
            status=relay.get("status", packets.NOT_SET),
            node_num=relay.get("node_num"),
        ).render()
        path = (via, row.get("rx_rssi"), row.get("rx_snr"), row.get("hops_taken"))
        ts = row.get("ts") or 0.0

        existing = self._sightings.get(key)
        if existing is None:
            self._sightings[key] = Sighting(
                packet_id=pid,
                observer=obs,
                direction=row.get("dir") or packets.SEEN,
                portnum=row.get("portnum"),
                from_node=row.get("from_node"),
                to_node=row.get("to_node"),
                channel=row.get("channel"),
                status=row.get("status") or packets.ST_OK,
                first_ts=ts,
                last_ts=ts,
                paths=[path],
                via_mqtt=bool(row.get("via_mqtt")),
                payload_size=row.get("payload_size"),
            )
            return

        # A repeat of an id this observer already saw: a rebroadcast, not new information
        # about delivery - but the PATH may be new, and that is information.
        existing.rebroadcast_count += 1
        existing.last_ts = max(existing.last_ts, ts)
        if path not in existing.paths:
            existing.paths.append(path)

    # -- access ----------------------------------------------------------------

    def sightings(self) -> list[Sighting]:
        return sorted(self._sightings.values(), key=lambda s: s.first_ts)

    def by_observer(self, observer: str) -> list[Sighting]:
        return [s for s in self.sightings() if s.observer == observer]

    def correlate(self) -> dict[int | None, list[Sighting]]:
        """Every observer's view of each packet id.

        This is what lets one line say that A sent it, B received it direct at -70 dBm,
        and the observer saw it relayed at -38 dBm. Correlating on id is the only way to
        tie a transmission to a reception; inferring one from the absence of the other is
        the mistake this whole bench exists to stop.
        """
        out: dict[int | None, list[Sighting]] = defaultdict(list)
        for s in self.sightings():
            out[s.packet_id].append(s)
        return dict(out)

    # -- counting --------------------------------------------------------------

    def count(
        self,
        observer: str | None = None,
        portnum: str | None = None,
        from_node: str | None = None,
        status: str | None = None,
        direction: str | None = None,
        rf_only: bool = False,
    ) -> int:
        """Distinct packets matching a filter. The assertion primitive."""
        return len(
            self.select(
                observer=observer,
                portnum=portnum,
                from_node=from_node,
                status=status,
                direction=direction,
                rf_only=rf_only,
            )
        )

    def select(
        self,
        observer: str | None = None,
        portnum: str | None = None,
        from_node: str | None = None,
        status: str | None = None,
        direction: str | None = None,
        rf_only: bool = False,
    ) -> list[Sighting]:
        out = []
        for s in self.sightings():
            if observer is not None and s.observer != observer:
                continue
            if portnum is not None and s.portnum != portnum:
                continue
            if from_node is not None and s.from_node != from_node:
                continue
            if status is not None and s.status != status:
                continue
            if direction is not None and s.direction != direction:
                continue
            # An RF statistic computed over packets that never crossed the air is
            # meaningless; rf_only drops the node's own local traffic and anything that
            # arrived over MQTT. Either RSSI or SNR is enough to prove it was received
            # off the radio - the library populates them independently, and a packet
            # with SNR but no RSSI still demonstrably crossed the air.
            if rf_only and (s.via_mqtt or not (s.rssis or s.snrs)):
                continue
            out.append(s)
        return out

    def decrypt_failures_by_source(self) -> dict[str | None, int]:
        """Decrypt failures per source node.

        How you notice a node is on the wrong key rather than out of range - the beacon
        run's R1 spent hours being read as a range problem when every failing packet was
        arriving at -64 dBm with healthy SNR.
        """
        out: dict[str | None, int] = defaultdict(int)
        for s in self.sightings():
            if s.status == packets.ST_DECRYPT_FAIL:
                out[s.from_node] += 1
        return dict(out)

    def rf_stats(self, observer: str | None = None) -> dict:
        """Median and spread, never mean alone, over genuinely-RF sightings."""
        rssis: list[float] = []
        snrs: list[float] = []
        for s in self.select(observer=observer, rf_only=True):
            rssis.extend(s.rssis)
            snrs.extend(s.snrs)
        return {
            "observer": observer,
            "samples": len(rssis),
            "rssi": _spread(rssis),
            "snr": _spread(snrs),
        }

    def summary(self) -> dict:
        observers = sorted({s.observer for s in self.sightings() if s.observer})
        return {
            "distinct_packets": len(self._sightings),
            "total_rebroadcasts": sum(s.rebroadcast_count for s in self.sightings()),
            "by_observer": {
                o: {
                    "packets": self.count(observer=o),
                    "rf": self.rf_stats(o),
                }
                for o in observers
            },
            "decrypt_failures_by_source": self.decrypt_failures_by_source(),
        }


def _spread(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    vals = sorted(values)
    return {
        "n": len(vals),
        "median": round(statistics.median(vals), 2),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
        # Spread matters more than centre when two paths are in play: a wide spread is
        # the signature of direct-plus-relayed reception, which a mean hides entirely.
        "p25": round(vals[len(vals) // 4], 2),
        "p75": round(vals[(3 * len(vals)) // 4], 2),
        "stdev": round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0,
    }


class LogLane:
    """Counted firmware log events.

    Every pattern is a list of alternatives, never a single string. Log wording is not a
    stable contract: it varies with firmware version, build flags and - critically -
    whether a client is attached, because an attached client makes the node route
    received packets to it and log them differently. The identical reception appears as
    "Received text msg from=..." with no client and "phone downloaded packet (id=...)"
    with one. A check that matched only the first scored a working link as zero received,
    twice.
    """

    def __init__(self, rows: Iterable[dict]) -> None:
        self.rows = list(rows)

    def matching(
        self,
        patterns: Sequence[str],
        node: str | None = None,
        level: str | None = None,
    ) -> list[dict]:
        regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
        out = []
        for row in self.rows:
            if node is not None and row.get("node") != node:
                continue
            if level is not None and row.get("level") != level:
                continue
            line = row.get("line") or row.get("msg") or ""
            if any(r.search(line) for r in regexes):
                out.append(row)
        return out

    def count(
        self,
        patterns: Sequence[str],
        node: str | None = None,
        level: str | None = None,
    ) -> int:
        return len(self.matching(patterns, node=node, level=level))

    def by_node(self) -> dict[str | None, int]:
        out: dict[str | None, int] = defaultdict(int)
        for row in self.rows:
            out[row.get("node")] += 1
        return dict(out)

    def levels(self) -> dict[str | None, int]:
        out: dict[str | None, int] = defaultdict(int)
        for row in self.rows:
            out[row.get("level")] += 1
        return dict(out)

    def summary(self) -> dict:
        return {"lines": len(self.rows), "by_node": self.by_node(), "by_level": self.levels()}


@dataclass
class Ledger:
    """Both lanes over one slice of a run."""

    packets: PacketLane
    logs: LogLane
    start_ts: float | None = None
    end_ts: float | None = None
    label: str | None = None

    @classmethod
    def from_run(
        cls,
        run_dir: Path,
        start: float | None = None,
        end: float | None = None,
        label: str | None = None,
    ) -> Ledger:
        return cls(
            packets=PacketLane(streams.window(run_dir, streams.PACKETS, start, end)),
            logs=LogLane(streams.window(run_dir, streams.LOGS, start, end)),
            start_ts=start,
            end_ts=end,
            label=label,
        )

    @classmethod
    def for_scenario(cls, run_dir: Path, scenario_id: str) -> Ledger:
        """The slice between this scenario's start and end markers."""
        found = {m["label"]: m["ts"] for m in streams.marks(run_dir)}
        return cls.from_run(
            run_dir,
            start=found.get(f"{scenario_id}:start"),
            end=found.get(f"{scenario_id}:end"),
            label=scenario_id,
        )

    def summary(self) -> dict:
        return {
            "label": self.label,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "packets": self.packets.summary(),
            "logs": self.logs.summary(),
        }

    def render(self, limit: int = 40) -> str:
        """Fixed-width ledger, one line per distinct path a packet arrived by."""
        lines = [packets.DISPLAY_HEADER]
        for sighting in self.packets.sightings()[:limit]:
            for via, rssi, snr, hops in sighting.paths:
                lines.append(
                    packets.display_row(
                        {
                            "ts": sighting.first_ts,
                            "observer": sighting.observer,
                            "dir": sighting.direction,
                            "id": sighting.packet_id,
                            "portnum": sighting.portnum,
                            "from_node": sighting.from_node,
                            "to_node": sighting.to_node,
                            "channel": sighting.channel,
                            "rx_rssi": rssi,
                            "rx_snr": snr,
                            "hops": hops,
                            "payload_size": sighting.payload_size,
                            "status": sighting.status,
                            "via": via,
                        }
                    )
                )
        return "\n".join(lines)
