"""Stage the model artefacts listed in models.lock, and verify them.

Run by the `stage` service in compose.tools.yml, so the only tool a reviewer
needs on any OS is Docker. It replaces the three hand-typed recipes the README
used to carry -- `Invoke-WebRequest` + `Get-FileHash` on Windows, `curl` +
`shasum` on macOS, `curl` + `sha256sum` on Linux -- with one implementation.

models.lock is the single source of truth for what is staged and for its
checksum. Nothing here hard-codes a hash.

Deliberately stdlib-only: this runs in the pinned python:3.12-slim image with
no pip install, so staging has nothing to download except the artefacts
themselves.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

CHUNK = 1 << 20  # 1 MiB


def parse_lock(lock_path: Path) -> list[tuple[str, Path]]:
    """Read a sha256sum-format lock file into (digest, path) pairs.

    Comments and blank lines are skipped, and the binary-mode `*` prefix that
    `sha256sum` writes is stripped. GNU coreutils tolerates the comment line in
    models.lock and busybox does not, which is exactly the kind of divergence
    this script exists to remove.
    """
    entries: list[tuple[str, Path]] = []
    for lineno, raw in enumerate(lock_path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition(" ")
        name = name.strip().lstrip("*")
        if len(digest) != 64 or not name:
            sys.exit(f"{lock_path}:{lineno}: not a sha256sum line: {raw!r}")
        entries.append((digest.lower(), Path(name)))
    if not entries:
        sys.exit(f"{lock_path}: no artefacts listed")
    return entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> str:
    """Stream `url` to `dest`, returning the sha256 of what was written.

    Writes to a sibling .part file and renames on success, so an interrupted
    download never leaves something at the real path that a later run would
    mistake for a staged artefact.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    written = 0
    next_report = 0

    with urllib.request.urlopen(url) as response:  # noqa: S310 - pinned https URL from config
        total = int(response.headers.get("Content-Length") or 0)
        with part.open("wb") as handle:
            while chunk := response.read(CHUNK):
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if written >= next_report:
                    mb = written // (1 << 20)
                    if total:
                        print(f"  {mb} MiB / {total // (1 << 20)} MiB", flush=True)
                    else:
                        print(f"  {mb} MiB", flush=True)
                    next_report = written + (64 << 20)

    part.replace(dest)
    return digest.hexdigest()


def stage(entry: tuple[str, Path], base_url: str) -> None:
    expected, path = entry

    if path.exists():
        print(f"{path} is already here, verifying it")
        actual = sha256_file(path)
        if actual == expected:
            print(f"  OK  {expected}")
            return
        # A present-but-wrong file is a hard stop, never a silent re-download:
        # it is either a truncated earlier attempt or a different artefact, and
        # both are things the operator should see.
        sys.exit(
            f"{path} does not match models.lock.\n"
            f"  expected {expected}\n"
            f"  actual   {actual}\n"
            f"Delete the file and run staging again to re-fetch it."
        )

    url = f"{base_url.rstrip('/')}/{path.name}"
    print(f"{path} is missing, fetching it from {url}")
    actual = download(url, path)
    if actual != expected:
        path.unlink(missing_ok=True)
        sys.exit(
            f"{path} failed its checksum and has been removed.\n"
            f"  expected {expected}\n"
            f"  actual   {actual}"
        )
    print(f"  OK  {expected}")


def main(argv: list[str]) -> int:
    lock_path = Path(argv[1] if len(argv) > 1 else "models.lock")
    if not lock_path.is_file():
        sys.exit(f"{lock_path}: not found")

    # The one place the download location is configured. Point it at an
    # internal mirror (Harbor, Artifactory, a file server) to stage without
    # reaching the public internet; the checksum check is unchanged either way.
    base_url = os.environ.get("MODEL_BASE_URL", "").strip()

    entries = parse_lock(lock_path)
    missing = [path for _, path in entries if not path.exists()]
    if missing and not base_url:
        sys.exit(
            "MODEL_BASE_URL is not set, and these artefacts are not staged yet: "
            + ", ".join(str(path) for path in missing)
        )

    for entry in entries:
        stage(entry, base_url)

    print(f"\nStaged {len(entries)} artefact(s), all matching {lock_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
