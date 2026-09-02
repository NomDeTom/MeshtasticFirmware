"""A local store of known-good firmware images, owned by the bench.

Populated when a bench is commissioned and kept thereafter, so a run can flash a known
reference without a compiler, a network, or a path hardcoded into a scenario. That
matters for three separate reasons:

  A baseline that is not the tree under test. Building the control from the same checkout
  as the subject means a local mistake can produce a pass in both, and neither would
  disagree with the other. A stock upstream release cannot do that.

  Speed. Building this firmware takes about half an hour, which is far too long to sit
  between "is the bench working?" and the answer.

  Reproducibility. Six months later, "we flashed the stock release" is only a fact if the
  bytes are still here and still the same bytes.

"Known-good" is a claim, so it is checked rather than assumed. Every image is stored under
its own SHA-256, the digest is recorded, and it is re-verified before use: an image whose
content no longer matches its digest is refused, never silently flashed. The board it
targets is recorded too, so the store cannot hand out an image for the wrong hardware.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UPSTREAM = "meshtastic/firmware"
USER_AGENT = "meshtastic-bench"


class FirmwareError(RuntimeError):
    pass


def default_root() -> Path:
    """Where the store lives. Local disk, never the repo.

    Deliberately outside the checkout: images are large, they are not source, and a bench
    whose checkout sits on removable storage should not lose its reference firmware with
    the drive.
    """
    override = os.environ.get("BENCH_FIRMWARE_ROOT")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    try:
        if home.is_dir():
            return home / "bench-firmware"
    except OSError:
        pass
    return Path("bench-firmware")


@dataclass
class Image:
    """One stored image and everything known about where it came from."""

    sha256: str
    filename: str
    board: str
    version: str
    source: str
    bytes: int
    added_at: float
    note: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def path(self, root: Path) -> Path:
        return Path(root) / self.filename

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d", time.localtime(self.added_at))
        return f"{self.version:24} {self.board:22} {self.sha256[:12]}  {when}  {self.note}"


class FirmwareStore:
    """Known-good images, indexed by digest, verified before use."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_root()
        self.index_path = self.root / "index.json"
        self.images: dict[str, Image] = {}
        if self.index_path.exists():
            self.load()

    # -- persistence -----------------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.images = {k: Image(**v) for k, v in (data.get("images") or {}).items()}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps({"images": {k: v.to_dict() for k, v in self.images.items()}}, indent=2),
            encoding="utf-8",
        )

    # -- adding ----------------------------------------------------------------

    def add_bytes(
        self,
        payload: bytes,
        filename: str,
        board: str,
        version: str,
        source: str,
        note: str = "",
        tags: list[str] | None = None,
    ) -> Image:
        digest = hashlib.sha256(payload).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        # Stored under the digest so two releases that happen to share a filename cannot
        # overwrite one another, and so the name on disk states what the file is.
        stored = f"{digest[:12]}-{filename}"
        (self.root / stored).write_bytes(payload)

        image = Image(
            sha256=digest, filename=stored, board=board, version=version,
            source=source, bytes=len(payload), added_at=time.time(),
            note=note, tags=list(tags or []),
        )
        self.images[digest] = image
        self.save()
        return image

    def add_file(self, path: Path, board: str, version: str, note: str = "") -> Image:
        path = Path(path)
        return self.add_bytes(
            path.read_bytes(), path.name, board, version,
            source=f"file:{path}", note=note,
        )

    # -- fetching from upstream ------------------------------------------------

    def fetch_release(
        self,
        board_env: str,
        tag: str | None = None,
        allow_prerelease: bool = False,
        prerelease_only: bool = False,
        timeout: float = 300.0,
    ) -> Image:
        """Pull one board's image out of an upstream release archive.

        Takes the PlatformIO env name, because that is what a scenario already knows and
        what the release names its images by - so the mapping needs no second table to
        drift out of step.
        """
        arch = _arch_for(board_env)
        release, asset = self._release_with_asset(
            tag, allow_prerelease or prerelease_only, arch, timeout,
            prerelease_only=prerelease_only,
        )

        blob = _get(asset["browser_download_url"], timeout)
        archive = zipfile.ZipFile(io.BytesIO(blob))
        wanted = [
            n for n in archive.namelist()
            if Path(n).name.startswith(f"firmware-{board_env}-") and n.endswith(".uf2")
        ]
        if not wanted:
            raise FirmwareError(f"{asset['name']} contains no .uf2 for {board_env}")
        member = wanted[0]
        payload = archive.read(member)
        return self.add_bytes(
            payload,
            Path(member).name,
            board=_board_slug(board_env),
            version=release["tag_name"],
            source=asset["browser_download_url"],
            note="upstream prerelease" if release.get("prerelease") else "upstream release",
            tags=["upstream", "alpha" if release.get("prerelease") else "stable", "known-good"],
        )

    def _release_with_asset(
        self, tag: str | None, allow_prerelease: bool, arch: str, timeout: float,
        prerelease_only: bool = False,
    ) -> tuple[dict, dict]:
        """The newest release that actually carries an image for this architecture.

        "Latest alpha" has to mean the latest one with something in it. Upstream tags
        releases before CI finishes publishing, so the top of the list is routinely a tag
        with zero assets - taking it and failing would report an empty shelf as a broken
        fetch.
        """
        if tag:
            releases = [json.loads(_get(
                f"https://api.github.com/repos/{UPSTREAM}/releases/tags/{tag}", timeout))]
        else:
            releases = json.loads(_get(
                f"https://api.github.com/repos/{UPSTREAM}/releases?per_page=25", timeout))

        skipped: list[str] = []
        for release in releases:
            pre = bool(release.get("prerelease"))
            if not allow_prerelease and pre:
                continue
            # "The latest alpha" has to be an alpha. Falling back to a stable build would
            # hand back the same bytes under a different name, and the store would then
            # hold one image claiming to be two things.
            if prerelease_only and not pre:
                continue
            asset = next(
                (a for a in release.get("assets", [])
                 if a["name"].startswith(f"firmware-{arch}-") and a["name"].endswith(".zip")),
                None,
            )
            if asset is not None:
                return release, asset
            skipped.append(release["tag_name"])
        raise FirmwareError(
            f"no release carries a firmware-{arch} archive"
            + (f" (skipped, not yet published: {', '.join(skipped[:4])})" if skipped else "")
        )

    # -- using -----------------------------------------------------------------

    def verify(self, image: Image) -> bool:
        """Re-hash the stored bytes. A known-good image has to still be those bytes."""
        path = image.path(self.root)
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == image.sha256
        except OSError:
            return False

    def get(self, board: str, version: str | None = None, channel: str | None = None) -> Image:
        """The newest verified image for a board, or a specific version.

        Raises rather than returning None: a scenario asking for a known-good image and
        silently getting nothing would flash whatever was lying around, or nothing at all,
        and either way the row would not mean what it says.
        """
        board_key = _norm(board)
        found = [
            img for img in self.images.values()
            if _norm(img.board) == board_key
            and (version is None or img.version == version)
            # "stable" and "alpha" are different questions. Newest-wins across both would
            # hand a smoke test a prerelease simply because it was fetched last.
            and (channel is None or channel in img.tags)
        ]
        if not found:
            have = sorted({i.board for i in self.images.values()}) or ["nothing"]
            raise FirmwareError(
                f"no stored image for {board!r}"
                + (f" on the {channel} channel" if channel else "")
                + (f" at {version}" if version else "")
                + f". The store holds: {', '.join(have)}. "
                "Commission it with: python -m bench firmware fetch <env>"
            )
        found.sort(key=lambda i: i.added_at, reverse=True)
        for image in found:
            if self.verify(image):
                return image
            # Refused rather than repaired: an image whose bytes no longer match its
            # digest is not the thing that was tested, whatever its filename says.
        raise FirmwareError(
            f"every stored image for {board!r} failed verification - the files on disk no "
            "longer match the digests recorded for them"
        )

    def path_for(
        self, board: str, version: str | None = None, channel: str | None = None
    ) -> Path:
        return self.get(board, version, channel).path(self.root)

    def summary(self) -> str:
        if not self.images:
            return f"  (empty - {self.root})"
        rows = sorted(self.images.values(), key=lambda i: (i.board, i.added_at))
        lines = [f"  {'version':24} {'board':22} {'digest':12}  added       note"]
        for image in rows:
            ok = "" if self.verify(image) else "  [FAILS VERIFICATION]"
            lines.append("  " + image.describe() + ok)
        return "\n".join(lines)


# -- helpers --------------------------------------------------------------------


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout).read()


def _norm(value: str) -> str:
    return str(value or "").strip().upper().replace("-", "_")


def _arch_for(board_env: str) -> str:
    """Which release archive a board's image lives in."""
    env = board_env.lower()
    if "nrf52" in env:
        return "nrf52840"
    if "rp2040" in env or "rp2350" in env or "pico" in env:
        return "rp2040"
    if "stm32" in env:
        return "stm32"
    return "esp32"


def _board_slug(board_env: str) -> str:
    """The hardware model a PlatformIO env targets, read from the variant's own ini.

    Falls back to the env name so the store still records something usable when run
    outside a firmware checkout.
    """
    from . import hardware

    slug = hardware.hw_model_for_env(Path("."), board_env)
    return slug or board_env
