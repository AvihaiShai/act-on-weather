"""Does the offline bundle's archive actually hold loadable images?

`test_bundle_tamper.py` asks whether the release refuses to install once someone
has *changed* it. These ask a different question, and the one that cost a real
release: whether the release refuses to install when `docker save` quietly wrote
less than it was asked for. Nothing had been changed in that bundle. SHA256SUMS
passed, every manifest digest matched, and the two application images were in
`images.tar` as a manifest blob and nothing else -- no config, no layers, exit
status 0. See docs/RELEASE-PROOF.md section 1.

`scripts/verify-bundle-images.sh` grew a completeness gate for that, but the
gate had no test: the tamper fixture builds complete, single-platform, amd64
images, so it exercises the pass path by accident and none of the fail paths at
all. The cases below build the archive shapes that matter -- the empty one, the
wrong-architecture one, and the multi-platform one that must stay legal -- and
run the real script against them. No Docker, no network.

The second half is about the other end of the same defect. A `docker load` on an
engine that already holds the content succeeds from an incomplete archive, which
is why the bad bundle installed perfectly on the machine that packaged it and
nowhere else. `scripts/install-offline.sh` now takes a census of release tags
before it loads, so the install identifies content it might have reused; those
tests drive the real installer with a stub `docker` on PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

# The same shell-lookup guard, for the same reason: on the Windows development
# machine `bash` on PATH is often the WSL launcher, which cannot execute
# anything in this checkout. Defined once, in the older of the two files.
from tests.unit.test_bundle_tamper import BASH

REPO = Path(__file__).resolve().parents[2]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
GHCR = "ghcr.io/example/act-on-weather"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no POSIX shell with coreutils (the CI test image has one)",
)


# ------------------------------------------------------------ archive builder --


class Archive:
    """A `docker save`-shaped OCI archive, assembled blob by blob.

    Every knob here exists because some real archive had that shape. `content`
    off is the F10 defect: the manifest is written, the blobs it names are not.
    `present` off on a child is the normal multi-platform case, where the index
    lists every platform upstream published and the archive holds only the one
    that was exported.
    """

    def __init__(self, work: Path) -> None:
        self.work = work
        self.blobs = work / "blobs" / "sha256"
        self.blobs.mkdir(parents=True)
        (work / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
        self.entries: list[dict] = []
        self.digests: dict[str, str] = {}

    def _blob(self, data: bytes, *, store: bool = True, on_disk: bytes | None = None):
        """Return this content's digest, writing it only if `store`.

        `on_disk` writes different bytes under the true digest's name, which is
        a corrupt blob rather than a missing one.
        """
        digest = hashlib.sha256(data).hexdigest()
        if store:
            (self.blobs / digest).write_bytes(data if on_disk is None else on_disk)
        return f"sha256:{digest}", len(data)

    def _image(
        self,
        alias: str,
        *,
        platform: str = "linux/amd64",
        layers: int = 2,
        content: bool = True,
        corrupt_layer: bool = False,
        present: bool = True,
    ):
        """Write one image manifest and, if `content`, its config and layers."""
        os_name, _, architecture = platform.partition("/")
        config, config_size = self._blob(
            json.dumps({"os": os_name, "architecture": architecture, "alias": alias}).encode(),
            store=content,
        )
        described = []
        for number in range(layers):
            # The platform is part of the bytes so that two children of one
            # image cannot accidentally share a layer digest, which would make
            # an absent child look complete.
            data = f"layer {number} of {alias} on {platform}".encode()
            corrupt = b"not the bytes this digest names" if corrupt_layer and number == 0 else None
            digest, size = self._blob(data, store=content, on_disk=corrupt)
            described.append({"digest": digest, "size": size})
        return self._blob(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": OCI_MANIFEST,
                    "config": {"digest": config, "size": config_size},
                    "layers": described,
                }
            ).encode(),
            store=present,
        )

    def _name(self, alias: str, digest: str, size: int, media_type: str) -> None:
        # Key order matters: bundle-image-manifests.sh takes the digest that
        # precedes the name annotation, exactly as `docker save` writes it.
        self.digests[alias] = digest
        self.entries.append(
            {
                "mediaType": media_type,
                "digest": digest,
                "size": size,
                "annotations": {
                    "io.containerd.image.name": f"docker.io/aow-bundle/{alias}:{COMMIT}"
                },
            }
        )

    def image(self, alias: str, **kwargs) -> None:
        """An image published as a bare manifest -- how CI published `services`."""
        digest, size = self._image(alias, **kwargs)
        self._name(alias, digest, size, OCI_MANIFEST)

    def index(self, alias: str, children: list[dict]) -> None:
        """An image published as an index, with a child per platform."""
        manifests = []
        for child in children:
            platform = child["platform"]
            present = child.get("present", True)
            digest, size = self._image(alias, platform=platform, content=present, present=present)
            os_name, _, architecture = platform.partition("/")
            manifests.append(
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": digest,
                    "size": size,
                    "platform": {"architecture": architecture, "os": os_name},
                }
            )
        digest, size = self._blob(
            json.dumps(
                {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests}
            ).encode()
        )
        self._name(alias, digest, size, OCI_INDEX)

    def write(self, path: Path) -> None:
        (self.work / "index.json").write_bytes(
            json.dumps(
                {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": self.entries}
            ).encode()
        )
        with tarfile.open(path, "w") as archive:
            for member in sorted(self.work.rglob("*")):
                archive.add(member, arcname=member.relative_to(self.work).as_posix())


def archive_dir(tmp_path: Path, build) -> Path:
    """A directory verify-bundle-images.sh can be pointed at.

    It needs three things and no more: the archive, the lock that says what the
    archive is supposed to hold, and the two scripts, because the verifier calls
    bundle-image-manifests.sh by a relative path after chdir.
    """
    root = tmp_path / "bundle"
    (root / "scripts").mkdir(parents=True)
    for script in ("verify-bundle-images.sh", "bundle-image-manifests.sh"):
        (root / "scripts" / script).write_bytes((REPO / "scripts" / script).read_bytes())

    builder = Archive(tmp_path / "archive")
    build(builder)
    builder.write(root / "images.tar")
    (root / "images.bundle.lock").write_bytes(
        "".join(
            f"{alias} aow-bundle/{alias}@{digest}\n" for alias, digest in builder.digests.items()
        ).encode()
    )
    return root


def verify(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "scripts/verify-bundle-images.sh", "."],
        cwd=root,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# --------------------------------------------------------- the completeness gate --


def test_a_complete_single_platform_archive_passes(tmp_path: Path) -> None:
    result = verify(archive_dir(tmp_path, lambda a: a.image("services")))
    assert result.returncode == 0, output(result)
    assert "images.tar is complete" in result.stdout


def test_manifest_blob_with_no_config_or_layers_fails(tmp_path: Path) -> None:
    """The F10 defect itself, and the shape every other check passed.

    `docker save` on a containerd image store wrote the manifest for a
    platform-less registry image and stopped. The message has to name the alias
    and how much is missing, because the install failure it replaces
    ("failed to read config content") names neither.
    """
    root = archive_dir(tmp_path, lambda a: a.image("services", content=False))
    result = verify(root)
    assert result.returncode == 1
    assert "images.tar cannot load services as linux/amd64" in output(result)
    # One config plus two layers, none of them in the archive.
    assert "3 of 3 blobs (its config and layers) are not in the archive" in output(result)


def test_an_image_built_for_the_wrong_architecture_fails(tmp_path: Path) -> None:
    """Complete is not sufficient: it also has to be this release's platform.

    An arm64 archive loads without complaint and then fails at container start,
    which is the same class of late failure as the empty one.
    """
    root = archive_dir(tmp_path, lambda a: a.image("services", platform="linux/arm64"))
    result = verify(root)
    assert result.returncode == 1
    assert "complete, but it is linux/arm64" in output(result)


def test_a_multi_platform_index_with_only_the_target_present_passes(tmp_path: Path) -> None:
    """The property that must not regress.

    `docker save` writes the upstream index listing every platform and stores
    only the one it exported, so absent arm64/s390x children are expected and
    legal -- docs/RELEASE-PROOF.md section 1 says so, and every upstream image
    in a real bundle has this shape. A gate that required every blob an index
    mentions would fail every release. The attestation child is here for the
    same reason: buildx writes one, it declares unknown/unknown, and it is not
    something to load.
    """
    root = archive_dir(
        tmp_path,
        lambda a: a.index(
            "postgres",
            [
                {"platform": "linux/amd64"},
                {"platform": "unknown/unknown"},
                {"platform": "linux/arm64", "present": False},
                {"platform": "linux/s390x", "present": False},
            ],
        ),
    )
    result = verify(root)
    assert result.returncode == 0, output(result)
    assert "images.tar is complete" in result.stdout


def test_a_multi_platform_index_missing_the_target_fails(tmp_path: Path) -> None:
    """The other half: absent children are legal, but not all of them.

    An arm64-only export of a multi-platform image is a complete archive of the
    wrong thing.
    """
    root = archive_dir(
        tmp_path,
        lambda a: a.index(
            "postgres",
            [
                {"platform": "linux/amd64", "present": False},
                {"platform": "linux/arm64"},
            ],
        ),
    )
    result = verify(root)
    assert result.returncode == 1
    assert "images.tar cannot load postgres as linux/amd64" in output(result)
    assert "complete, but it is linux/arm64" in output(result)


def test_an_index_whose_children_are_all_absent_fails(tmp_path: Path) -> None:
    """The empty archive again, in its index-shaped form.

    Both application images are published as an index now, so this is the shape
    the original defect would take if it came back. Until this test the gate
    passed it: candidates were collected only from children whose blob was
    present, and an alias with no candidates fell out of the per-alias loop
    without ever being judged.
    """
    root = archive_dir(
        tmp_path,
        lambda a: a.index(
            "services",
            [
                {"platform": "linux/amd64", "present": False},
                {"platform": "linux/arm64", "present": False},
            ],
        ),
    )
    result = verify(root)
    assert result.returncode == 1
    assert "images.tar cannot load services as linux/amd64" in output(result)
    assert "no manifest whose blobs are in the archive" in output(result)


def test_the_gate_checks_that_blobs_are_present_not_that_they_are_intact(tmp_path: Path) -> None:
    """Documented behaviour, not an aspiration.

    The completeness gate asks whether each config and layer blob is in the
    archive; it does not re-hash them. Hashing them would mean hashing the whole
    1.9 GB bundle a second time, and two other things already cover it: the
    archive as a whole is in SHA256SUMS, and `docker load` refuses a blob whose
    bytes do not match the digest its manifest names, which is why
    install-offline.sh reads that command's output rather than its exit status.

    This test exists so nobody reads the gate's success line as a content check.
    """
    root = archive_dir(tmp_path, lambda a: a.image("services", corrupt_layer=True))
    result = verify(root)
    assert result.returncode == 0, output(result)
    assert "images.tar is complete" in result.stdout


def test_the_target_platform_is_configurable(tmp_path: Path) -> None:
    """AOW_BUNDLE_PLATFORM is what an arm64 release would set, so it has to work."""
    root = archive_dir(tmp_path, lambda a: a.image("services", platform="linux/arm64"))
    assert verify(root, AOW_BUNDLE_PLATFORM="linux/arm64").returncode == 0


# ----------------------------------------------------- the installer's census --

STUB_DOCKER = """#!/bin/sh
# Stand-in for the Docker CLI, so the installer's post-load logic and its
# upgrade branch can be tested without a daemon. It answers only what
# install-offline.sh asks, and it is strict where being permissive would hide
# the mistakes these tests exist to catch.

