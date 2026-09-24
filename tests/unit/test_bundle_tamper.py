"""Negative tests for the offline bundle's verification chain.

Every other test here asks whether the system works. These ask whether the
release refuses to install once someone has changed it. Each case builds a tiny
synthetic release folder -- a few text files and a `docker save`-shaped archive
of four kilobyte-sized images -- tampers with exactly one thing, and asserts
that `scripts/verify-bundle.sh` fails for the right reason.

The synthetic archive is the point: the real one is 1.8 GB, and a test that
needs it is a test nobody runs. Its shape is taken from a real `docker save` on
Docker 29.8 -- an OCI layout with `index.json`, an `io.containerd.image.name`
annotation per image, and content-addressed blobs.

Several cases re-hash the folder after tampering, because SHA256SUMS is
self-attesting: anyone who can rewrite the files can rewrite the checksums with
them. Those cases are the ones that show what is left when that layer is gone
-- the CI run manifest, the committed IMAGES.lock and models.lock, and the
blob digests inside the archive.

Every case fails before `docker load`, because the verification scripts never
call Docker at all; `test_verification_never_calls_docker` and
`test_installer_verifies_before_it_loads` are what keep that true.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = ("verify-bundle.sh", "verify-bundle-images.sh", "bundle-image-manifests.sh")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
GHCR = "ghcr.io/example/act-on-weather"
ALIASES = ("services", "ui", "postgres", "demos")


def _find_shell() -> str | None:
    """A shell that can run the bundle scripts, or None to skip.

    In CI these tests run inside the Linux test image, where this is simply
    `bash`. On the Windows development machine `bash` on PATH is often the WSL
    launcher in System32, which cannot execute anything in this checkout, so
    Git for Windows' shell is used instead. Set AOW_TEST_BASH to override.
    """
    override = os.environ.get("AOW_TEST_BASH")
    if override:
        return override
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = _find_shell()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no POSIX shell with coreutils (the CI test image has one)",
)


# ------------------------------------------------------------------ fixtures --


def _write(path: Path, text: str) -> None:
    """Write LF-terminated text on every platform.

    `Path.write_text` translates newlines, and a SHA256SUMS whose lines end in
    CRLF makes `sha256sum -c` look for files whose names end in a carriage
    return. The real bundle is written on Linux; the fixture matches it.
    """
    path.write_bytes(text.encode())


def _put(blobs: Path, data: bytes) -> tuple[str, int]:
    digest = hashlib.sha256(data).hexdigest()
    (blobs / digest).write_bytes(data)
    return f"sha256:{digest}", len(data)


def _write_images_tar(path: Path, work: Path) -> dict[str, str]:
    """Write a docker-save-shaped archive; return alias -> manifest digest."""
    blobs = work / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    _write(work / "oci-layout", '{"imageLayoutVersion":"1.0.0"}')

    manifests = []
    digests: dict[str, str] = {}
    for alias in ALIASES:
        # A real image config carries both `os` and `architecture`, and
        # verify-bundle-images.sh reads them to check the archive holds this
        # release's platform. A config without `os` is not a shape docker ever
        # writes, so the fixture must not invent one.
        config, config_size = _put(
            blobs, f'{{"os":"linux","architecture":"amd64","alias":"{alias}"}}'.encode()
        )
        layer, layer_size = _put(blobs, f"layer bytes for {alias}".encode())
        manifest, manifest_size = _put(
            blobs,
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"digest": config, "size": config_size},
                    "layers": [{"digest": layer, "size": layer_size}],
                }
            ).encode(),
        )
        digests[alias] = manifest
        # Key order matters: bundle-image-manifests.sh takes the digest that
        # precedes the name annotation, exactly as `docker save` writes it.
        manifests.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest,
                "size": manifest_size,
                "annotations": {
                    "io.containerd.image.name": f"docker.io/aow-bundle/{alias}:{COMMIT}"
                },
            }
        )

    _write(
        work / "index.json",
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": manifests,
            }
        ),
    )
    with tarfile.open(path, "w") as archive:
        for member in sorted(work.rglob("*")):
            archive.add(member, arcname=member.relative_to(work).as_posix())
    return digests


def rewrite_sums(bundle: Path) -> None:
    """Regenerate SHA256SUMS the way scripts/package-offline.sh does."""
    lines = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
        name = path.relative_to(bundle).as_posix()
        if name == "SHA256SUMS":
            continue
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{name}\n")
    _write(bundle / "SHA256SUMS", "".join(lines))


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A minimal, internally consistent offline release folder."""
    root = tmp_path / "aow-release"
    (root / "scripts").mkdir(parents=True)
    for script in SCRIPTS:
        shutil.copy(REPO / "scripts" / script, root / "scripts" / script)

    digests = _write_images_tar(root / "images.tar", tmp_path / "archive")

    # The CI run artifact, under the name the bundle gives it: "images.lock"
    # would collide with IMAGES.lock on a case-insensitive filesystem.
    _write(
        root / "ci-images.lock",
        f"commit {COMMIT}\n"
        f"services {GHCR}/services@{digests['services']}\n"
        f"ui {GHCR}/ui@{digests['ui']}\n",
    )
    # Committed to the repository, and checked by CI against the Compose files.
    _write(root / "IMAGES.lock", f"# upstream images\npostgres@{digests['postgres']}\n")
    _write(
        root / "images.bundle.lock",
        f"services {GHCR}/services@{digests['services']}\n"
        f"ui {GHCR}/ui@{digests['ui']}\n"
        f"postgres postgres:17-alpine@{digests['postgres']}\n"
        f"demos aow-bundle/demos@{digests['demos']}\n",
    )
    _write(root / "release-version.txt", f"{COMMIT}\n")

    (root / "models").mkdir()
    model = root / "models" / "model.gguf"
    model.write_bytes(b"pretend this is 1.1 GB of model weights")
    _write(
        root / "models.lock",
        f"# Model artefacts\n{hashlib.sha256(model.read_bytes()).hexdigest()} *models/model.gguf\n",
    )
    # Something ordinary to flip a byte in: the code travels by file copy too.
    _write(root / "compose.yml", "name: aow\nservices: {}\n")

    rewrite_sums(root)
    return root


