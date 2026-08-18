"""Hills, valleys, land cover and real node geometry, for the SF++ transport.

Everything in this file is an adapter onto code that already exists in `sim/meshtasticator/lib/` -
Komzpa's SRTM terrain, OSM land-cover clutter and the fitted link-calibration model from
Meshtasticator PR #77. None of it is reimplemented here. The vendored functions are called with the
vendored `Config` fields set the way the vendored simulator sets them, so a link budget computed
through this file and one computed by `loraMesh.py --preset batumi` differ only where this transport
deliberately differs (its own MAC, its own noise floor when `--noise-model thermal` is left on).

What this file adds is the three things that stood between that code and this simulator:

  * **a scenario**, so a run can take its node geometry from a real mesh instead of drawing points
    in a square. `batumi` is the packaged one; `map` pulls the public map and cuts a bounding box
    out of it, which is how Komzpa's original scenarios were built;
  * **a terrain source for a mesh that is not a real place.** A generated topology has no elevation
    to look up, and "3.2 km of reach in every direction" is the assumption a flat model makes.
    `synthetic` puts ridges and valleys under a generated mesh so the shape of the ground is a
    variable rather than a constant, and it is hashed rather than drawn so switching it on does not
    move the traffic schedule (the same discipline `NoiseField` follows, and for the same reason);
  * **an index**, because `TerrainGrid.elevation_at` sorts every sample on every call. That is fine
    for the packaged 78-point Batumi grid and unusable for an SRTM tile: a 60-node mesh asks for
    85 000 elevations before it has sent a packet. `IndexedTerrainGrid` answers the identical
    inverse-distance question over a bucketed candidate set, and `test_terrain_index_matches_vendored`
    holds it to the vendored answer.

The three loss terms stay separable on purpose, because they are three different claims and the
register has to be able to price them apart:

  | term                | flag                    | what it says                                          |
  | ------------------- | ----------------------- | ----------------------------------------------------- |
  | terrain             | `--terrain`             | a ridge is between these two nodes                    |
  | clutter             | `--clutter`             | the path crosses apartment blocks, forest, or water   |
  | link calibration    | `--link-calibration`    | a ridge fit against 296 observed Batumi links         |

`--scenario batumi` turns on all three, since that is the combination the coefficients were fitted
under and the only one where the calibration term means anything. Any of them can be turned off
again to ask what that term alone was worth.
"""

import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meshtasticator"
    ),
)

VENDOR_ROOT = Path(__file__).resolve().parents[1] / "meshtasticator"
PRESET_ROOT = VENDOR_ROOT / "presets"

# Where a downloaded map payload, SRTM tile or rasterised clutter grid is kept. Under the vendored
# tree's own cache directory so a scenario fetched by `loraMesh.py` is reused here and vice versa.
CACHE_ROOT = Path(
    os.environ.get("SFPP_SCENARIO_CACHE", VENDOR_ROOT / "cache")
).expanduser()


# ---- the terrain index ----------------------------------------------------------------------