# What `pg_dump --clean --if-exists` ends with. The installer looks for that
# closing marker rather than for a non-empty file, because pg_dump streams and a
# truncated dump is not empty.
DEFAULT_DUMP='-- PostgreSQL database dump
DROP TABLE IF EXISTS itineraries;
CREATE TABLE itineraries (id text);
--
-- PostgreSQL database dump complete
--
'

case "$1" in
  info) echo amd64 ;;
  ps)
    # The installer's one query: the running postgres of this project. Empty
    # unless a test is exercising an upgrade.
    if [ -n "${AOW_STUB_RUNNING_PG:-}" ]; then echo "$AOW_STUB_RUNNING_PG"; fi
    ;;
  volume)
    # `docker volume ls -q --filter name=^<project>_pgdata$`: a database this
    # host already holds. Empty unless a test says otherwise.
    if [ -n "${AOW_STUB_PGDATA_VOLUME:-}" ]; then echo "$AOW_STUB_PGDATA_VOLUME"; fi
    ;;
  exec)
    # Two forms, both against the previous release's postgres container:
    # `printenv <KEY>` for the credential guard, and `sh -c '<pg_dump ...>'`
    # for the pre-upgrade dump itself.
    shift            # exec
    shift            # the container id
    case "$1" in
      printenv)
        # The defaults agree with the release fixture's .env, so an upgrade
        # passes the guard unless a test changes one of them.
        case "$2" in
          POSTGRES_USER)     printf '%s\\n' "${AOW_STUB_PG_USER-aow}" ;;
          POSTGRES_DB)       printf '%s\\n' "${AOW_STUB_PG_DB-aow}" ;;
          POSTGRES_PASSWORD) printf '%s\\n' "${AOW_STUB_PG_PASSWORD-placeholder}" ;;
        esac
        ;;
      *)
        printf '%s' "${AOW_STUB_DUMP-$DEFAULT_DUMP}"
        ;;
    esac
    ;;
  compose)
    # The prerequisite check for the plugin itself. It reads no Compose file and
    # resolves no image, so it is the one call with no overlay to carry.
    if [ "$2" = version ]; then echo "Docker Compose version v2.0.0-stub"; exit 0; fi
    # Not a no-op. Answering 0 to anything is how a missing overlay -- the one
    # edit that turns an offline install into a pull or a build -- would go
    # unnoticed by every test in this file.
    case " $* " in
      *" -f compose.bundle.yml "*) ;;
      *) echo "stub docker: compose called without -f compose.bundle.yml: $*" >&2; exit 64 ;;
    esac
    test -f compose.bundle.yml \
      || { echo "stub docker: compose.bundle.yml is not in this release folder" >&2; exit 64; }
    if [ -n "${AOW_STUB_COMPOSE_LOG:-}" ]; then
      shift
      echo "$*" >> "$AOW_STUB_COMPOSE_LOG"
    fi
    ;;
  image)
    # docker image inspect <ref>: present only if the test said so.
    shift 2
    for ref in $AOW_STUB_PRESENT; do
      [ "$ref" = "$1" ] && exit 0
    done
    exit 1
    ;;
  load)
    if [ -n "${AOW_STUB_LOAD_MARKER:-}" ]; then
      echo called > "$AOW_STUB_LOAD_MARKER"
    fi
    # `docker load` prints this line whether it unpacked the archive's bytes or
    # found the content already in the store. That is the whole problem, so the
    # stub is deliberately just as uninformative as the real thing.
    while read -r alias _rest; do
      [ -n "$alias" ] || continue
      echo "Loaded image: aow-bundle/$alias:$AOW_STUB_VERSION"
    done < "$AOW_STUB_LOCK"
    ;;
