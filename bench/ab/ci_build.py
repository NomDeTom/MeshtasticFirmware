"""Build a bench image on GitHub Actions and fetch it into ~/bench-firmware/ab/<tag>.uf2 (+ <tag>-ota.zip).

  python ci_build.py TAG [--ref BRANCH] [--target PIO_ENV] [--repo OWNER/NAME] [--run RUN_ID]

Dispatches "Build One Target" (build_one_target.yml) on BRANCH (default: the current branch, which must be
pushed), waits for it, and extracts the UF2 from the firmware artifact. --run skips the dispatch and fetches
an existing run. The token comes from git's credential helper for github.com.
"""

import argparse
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def token():
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    ).stdout
    return next(
        line.split("=", 1)[1]
        for line in out.splitlines()
        if line.startswith("password=")
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument(
        "--ref",
        default=subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        ).stdout.strip(),
    )
    ap.add_argument("--target", default="nrf52_promicro_diy_tcxo")
    ap.add_argument("--repo", default="NomDeTom/MeshtasticFirmware")
    ap.add_argument("--run", type=int)
    a = ap.parse_args()
    tok = token()
    api = f"https://api.github.com/repos/{a.repo}"

    def call(url, data=None, raw=False):
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/vnd.github+json",
            },
        )
        body = urllib.request.urlopen(req).read()
        return body if raw else (json.loads(body) if body else None)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def download(url):
        # Artifact downloads redirect to blob storage, which rejects the GitHub token: follow it without auth.
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            return urllib.request.build_opener(NoRedirect).open(req).read()
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            return urllib.request.urlopen(e.headers["Location"]).read()

    run_id = a.run
    if not run_id:
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        call(
            f"{api}/actions/workflows/build_one_target.yml/dispatches",
            {"ref": a.ref, "inputs": {"target": a.target, "arch": "all"}},
        )
        print(f"dispatched {a.target} on {a.ref}")
        while not run_id:
            time.sleep(10)
            runs = call(
                f"{api}/actions/workflows/build_one_target.yml/runs?branch={a.ref}&event=workflow_dispatch"
                f"&created=>={started}&per_page=1"
            )["workflow_runs"]
            run_id = runs[0]["id"] if runs else None
        print(f"run {run_id}: https://github.com/{a.repo}/actions/runs/{run_id}")

    while True:
        r = call(f"{api}/actions/runs/{run_id}")
        if r["status"] == "completed":
            if r["conclusion"] != "success":
                sys.exit(f"run {run_id} finished {r['conclusion']}")
            arts = [
                x
                for x in call(f"{api}/actions/runs/{run_id}/artifacts")["artifacts"]
                if x["name"].startswith("firmware-")
            ]
            if arts:
                break
        time.sleep(60)

    out = Path.home() / "bench-firmware" / "ab"
    out.mkdir(parents=True, exist_ok=True)

    def walk(zf):
        for n in zf.namelist():
            if n.endswith(".zip") and not n.endswith("-ota.zip"):
                yield from walk(zipfile.ZipFile(io.BytesIO(zf.read(n))))
            else:
                yield n, zf

    # The repackaged single zip is the largest firmware-* artifact; the per-build one is nested inside it anyway.
    art = max(arts, key=lambda x: x["size_in_bytes"])
    for n, zf in walk(
        zipfile.ZipFile(io.BytesIO(download(art["archive_download_url"])))
    ):
        name = Path(n).name
        if name.endswith("-ota.zip"):
            (out / f"{a.tag}-ota.zip").write_bytes(zf.read(n))
        elif name.endswith(".uf2") and name.startswith("firmware-"):
            (out / f"{a.tag}.uf2").write_bytes(zf.read(n))
    got = sorted(p.name for p in out.glob(f"{a.tag}*"))
    print(f"{a.tag} fetched: {got}")
    if f"{a.tag}.uf2" not in got:
        sys.exit("no UF2 in the artifact")


if __name__ == "__main__":
    main()
