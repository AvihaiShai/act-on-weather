"""Write the promotion record for one release, into the bundle it describes.

By the time this script runs, every gate in ``.github/workflows/release.yml``
has already passed -- the job runs under ``set -euo pipefail`` with no
``continue-on-error``, so a failed gate stops the job before this step. This
script does not re-check anything; it collects what already happened into one
machine-readable file and one human-readable summary ($GITHUB_STEP_SUMMARY),
so a reviewer does not have to reconstruct the release from scattered log
lines, and so does an operator standing at the offline host, where the log is
gone but the bundle is not.

The record is written to ``<bundle dir>/promotion-record.json`` (see the
OUTPUT variable below), inside the same folder scripts/package-offline.sh built. The
workflow reseals SHA256SUMS after this runs, so the record is covered by the
same one-command checksum verification as everything else in the bundle --
see the "Reseal SHA256SUMS" step in release.yml.

Deliberately stdlib-only, like scripts/stage_model.py: this runs in a CI step
that already has Python, and nothing here is worth a dependency.

Inputs are environment variables (the workflow sets them; see release.yml).
Required:
  RELEASE_SHA           the 40-hex commit released
  CI_RUN_ID             the ci.yml run id that built and scanned this commit
  CI_RUN_URL            its html_url
  RELEASE_LOCK          path to the downloaded release/images.lock (what CI
                        published; services/ui only)
  BUNDLE_LOCK           path to <bundle dir>/images.bundle.lock (every image
                        alias actually docker-save'd into images.tar)
  BUNDLE_CHECKSUMS      path to <bundle dir>/SHA256SUMS, as
                        scripts/package-offline.sh sealed it (before this
                        script's own output is added and it is resealed)
  MODELS_LOCK           path to models.lock
  WORKFLOW_RUN_URL      this release workflow run's own html_url
  BRANCH_PROTECTION_FILE
                        path to the JSON the workflow's live branch-protection
                        read step wrote -- either
                        {"verified": true, "required_status_checks": [...],
                        "enforce_admins": bool} or, if that read failed (as it
                        will with a plain GITHUB_TOKEN -- see release.yml),
                        {"verified": false, "reason": "...", "http_status":
                        "..."}. This script does not guess a fallback value:
                        a promotion record that could not confirm branch
                        protection says so, rather than asserting a constant
                        that might be stale.
  OUTPUT                where to write the JSON record
Optional:
  RELEASE_TAG           an operator-supplied label, recorded verbatim, not
                        verified against any git tag (see docs/RELEASE.md)
  SUMMARY_OUTPUT        path to append a Markdown rendering to (typically
                        $GITHUB_STEP_SUMMARY); skipped if unset
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# The gates this workflow enforces, in the order it enforces them. Recorded as
# a fixed list, not a live re-check -- see the module docstring for why that
# is sound here.
GATES = [
    "sha_format_valid",
    "checkout_matches_sha",
    "ci_run_found_for_push_to_main",
    "ci_run_success",
    "images_lock_commit_matches_sha",
    "services_digest_independently_reresolved",
    "ui_digest_independently_reresolved",
    "model_checksum_verified",
    "bundle_built_by_package_offline_sh",
    "bundle_verified_by_verify_bundle_images_sh",
    "bundle_installed_without_pulls",
    "release_smoke_serves_data",
]

# Not every image alias in the bundle carries the same proof. Three tiers,
# weakest fact first:
#
#   self_attested_build        demos is built on the staging machine (here,
#                               this workflow's runner) and never published to
#                               any registry. Its digest in images.bundle.lock
#                               comes from scripts/bundle-image-manifests.sh
#                               reading the archive this same job just wrote --
#                               it is the bundle attesting to itself. There is
#                               no external source to cross-check it against.
#
#   digest_pinned_pull_verified_by_docker
#                               postgres, rabbitmq, llm, edge, prometheus,
#                               grafana and stage are pulled
#                               straight from IMAGES.lock's pinned digests.
#                               Docker refuses a pull whose content does not
#                               hash to the requested digest, so this is a
#                               real integrity check -- just not one this
#                               workflow performs itself, and not a check
#                               against a moving tag the way services/ui are.
#
#   registry_reresolved_by_workflow
#                               services and ui were published by the CI run
#                               under a tag (sha-<commit>). This workflow
#                               re-queried that tag against the registry,
#                               independently of the CI artifact, and asserted
#                               the digest it resolves to right now still
#                               matches what the artifact recorded. This is
#                               the strongest tier: it is the only one with an
#                               external anchor outside this release's own
#                               paperwork (SHA256SUMS and images.bundle.lock
#                               are both self-attesting -- whoever swaps
#                               images.tar swaps them with it).
PROVENANCE_BY_ALIAS = {
    "services": "registry_reresolved_by_workflow",
    "ui": "registry_reresolved_by_workflow",
    "postgres": "digest_pinned_pull_verified_by_docker",
    "rabbitmq": "digest_pinned_pull_verified_by_docker",
    "llm": "digest_pinned_pull_verified_by_docker",
    "edge": "digest_pinned_pull_verified_by_docker",
    "prometheus": "digest_pinned_pull_verified_by_docker",
    "grafana": "digest_pinned_pull_verified_by_docker",
    "stage": "digest_pinned_pull_verified_by_docker",
    "demos": "self_attested_build",
}

# No hardcoded fallback here on purpose. branch_protection in the record
# comes entirely from BRANCH_PROTECTION_FILE, which the workflow's live read
# of `GET /repos/.../branches/main/protection` wrote. If that read failed --
# which it will with a plain GITHUB_TOKEN; `administration` is not one of the
# permission scopes GITHUB_TOKEN can be granted at all (confirmed against
# GitHub's own workflow-syntax schema, which enums the valid keys and forbids
# any other), so a repo admin PAT would be required to change this -- the
# record says `"verified": false` with the reason, and does not assert
# required_status_checks or enforce_admins. A promotion record that names a
# repository setting it did not confirm is worse than one that admits it
# could not confirm it.


def read_lock_pairs(path: Path) -> dict[str, str]:
    """Parse a `key value` lock file (images.lock / images.bundle.lock) into a dict."""
    pairs: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        pairs[key] = value.strip()
    return pairs


def read_checksums(path: Path) -> dict[str, str]:
    """Parse a `sha256sum`-format file into {relative path: digest}."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if not name:
            digest, _, name = line.partition(" ")
        out[name.lstrip("*")] = digest
    return out


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set")
    return value