esac
exit 0
"""

INSTALL_SCRIPTS = (
    "install-offline.sh",
    "verify-bundle.sh",
    "verify-bundle-images.sh",
    "bundle-image-manifests.sh",
    "release-smoke.py",
)
INSTALL_ALIASES = ("services", "ui", "postgres", "demos")


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A minimal offline release folder that install-offline.sh will accept.

    Smaller than the real one by four orders of magnitude and the same shape:
    the installer runs the whole verification chain before it loads anything, so
    every lock file it cross-checks has to be here and has to agree.
    """
    builder = Archive(tmp_path / "archive")
    for alias in INSTALL_ALIASES:
        builder.image(alias)

    root = tmp_path / "release"
    (root / "scripts").mkdir(parents=True)
    for script in INSTALL_SCRIPTS:
        (root / "scripts" / script).write_bytes((REPO / "scripts" / script).read_bytes())
    builder.write(root / "images.tar")

    digests = builder.digests
    (root / "ci-images.lock").write_bytes(
        (
            f"commit {COMMIT}\n"
            f"services {GHCR}/services@{digests['services']}\n"
            f"ui {GHCR}/ui@{digests['ui']}\n"
        ).encode()
    )
    (root / "IMAGES.lock").write_bytes(f"postgres@{digests['postgres']}\n".encode())
    (root / "images.bundle.lock").write_bytes(
        (
            f"services {GHCR}/services@{digests['services']}\n"
            f"ui {GHCR}/ui@{digests['ui']}\n"
            f"postgres postgres:17-alpine@{digests['postgres']}\n"
            f"demos aow-bundle/demos@{digests['demos']}\n"
        ).encode()
    )
    (root / "release-version.txt").write_bytes(f"{COMMIT}\n".encode())
    (root / "compose.yml").write_bytes(b"name: aow\nservices: {}\n")
    # Shaped like the real overlay, because the installer refuses a release
    # folder without one and the stub Docker refuses a compose call that does
    # not pass it.
    (root / "compose.bundle.yml").write_bytes(
        b"services:\n  api:\n    image: aow-bundle/services:${AOW_IMAGE_VERSION:?set AOW_IMAGE_VERSION}\n"
    )
    (root / "models").mkdir()
    model = root / "models" / "model.gguf"
    model.write_bytes(b"pretend this is 1.1 GB of model weights")
    (root / "models.lock").write_bytes(
        f"{hashlib.sha256(model.read_bytes()).hexdigest()} *models/model.gguf\n".encode()
    )

    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        name = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{name}\n")
    (root / "SHA256SUMS").write_bytes("".join(lines).encode())
    # Written after the checksums on purpose: the operator creates it, and
    # verify-bundle.sh excludes it from both the sums and the unlisted check.
    (root / ".env").write_bytes(b"POSTGRES_PASSWORD=placeholder\n")
    return root


