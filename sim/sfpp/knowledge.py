"""What each node knows, what it has forgotten, and what that costs when the mesh breaks.

The transport models the firmware's bounded stores: a NodeDB capped at MAX_NUM_NODES and a packet
history of twice that. Both silently discard things. This module is the instrument for that - it
samples what every node holds, and answers the questions the caps actually raise:

- **Does the mesh fit?** A 60-node mesh does not fit in an STM32WL's ten slots, so its routing sees
  a different mesh from the one that exists.
- **What is a route worth?** A learned next hop can be lost three different ways on three different
  clocks, and only one of them is the 30-minute TTL people quote - plus a fourth thing that looks
  like a loss in the counters and is not one, an ambiguous relay byte refusing to resolve.
- **What survives a break?** After a partition or a node failure, how much of what the mesh knew is
  still true, and how long does it take to notice it is not?

`snapshot()` is cheap enough to call on a timer; `Recorder` does that and keeps the series.
`render()` draws it. Nothing here mutates the mesh.
"""

import json
import math

# A stored route dies three ways, on three different clocks, and only the first is the 30-minute
# TTL people quote. Naming them here because the distinction is the entire point of the instrument.
ROUTE_DEATHS = (
    ("route_expired_ttl", "TTL, 30 min unconfirmed"),
    ("route_expired_failures", "3 failed directed deliveries"),
    ("routes_lost_to_eviction", "destination evicted from NodeDB"),
)

# Not a death - a refusal. An ambiguous relay byte does not delete anything; it makes this one
# decision fall back to the safe branch, and the next decision may resolve cleanly. Counted apart
# from the deaths because conflating the two would make a healthy dense mesh look like a broken one.
RESOLUTION_FAILURES = (("next_hop_ambiguous", "relay byte shared by two known nodes"),)


def node_knowledge(mesh, index):
    """What one node holds right now, split into what is still true and what is not.

    Coverage is measured against what this node can reach *now*, and counts only records for nodes
    it can still reach. Records for anything else are counted separately as `stale_records`: after a
    partition a node keeps everything it learned about the far side, and a denominator that ignored
    that would report coverage above 100% - which is how the split was found to be a no-op.
    """
    node = mesh.nodes[index]
    # Reachable over the live graph, so a partition or a downed node shrinks it as it should.
    reachable = {
        peer
        for peer in mesh.hops_from([index])
        if peer != index and mesh.nodes[peer].online
    }
    fresh_cutoff = mesh.now - 2 * 60 * 60 * 1000.0
    known = len(node.nodedb)
    known_reachable = sum(1 for peer in node.nodedb if peer in reachable)
    return {
        "node": index,
        "platform": node.platform,
        "firmware": node.profile.name,
        "role": node.role,
        "online": node.online,
        "store_capacity": node.max_num_nodes,
        "known": known,
        "known_reachable": known_reachable,
        # Records for peers this node can no longer reach: knowledge that has outlived its subject.
        "stale_records": known - known_reachable,
        "reachable": len(reachable),
        "known_fresh": sum(
            1 for r in node.nodedb.values() if r.last_heard >= fresh_cutoff
        ),
        "neighbours_known": sum(1 for r in node.nodedb.values() if r.hops_away == 0),
        "routes_held": sum(1 for r in node.nodedb.values() if r.next_hop),
        "store_full": known >= node.max_num_nodes,
        # Of everything this node could learn about right now, how much does it hold?
        "coverage": (known_reachable / len(reachable)) if reachable else 1.0,
        "history_used": len(node.history),
        "history_capacity": node.history_max,
        "queue_depth": len(node.queue),
    }


def snapshot(mesh):
    """Mesh-wide knowledge, as one flat record. Safe to call on a timer."""
    per_node = [node_knowledge(mesh, i) for i in range(len(mesh.nodes))]
    live = [k for k in per_node if k["online"]]
    reachable_total = sum(k["known"] for k in live)
    by_platform = {}
    for k in live:
        entry = by_platform.setdefault(
            k["platform"], {"nodes": 0, "coverage": 0.0, "full": 0}
        )
        entry["nodes"] += 1
        entry["coverage"] += k["coverage"]
        entry["full"] += 1 if k["store_full"] else 0
    for entry in by_platform.values():
        entry["coverage"] = round(entry["coverage"] / entry["nodes"], 3)

    return {
        "t_hours": round(mesh.now / 3600_000.0, 3),
        "online": len(live),
        "offline": len(per_node) - len(live),
        "mean_coverage": (
            round(sum(k["coverage"] for k in live) / len(live), 3) if live else 0.0
        ),
        "min_coverage": round(min((k["coverage"] for k in live), default=0.0), 3),
        "stores_full": sum(1 for k in live if k["store_full"]),
        "stale_records": sum(k["stale_records"] for k in live),
        "routes_held": sum(k["routes_held"] for k in live),
        "mean_known": round(reachable_total / len(live), 1) if live else 0.0,
        "by_platform": by_platform,
        "deaths": {key: mesh.stats.get(key, 0) for key, _ in ROUTE_DEATHS},
        "refusals": {key: mesh.stats.get(key, 0) for key, _ in RESOLUTION_FAILURES},
        "evictions": mesh.stats.get("nodedb_evictions", 0),
    }


