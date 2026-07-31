"""Exercises the actual pre-commit framework + git hook mechanics -- not
mocked at the CLI level. A fake `review-pipeline` shim on PATH stands in for
the real (LLM-backed) command so this runs without a live API key, but
`pre-commit install`, the generated .git/hooks/pre-commit, and git's own
--no-verify handling are all real.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import git

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_fake_review_pipeline(bin_dir: Path, behavior: str) -> None:
    """behavior: 'block' (exit 1, as if blocking findings remain) or
    'pass' (exit 0, as if review-fix cleared)."""
    exit_code = 1 if behavior == "block" else 0
    shim = bin_dir / "review-pipeline"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"echo 'fake review-pipeline invoked with: ' \"$@\" >&2\n"
        f"exit {exit_code}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _install_precommit_hook(repo: Path):
    (repo / ".pre-commit-config.yaml").write_text(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    )
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    hook_script = REPO_ROOT / "scripts" / "pre-commit-review.sh"
    dest = scripts_dir / "pre-commit-review.sh"
    dest.write_text(hook_script.read_text())
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    git("add", ".pre-commit-config.yaml", "scripts/pre-commit-review.sh", cwd=repo)
    git("commit", "-q", "-m", "add pre-commit config", cwd=repo)

    pre_commit_bin = REPO_ROOT / ".venv" / "bin" / "pre-commit"
    subprocess.run(
        [str(pre_commit_bin), "install"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _commit_count(repo: Path) -> int:
    return int(git("rev-list", "--count", "HEAD", cwd=repo).strip())


@pytest.fixture
def hooked_repo(scratch_repo, tmp_path):
    _install_precommit_hook(scratch_repo)
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    return scratch_repo, fake_bin


def _env_with_fake_bin(fake_bin: Path, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    if extra:
        env.update(extra)
    return env


def test_hook_blocks_commit_when_review_fix_fails(hooked_repo):
    repo, fake_bin = hooked_repo
    _make_fake_review_pipeline(fake_bin, "block")

    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git("add", "calc.py", cwd=repo)

    before = _commit_count(repo)
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "buggy change"],
        cwd=repo,
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert _commit_count(repo) == before  # commit did not happen


def test_hook_allows_commit_when_review_fix_passes(hooked_repo):
    repo, fake_bin = hooked_repo
    _make_fake_review_pipeline(fake_bin, "pass")

    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    git("add", "calc.py", cwd=repo)

    before = _commit_count(repo)
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "clean change"],
        cwd=repo,
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _commit_count(repo) == before + 1


def test_skip_review_env_var_bypasses_with_warning(hooked_repo):
    # pre-commit only surfaces a hook's own stdout/stderr on failure (or in
    # -v mode) -- it prints just a "Passed" summary line otherwise. So we
    # check the git-commit-goes-through behavior via the real hook here...
    repo, fake_bin = hooked_repo
    _make_fake_review_pipeline(fake_bin, "block")  # would fail if actually run

    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git("add", "calc.py", cwd=repo)

    before = _commit_count(repo)
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "buggy change, skipped review"],
        cwd=repo,
        env=_env_with_fake_bin(fake_bin, {"SKIP_REVIEW": "1"}),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _commit_count(repo) == before + 1


def test_skip_review_script_prints_warning_to_stderr(hooked_repo):
    # ...and check the warning text by invoking the wrapper script directly,
    # bypassing pre-commit's output suppression.
    repo, fake_bin = hooked_repo
    _make_fake_review_pipeline(fake_bin, "block")

    result = subprocess.run(
        [str(repo / "scripts" / "pre-commit-review.sh")],
        cwd=repo,
        env=_env_with_fake_bin(fake_bin, {"SKIP_REVIEW": "1"}),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP_REVIEW=1" in result.stderr
    assert "WARNING" in result.stderr
    assert "fake review-pipeline invoked" not in result.stderr  # never called review-pipeline at all


def test_no_verify_bypasses_silently(hooked_repo):
    repo, fake_bin = hooked_repo
    _make_fake_review_pipeline(fake_bin, "block")  # would fail if actually run

    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git("add", "calc.py", cwd=repo)

    before = _commit_count(repo)
    result = subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "buggy change, --no-verify"],
        cwd=repo,
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _commit_count(repo) == before + 1
    # the hook never ran at all -- no output from either the shim or our script
    assert "fake review-pipeline invoked" not in result.stderr
    assert "WARNING" not in result.stderr