class IndexedTerrainGrid:
    """`TerrainGrid` with a spatial index. Same answers, without the per-call sort.

    The vendored grid sorts all N samples by distance on every lookup and keeps the nearest eight
    for an inverse-square weighting. This buckets the samples on a cell of the grid's own spacing
    and widens a ring until it can prove no unexamined bucket can hold a closer sample than the
    eighth it already has - so the candidate set is a superset of the vendored nearest eight, and
    the weighting that follows is the vendored one applied to the same eight points.

    Ties are broken on sample order, as a stable sort does, so a point equidistant from two samples
    resolves the way the vendored grid resolves it.
    """

    NEAREST = 8

    def __init__(self, samples):
        self.samples = list(samples)
        if not self.samples:
            raise ValueError("terrain grid has no samples")
        xs = sorted({x for x, _, _ in self.samples})
        ys = sorted({y for _, y, _ in self.samples})
        # The bucket side is the grid's own spacing where it has one, and the bounding box divided
        # into roughly sqrt(N) cells per axis where it does not. Either way a bucket holds O(1)
        # samples, which is what makes the ring search terminate quickly.
        self.cell = max(1.0, _median_spacing(xs, ys, len(self.samples)))
        self._buckets = {}
        for index, (x, y, elevation) in enumerate(self.samples):
            self._buckets.setdefault(
                (int(math.floor(x / self.cell)), int(math.floor(y / self.cell))), []
            ).append((x, y, elevation, index))
        self._cache = {}

    @classmethod
    def from_rows(cls, rows):
        samples = []
        for row_number, row in enumerate(rows, start=1):
            try:
                x, y, elevation = row
            except (TypeError, ValueError) as err:
                raise ValueError(
                    f"terrain sample {row_number} must have x, y, and elevation"
                ) from err
            x, y, elevation = float(x), float(y), float(elevation)
            if not (
                math.isfinite(x) and math.isfinite(y) and math.isfinite(elevation)
            ):
                raise ValueError(f"terrain sample {row_number} values must be finite")
            samples.append((x, y, elevation))
        return cls(samples)

    def elevation_at(self, x, y):
        # A path profile walks the same handful of coordinates for every pair that shares an
        # endpoint, and the knife-edge loop asks for 24 of them per link. Rounding to a decimetre
        # before caching costs nothing the model can see and removes most of the lookups.
        key = (round(x, 1), round(y, 1))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        value = self._compute(x, y)
        self._cache[key] = value
        return value

    def _compute(self, x, y):
        cx = int(math.floor(x / self.cell))
        cy = int(math.floor(y / self.cell))

        found = []
        ring = 0
        while True:
            for bx in range(cx - ring, cx + ring + 1):
                for by in range(cy - ring, cy + ring + 1):
                    # Only the newly exposed shell of the square, not the whole square again.
                    if ring and max(abs(bx - cx), abs(by - cy)) != ring:
                        continue
                    for sx, sy, elevation, index in self._buckets.get((bx, by), ()):
                        found.append((math.hypot(x - sx, y - sy), index, elevation))
            found.sort()
            # Anything outside the examined square is at least `ring * cell` away, measured from
            # the nearest cell edge. Stop once the eighth candidate is closer than that.
            if len(found) >= self.NEAREST:
                guaranteed = ring * self.cell
                if found[self.NEAREST - 1][0] <= guaranteed:
                    break
            if ring > 0 and (2 * ring + 1) ** 2 >= len(self._buckets) + 8:
                break  # the whole grid is in hand
            ring += 1

        weighted_sum = 0.0
        weight_total = 0.0
        for distance, _index, elevation in found[: self.NEAREST]:
            if distance < 0.01:
                return elevation
            weight = 1.0 / (distance * distance)
            weighted_sum += elevation * weight
            weight_total += weight
        return weighted_sum / weight_total