def partitions(mesh):
    """Connected components over the *live* link graph, largest first.

    After a break this is the honest picture of the mesh: not "how many nodes are up" but "how many
    separate meshes are there now", which is what decides whether an archive can still reconcile.
    """
    seen = set()
    components = []
    for start in range(len(mesh.nodes)):
        if start in seen or not mesh.nodes[start].online:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for peer in mesh.neighbours[node]:
                if peer not in seen and mesh.nodes[peer].online:
                    seen.add(peer)
                    stack.append(peer)
        components.append(sorted(component))
    components.sort(key=len, reverse=True)
    return components


def stale_beliefs(mesh):
    """Routes that point through a node that is no longer there.

    This is the cost of a break that no counter catches at the moment it happens: the mesh does not
    know it is wrong yet. Every one of these is a directed delivery that will fail, three times,
    before the route decays - so this number is a *prediction* of wasted airtime, not a record of it.
    """
    dead_bytes = {}
    for node in mesh.nodes:
        if not node.online:
            dead_bytes.setdefault(node.relay_byte, []).append(node.index)

    stale = 0
    holders = set()
    for node in mesh.nodes:
        if not node.online:
            continue
        for dest, record in node.nodedb.items():
            if record.next_hop and record.next_hop in dead_bytes:
                stale += 1
                holders.add(node.index)
    return {"stale_routes": stale, "nodes_holding_one": len(holders)}