def main() -> int:
    release_sha = require_env("RELEASE_SHA")
    ci_run_id = require_env("CI_RUN_ID")
    ci_run_url = require_env("CI_RUN_URL")
    release_lock_path = Path(require_env("RELEASE_LOCK"))
    bundle_lock_path = Path(require_env("BUNDLE_LOCK"))
    bundle_checksums_path = Path(require_env("BUNDLE_CHECKSUMS"))
    models_lock_path = Path(require_env("MODELS_LOCK"))
    workflow_run_url = require_env("WORKFLOW_RUN_URL")
    branch_protection_path = Path(require_env("BRANCH_PROTECTION_FILE"))
    output_path = Path(require_env("OUTPUT"))
    release_tag = os.environ.get("RELEASE_TAG", "").strip() or None
    summary_path = os.environ.get("SUMMARY_OUTPUT", "").strip()

    branch_protection_result = json.loads(branch_protection_path.read_text())
    if "verified" not in branch_protection_result:
        sys.exit(f"{branch_protection_path}: missing required 'verified' field")

    release_lock = read_lock_pairs(release_lock_path)
    bundle_lock = read_lock_pairs(bundle_lock_path)
    checksums = read_checksums(bundle_checksums_path)
    model_digest, _, model_name = models_lock_path.read_text().strip().partition(" ")
    model_name = model_name.strip().lstrip("*")

    unknown_aliases = sorted(set(bundle_lock) - set(PROVENANCE_BY_ALIAS))
    if unknown_aliases:
        sys.exit(
            "images.bundle.lock has alias(es) with no documented provenance tier: "
            + ", ".join(unknown_aliases)
            + " -- add them to PROVENANCE_BY_ALIAS in this script before releasing."
        )

    images = {
        alias: {"ref": ref, "provenance": PROVENANCE_BY_ALIAS[alias]}
        for alias, ref in bundle_lock.items()
    }
    # Sanity check, not a re-verification: the artifact's services/ui digests
    # (what CI published) should be the exact ones the bundle was built from.
    # The actual proof that they are trustworthy happened earlier, in the
    # workflow's registry re-resolution step.
    for component in ("services", "ui"):
        if release_lock.get(component) != bundle_lock.get(component):
            sys.exit(
                f"{component}: release/images.lock has {release_lock.get(component)}, "
                f"but the bundle was built from {bundle_lock.get(component)}"
            )

    record = {
        "schema_version": "1",
        "release_commit": release_sha,
        "release_tag": release_tag,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ci_run": {"id": ci_run_id, "url": ci_run_url},
        "release_workflow_run": {"url": workflow_run_url},
        "branch_protection": {
            "branch": "main",
            **branch_protection_result,
            "satisfied_by_ci_run": {"id": ci_run_id, "url": ci_run_url},
        },
        "gates_passed": GATES,
        "images": images,
        "model": {"file": model_name, "sha256": model_digest},
        "bundle_checksums": checksums,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"wrote {output_path}")

    if summary_path:
        if branch_protection_result.get("verified"):
            checks = branch_protection_result.get("required_status_checks", [])
            enforce_admins = branch_protection_result.get("enforce_admins")
            protection_line = (
                f"- Branch protection on main (live-verified): required checks "
                f"{', '.join(checks) or '(none)'}; enforce_admins={enforce_admins}"
            )
        else:
            protection_line = (
                "- Branch protection on main: **could not confirm** ("
                f"{branch_protection_result.get('reason', 'unknown')}, "
                f"HTTP {branch_protection_result.get('http_status', '?')})"
            )
        lines = [
            "## Release promotion record",
            "",
            f"- Commit: `{release_sha}`" + (f" (tag `{release_tag}`)" if release_tag else ""),
            f"- CI run: {ci_run_url}",
            f"- This release run: {workflow_run_url}",
            protection_line,
            f"- Services image: `{images['services']['ref']}` ({images['services']['provenance']})",
            f"- UI image: `{images['ui']['ref']}` ({images['ui']['provenance']})",
            f"- Model: `{model_name}` sha256 `{model_digest}`",
            f"- Bundle files checksummed: {len(checksums)}",
            "",
            "Image provenance (not all images have the same strength of proof):",
            *[f"- `{alias}`: {info['provenance']}" for alias, info in sorted(images.items())],
            "",
            "Gates passed:",
            *[f"- {gate}" for gate in GATES],
            "",
            "The bundle itself (images.tar, the model weights, the full "
            "release tree) is not attached to this workflow run -- see "
            "docs/RELEASE.md for why and for the manual transfer/install "
            "steps that follow this promotion record.",
            "",
        ]
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