def verify(bundle: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "scripts/verify-bundle.sh", "."],
        cwd=bundle,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# ----------------------------------------------------------------- the cases --


def test_clean_bundle_passes(bundle: Path) -> None:
    result = verify(bundle)
    assert result.returncode == 0, output(result)
    assert "images.tar matches images.bundle.lock" in result.stdout
    assert "Bundle verified" in result.stdout


def test_flipped_byte_in_a_bundled_file(bundle: Path) -> None:
    _write(bundle / "compose.yml", "name: aow\nservices: {}\n# one more line\n")
    result = verify(bundle)
    assert result.returncode != 0
    assert "./compose.yml: FAILED" in output(result)


def test_added_file_that_sha256sums_does_not_list(bundle: Path) -> None:
    # `sha256sum -c` is silent about a file nobody listed, so the release lists
    # what should be there and refuses anything else.
    _write(bundle / "scripts" / "helpful.sh", "#!/bin/sh\necho hello\n")
    result = verify(bundle)
    assert result.returncode != 0
    assert "does not list" in output(result)
    assert "./scripts/helpful.sh" in output(result)


def test_corrupted_model_fails_against_models_lock(bundle: Path) -> None:
    # Re-hashed, so this is not SHA256SUMS catching it: models.lock is
    # committed, so its digest was fixed before the bundle existed.
    (bundle / "models" / "model.gguf").write_bytes(b"pretend this is a different model")
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "models/model.gguf: FAILED" in output(result)


def test_one_image_swapped_for_another(bundle: Path) -> None:
    # The classic swap, done properly: the postgres entry now points at a real,
    # self-consistent manifest that happens to be a different image. The
    # archive is internally valid, and the folder was re-hashed afterwards.
    # What it cannot be made consistent with is images.bundle.lock, whose
    # digests come from the CI manifest and from the committed IMAGES.lock.
    index = _read_index(bundle)
    index["manifests"][2]["digest"] = index["manifests"][1]["digest"]
    _write_index(bundle, index)
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "release expects" in output(result)


def test_manifest_bytes_rewritten_under_their_own_digest(bundle: Path) -> None:
    # The other way to swap an image: keep every digest the release names, and
    # change what the blob of that name contains. index.json is only a label,
    # so the label is checked against the content it names.
    index = _read_index(bundle)
    postgres = index["manifests"][2]["digest"].removeprefix("sha256:")
    _rewrite_member(bundle, f"blobs/sha256/{postgres}", b'{"schemaVersion":2,"layers":[]}')
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "holds different bytes" in output(result)


def test_layer_bytes_replaced_inside_the_archive(bundle: Path) -> None:
    # A layer is named by digest inside a manifest this gate has already
    # verified, so changing one either breaks SHA256SUMS -- as here -- or, if
    # the folder is re-hashed, leaves an archive whose manifest names a blob
    # that is no longer there. Docker refuses to store that blob, which is why
    # install-offline.sh reads the output of `docker load` rather than trusting
    # its exit status.
    _rewrite_member(
        bundle, None, b"layer bytes for something else", match=b"layer bytes for postgres"
    )
    result = verify(bundle)
    assert result.returncode != 0
    assert "./images.tar: FAILED" in output(result)


