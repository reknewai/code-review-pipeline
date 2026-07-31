import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def dummy_api_key(monkeypatch):
    """cli.py checks for ANTHROPIC_API_KEY before making a call; tests mock
    the call itself, so a real key is never needed, just a truthy env var."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")


def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def scratch_repo(tmp_path):
    """A throwaway git repo with one committed file, ready to have a buggy
    change staged on top of it."""
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("checkout", "-q", "-b", "main", cwd=repo)

    (repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
    )
    _git("add", "calc.py", cwd=repo)
    _git("commit", "-q", "-m", "initial commit", cwd=repo)
    return repo


def git(*args, cwd):
    return _git(*args, cwd=cwd)
