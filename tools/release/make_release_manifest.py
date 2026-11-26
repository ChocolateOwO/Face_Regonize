"""Build the integrity asset for one Reconize GitHub Release.

    python tools/release/make_release_manifest.py v1.0.2 [--out dist-release]

Writes two files to attach to the GitHub Release of that tag:
    release-manifest.json         version, tag, commit and source-tree hash
    release-manifest.json.sha256  SHA-256 of the manifest ("<hex>  release-manifest.json")

The web updater installs a release only when this manifest is present, its
SHA-256 matches (GitHub's own asset digest, or the .sha256 asset), and the
commit/tree it names are exactly what the tag points at. It reads the local
repository only: nothing is pushed, tagged or published by this script.
See docs/RELEASING.md for the whole release procedure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", help="release tag, e.g. v1.0.2 (must already exist locally)")
    ap.add_argument("--out", default="dist-release", help="output folder (default: dist-release)")
    ap.add_argument("--repo", default="", help="owner/repo recorded in the manifest (default: from origin)")
    a = ap.parse_args(argv)

    tag = a.tag
    if not tag.startswith("v"):
        raise SystemExit("Tags look like v1.2.3.")
    version = tag[1:]
    commit = git("rev-parse", f"{tag}^{{commit}}")
    tree = git("rev-parse", f"{tag}^{{tree}}")
    tagged_version = git("show", f"{commit}:VERSION").strip()
    if tagged_version != version:
        raise SystemExit(f"VERSION at {tag} says '{tagged_version}', not '{version}'. Fix VERSION and re-tag.")
    repo = a.repo
    if not repo:
        url = git("remote", "get-url", "origin")
        repo = url.rstrip("/").removesuffix(".git").split("github.com/")[-1]

    manifest = {
        "schema": 1,
        "app": "Reconize",
        "repo": repo,
        "tag": tag,
        "version": version,
        "commit": commit,
        "tree": tree,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out = (ROOT / a.out) if not Path(a.out).is_absolute() else Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (out / "release-manifest.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (out / "release-manifest.json.sha256").write_text(f"{digest}  release-manifest.json\n", encoding="utf-8")
    print(f"{tag}: commit {commit[:12]}, tree {tree[:12]}, manifest sha256 {digest}")
    print(f"Attach both files in {out} to the GitHub Release {tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