class Recorder:
    """Samples knowledge on a timer and keeps the series.

    Attach before running. The sample interval is in simulated milliseconds; an hour is usually
    right for a multi-day run, and anything finer just makes the plot noisy.
    """

    def __init__(self, mesh, every_ms=3600_000.0):
        self.mesh = mesh
        self.every_ms = every_ms
        self.series = []
        self.events = (
            []
        )  # (t_hours, label) - what was done to the mesh, for the plot's rules
        self._schedule()

    def _schedule(self):
        self.mesh.at(self.mesh.now + self.every_ms, self._tick)

    def _tick(self):
        self.sample()
        self._schedule()

    def sample(self, label=None):
        record = snapshot(self.mesh)
        record.update(stale_beliefs(self.mesh))
        parts = partitions(self.mesh)
        record["components"] = len(parts)
        record["largest_component"] = len(parts[0]) if parts else 0
        if label:
            record["label"] = label
        self.series.append(record)
        return record

    def note(self, label):
        """Mark something that was done to the mesh, and sample either side of it."""
        self.events.append((round(self.mesh.now / 3600_000.0, 3), label))
        return self.sample(label=label)

    def to_json(self, path):
        with open(path, "w") as handle:
            json.dump({"series": self.series, "events": self.events}, handle, indent=2)

    # ---- reporting ---------------------------------------------------------------------

    def summary(self):
        """A short text report. Prints the things a plot would show, for runs without matplotlib."""
        if not self.series:
            return "no samples"
        first, last = self.series[0], self.series[-1]
        lines = [
            f"knowledge over {last['t_hours']:.1f} h, {len(self.series)} samples",
            f"  coverage      {first['mean_coverage']:.0%} -> {last['mean_coverage']:.0%} "
            f"(worst node {last['min_coverage']:.0%})",
            f"  stores full   {last['stores_full']} of {last['online']} online",
            f"  stale records {last['stale_records']} (peers held but no longer reachable)",
            f"  routes held   {last['routes_held']}",
            f"  components    {last['components']} (largest {last['largest_component']})",
            f"  stale routes  {last['stale_routes']} held by {last['nodes_holding_one']} nodes",
            "  routes lost:",
        ]
        for key, label in ROUTE_DEATHS:
            lines.append(f"    {last['deaths'].get(key, 0):6d}  {label}")
        lines.append("  resolution refused (not a loss - the safe branch was taken):")
        for key, label in RESOLUTION_FAILURES:
            lines.append(f"    {last['refusals'].get(key, 0):6d}  {label}")
        if last["by_platform"]:
            lines.append("  coverage by board:")
            for platform in sorted(last["by_platform"]):
                entry = last["by_platform"][platform]
                lines.append(
                    f"    {platform:14s} {entry['coverage']:.0%} over {entry['nodes']} nodes, "
                    f"{entry['full']} full"
                )
        for when, label in self.events:
            lines.append(f"  event @ {when:.1f} h: {label}")
        return "\n".join(lines)

    def render(self, path, title="What the mesh knows"):
        """Four panels: coverage, what is held, how broken the mesh is, and how a route died.

        Returns the path, or None when matplotlib is not installed - a headless run should not fail
        because it could not draw a picture.
        """
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return None
        if not self.series:
            return None

        t = [s["t_hours"] for s in self.series]
        fig, axes = plt.subplots(2, 2, figsize=(13, 8))
        fig.suptitle(title, fontsize=13)

        # Coverage: the share of the reachable mesh each node actually holds. The band is the
        # spread between the best-informed node and the worst, which is where the boards separate.
        ax = axes[0][0]
        ax.plot(t, [s["mean_coverage"] for s in self.series], label="mean", lw=2)
        ax.plot(
            t,
            [s["min_coverage"] for s in self.series],
            label="worst node",
            lw=1.2,
            ls="--",
        )
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("share of reachable mesh held")
        ax.set_title("NodeDB coverage")
        ax.legend(fontsize=8)

        # Per-board coverage, because the whole reason to model boards is that they diverge here.
        ax = axes[0][1]
        platforms = sorted({p for s in self.series for p in s["by_platform"]})
        for platform in platforms:
            ax.plot(
                t,
                [
                    s["by_platform"].get(platform, {}).get("coverage", math.nan)
                    for s in self.series
                ],
                label=platform,
                lw=1.4,
            )
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("coverage")
        ax.set_title("by board")
        ax.legend(fontsize=7)

        # How broken is it: components should be 1 on a healthy mesh, and the stale-route line is
        # the mesh's own wrongness - routes still pointing through nodes that have gone.
        ax = axes[1][0]
        ax.plot(t, [s["components"] for s in self.series], label="components", lw=2)
        ax.plot(t, [s["offline"] for s in self.series], label="nodes down", lw=1.2)
        ax.plot(
            t,
            [s["stale_routes"] for s in self.series],
            label="routes to dead nodes",
            lw=1.2,
            ls=":",
        )
        ax.set_ylabel("count")
        ax.set_xlabel("hours")
        ax.set_title("how broken")
        ax.legend(fontsize=8, loc="upper left")
        # Stale records run into the thousands where components and downed nodes are single
        # digits, so they get their own axis rather than flattening everything else to a line.
        twin = ax.twinx()
        twin.plot(
            t,
            [s.get("stale_records", 0) for s in self.series],
            label="records for unreachable peers",
            lw=1.0,
            ls="-.",
            color="grey",
        )
        twin.set_ylabel("stale records", color="grey", fontsize=8)
        twin.tick_params(axis="y", labelcolor="grey", labelsize=8)
        twin.legend(fontsize=7, loc="lower right")

        # Cumulative route losses by cause, with resolution refusals dashed alongside. Three
        # clocks for the losses; this panel is which of them is actually binding on this mesh.
        ax = axes[1][1]
        for key, label in ROUTE_DEATHS:
            ax.plot(
                t, [s["deaths"].get(key, 0) for s in self.series], label=label, lw=1.4
            )
        for key, label in RESOLUTION_FAILURES:
            ax.plot(
                t,
                [s.get("refusals", {}).get(key, 0) for s in self.series],
                label=f"{label} (refused, not lost)",
                lw=1.2,
                ls="--",
            )
        ax.set_ylabel("cumulative")
        ax.set_xlabel("hours")
        ax.set_title("how routes were lost")
        ax.legend(fontsize=7)

        for row in axes:
            for cell in row:
                cell.grid(alpha=0.25)
                for when, label in self.events:
                    cell.axvline(when, color="crimson", lw=0.9, alpha=0.6)
                    cell.annotate(
                        label,
                        xy=(when, cell.get_ylim()[1]),
                        fontsize=6,
                        rotation=90,
                        va="top",
                        ha="right",
                        color="crimson",
                    )

        fig.tight_layout()
        fig.savefig(path, dpi=130)
        plt.close(fig)
        return path
