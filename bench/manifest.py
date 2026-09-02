"""Image identity: what a bake is, what it can prove, and whether it drifted.

Three problems this solves, all of which produced wrong results rather than errors.

Identity. APP_VERSION is a property of the commit, not the build. A bench varies builds
by userPrefs and -D flags, neither of which moves it, so every bake of one commit is
byte-identical in its reported identity - in the log, on the wire, and in --info. The
beacon run produced 18 distinct images that all reported the same version. A row flashed
with the wrong image asserts against firmware that does not implement the scenario and
looks entirely normal doing it. So each bake is content-hashed and the hash is compiled
in as BENCH_BUILD_TAG, which the firmware echoes once at boot.

Capability. A gated feature that is not compiled in cannot be observed, and an assertion
keyed on it scores NOT OBSERVED forever against firmware that works perfectly.
MESHTASTIC_TRACE_LOGGING defaults to 0 on every non-portduino target, so every LOG_TRACE
in the tree compiles to nothing on real hardware; under USE_SEGGER the whole log family
routes to RTT and never reaches the API path at all. Each image therefore declares what
it can emit, and an assertion that needs something absent is INVALID at provision time
rather than a false negative at execution time.

Release representativeness. The same table, read in the other direction: a bake carrying
a flag outside the shipping allowlist cannot speak for a release build.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# -- capabilities ---------------------------------------------------------------
# What an image can emit or do. Assertions declare requirements against these names.

LOG_DEBUG = "log.DEBUG"
LOG_INFO = "log.INFO"
LOG_WARN = "log.WARN"
LOG_ERROR = "log.ERROR"
LOG_CRIT = "log.CRIT"
LOG_TRACE = "log.TRACE"
LOG_SINK_API = "log.sink.api"  # reaches a client over the protobuf link
LOG_SINK_RTT = "log.sink.rtt"  # SEGGER only; never reaches the recorder
FRAME_INJECTION = "feature.frame_injection"
HEAP_DEBUG = "feature.heap_debug"
BUILD_TAG = "feature.build_tag"

# Always compiled in on a normal build (DEBUG_PORT defined, DEBUG_MUTE not).
_BASE_LOG_LEVELS = frozenset({LOG_DEBUG, LOG_INFO, LOG_WARN, LOG_ERROR, LOG_CRIT})

# Flags that do not stop an image speaking for a release build.
#
# Two different questions get conflated here, and keeping them apart is the point.
# "Should this flag ship?" is not the same as "does this flag change what the firmware
# does?" - and it is only the second that decides whether a row's result generalises to a
# release. BENCH_BUILD_TAG must never ship, but it is a string the firmware echoes once
# at boot and never parses or branches on, so a row proven against a tagged image says
# exactly as much about release behaviour as one proven without it.
#
# Treating it otherwise would be self-defeating: every bench image carries the tag by
# construction, so counting it as disqualifying would mark every row non-representative
# and leave the signal meaning nothing at all.
BEHAVIOURALLY_INERT = frozenset(
    {
        "BENCH_BUILD_TAG",  # identity only; echoed, never parsed or branched on
        "DEBUG_HEAP",  # a diagnostic prefix on log lines, not a behaviour change
    }
)

# Flags that must never reach a shipping build, whether or not they change behaviour.
NEVER_SHIP = frozenset({"BENCH_BUILD_TAG", "MESHTASTIC_ENABLE_FRAME_INJECTION"})


def _truthy(value: Any) -> bool:
    """A -D flag's value, in the C preprocessor's terms rather than Python's.

    -DFOO=0 defines FOO but disables it; -DFOO with no value is presence-only and true.
    Getting this backwards would silently mark a disabled feature as available.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    text = str(value).strip()
    return text not in ("0", "", "false", "False")