def test_image_digest_changed_in_the_index(bundle: Path) -> None:
    # The other half of the swap: relabel the archive instead of rewriting it.
    index = _read_index(bundle)
    index["manifests"][2]["digest"] = "sha256:" + "0" * 64
    _write_index(bundle, index)
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "images.tar has sha256:" + "0" * 64 in output(result)


def test_image_missing_from_the_archive(bundle: Path) -> None:
    index = _read_index(bundle)
    index["manifests"] = [
        manifest
        for manifest in index["manifests"]
        if "postgres" not in manifest["annotations"]["io.containerd.image.name"]
    ]
    _write_index(bundle, index)
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "images.tar does not contain postgres" in output(result)


def test_upstream_image_swapped_for_one_the_repository_does_not_pin(bundle: Path) -> None:
    # A fully re-hashed, internally consistent bundle still fails: the upstream
    # digest has to match the IMAGES.lock that is committed to git.
    index = _read_index(bundle)
    postgres = index["manifests"][2]["digest"]
    other = index["manifests"][1]["digest"]  # the ui manifest: a real blob
    text = (bundle / "images.bundle.lock").read_text()
    _write(bundle / "images.bundle.lock", text.replace(postgres, other))
    index["manifests"][2]["digest"] = other
    _write_index(bundle, index)
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "IMAGES.lock has" in output(result)


def test_application_image_not_the_one_ci_published(bundle: Path) -> None:
    index = _read_index(bundle)
    services = index["manifests"][0]["digest"]
    other = index["manifests"][1]["digest"]
    text = (bundle / "images.bundle.lock").read_text()
    _write(bundle / "images.bundle.lock", text.replace(f"services@{services}", f"services@{other}"))
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "the CI manifest has" in output(result)


def test_release_version_does_not_match_the_ci_manifest(bundle: Path) -> None:
    _write(bundle / "release-version.txt", "f" * 40 + "\n")
    rewrite_sums(bundle)
    result = verify(bundle)
    assert result.returncode != 0
    assert "the CI manifest was built for" in output(result)


def test_out_of_band_digest_is_checked_when_supplied(bundle: Path) -> None:
    # The one anchor that does not live in the folder, for an operator who was
    # handed the digest by a different route.
    result = verify(bundle, AOW_SHA256SUMS="sha256:" + "0" * 64)
    assert result.returncode != 0
    assert "supplied out of band" in output(result)

    good = hashlib.sha256((bundle / "SHA256SUMS").read_bytes()).hexdigest()
    assert verify(bundle, AOW_SHA256SUMS=good).returncode == 0


def test_verification_never_calls_docker() -> None:
    # A gate that needed the daemon could not run before `docker load`. The
    # comments in those scripts discuss Docker at length, so only code counts.
    for script in SCRIPTS:
        code = [
            line
            for line in (REPO / "scripts" / script).read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert "docker" not in "\n".join(code)


def test_installer_verifies_before_it_loads() -> None:
    lines = (REPO / "scripts" / "install-offline.sh").read_text().splitlines()
    verified_at = next(i for i, line in enumerate(lines) if "verify-bundle.sh" in line)
    loaded_at = next(i for i, line in enumerate(lines) if line.startswith("docker load"))
    assert verified_at < loaded_at


# ------------------------------------------------------------- archive edits --


def _read_index(bundle: Path) -> dict:
    with tarfile.open(bundle / "images.tar") as archive:
        return json.loads(archive.extractfile("index.json").read())


def _replace_members(bundle: Path, edit) -> None:
    """Rewrite images.tar, passing every member's bytes through `edit`."""
    source = bundle / "images.tar"
    rewritten = bundle.parent / "rewritten.tar"
    with tarfile.open(source) as old, tarfile.open(rewritten, "w") as new:
        for member in old.getmembers():
            if not member.isfile():
                new.addfile(member)
                continue
            data = edit(member.name, old.extractfile(member).read())
            member.size = len(data)
            new.addfile(member, io.BytesIO(data))
    rewritten.replace(source)


def _write_index(bundle: Path, index: dict) -> None:
    encoded = json.dumps(index).encode()
    _replace_members(bundle, lambda name, data: encoded if name == "index.json" else data)


def _rewrite_member(
    bundle: Path, member: str | None, new: bytes, match: bytes | None = None
) -> None:
    """Replace a member by name, or any member whose bytes are `match`."""
    _replace_members(
        bundle,
        lambda name, data: new
        if (name == member or (match is not None and data == match))
        else data,
    )