def _median_spacing(xs, ys, sample_count):
    """The grid's step where it is regular, and a bounding-box estimate where it is not."""
    steps = []
    for values in (xs, ys):
        gaps = sorted(b - a for a, b in zip(values, values[1:]) if b > a)
        if gaps:
            steps.append(gaps[len(gaps) // 2])
    if steps:
        return max(steps)
    span = max(
        (xs[-1] - xs[0]) if len(xs) > 1 else 0.0,
        (ys[-1] - ys[0]) if len(ys) > 1 else 0.0,
    )
    return span / max(1.0, math.sqrt(sample_count)) if span else 1.0


# ---- synthetic ground -----------------------------------------------------------------------


def _mix64(x):
    """SplitMix64's finaliser, as `mesh.NoiseField` uses it. Nothing is drawn from an RNG here."""
    x &= 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return (x ^ (x >> 31)) & 0xFFFFFFFFFFFFFFFF


def _unit(*key):
    h = 0x9E3779B97F4A7C15
    for k in key:
        h = _mix64(h ^ (int(k) & 0xFFFFFFFFFFFFFFFF))
    return h / 18446744073709551616.0


# Named ground shapes. `relief_m` is the peak-to-trough elevation range and `ridges` the number of
# ridge lines across the area; `roughness` adds a shorter-wavelength component on top so a valley
# floor is not perfectly flat. None of these are measured - see the register - and they exist to
# make the shape of the ground a variable rather than the constant zero.
LANDFORMS = {
    # No relief at all. The control, and identical to running with terrain off except that the
    # elevation term is exercised, so it is also the test that terrain costs nothing on flat ground.
    "flat": {"relief_m": 0.0, "ridges": 1, "roughness": 0.0},
    # Gentle rolling country: nothing blocks a link outright, margins move by a few dB.
    "rolling": {"relief_m": 120.0, "ridges": 3, "roughness": 0.25},
    # One ridge across the middle, which is the case that splits a mesh in two.
    "ridge": {"relief_m": 400.0, "ridges": 1, "roughness": 0.15},
    # Steep valleys: most nodes are in a basin and hear their own basin.
    "valleys": {"relief_m": 500.0, "ridges": 4, "roughness": 0.35},
    # Mountains, where line of sight is the exception rather than the rule.
    "alpine": {"relief_m": 900.0, "ridges": 5, "roughness": 0.45},
    # A coastal shelf: flat and low on one side, rising hard on the other. Batumi's own shape, and
    # the reason a mesh there is not a disc.
    "coastal": {"relief_m": 600.0, "ridges": 2, "roughness": 0.2},
}


def synthetic_terrain_rows(landform, area, seed, step=None):
    """A regular elevation grid over `[-area, area]^2`, hashed from `seed`.

    Two sinusoidal ridge systems at an angle to each other, plus a hashed lattice perturbation, so
    the field has both a long wavelength a link can be blocked by and a short one that moves a
    margin. `coastal` replaces one system with a monotone rise, which is what a shoreline is.
    """
    shape = LANDFORMS[landform]
    relief = shape["relief_m"]
    ridges = shape["ridges"]
    roughness = shape["roughness"]
    span = area
    step = step or max(200.0, 2 * span / 48.0)

    phase_a = _unit(seed, 1) * 2 * math.pi
    phase_b = _unit(seed, 2) * 2 * math.pi
    tilt = _unit(seed, 3) * math.pi

    rows = []
    n = int(math.ceil(2 * span / step)) + 1
    for i in range(n):
        x = -span + i * step
        for j in range(n):
            y = -span + j * step
            u = (x * math.cos(tilt) + y * math.sin(tilt)) / span
            v = (-x * math.sin(tilt) + y * math.cos(tilt)) / span
            if landform == "coastal":
                # A shoreline along one axis: sea level on the low side, rising to the ridge line.
                base = 0.5 * (1.0 + math.tanh(2.2 * (u + 0.35)))
                base *= 0.5 * (1.0 + math.cos(math.pi * ridges * v + phase_b))
            else:
                base = 0.5 * (1.0 + math.cos(math.pi * ridges * u + phase_a))
                base *= 0.5 * (1.0 + math.cos(math.pi * ridges * v + phase_b))
            # Irwin-Hall over four hashed draws, so the perturbation is roughly normal and depends
            # on nothing but the lattice index.
            jitter = (
                _unit(seed, i, j, 0)
                + _unit(seed, i, j, 1)
                + _unit(seed, i, j, 2)
                + _unit(seed, i, j, 3)
            ) / 4.0 - 0.5
            elevation = relief * (base + roughness * jitter)
            rows.append((x, y, max(0.0, elevation)))
    return rows


# ---- the scenario ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """A place, as far as this simulator is concerned: where the nodes are and what is under them.

    `points` are local metres from `origin`, matching the vendored projection exactly, so a node
    index here and the same index in `loraMesh.py --preset batumi` are the same node in the same
    spot. `antenna_height` stays height above local ground and never becomes altitude: the path-loss
    models take an antenna height term and handing them metres above sea level would silently make
    every node a mountaintop.
    """

    name: str
    points: list
    antenna_height: list
    origin: tuple = None
    absolute_altitude: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    hop_limits: list = field(default_factory=list)
    antenna_gain: list = field(default_factory=list)
    terrain_rows: list = None
    clutter_file: Path = None
    calibration: dict = field(default_factory=dict)
    # Set when the geometry is a real mesh rather than points under a generated one. A run over real
    # geometry must not also be told a node count or an area; the place decides both.
    fixed_geometry: bool = False

    def __len__(self):
        return len(self.points)

    @property
    def node_count(self):
        return len(self.points)

    def extent(self):
        """Half-width of the smallest square centred on the origin that holds every node."""
        if not self.points:
            return 0.0
        return max(max(abs(x), abs(y)) for x, y in self.points)

    def summary(self):
        """What went into the run, for the JSON. Every claim a result rests on, in one dict."""
        elevations = [z for _, _, z in (self.terrain_rows or [])]
        out = {
            "scenario": self.name,
            "nodes": self.node_count,
            "fixed_geometry": self.fixed_geometry,
            "origin": list(self.origin) if self.origin else None,
            "extent_m": round(self.extent(), 1),
            "terrain_samples": len(self.terrain_rows or []),
            "clutter": self.clutter_file.name if self.clutter_file else None,
            "link_calibration": bool(
                self.calibration.get("link_calibration_model", {}).get("coefficients")
            ),
        }
        if elevations:
            out["ground_elevation_m"] = {
                "min": round(min(elevations), 1),
                "max": round(max(elevations), 1),
                "mean": round(sum(elevations) / len(elevations), 1),
            }
        return out


def preset_scenario(name="batumi"):
    """The packaged real-mesh snapshot, geometry and ground together.

    Node roles, hop limits and mute flags come from the snapshot, so a run over this scenario starts
    from the roles the mesh actually runs rather than from `--router-fraction`.
    """
    import yaml

    nodes_path = PRESET_ROOT / f"{name}.yaml"
    if not nodes_path.exists():
        raise ValueError(
            f"unknown scenario preset {name!r}; have "
            f"{', '.join(sorted(p.stem for p in PRESET_ROOT.glob('*.yaml')))}"
        )
    with nodes_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    origin = None
    if isinstance(raw.get("origin"), dict):
        origin = (float(raw["origin"]["lat"]), float(raw["origin"]["lon"]))

    points, heights, roles, hop_limits, gains, altitudes = [], [], [], [], [], []
    for _key, node in sorted(raw["nodes"].items(), key=lambda kv: int(kv[0])):
        points.append((float(node["x"]), float(node["y"])))
        heights.append(float(node.get("z", 1.5)))
        altitudes.append(node.get("absoluteAltitude"))
        gains.append(float(node.get("antennaGain", 0.0)))
        hop_limits.append(int(node.get("hopLimit", 3)))
        roles.append(_role_from_flags(node))

    terrain_path = PRESET_ROOT / f"{name}_terrain.csv"
    clutter_path = PRESET_ROOT / f"{name}_clutter.csv"
    return Scenario(
        name=name,
        points=points,
        antenna_height=heights,
        origin=origin,
        absolute_altitude=altitudes,
        roles=roles,
        hop_limits=hop_limits,
        antenna_gain=gains,
        terrain_rows=_terrain_rows_from_csv(terrain_path)
        if terrain_path.exists()
        else None,
        clutter_file=clutter_path if clutter_path.exists() else None,
        calibration=raw.get("radio_calibration", {}) or {},
        fixed_geometry=True,
    )


def _role_from_flags(node):
    if node.get("isRouter"):
        return "ROUTER"
    if node.get("isRepeater"):
        return "REPEATER"
    if node.get("isClientMute"):
        return "CLIENT_MUTE"
    return "CLIENT"


def _terrain_rows_from_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return [
            (float(row["x_m"]), float(row["y_m"]), float(row["elevation_m"]))
            for row in csv.DictReader(fh)
        ]


def map_scenario(
    bbox,
    payload=None,
    limit=None,
    antenna_height=1.5,
    hop_limit=3,
    terrain_step_m=600.0,
    clutter_step_m=500.0,
    offline=False,
):
    """A mesh cut out of the public map, with SRTM ground and OSM land cover under it.

    This is the path Komzpa's scenarios were built on and it is unchanged: `lib.map_input` decides
    which map rows are usable and where they land, `lib.srtm` fetches the elevation tiles, and
    `lib.osm_clutter` rasterises the land cover from Overpass. All three cache into `CACHE_ROOT`,
    and `offline=True` refuses to reach the network so an unattended run fails loudly rather than
    hanging on a fetch.

    `bbox` is `(min_lat, min_lon, max_lat, max_lon)`.
    """
    from lib.map_input import (
        fetch_map_payload,
        filter_positioned_map_nodes,
        node_configs_from_positioned_rows,
        payload_nodes,
    )

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if payload is None:
        cached = CACHE_ROOT / "map_nodes.json"
        if cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
        elif offline:
            raise RuntimeError(
                f"offline and no cached map payload at {cached}; fetch one first"
            )
        else:
            payload = fetch_map_payload()
            cached.write_text(json.dumps(payload), encoding="utf-8")
    elif isinstance(payload, (str, Path)):
        payload = json.loads(Path(payload).read_text(encoding="utf-8"))

    positioned = filter_positioned_map_nodes(payload_nodes(payload), bbox)
    if limit:
        positioned = positioned[:limit]
    if not positioned:
        raise ValueError("map payload produced no positioned nodes in that bounding box")

    configs, origin = node_configs_from_positioned_rows(
        positioned,
        period=0,
        antenna_height=antenna_height,
        hop_limit=hop_limit,
        return_origin=True,
    )

    points = [(c.position.x, c.position.y) for c in configs]
    return Scenario(
        name=f"map:{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f}",
        points=points,
        antenna_height=[c.antenna_height for c in configs],
        origin=origin,
        absolute_altitude=[c.absolute_altitude for c in configs],
        roles=[_role_from_config(c) for c in configs],
        hop_limits=[c.hop_limit for c in configs],
        antenna_gain=[c.antenna_gain for c in configs],
        terrain_rows=_srtm_rows(bbox, origin, terrain_step_m, offline),
        clutter_file=_osm_clutter(bbox, origin, clutter_step_m, offline),
        fixed_geometry=True,
    )


def _role_from_config(config):
    name = getattr(config.role, "name", str(config.role))
    return name if name in {"ROUTER", "REPEATER", "CLIENT_MUTE", "ROUTER_LATE"} else "CLIENT"


def _srtm_rows(bbox, origin, step_m, offline):
    """SRTM elevation over the bounding box, projected into the scenario's local metres.

    The vendored generator yields lat/lon rows; the projection is the same `latlon_to_xy` the node
    positions went through, so a sample and a node that share a coordinate share a coordinate.
    """
    from lib.srtm import terrain_rows_from_srtm
    from lib.terrain import latlon_to_xy

    cached = CACHE_ROOT / f"terrain_{_bbox_slug(bbox)}_{int(step_m)}.csv"
    if cached.exists():
        return _terrain_rows_from_csv(cached)
    if offline:
        raise RuntimeError(f"offline and no cached terrain at {cached}")
    rows = [
        (
            *latlon_to_xy(float(row["lat"]), float(row["lon"]), origin[0], origin[1]),
            float(row["elevation_m"]),
        )
        for row in terrain_rows_from_srtm(bbox, step_m, str(CACHE_ROOT))
    ]
    with cached.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("x_m", "y_m", "elevation_m"))
        writer.writerows(rows)
    return rows


def _osm_clutter(bbox, origin, step_m, offline):
    from lib.osm_clutter import fetch_overpass_payload, rasterize_clutter, write_clutter_csv

    cached = CACHE_ROOT / f"clutter_{_bbox_slug(bbox)}_{int(step_m)}.csv"
    if cached.exists():
        return cached
    if offline:
        return None
    rows = rasterize_clutter(
        fetch_overpass_payload(bbox), bbox, origin=origin, step_m=step_m
    )
    write_clutter_csv(rows, cached)
    return cached


def _bbox_slug(bbox):
    return "_".join(f"{v:.4f}" for v in bbox)


def synthetic_scenario(landform, area, seed, name=None):
    """Ground under a generated mesh. No geometry of its own - `build()` still places the nodes."""
    if landform not in LANDFORMS:
        raise ValueError(
            f"unknown landform {landform!r}; have {', '.join(sorted(LANDFORMS))}"
        )
    return Scenario(
        name=name or f"synthetic:{landform}",
        points=[],
        antenna_height=[],
        terrain_rows=synthetic_terrain_rows(landform, area, seed),
        fixed_geometry=False,
    )


def load(spec, area=8000.0, seed=0, bbox=None, limit=None, offline=False):
    """Resolve a `--scenario` string.

    `batumi` (or any packaged preset name) is real geometry over real ground; a landform name is
    ground under a generated mesh; `map` cuts a bounding box out of the public map.
    """
    if spec in (None, "", "none"):
        return None
    if spec in LANDFORMS:
        return synthetic_scenario(spec, area, seed)
    if spec == "map":
        if bbox is None:
            raise ValueError("--scenario map needs --bbox min_lat,min_lon,max_lat,max_lon")
        return map_scenario(bbox, limit=limit, offline=offline)
    return preset_scenario(spec)


def available():
    """Every scenario name `load` accepts, for the CLI's help and for `--list`."""
    return sorted(
        [p.stem for p in PRESET_ROOT.glob("*.yaml")] + list(LANDFORMS) + ["map"]
    )


# ---- applying it to the vendored Config ------------------------------------------------------


def apply(conf, scenario, terrain=True, clutter=True, link_calibration=True):
    """Set the vendored terrain/clutter/calibration fields from a scenario.

    Returns the terrain grid so the caller can lift node z coordinates with it. Each of the three
    terms is separately refusable, because each is a separate claim: terrain is geometry anyone can
    check, clutter is a land-cover raster, and the calibration is a ridge fit over 296 observed
    links in one city. A run that keeps the first and drops the third is asking what the ground
    alone does.
    """
    grid = None
    if scenario is None:
        return None

    if scenario.origin:
        conf.GEO_ORIGIN_LAT, conf.GEO_ORIGIN_LON = scenario.origin

    if terrain and scenario.terrain_rows:
        grid = IndexedTerrainGrid.from_rows(scenario.terrain_rows)
        conf.TERRAIN_ENABLED = True
        conf.TERRAIN_GRID = grid
    else:
        conf.TERRAIN_ENABLED = False
        conf.TERRAIN_GRID = None

    if clutter and scenario.clutter_file:
        conf.CLUTTER_ENABLED = True
        conf.CLUTTER_GRID_FILE = str(scenario.clutter_file)
    else:
        conf.CLUTTER_ENABLED = False
        conf.CLUTTER_GRID_FILE = None

    calibration = scenario.calibration or {}
    # The noise floor and the near-field distance floor are field measurements of the place, not of
    # the link model, so they come in with the scenario whatever the calibration flag says. The
    # thermal floor this transport computes per preset is the better answer for a generated mesh and
    # the wrong one for a mesh whose observed SNRs were fitted against a measured floor, so a
    # scenario that carries one wins - `--noise-model` set explicitly on the command line still
    # overrides it, since it is applied after this.
    for source, target in (
        ("noise_level", "NOISE_LEVEL"),
        ("path_loss_distance_floor_m", "PATH_LOSS_DISTANCE_FLOOR_M"),
        ("reported_snr_min_db", "REPORTED_SNR_MIN_DB"),
        ("reported_snr_max_db", "REPORTED_SNR_MAX_DB"),
    ):
        if source in calibration:
            setattr(conf, target, float(calibration[source]))

    model = calibration.get("link_calibration_model", {}) if link_calibration else {}
    conf.LINK_CALIBRATION_MODEL_ENABLED = bool(model.get("coefficients"))
    conf.LINK_CALIBRATION_COEFFICIENTS = {
        str(k): float(v) for k, v in model.get("coefficients", {}).items()
    }
    conf.LINK_CALIBRATION_SNR_MIN_DB = (
        float(model["snr_min_db"]) if "snr_min_db" in model else None
    )
    conf.LINK_CALIBRATION_SNR_MAX_DB = (
        float(model["snr_max_db"]) if "snr_max_db" in model else None
    )

    # Anything cached against the previous configuration is stale now.
    for attr in ("_terrain_loss_cache", "_clutter_loss_cache", "_clutter_grid"):
        if hasattr(conf, attr):
            setattr(conf, attr, None if attr == "_clutter_grid" else {})
    return grid


def ground_elevation(grid, x, y):
    """Ground height under a point, or 0 where there is no terrain."""
    return 0.0 if grid is None else grid.elevation_at(x, y)


class Point:
    """The two coordinates and the altitude the vendored terrain and clutter code asks for.

    `lib.terrain` and `lib.clutter` take objects with `.x`, `.y`, `.z`, where z is absolute antenna
    altitude once terrain is in play. This transport keeps its nodes as flat records, so this is the
    adapter between the two rather than a second position type.
    """

    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def euclidean_distance(self, other):
        return math.dist((self.x, self.y, self.z), (other.x, other.y, other.z))

    def __repr__(self):
        return f"Point(x={self.x}, y={self.y}, z={self.z})"