def install(release: Path, tmp_path: Path, present: tuple[str, ...] = (), **env: str):
    """Run the real installer with the stub CLI as the only `docker` on PATH."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_bytes(STUB_DOCKER.encode())
    stub.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AOW_STUB_PRESENT": " ".join(present),
        "AOW_STUB_VERSION": COMMIT,
        "AOW_STUB_LOCK": str(release / "images.bundle.lock"),
        # Belt and braces. If the stub were somehow not the `docker` that runs,
        # this one cannot reach a daemon, so the worst case is a failed test
        # rather than a real stack being touched.
        "DOCKER_HOST": "tcp://127.0.0.1:1",
        **env,
    }
    resolved = subprocess.run(
        [BASH, "-c", "command -v docker"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=environment,
        check=False,
    )
    if stub.name not in resolved.stdout or "stub" not in resolved.stdout:
        pytest.skip(f"the stub docker is not first on PATH here: {resolved.stdout.strip()!r}")

    return subprocess.run(
        [BASH, "scripts/install-offline.sh"],
        cwd=release,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=environment,
        check=False,
    )


def test_an_install_on_an_empty_store_says_the_archive_supplied_everything(
    release: Path, tmp_path: Path
) -> None:
    result = install(release, tmp_path)
    assert result.returncode == 0, output(result)
    assert "held none of the 4 release tags" in result.stdout
    assert "archive verification found their config and layers" in result.stdout
    assert f"Release {COMMIT} is up" in result.stdout
    # The closing line may not claim more than the run established. The smoke
    # test runs inside the api container and never reaches a published host
    # port, so the ports are reported as configuration, not as a result.
    assert "serving stored forecasts and scores from inside the stack" in result.stdout
    assert "Published on 127.0.0.1:8080 (UI) and 127.0.0.1:8000 (API)" in result.stdout
    assert "is serving on ports" not in result.stdout


def test_an_install_over_images_the_engine_already_had_says_so(
    release: Path, tmp_path: Path
) -> None:
    """The install that looked like a proof and was not.

    Every release drill before the F10 one ran on the machine that packaged the
    bundle, where these tags already existed. `docker load` said "Loaded image"
    for all of them and exited 0 from an archive that held almost nothing. The
    install still succeeds here -- refusing would break every legitimate
    re-install and upgrade -- but it can no longer be read as evidence that the
    archive is self-contained.
    """
    present = tuple(f"aow-bundle/{alias}:{COMMIT}" for alias in INSTALL_ALIASES)
    result = install(release, tmp_path, present=present)
    assert result.returncode == 0, output(result)
    assert "4 of 4 release tags were already in this engine" in result.stdout
    assert "does not show that images.tar is self-contained" in result.stdout
    assert "held none of" not in result.stdout


def test_one_image_already_present_is_still_reported(release: Path, tmp_path: Path) -> None:
    result = install(release, tmp_path, present=(f"aow-bundle/postgres:{COMMIT}",))
    assert result.returncode == 0, output(result)
    assert "1 of 4 release tags were already in this engine" in result.stdout
    assert "postgres" in result.stdout


def test_a_clean_store_can_be_demanded(release: Path, tmp_path: Path) -> None:
    """Report by default, gate on request.

    Nothing in CI can set this today: the only automated bundle install runs on
    the runner that just pulled and tagged every image in order to save them, so
    its store is populated by construction. It is here for the install that
    would prove something -- a second, empty engine -- so that drill is a flag
    rather than a reading of the log.
    """
    strict = {"AOW_REQUIRE_CLEAN_IMAGE_STORE": "1"}
    assert install(release, tmp_path, **strict).returncode == 0

    present = (f"aow-bundle/ui:{COMMIT}",)
    marker = tmp_path / "load-called"
    result = install(
        release,
        tmp_path,
        present=present,
        AOW_STUB_LOAD_MARKER=str(marker),
        **strict,
    )
    assert result.returncode == 1
    assert "AOW_REQUIRE_CLEAN_IMAGE_STORE" in result.stderr
    assert "ui" in output(result)
    assert not marker.exists(), "strict mode must refuse before docker load"


# ------------------------------------------------------- the upgrade branch --
#
# Everything below covers the half of the installer that CI cannot reach. The
# release workflow's install runs under a unique COMPOSE_PROJECT_NAME on an
# empty engine, so there is never a prior database and this branch has never
# executed there. It is also the branch that holds the only thing standing
# between a failed upgrade and an irreversible migration: the pre-upgrade dump.

UPGRADE = {"AOW_STUB_RUNNING_PG": "pg-of-the-previous-release"}


def dumps(release: Path) -> list[Path]:
    return sorted((release / "backup").glob("*.sql"))


def test_an_upgrade_dumps_the_running_database_first(release: Path, tmp_path: Path) -> None:
    result = install(release, tmp_path, **UPGRADE)
    assert result.returncode == 0, output(result)
    assert "upgrading a running installation" in result.stdout
    written = dumps(release)
    assert len(written) == 1, f"expected exactly one dump, got {written}"
    assert "PostgreSQL database dump complete" in written[0].read_text()


def test_an_unfinished_dump_stops_the_install_before_any_image_is_loaded(
    release: Path, tmp_path: Path
) -> None:
    """pg_dump streams, so a truncated dump is not an empty one.

    The old check was `test -s`, which a half-written dump passes -- and that
    file is the whole rollback path for a migration an image rollback cannot
    undo. Both the truncated and the empty case are refused here.
    """
    for label, body in (
        ("truncated", "-- PostgreSQL database dump\nDROP TABLE x;\n"),
        ("empty", ""),
    ):
        marker = tmp_path / f"load-called-{label}"
        result = install(
            release, tmp_path, AOW_STUB_DUMP=body, AOW_STUB_LOAD_MARKER=str(marker), **UPGRADE
        )
        assert result.returncode == 1, f"{label}: {output(result)}"
        assert "did not finish" in result.stderr, f"{label}: {output(result)}"
        assert not marker.exists(), f"{label}: nothing may be loaded after a bad dump"


def test_an_upgrade_with_regenerated_passwords_is_refused(release: Path, tmp_path: Path) -> None:
    """Confirmed on a live engine: `migrate exited 2`, "password authentication
    failed for user aow".

    Postgres fixes the superuser password inside pgdata when the volume is
    initialised and nothing updates it afterwards, so a new release folder with
    freshly generated passwords cannot authenticate against an existing
    database. Without this guard the installer took the dump, loaded the whole
    archive, and only then died in migrate.
    """
    marker = tmp_path / "load-called"
    result = install(
        release,
        tmp_path,
        AOW_STUB_PG_PASSWORD="what-the-volume-was-initialised-with",
        AOW_STUB_LOAD_MARKER=str(marker),
        **UPGRADE,
    )
    assert result.returncode == 1
    assert "POSTGRES_PASSWORD in .env does not match" in result.stderr
    assert "Reuse the previous release's .env verbatim" in result.stderr
    assert not marker.exists(), "the guard must refuse before docker load"
    assert not dumps(release), "nothing should be dumped once the credentials are known to be wrong"


def test_a_differing_user_or_database_is_refused_by_name(release: Path, tmp_path: Path) -> None:
    """The same trap, for the other two values that live in the volume. The
    writer and reader passwords are re-applied by 001_init.sql on every boot;
    these three are not, which is why only they are checked."""
    for key, stub in (("POSTGRES_USER", "AOW_STUB_PG_USER"), ("POSTGRES_DB", "AOW_STUB_PG_DB")):
        result = install(release, tmp_path, **{stub: "not-aow"}, **UPGRADE)
        assert result.returncode == 1, output(result)
        assert f"{key} in .env does not match" in result.stderr


def test_an_upgrade_stops_the_previous_readers_before_migrating(
    release: Path, tmp_path: Path
) -> None:
    """Every boot re-applies the migrations, and three of them ALTER TABLE --
    which takes ACCESS EXCLUSIVE before discovering the change is already made.
    One in-flight SELECT from the previous release's api, agent or enricher makes
    migrate queue behind it, and a queued exclusive request blocks every reader
    after it. `up -d` waits on migrate, so the install stalls."""
    log = tmp_path / "compose-calls"
    result = install(release, tmp_path, AOW_STUB_COMPOSE_LOG=str(log), **UPGRADE)
    assert result.returncode == 0, output(result)

    calls = log.read_text().splitlines()
    stopped = next((c for c in calls if " stop " in f" {c} "), None)
    assert stopped, f"no `stop` before the upgrade started the new release: {calls}"
    for service in ("ingestor", "enricher", "api", "agent", "ui"):
        assert service in stopped, f"{service} is left running through the migrations"
    assert calls.index(stopped) < next(i for i, c in enumerate(calls) if " up " in f" {c} ")


def test_a_first_install_neither_dumps_nor_stops_anything(release: Path, tmp_path: Path) -> None:
    """The other side of the branch: no prior database, so no dump to take and
    nothing to stop. Guards against the upgrade path firing on a clean host."""
    log = tmp_path / "compose-calls"
    result = install(release, tmp_path, AOW_STUB_COMPOSE_LOG=str(log))
    assert result.returncode == 0, output(result)
    assert "upgrading a running installation" not in result.stdout
    assert not (release / "backup").exists()
    assert not any(" stop " in f" {c} " for c in log.read_text().splitlines())


def test_a_stopped_previous_release_is_refused_rather_than_silently_migrated(
    release: Path, tmp_path: Path
) -> None:
    """The gap that made the dump optional in practice.

    An operator who runs `make down` before upgrading -- the natural thing to do
    -- leaves the pgdata volume in place with no container running. Asking
    `docker ps` alone found nothing, took no dump, and applied the migrations
    anyway, so the upgrade became irreversible with nothing saying so.
    """
    marker = tmp_path / "load-called"
    result = install(
        release, tmp_path, AOW_STUB_PGDATA_VOLUME="aow_pgdata", AOW_STUB_LOAD_MARKER=str(marker)
    )
    assert result.returncode == 1
    assert "aow_pgdata" in result.stderr
    assert "AOW_SKIP_PREUPGRADE_DUMP=1" in result.stderr
    assert not marker.exists(), "nothing may be loaded over a database that was not dumped"


# ------------------------------------------------------- the bind address --
#
# The closing report names the addresses the stack is published on. That is the
# one line in this script describing a security property, over an API whose
# write routes have no authentication, so it is the one line that must not be
# able to be wrong. compose.yml publishes on ${AOW_BIND_ADDR:-127.0.0.1} and
# compose.bundle.yml does not touch edge's ports, so the value has to be read
# from the same two places Compose reads it. These mirror the two tests
# scripts/bootstrap.sh has for the same report.


def test_a_widened_bind_address_in_the_env_file_is_reported_as_it_is(
    release: Path, tmp_path: Path
) -> None:
    """The regression this replaced: a hardcoded 127.0.0.1 in the closing line.

    .env is the documented way to move the boundary, and Compose reads it
    through --env-file, so a release whose .env says 0.0.0.0 must not be
    reported as loopback.
    """
    (release / ".env").write_bytes(b"POSTGRES_PASSWORD=placeholder\nAOW_BIND_ADDR=0.0.0.0\n")
    result = install(release, tmp_path)
    assert result.returncode == 0, output(result)
    assert "Published on 0.0.0.0:8080 (UI) and 0.0.0.0:8000 (API)" in result.stdout
    assert "127.0.0.1" not in result.stdout
    assert "WARNING: 0.0.0.0 is not loopback" in result.stdout
    assert "no authentication" in result.stdout


def test_the_shell_overrides_the_env_file_for_the_bind_address(
    release: Path, tmp_path: Path
) -> None:
    """Compose takes the shell environment ahead of --env-file, so the report
    has to as well -- otherwise it names an address nothing is listening on."""
    (release / ".env").write_bytes(b"POSTGRES_PASSWORD=placeholder\nAOW_BIND_ADDR=0.0.0.0\n")
    result = install(release, tmp_path, AOW_BIND_ADDR="192.168.1.10")
    assert result.returncode == 0, output(result)
    assert "Published on 192.168.1.10:8080 (UI) and 192.168.1.10:8000 (API)" in result.stdout
    assert "0.0.0.0" not in result.stdout
    assert "WARNING: 192.168.1.10 is not loopback" in result.stdout


def test_the_default_is_loopback_and_carries_no_warning(release: Path, tmp_path: Path) -> None:
    """Neither place sets it, so compose.yml's own default is what applies. A
    warning here would train an operator to ignore the one that matters."""
    result = install(release, tmp_path)
    assert result.returncode == 0, output(result)
    assert "Published on 127.0.0.1:8080 (UI) and 127.0.0.1:8000 (API)" in result.stdout
    assert "WARNING" not in result.stdout


def test_installing_over_a_stopped_database_without_a_dump_has_to_be_asked_for(
    release: Path, tmp_path: Path
) -> None:
    """And when it is asked for, the transcript records it. A skipped check and
    a passed check must not read identically once only the log is left."""
    result = install(
        release,
        tmp_path,
        AOW_STUB_PGDATA_VOLUME="aow_pgdata",
        AOW_SKIP_PREUPGRADE_DUMP="1",
    )
    assert result.returncode == 0, output(result)
    assert "NO pre-upgrade dump" in result.stdout
    assert "deliberately" in result.stdout
