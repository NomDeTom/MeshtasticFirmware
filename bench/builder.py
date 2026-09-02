"""Stage 1: scenario table to deduplicated images.

Builds cost ~29 minutes apiece, so three properties matter more than speed:

  * Deduplicate by content hash. In the beacon matrix 24 (scenario, role) pairs
    collapsed to 18 images and one listener bake served seven scenarios.
  * Resume. A stage-4 failure must never force a stage-1 repeat, so the manifest is
    written after every image and an already-built bake is skipped.
  * Refuse to guess. userPrefs are injected into the real file and then read back
    through a JSON parse, because a silently malformed bake still compiles - for forty
    minutes - and then fails a row for the wrong reason.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from . import hardware, manifest, proc

USERPREFS_NAME = "userPrefs.jsonc"
# Left beside userPrefs.jsonc only while it is modified. Its presence after a crash means
# the tree still holds an injected bake, and restore_userprefs() puts the original back.
BACKUP_SUFFIX = ".bench-orig"

MEMORY_RE = re.compile(
    r"(?P<kind>RAM|Flash):\s*\[[=\s]*\]\s*(?P<pct>[\d.]+)%\s*"
    r"\(used (?P<used>\d+) bytes from (?P<total>\d+) bytes\)"
)

BUILD_TIMEOUT_S = 3600.0


class BuildError(RuntimeError):
    pass


@contextmanager
def build_lock(root: Path, timeout: float = BUILD_TIMEOUT_S) -> Iterator[Path]:
    """Serialise builds against one .pio directory.

    Two pio builds sharing a build directory corrupt scons' .sconsign database: the
    second one's temp file is deleted underneath the first, and the run dies deep inside
    SCons with a FileNotFoundError that looks nothing like the concurrency it is. The
    userPrefs injection is equally unsafe in parallel - two bakes would race over one
    file and each could compile the other's values.

    An O_EXCL lock file holding the owner's pid, so a lock left behind by a killed build
    can be told from one a live build is using. A build interrupted mid-run never reaches
    its cleanup, and without that check the next run would block for the full timeout
    before telling a human to delete a file - which is a long wait to be told to do
    something the bench could establish for itself.
    """
    lock_path = root / ".pio" / "bench-build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_owner_is_gone(lock_path):
                lock_path.unlink(missing_ok=True)
                continue  # stale: the holder died before it could clean up
            if time.monotonic() > deadline:
                raise BuildError(
                    f"another build has held {lock_path} for over {timeout:.0f}s and its "
                    "owner is still alive. Wait for it, or stop it and retry."
                ) from None
            time.sleep(2.0)
    try:
        os.write(fd, f"pid={os.getpid()} started={time.time():.0f}\n".encode())
        os.close(fd)
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)



def _lock_owner_is_gone(lock_path: Path) -> bool:
    """True when the pid recorded in the lock is no longer running.

    A missing or unreadable pid is treated as gone: the lock is then unattributable, and
    an unattributable lock blocking every future build is worse than reclaiming one.
    """
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return True
    match = re.search(r"pid=(\d+)", text)
    if not match:
        return True
    pid = int(match.group(1))
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)  # signal 0 only tests for existence
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive and owned by someone else
    except OSError:
        return True
    return False


@contextmanager
def temporary_userprefs(root: Path, overrides: dict[str, str]) -> Iterator[Path]:
    """Inject factory-default overrides, then restore the file byte-for-byte.

    userPrefs values are FACTORY DEFAULTS: they apply on first boot and on factory reset
    and never again. A node with saved config ignores them entirely, which is why the
    provisioner factory-resets before it expects any of this to take effect.
    """
    path = root / USERPREFS_NAME
    if not overrides:
        yield path
        return

    original = path.read_bytes()
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    backup.write_bytes(original)
    try:
        text = original.decode("utf-8")
        block = ["  // ---- injected by the bench for one build - do not commit ----"]
        for key, value in overrides.items():
            block.append(f'  "{key}": "{value}",')
        brace = text.index("{") + 1
        merged = text[:brace] + "\n" + "\n".join(block) + "\n" + text[brace:].lstrip("\n")
        path.write_text(merged, encoding="utf-8")

        # Parse it back before spending half an hour compiling it. Comments are stripped
        # rather than parsed - this is a validity check, not a JSONC implementation.
        stripped = re.sub(r"^\s*//.*$", "", merged, flags=re.M)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BuildError(f"injected userPrefs is not valid JSON: {exc}") from exc
        missing = [k for k in overrides if k not in parsed]
        if missing:
            raise BuildError(f"userPrefs keys did not survive injection: {missing}")
        yield path
    finally:
        path.write_bytes(original)
        backup.unlink(missing_ok=True)


def restore_userprefs(root: Path) -> bool:
    """Put back an injected userPrefs left behind by a crashed build. True if it acted."""
    path = root / USERPREFS_NAME
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if backup.exists():
        path.write_bytes(backup.read_bytes())
        backup.unlink()
        return True
    return False


def build_flags_env(build_flags: dict) -> dict[str, str]:
    """Translate a flag dict into PLATFORMIO_BUILD_FLAGS.

    True becomes a bare -DNAME (presence-only); False and None drop the flag entirely,
    which is how a scenario turns an inherited flag off.
    """
    parts = []
    for key, value in sorted(build_flags.items()):
        if value is False or value is None:
            continue
        parts.append(f"-D{key}" if value is True else f"-D{key}={value}")
    return {"PLATFORMIO_BUILD_FLAGS": " ".join(parts)} if parts else {}


def parse_memory(output: str) -> dict:
    """Flash and RAM figures from pio's own report.

    Headroom is a real constraint, not a curiosity: the beacon target ran at 96.5% with
    ~28 KB spare, and every added format string came out of that.
    """
    out: dict = {}
    for m in MEMORY_RE.finditer(output):
        kind = m.group("kind").lower()
        out[f"{kind}_pct"] = float(m.group("pct"))
        out[f"{kind}_bytes"] = int(m.group("used"))
        out[f"{kind}_total"] = int(m.group("total"))
    return out


def artifacts_for(root: Path, env: str) -> list[str]:
    build_dir = root / ".pio" / "build" / env
    if not build_dir.is_dir():
        return []
    found: list[Path] = []
    for pattern in ("firmware*.uf2", "firmware*.hex", "firmware*.bin", "firmware*.zip"):
        found.extend(sorted(build_dir.glob(pattern)))
    return [str(p) for p in found]


@dataclass
class Builder:
    root: Path
    pio: str
    manifest: manifest.Manifest
    on_event: Callable[[str, dict], None] | None = None
    # Where per-build logs are written, so the status server can tail a build in
    # progress. Defaults beside the manifest.
    log_dir: Path | None = None

    def _emit(self, kind: str, **data) -> None:
        if self.on_event:
            self.on_event(kind, data)

    def _progress(self, bake_hash: str, line: str) -> None:
        """Surface the few build lines worth seeing from outside.

        Compiling is thousands of lines nobody reads; what a watcher needs is whether it
        is still moving, and the memory figures that decide whether the image fits.
        """
        if MEMORY_RE.search(line) or line.startswith(("Compiling", "Linking", "Building")):
            self._emit("build_progress", bake_hash=bake_hash, line=line.strip()[:200])

    def build_bake(self, bake: manifest.Bake, force: bool = False) -> manifest.ImageEntry:
        """Build one bake, tagging the image with its own content hash."""
        with build_lock(self.root):
            return self._build_bake_locked(bake, force=force)

    def _build_bake_locked(self, bake: manifest.Bake, force: bool = False) -> manifest.ImageEntry:
        sha, dirty = manifest.git_state(self.root)
        bake_hash = bake.content_hash(sha, dirty)

        if not force and self.manifest.has(bake_hash):
            entry = self.manifest.images[bake_hash]
            self._emit("build_skipped", bake_hash=bake_hash, reason="already built")
            return entry

        # The image carries its own identity. APP_VERSION cannot distinguish bakes of one
        # commit, so without this a row flashed with the wrong image looks entirely normal.
        tagged = bake.with_build_tag(bake_hash)
        env_vars = build_flags_env(tagged.build_flags)

        self._emit(
            "build_start",
            bake_hash=bake_hash,
            env=bake.env,
            label=bake.label,
            flags=env_vars.get("PLATFORMIO_BUILD_FLAGS", ""),
        )
        started = time.time()
        log_dir = self.log_dir or self.manifest.path.parent / "builds"
        log_path = Path(log_dir) / f"{bake_hash}.log"
        with temporary_userprefs(self.root, bake.userprefs):
            result = proc.stream(
                [self.pio, "run", "-e", bake.env],
                log_path=log_path,
                cwd=self.root,
                env=env_vars,
                timeout=BUILD_TIMEOUT_S,
                on_line=lambda line: self._progress(bake_hash, line),
            )

        mem = parse_memory(result.output)
        if not result.ok:
            self._emit(
                "build_failed",
                bake_hash=bake_hash,
                env=bake.env,
                returncode=result.returncode,
                tail=result.tail(30),
            )
            raise BuildError(
                f"build of {bake.env} (bake {bake_hash}) failed with "
                f"{result.returncode}:\n{result.tail(30)}"
            )

        artifacts = artifacts_for(self.root, bake.env)
        if not artifacts:
            raise BuildError(f"build of {bake.env} reported success but produced no artifacts")

        entry = manifest.ImageEntry(
            bake_hash=bake_hash,
            bake=tagged.fingerprint(sha, dirty),
            env=bake.env,
            artifacts=artifacts,
            git_sha=sha,
            dirty=dirty,
            capabilities=sorted(tagged.capabilities()),
            bench_only_flags=tagged.bench_only_flags(),
            release_representative=tagged.release_representative(),
            flash_bytes=mem.get("flash_bytes"),
            flash_pct=mem.get("flash_pct"),
            ram_bytes=mem.get("ram_bytes"),
            ram_pct=mem.get("ram_pct"),
            toolchain=_toolchain(result.output),
            hw_model=hardware.hw_model_for_env(self.root, bake.env),
            built_at=started,
            duration_s=result.duration_s,
        )
        self.manifest.add(entry)
        self.manifest.save()
        self._emit(
            "build_done",
            bake_hash=bake_hash,
            env=bake.env,
            duration_s=result.duration_s,
            flash_pct=entry.flash_pct,
            artifacts=len(artifacts),
        )
        return entry

    def build_all(
        self,
        wanted: Sequence[tuple[str, str, manifest.Bake]],
        force: bool = False,
    ) -> dict:
        """Build every distinct bake behind a list of (scenario, role, bake) triples.

        Deduplication happens here: identical bakes across scenarios compile once, and
        the assignment table records which pairs share the image.
        """
        sha, dirty = manifest.git_state(self.root)
        distinct: dict[str, manifest.Bake] = {}
        for scenario, role, bake in wanted:
            h = bake.content_hash(sha, dirty)
            distinct.setdefault(h, bake)
            self.manifest.assign(scenario, role, h)
        self.manifest.save()

        self._emit(
            "build_plan",
            pairs=len(wanted),
            distinct=len(distinct),
            saved=len(wanted) - len(distinct),
        )

        built, failed = [], []
        for h, bake in distinct.items():
            try:
                self.build_bake(bake, force=force)
                built.append(h)
            except BuildError as exc:
                failed.append({"bake_hash": h, "error": str(exc)})
        return {
            "pairs": len(wanted),
            "distinct": len(distinct),
            "built": built,
            "failed": failed,
        }


def _toolchain(output: str) -> str | None:
    m = re.search(r"PLATFORM:\s*(.+?)\s*\|", output)
    return m.group(1).strip() if m else None
