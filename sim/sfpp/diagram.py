"""SVG diagrams of the simulated mesh. No plotting dependency - the output is hand-written SVG.

Two pictures:

  topology    60 nodes placed in an area, links drawn where the link budget closes, three of them
              marked as SF++ servers, every other node shaded by how many hops it is from the
              nearest server. The physics is Meshtasticator's, not a sketch of it: distances go
              through estimate_path_loss() with the configured model and preset.
  convergence how much of the chain each server holds as messages arrive, against the chain itself.

Usage, from sim/:
    python3 -m sfpp.diagram --out /some/dir [--nodes 60] [--servers 3]
"""

import argparse
import math
import os
import random
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meshtasticator"
    ),
)

INK = "#1b1b1b"
MUTED = "#8a8a8a"
SERVER = "#B4472A"
LINK = "#9fb4c7"
HOPS = ["#2E5E7E", "#4E86A8", "#7FB0CB", "#B5D2E2", "#DCE9F1", "#F0F0F0"]
BG = "#FCFCFA"


def build_topology(node_count, area, seed, min_dist=300):
    """Place nodes, then join every pair whose link budget closes."""
    import lib.phy as phy
    from lib.config import Config

    conf = Config()
    rng = random.Random(seed)

    points = []
    while len(points) < node_count:
        p = (rng.uniform(0, area), rng.uniform(0, area))
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)

    budget_at = {}
    edges = []
    for i in range(node_count):
        for j in range(i + 1, node_count):
            d = max(1.0, math.dist(points[i], points[j]))
            budget = (
                conf.PTX
                + 2 * conf.GL
                - phy.estimate_path_loss(conf, d, conf.FREQ)
                - conf.current_preset["sensitivity"]
            )
            if budget >= 0:
                edges.append((i, j, budget))
                budget_at[(i, j)] = budget
    return conf, points, edges


def pick_servers(points, edges, count, node_count):
    """Spread the servers out: repeatedly take the node farthest from those already chosen.

    A real deployment would not place them at random, and clustering them would flatter the
    design by making every server's neighbourhood identical.
    """
    chosen = [max(range(node_count), key=lambda i: points[i][0] + points[i][1])]
    while len(chosen) < count:
        chosen.append(
            max(
                range(node_count),
                key=lambda i: min(math.dist(points[i], points[c]) for c in chosen),
            )
        )
    return chosen


def hops_from(servers, edges, node_count):
    """BFS over the link graph, so the shading is reachability rather than distance."""
    adjacency = {i: [] for i in range(node_count)}
    for i, j, _ in edges:
        adjacency[i].append(j)
        adjacency[j].append(i)

    depth = {s: 0 for s in servers}
    frontier = list(servers)
    while frontier:
        nxt = []
        for node in frontier:
            for peer in adjacency[node]:
                if peer not in depth:
                    depth[peer] = depth[node] + 1
                    nxt.append(peer)
        frontier = nxt
    return depth, adjacency


def svg_header(width, height, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">'
        f'<rect width="{width}" height="{height}" fill="{BG}"/>'
        f'<text x="24" y="34" font-size="17" font-weight="600" fill="{INK}">{title}</text>'
    )