@dataclass(frozen=True)
class Bake:
    """One distinct compilation. The unit of deduplication.

    Two (scenario, role) pairs with the same bake share one image: in the beacon matrix
    24 pairs collapsed to 18 images, and one listener bake served seven scenarios. At
    ~29 minutes each that is hours.
    """

    env: str
    userprefs: dict[str, str] = field(default_factory=dict)
    build_flags: dict[str, Any] = field(default_factory=dict)
    label: str | None = None

    # -- identity --------------------------------------------------------------

    def fingerprint(self, git_sha: str | None = None, dirty: bool | None = None) -> dict:
        """Everything that makes this bake distinct, in a stable, comparable form."""
        return {
            "env": self.env,
            "userprefs": {k: str(v) for k, v in sorted(self.userprefs.items())},
            "build_flags": {
                k: (True if v is True else str(v))
                for k, v in sorted(self.build_flags.items())
                if v is not False and v is not None
            },
            "git_sha": git_sha,
            "dirty": dirty,
        }

    def content_hash(self, git_sha: str | None = None, dirty: bool | None = None) -> str:
        """Short, stable hash over the fingerprint.

        Includes the git SHA and the dirty flag: the same flags against different source
        are not the same image, and a dirty tree is not reproducible. Twelve hex chars is
        ample for a bench matrix and short enough to read in a boot log.
        """
        blob = json.dumps(self.fingerprint(git_sha, dirty), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    # -- capability ------------------------------------------------------------

    def capabilities(self) -> set[str]:
        """What an image built from this bake can emit or do."""
        flags = {k: v for k, v in self.build_flags.items()}
        caps: set[str] = set(_BASE_LOG_LEVELS)

        if _truthy(flags.get("USE_SEGGER")):
            # Every level routes to RTT instead of DEBUG_PORT, so nothing reaches the
            # recorder over the API link. Capture from this image is not possible.
            caps.add(LOG_SINK_RTT)
        else:
            caps.add(LOG_SINK_API)

        # LOG_TRACE costs no flash unless enabled, and is off by default everywhere
        # except portduino. -DMESHTASTIC_TRACE_LOGGING=0 forces it off explicitly.
        if _truthy(flags.get("MESHTASTIC_TRACE_LOGGING")):
            caps.add(LOG_TRACE)

        if _truthy(flags.get("MESHTASTIC_ENABLE_FRAME_INJECTION")):
            caps.add(FRAME_INJECTION)
        if _truthy(flags.get("DEBUG_HEAP")):
            caps.add(HEAP_DEBUG)
        if "BENCH_BUILD_TAG" in flags:
            caps.add(BUILD_TAG)
        return caps

    def bench_only_flags(self) -> list[str]:
        """Flags that change behaviour, and so stop this image speaking for a release."""
        return sorted(
            k
            for k, v in self.build_flags.items()
            if v is not False and v is not None and k not in BEHAVIOURALLY_INERT
        )

    def must_not_ship(self) -> list[str]:
        """Flags that must never reach a release, regardless of representativeness."""
        return sorted(
            k
            for k, v in self.build_flags.items()
            if v is not False and v is not None and k in NEVER_SHIP
        )

    def release_representative(self) -> bool:
        return not self.bench_only_flags()

    def with_build_tag(self, tag: str) -> Bake:
        """A copy carrying its own content hash as BENCH_BUILD_TAG.

        The tag is opaque to the firmware: it never parses or branches on the value, it
        only echoes it once at boot. That keeps the mechanism generic - the meaning
        belongs entirely to whatever produced the build.
        """
        flags = dict(self.build_flags)
        flags["BENCH_BUILD_TAG"] = tag
        return Bake(env=self.env, userprefs=dict(self.userprefs), build_flags=flags, label=self.label)


@dataclass
class ImageEntry:
    """A built image, and everything needed to prove which one it is."""

    bake_hash: str
    bake: dict
    env: str
    artifacts: list[str]
    git_sha: str | None
    dirty: bool
    capabilities: list[str]
    bench_only_flags: list[str]
    release_representative: bool
    flash_bytes: int | None = None
    flash_pct: float | None = None
    ram_bytes: int | None = None
    ram_pct: float | None = None
    toolchain: str | None = None
    hw_model: str | None = None
    built_at: float | None = None
    duration_s: float | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def uf2(self) -> str | None:
        return next((a for a in self.artifacts if a.endswith(".uf2")), None)

    @property
    def hex_file(self) -> str | None:
        return next((a for a in self.artifacts if a.endswith(".hex")), None)

    @property
    def dfu_zip(self) -> str | None:
        """The nrfutil DFU package, if the build produced one.

        Preferred over the .uf2 where possible: serial DFU streams the image over the
        bootloader's CDC, where the UF2 path copies megabytes onto a mass-storage volume
        the OS has just mounted over USB. On a bench whose artifacts live on an external
        USB drive, that write disturbs the very bus the run is recording to.
        """
        return next((a for a in self.artifacts if a.endswith(".zip")), None)


class DriftError(RuntimeError):
    """A scenario's current definition no longer matches the image built for it.

    Editing a scenario silently invalidates its image. Without this check the bench
    asserts new expectations against old firmware and reports the result as a real one.
    """


class Manifest:
    """(scenario, role) -> image, keyed on content hash so bakes deduplicate."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.images: dict[str, ImageEntry] = {}
        self.assignments: dict[str, str] = {}  # "scenario/role" -> bake_hash
        if self.path.exists():
            self.load()

    # -- persistence -----------------------------------------------------------

    def load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.images = {k: ImageEntry(**v) for k, v in data.get("images", {}).items()}
        self.assignments = dict(data.get("assignments", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "images": {k: v.to_dict() for k, v in self.images.items()},
                    "assignments": self.assignments,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # -- use -------------------------------------------------------------------

    def assign(self, scenario: str, role: str, bake_hash: str) -> None:
        self.assignments[f"{scenario}/{role}"] = bake_hash

    def image_for(self, scenario: str, role: str) -> ImageEntry | None:
        h = self.assignments.get(f"{scenario}/{role}")
        return self.images.get(h) if h else None

    def add(self, entry: ImageEntry) -> ImageEntry:
        self.images[entry.bake_hash] = entry
        return entry

    def has(self, bake_hash: str) -> bool:
        entry = self.images.get(bake_hash)
        if entry is None:
            return False
        # A manifest entry whose artifact has since been deleted is not a built image.
        return all(Path(a).exists() for a in entry.artifacts) and bool(entry.artifacts)

    def check_drift(self, scenario: str, role: str, bake: Bake, git_sha: str, dirty: bool) -> None:
        """Refuse to flash an image that no longer matches its scenario's definition."""
        want = bake.content_hash(git_sha, dirty)
        have = self.assignments.get(f"{scenario}/{role}")
        if have is None:
            raise DriftError(f"{scenario}/{role} has no built image; run the build stage")
        if have != want:
            raise DriftError(
                f"{scenario}/{role} was built from bake {have} but its current definition "
                f"hashes to {want}. The scenario changed since the image was built - "
                "rebuild rather than asserting new expectations against old firmware."
            )

    def missing_capabilities(self, scenario: str, role: str, required: Iterable[str]) -> list[str]:
        """Required capabilities this image cannot provide."""
        entry = self.image_for(scenario, role)
        if entry is None:
            return sorted(set(required))
        have = set(entry.capabilities)
        return sorted(set(required) - have)

    def summary(self) -> dict:
        return {
            "images": len(self.images),
            "assignments": len(self.assignments),
            "deduplication": {
                "pairs": len(self.assignments),
                "distinct_images": len(set(self.assignments.values())),
            },
            "not_release_representative": sorted(
                h for h, e in self.images.items() if not e.release_representative
            ),
        }


# -- tree state -----------------------------------------------------------------


# Paths whose contents can change the compiled firmware. Everything else in the tree -
# the bench itself, CI config, notes - cannot, and must not invalidate an image.
#
# Scoping this matters in practice: hashing the whole repo meant editing a bench module
# invalidated every image and forced a ~29 minute rebuild before a run that changed no
# firmware at all. The list errs towards including too much, because under-invalidating
# means asserting against stale firmware, which is the failure this whole mechanism
# exists to prevent.
FIRMWARE_PATHS = (
    "src",
    "lib",
    "variants",
    "boards",
    "arch",
    "protobufs",
    "platformio.ini",
    "userPrefs.jsonc",
    "userPrefs.default.jsonc",
    "partition-table.csv",
)


def git_state(root: Path) -> tuple[str | None, bool]:
    """(short sha, dirty) over the firmware-relevant paths only.

    Both feed the content hash. The SHA is the last commit that touched firmware, so
    bench-side commits do not invalidate images; dirty is computed over the same paths,
    because an uncommitted firmware edit is not reproducible and must not be cached.
    """
    try:
        existing = [p for p in FIRMWARE_PATHS if (root / p).exists()]
        if not existing:
            return None, True

        sha = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", *existing],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *existing],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        return (sha or None), bool(status)
    except (OSError, subprocess.SubprocessError):
        return None, True  # unknown provenance is treated as dirty, never as clean
