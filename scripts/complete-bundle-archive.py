"""Fill Docker's incomplete OCI save with verified GHCR blobs.

Docker's containerd image store can omit config/layer blobs from ``docker save``
when images share layers. The archive still contains the correct index and
manifest digests, so fetch only the missing content by its digest. The usual
bundle verifier then checks the completed archive before it can be shipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def docker_basic_credentials() -> tuple[str, str] | None:
    """Use the GHCR login the operator already made with ``docker login``."""
    config_dir = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker"))
    config_path = config_dir / "config.json"
    if not config_path.is_file():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auths = config.get("auths", {})
    entry = auths.get("ghcr.io") or auths.get("https://ghcr.io") or {}
    if entry.get("auth"):
        decoded = base64.b64decode(entry["auth"]).decode()
        user, secret = decoded.split(":", 1)
        return user, secret
    helper = config.get("credHelpers", {}).get("ghcr.io") or config.get("credsStore")
    if not helper:
        return None
    try:
        result = subprocess.run(
            [f"docker-credential-{helper}", "get"],
            input="ghcr.io",
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    account = json.loads(result.stdout)
    return account["Username"], account["Secret"]


def token(repo: str) -> str:
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repo}:pull"})
    headers = {}
    if os.environ.get("GHCR_TOKEN"):
        user = os.environ.get("GHCR_USER", "")
        if not user:
            raise ValueError("GHCR_USER is required when GHCR_TOKEN is set")
        credentials = user, os.environ["GHCR_TOKEN"]
    else:
        credentials = docker_basic_credentials()
    if credentials:
        basic = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode())
        headers["Authorization"] = f"Basic {basic.decode()}"
    request = urllib.request.Request(f"https://ghcr.io/token?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    return result.get("token") or result["access_token"]


def fetch_blob(repo: str, digest: str, bearer: str, dest: Path) -> None:
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"invalid blob digest: {digest}")
    url = f"https://ghcr.io/v2/{repo}/blobs/{digest}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    calculated = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
            calculated.update(chunk)
    if calculated.hexdigest() != digest[7:]:
        dest.unlink(missing_ok=True)
        raise ValueError(f"registry returned wrong bytes for {digest}")


def json_blob(archive: tarfile.TarFile, digest: str) -> dict:
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"invalid manifest digest: {digest}")
    member = archive.extractfile(f"blobs/sha256/{digest[7:]}")
    if member is None:
        raise ValueError(f"archive is missing manifest {digest}")
    contents = member.read()
    if hashlib.sha256(contents).hexdigest() != digest[7:]:
        raise ValueError(f"archive manifest has wrong bytes: {digest}")
    return json.loads(contents)


def missing_blobs(archive: tarfile.TarFile, locks: dict[str, str]) -> dict[str, str]:
    present = set(archive.getnames())
    missing = {}
    for component in ("services", "ui"):
        reference = locks[component]
        match = re.fullmatch(
            r"ghcr\.io/([a-z0-9._-]+/[a-z0-9._-]+/" + component + r")@(sha256:[0-9a-f]{64})",
            reference,
        )
        if not match:
            raise ValueError(f"invalid {component} GHCR reference in images.bundle.lock")
        repo, root_digest = match.groups()
        root = json_blob(archive, root_digest)
        children = [
            item["digest"]
            for item in root.get("manifests", [])
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(children) != 1:
            raise ValueError(f"{component}: expected one linux/amd64 child manifest")
        manifest = json_blob(archive, children[0])
        descriptors = [manifest["config"], *manifest["layers"]]
        for descriptor in descriptors:
            digest = descriptor["digest"]
            if not DIGEST.fullmatch(digest):
                raise ValueError(f"{component}: invalid layer/config digest")
            if f"blobs/sha256/{digest[7:]}" not in present:
                previous = missing.setdefault(digest, repo)
                if previous != repo:
                    # Identical digest means identical bytes, so either repo
                    # could serve it. Keep the first source in the lock.
                    pass
    return missing


def main(archive_path: Path, lock_path: Path) -> None:
    locks = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        alias, reference = line.split()
        locks[alias] = reference
    with tarfile.open(archive_path, "r:") as archive:
        missing = missing_blobs(archive, locks)
    if not missing:
        print("OCI archive already contains all services and UI blobs")
        return

    print(f"Completing OCI archive: {len(missing)} missing services/UI blobs")
    credentials = {repo: token(repo) for repo in set(missing.values())}
    with tempfile.TemporaryDirectory() as scratch:
        paths = {}
        for digest, repo in missing.items():
            path = Path(scratch) / digest[7:]
            fetch_blob(repo, digest, credentials[repo], path)
            paths[digest] = path
        with tarfile.open(archive_path, "a:") as archive:
            for digest, path in paths.items():
                archive.add(path, arcname=f"blobs/sha256/{digest[7:]}", recursive=False)
    print(f"Added {len(missing)} blobs, each verified against its registry digest")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: complete-bundle-archive.py images.tar images.bundle.lock")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