def topology_svg(conf, points, edges, servers, depth, area, path):
    W, H = 860, 720
    pad, top = 40, 60
    span = H - top - pad - 70
    scale = span / area

    def sx(x):
        return pad + x * scale + 60

    def sy(y):
        return top + span - y * scale

    parts = [
        svg_header(
            W, H, f"Simulated mesh: {len(points)} nodes, {len(servers)} SF++ servers"
        )
    ]
    parts.append(
        f'<text x="24" y="54" font-size="11.5" fill="{MUTED}">'
        f"{conf.MODEM_PRESET} &#183; path loss model {conf.MODEL} &#183; "
        f'{conf.PTX} dBm &#183; sensitivity {conf.current_preset["sensitivity"]} dBm &#183; '
        f"{area/1000:.0f} x {area/1000:.0f} km &#183; a link is drawn where the budget closes</text>"
    )

    # Links first, faint, so nodes read on top. Opacity carries the margin: a 0 dB link is a
    # link that only just exists, and drawing it like a solid one would overstate the mesh.
    for i, j, budget in edges:
        alpha = 0.10 + 0.5 * min(1.0, budget / 25.0)
        parts.append(
            f'<line x1="{sx(points[i][0]):.1f}" y1="{sy(points[i][1]):.1f}" '
            f'x2="{sx(points[j][0]):.1f}" y2="{sy(points[j][1]):.1f}" '
            f'stroke="{LINK}" stroke-width="0.9" opacity="{alpha:.2f}"/>'
        )

    unreachable = 0
    for index, (x, y) in enumerate(points):
        d = depth.get(index)
        if d is None:
            unreachable += 1
        if index in servers:
            continue
        colour = HOPS[min(d, len(HOPS) - 1)] if d is not None else "#FFFFFF"
        stroke = MUTED if d is not None else SERVER
        parts.append(
            f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="6.5" fill="{colour}" '
            f'stroke="{stroke}" stroke-width="0.9"/>'
        )

    for rank, index in enumerate(servers):
        x, y = points[index]
        parts.append(
            f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="12" fill="{SERVER}" '
            f'stroke="#FFFFFF" stroke-width="2.2"/>'
            f'<text x="{sx(x):.1f}" y="{sy(y)+4:.1f}" font-size="11" font-weight="700" '
            f'fill="#FFFFFF" text-anchor="middle">{rank + 1}</text>'
        )

    # Legend
    ly = H - 48
    parts.append(
        f'<text x="{pad}" y="{ly - 14}" font-size="11.5" fill="{INK}">hops to the nearest SF++ server</text>'
    )
    for step, colour in enumerate(HOPS):
        lx = pad + step * 62
        label = f"{step}" if step < len(HOPS) - 1 else f"{len(HOPS)-1}+"
        parts.append(
            f'<rect x="{lx}" y="{ly}" width="44" height="12" fill="{colour}" stroke="{MUTED}" stroke-width="0.6"/>'
            f'<text x="{lx + 22}" y="{ly + 27}" font-size="10.5" fill="{MUTED}" text-anchor="middle">{label}</text>'
        )
    parts.append(
        f'<circle cx="{pad + 6 * 62 + 30}" cy="{ly + 6}" r="9" fill="{SERVER}"/>'
        f'<text x="{pad + 6 * 62 + 46}" y="{ly + 10}" font-size="11.5" fill="{INK}">SF++ server</text>'
    )

    degree = 2 * len(edges) / len(points)
    reach = [d for d in depth.values() if d is not None]
    parts.append(
        f'<text x="{W - 24}" y="{ly + 10}" font-size="11.5" fill="{MUTED}" text-anchor="end">'
        f"{len(edges)} links &#183; mean degree {degree:.1f} &#183; "
        f"deepest {max(reach)} hops &#183; {unreachable} unreachable</text>"
    )
    parts.append("</svg>")
    open(path, "w").write("".join(parts))
    return {
        "links": len(edges),
        "degree": degree,
        "deepest": max(reach),
        "unreachable": unreachable,
    }


