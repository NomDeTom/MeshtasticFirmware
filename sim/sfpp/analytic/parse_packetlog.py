#!/usr/bin/env python3
"""Extract per-packet facts from a meshtastic-compression-test packet log.

The log is one decrypted packet per line, hex bytes separated by spaces: a
16-byte radio header followed by the Data protobuf, so byte 16 is field 1's tag
and byte 17 the port number. Only packets the capture could decrypt are present,
which means default-key traffic only - see the note on what that biases.

Usage: parse_packetlog.py <packetlog.txt.gz> [--csv out.csv]
"""

import argparse
import collections
import gzip
import json
import sys

# Port numbers we care about naming; everything else is reported by number.
PORTS = {
    0: "UNKNOWN",
    1: "TEXT_MESSAGE",
    3: "POSITION",
    4: "NODEINFO",
    5: "ROUTING",
    6: "ADMIN",
    7: "TEXT_MESSAGE_COMPRESSED",
    8: "WAYPOINT",
    10: "DETECTION_SENSOR",
    12: "KEY_VERIFICATION",
    34: "PAXCOUNTER",
    35: "STORE_FORWARD_PLUSPLUS",
    65: "STORE_FORWARD",
    66: "RANGE_TEST",
    67: "TELEMETRY",
    70: "TRACEROUTE",
    71: "NEIGHBORINFO",
    73: "MAP_REPORT",
}

HEADER_LEN = 16
BROADCAST = 0xFFFFFFFF


def read_varint(data, i):
    """Returns (value, next_index), or (None, i) if the buffer runs out."""
    value = 0
    shift = 0
    while i < len(data):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 28:
            break
    return None, i


def parse_line(line):
    try:
        raw = bytes.fromhex(line.replace(" ", ""))
    except ValueError:
        return None
    if len(raw) < HEADER_LEN + 2 or raw[HEADER_LEN] != 0x08:
        return None

    portnum, i = read_varint(raw, HEADER_LEN + 1)
    if portnum is None:
        return None

    payload_len = 0
    if i < len(raw) and raw[i] == 0x12:  # field 2, length-delimited payload
        payload_len, _ = read_varint(raw, i + 1)
        payload_len = payload_len or 0

    # The capture stores {from, to, id}, NOT the on-air PacketHeader order of {to, from, id}:
    # 0xFFFFFFFF appears at offset 4 in 67.6% of packets and never at offset 0.
    return {
        "src": int.from_bytes(raw[0:4], "little"),
        "dst": int.from_bytes(raw[4:8], "little"),
        "id": int.from_bytes(raw[8:12], "little"),
        "portnum": portnum,
        "payload_len": payload_len,
        "packet_len": len(raw),
        "broadcast": int.from_bytes(raw[4:8], "little") == BROADCAST,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("--csv")
    args = ap.parse_args()

    opener = gzip.open if args.logfile.endswith(".gz") else open
    by_port = collections.Counter()
    bcast_by_port = collections.Counter()
    bytes_by_port = collections.Counter()
    text_sizes = []
    all_sizes = []
    senders = collections.Counter()
    total = 0
    unparsed = 0

    rows = []
    with opener(args.logfile, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = parse_line(line)
            if rec is None:
                unparsed += 1
                continue
            total += 1
            by_port[rec["portnum"]] += 1
            bytes_by_port[rec["portnum"]] += rec["packet_len"]
            if rec["broadcast"]:
                bcast_by_port[rec["portnum"]] += 1
            all_sizes.append(rec["packet_len"])
            senders[rec["src"]] += 1
            if rec["portnum"] in (1, 7):
                text_sizes.append(rec["packet_len"])
            if args.csv:
                rows.append(rec)

    if args.csv:
        import csv

        with open(args.csv, "w", newline="") as out:
            w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    text = by_port[1] + by_port[7]
    summary = {
        "packets": total,
        "unparsed_lines": unparsed,
        "distinct_senders": len(senders),
        "text_packets": text,
        "text_share": round(text / total, 5) if total else 0,
        "text_broadcast": bcast_by_port[1] + bcast_by_port[7],
        "text_broadcast_share_of_text": (
            round((bcast_by_port[1] + bcast_by_port[7]) / text, 4) if text else 0
        ),
        "mean_packet_bytes": (
            round(sum(all_sizes) / len(all_sizes), 1) if all_sizes else 0
        ),
        "mean_text_bytes": (
            round(sum(text_sizes) / len(text_sizes), 1) if text_sizes else 0
        ),
        "by_port": [
            {
                "port": p,
                "name": PORTS.get(p, str(p)),
                "packets": c,
                "share": round(c / total, 5),
                "broadcast_share": round(bcast_by_port[p] / c, 3),
                "mean_bytes": round(bytes_by_port[p] / c, 1),
            }
            for p, c in by_port.most_common()
        ],
    }
    json.dump(summary, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
