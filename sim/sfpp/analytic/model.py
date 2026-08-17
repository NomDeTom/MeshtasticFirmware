#!/usr/bin/env python3
"""Closed-form expected cost of each catch-up strategy.

This exists to disagree with simulate.py. The Monte Carlo draws gaps at random and
counts what happens; this computes what should happen, from the same wire constants and
independent arithmetic. Where the two agree, the number is probably right. Where they
diverge, one of them is wrong and validate.py says so rather than letting a chart go out
with a confident line through it.

The one interesting piece of arithmetic is the chain walk. LINK_REQUEST/LINK_PROVIDE
returns the single link after a known commit hash, so a node resumes at its earliest gap
and re-receives every link after it. For d gaps uniformly placed among n objects, the
expected index of the first gap is (n - d) / (d + 1), so the expected walk is

    E[links walked] = n - (n - d) / (d + 1)

which is already most of the window at d = 3 and effectively all of it by d = 8. That
is the cost this design removes, and it is worth stating in closed form rather than only
observing it.
"""

WIRE = {
    "sr_envelope": 18,  # type + version + scope + bucket + count
    "sr_checksum": 9,
    "short_id": 4,
    "object_id": 32,  # a manifest that names full hashes
    "sfpp_request": 30,  # LINK_REQUEST
    "sfpp_overhead": 14,  # SFPP framing around a provided object
}


def expected_walk(n, d):
    """Links a chain walk transfers to resolve d gaps among n."""
    if d == 0:
        return 0.0
    return n - (n - d) / (d + 1)


def chain_bytes(n, d, mean_object):
    walked = expected_walk(n, d)
    return walked * (WIRE["sfpp_request"] + mean_object + WIRE["sfpp_overhead"])


def chain_round_trips(n, d):
    return expected_walk(n, d)


def enum_bytes(n, d, mean_object, id_bytes=WIRE["object_id"]):
    """Ask, receive the whole list, request the missing, receive them."""
    cost = WIRE["sr_envelope"]  # ENUM_REQUEST
    cost += WIRE["sr_envelope"] + id_bytes * n  # the list
    if d:
        cost += WIRE["sr_envelope"] + WIRE["short_id"] * d  # ITEM_REQUEST
        cost += d * (mean_object + WIRE["sfpp_overhead"])
    return cost


def enum_round_trips(n, d):
    return 1 + (1 if d else 0)


def sketch_bytes(n, d, mean_object, capacity):
    """One advert, one request, the objects - plus enumeration if capacity is short."""
    cost = WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * capacity
    if d == 0:
        return cost
    if d <= capacity:
        cost += WIRE["sr_envelope"] + WIRE["short_id"] * d
        cost += d * (mean_object + WIRE["sfpp_overhead"])
        return cost
    return cost + enum_bytes(n, d, mean_object)


def sketch_round_trips(n, d, capacity):
    if d == 0:
        return 0
    if d <= capacity:
        return 1
    return 1 + enum_round_trips(n, d)


def costs(n, d, mean_object, capacity, id_bytes=WIRE["object_id"]):
    return {
        "chain": {
            "bytes": chain_bytes(n, d, mean_object),
            "round_trips": chain_round_trips(n, d),
        },
        "enum": {
            "bytes": enum_bytes(n, d, mean_object, id_bytes),
            "round_trips": enum_round_trips(n, d),
        },
        "sketch": {
            "bytes": sketch_bytes(n, d, mean_object, capacity),
            "round_trips": sketch_round_trips(n, d, capacity),
        },
    }


def sketch_vs_enum_crossover(
    n, mean_object, id_bytes=WIRE["object_id"], capacity_rule=lambda d: d
):
    """Smallest d > 0 at which enumeration becomes the cheaper way to identify."""
    for d in range(1, n + 1):
        if sketch_bytes(n, d, mean_object, capacity_rule(d)) > enum_bytes(
            n, d, mean_object, id_bytes
        ):
            return d
    return None