def convergence_svg(trace, total, capacity, cadence, loss, path):
    W, H = 860, 460
    left, right, top, bottom = 70, 28, 70, 58
    plot_w, plot_h = W - left - right, H - top - bottom
    xs = [t[0] for t in trace]
    xmax = max(xs)
    # The chain is the ceiling, so both axes share a scale - otherwise the dashed reference line
    # is not the line y=x and the gap between it and a server is not the shortfall.
    ymax = max(max(max(t[1]) for t in trace), xmax) or 1

    def px(v):
        return left + plot_w * v / xmax

    def py(v):
        return top + plot_h - plot_h * v / ymax

    parts = [svg_header(W, H, "Chain convergence: what each server holds")]
    parts.append(
        f'<text x="24" y="54" font-size="11.5" fill="{MUTED}">'
        f"capacity {capacity} &#183; reconcile every {cadence} objects &#183; {loss:.0%} loss per server &#183; "
        f"the dashed line is the chain itself</text>"
    )

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = top + plot_h - plot_h * frac
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#E4E4E0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" font-size="10.5" fill="{MUTED}" text-anchor="end">{int(ymax * frac)}</text>'
        )

    # The chain: one object per counter, which is the ceiling every server is chasing.
    parts.append(
        f'<path d="M {px(xs[0]):.1f} {py(xs[0]):.1f} L {px(xmax):.1f} {py(xmax):.1f}" '
        f'stroke="{INK}" stroke-width="1.4" stroke-dasharray="5 4" fill="none"/>'
    )

    colours = ["#2E5E7E", "#B4472A", "#4C7A34", "#7A5C9E"]
    for server in range(len(trace[0][1])):
        pts = " ".join(f"{px(c):.1f},{py(h[server]):.1f}" for c, h, _ in trace)
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{colours[server % len(colours)]}" stroke-width="1.7" opacity="0.9"/>'
        )

    for server in range(len(trace[0][1])):
        final = trace[-1][1][server]
        parts.append(
            f'<text x="{left + plot_w + 4}" y="{py(final) + 4:.1f}" font-size="10.5" '
            f'fill="{colours[server % len(colours)]}">{final}</text>'
        )

    parts.append(
        f'<text x="{left + plot_w / 2}" y="{H - 22}" font-size="11.5" fill="{MUTED}" text-anchor="middle">objects on the chain</text>'
    )
    parts.append(
        f'<text x="{left}" y="{H - 22}" font-size="11.5" fill="{MUTED}">0</text>'
    )
    parts.append(
        f'<text x="{left + plot_w}" y="{H - 22}" font-size="11.5" fill="{MUTED}" text-anchor="end">{xmax}</text>'
    )
    behind = [total - t for t in trace[-1][1]]
    parts.append(
        f'<text x="{W - 24}" y="{top - 12}" font-size="11.5" fill="{MUTED}" text-anchor="end">'
        f"final shortfall {behind} of {total}</text>"
    )
    parts.append("</svg>")
    open(path, "w").write("".join(parts))
    return behind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--nodes", type=int, default=60)
    ap.add_argument("--servers", type=int, default=3)
    ap.add_argument("--area", type=float, default=8000)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--messages", type=int, default=400)
    ap.add_argument("--capacity", type=int, default=4)
    ap.add_argument("--cadence", type=int, default=8)
    ap.add_argument("--loss", type=float, default=0.15)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    conf, points, edges = build_topology(args.nodes, args.area, args.seed)
    servers = pick_servers(points, edges, args.servers, args.nodes)
    depth, _ = hops_from(servers, edges, args.nodes)
    topo_path = os.path.join(args.out, "mesh-topology.svg")
    stats = topology_svg(conf, points, edges, servers, depth, args.area, topo_path)
    print(f"{topo_path}: {stats}")

    from . import experiment, feed

    root_hash = bytes(range(16))
    messages = feed.synthetic(args.messages, root_hash)
    trace = []
    counters, total, held, missing = experiment.run(
        messages,
        servers=args.servers,
        capacity=args.capacity,
        loss=args.loss,
        cadence=args.cadence,
        trace=trace,
    )
    conv_path = os.path.join(args.out, "convergence.svg")
    behind = convergence_svg(
        trace, total, args.capacity, args.cadence, args.loss, conv_path
    )
    print(
        f"{conv_path}: shortfall {behind}, {counters.total_bytes} B, silent losses {counters.silent_losses}"
    )


if __name__ == "__main__":
    main()
