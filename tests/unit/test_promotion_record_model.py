"""A promotion record must name the verified model, not a lock-file comment."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cd-promotion-record.py"
read_model_lock = runpy.run_path(str(SCRIPT))["read_model_lock"]
write_record = runpy.run_path(str(SCRIPT))["main"]


def test_model_lock_comment_is_ignored(tmp_path):
    lock = tmp_path / "models.lock"
    lock.write_text("# how to verify\n" + "a" * 64 + " *models/Qwen3-1.7B-Q4_K_M.gguf\n")
    digest, name = read_model_lock(lock)
    assert digest == "a" * 64
    assert name == "models/Qwen3-1.7B-Q4_K_M.gguf"


@pytest.mark.parametrize("extra", ["not a checksum", ""])
def test_model_lock_must_have_exactly_one_valid_entry(tmp_path, extra):
    lock = tmp_path / "models.lock"
    lock.write_text("# heading\n" + extra)
    with pytest.raises(ValueError):
        read_model_lock(lock)


def test_record_model_matches_verified_bundle_checksum(tmp_path, monkeypatch):
    digest = "a" * 64
    model = "models/probe.gguf"
    image = "example@sha256:" + "b" * 64
    files = {
        "release.lock": f"services {image}\nui {image}\n",
        "bundle.lock": f"services {image}\nui {image}\n",
        "SHA256SUMS": f"{digest}  ./{model}\n",
        "models.lock": f"# verification instructions\n{digest} *{model}\n",
        "branch.json": '{"verified": false, "reason": "test"}',
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    env = {
        "RELEASE_SHA": "c" * 40,
        "CI_RUN_ID": "123",
        "CI_RUN_URL": "https://example.test/ci/123",
        "RELEASE_LOCK": str(tmp_path / "release.lock"),
        "BUNDLE_LOCK": str(tmp_path / "bundle.lock"),
        "BUNDLE_CHECKSUMS": str(tmp_path / "SHA256SUMS"),
        "MODELS_LOCK": str(tmp_path / "models.lock"),
        "WORKFLOW_RUN_URL": "https://example.test/release/456",
        "BRANCH_PROTECTION_FILE": str(tmp_path / "branch.json"),
        "OUTPUT": str(tmp_path / "promotion-record.json"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SUMMARY_OUTPUT", raising=False)

    assert write_record() == 0
    record = json.loads((tmp_path / "promotion-record.json").read_text())
    assert record["model"] == {"file": model, "sha256": digest}

    (tmp_path / "SHA256SUMS").write_text(f"{'d' * 64}  ./{model}\n")
    with pytest.raises(SystemExit, match="model checksum differs"):
        write_record()
