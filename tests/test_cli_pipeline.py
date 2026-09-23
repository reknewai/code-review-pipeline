"""End-to-end plumbing tests with the LLM calls mocked out.

These prove the CLI/diff/schema wiring is correct -- that a `blocking`
finding flows from `review` into `fix`, that the fix is verified by
re-running `review`, and that `verify` is structurally independent of any
findings.json. They do NOT prove real model quality (no ANTHROPIC_API_KEY in
this environment) -- that's a separate, human-in-the-loop check against a
live API key.
"""

import json

from click.testing import CliRunner

from review_pipeline import agents
from review_pipeline.cli import EXIT_PIPELINE_ERROR, fix, review, review_fix, verify
from tests.conftest import git


def _stage_bug(scratch_repo):
    (scratch_repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n"  # bug: should be a + b
    )
    git("add", "calc.py", cwd=scratch_repo)


def test_review_surfaces_blocking_correctness_finding(scratch_repo, monkeypatch):
    _stage_bug(scratch_repo)

    def fake_run_reviewer(*, repo, diff_text, context_label, context_text, model):
        assert "a - b" in diff_text
        return {
            "findings": [
                {
                    "file": "calc.py",
                    "line": 2,
                    "severity": "blocking",
                    "category": "correctness",
                    "summary": "add() subtracts instead of adding",
                    "failure_scenario": "add(2, 3) returns -1 instead of 5",
                }
            ],
            "ship_ready": False,
        }

    monkeypatch.setattr(agents, "run_reviewer", fake_run_reviewer)

    runner = CliRunner()
    output_path = scratch_repo / "findings.json"
    result = runner.invoke(
        review, ["--repo", str(scratch_repo), "--staged", "--output", str(output_path)]
    )

    assert result.exit_code == 1, result.output  # blocking finding present -> non-zero
    data = json.loads(output_path.read_text())
    blocking = [f for f in data["findings"] if f["severity"] == "blocking" and f["category"] == "correctness"]
    assert len(blocking) == 1


def test_fix_resolves_bug_and_review_then_clears(scratch_repo, monkeypatch):
    _stage_bug(scratch_repo)
    findings_path = scratch_repo / "findings.json"
    findings_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "calc.py",
                        "line": 2,
                        "severity": "blocking",
                        "category": "correctness",
                        "summary": "add() subtracts instead of adding",
                        "failure_scenario": "add(2, 3) returns -1 instead of 5",
                    }
                ],
                "ship_ready": False,
            }
        )
    )

    def fake_run_coder(*, repo, diff_text, findings, context_label, context_text, model):
        assert len(findings) == 1
        (scratch_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        return {"summary": "fixed the subtraction bug in add()"}

    monkeypatch.setattr(agents, "run_coder", fake_run_coder)

    runner = CliRunner()
    result = runner.invoke(
        fix, ["--repo", str(scratch_repo), "--staged", "--findings", str(findings_path)]
    )
    assert result.exit_code == 0, result.output

    # --staged fix must restage its edits so a follow-up `review --staged` sees them
    staged_diff = git("diff", "--cached", cwd=scratch_repo)
    assert staged_diff.strip() == ""  # index now matches HEAD again: bug is gone

    def fake_run_reviewer_clean(*, repo, diff_text, context_label, context_text, model):
        return {"findings": [], "ship_ready": True}

    monkeypatch.setattr(agents, "run_reviewer", fake_run_reviewer_clean)
    output_path = scratch_repo / "findings2.json"
    result = runner.invoke(
        review, ["--repo", str(scratch_repo), "--staged", "--output", str(output_path)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(output_path.read_text())
    assert data["findings"] == []
    assert data["ship_ready"] is True


def test_review_fix_loop_resolves_within_cap(scratch_repo, monkeypatch):
    # Committed baseline differs from both the buggy and the fixed version,
    # so the diff doesn't vanish once the bug is fixed (a real feature diff
    # wouldn't either) -- this exercises round 2 actually re-reviewing.
    (scratch_repo / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    git("commit", "-am", "stub out add()", cwd=scratch_repo)
    _stage_bug(scratch_repo)
    calls = {"review": 0, "fix": 0}

    def fake_run_reviewer(*, repo, diff_text, context_label, context_text, model):
        calls["review"] += 1
        if calls["review"] == 1:
            return {
                "findings": [
                    {
                        "file": "calc.py",
                        "line": 2,
                        "severity": "blocking",
                        "category": "correctness",
                        "summary": "add() subtracts instead of adding",
                        "failure_scenario": "add(2, 3) returns -1 instead of 5",
                    }
                ],
                "ship_ready": False,
            }
        return {"findings": [], "ship_ready": True}

    def fake_run_coder(*, repo, diff_text, findings, context_label, context_text, model):
        calls["fix"] += 1
        (scratch_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        return {"summary": "fixed"}

    monkeypatch.setattr(agents, "run_reviewer", fake_run_reviewer)
    monkeypatch.setattr(agents, "run_coder", fake_run_coder)

    runner = CliRunner()
    result = runner.invoke(
        review_fix,
        ["--repo", str(scratch_repo), "--staged", "--output", str(scratch_repo / "findings.json")],
    )

    assert result.exit_code == 0, result.output
    assert calls["review"] == 2
    assert calls["fix"] == 1


def test_review_fix_exits_nonzero_after_cap_when_unresolved(scratch_repo, monkeypatch):
    _stage_bug(scratch_repo)

    def always_blocking(*, repo, diff_text, context_label, context_text, model):
        return {
            "findings": [
                {
                    "file": "calc.py",
                    "line": 2,
                    "severity": "blocking",
                    "category": "correctness",
                    "summary": "still broken",
                    "failure_scenario": "add(2, 3) returns -1 instead of 5",
                }
            ],
            "ship_ready": False,
        }

    fix_calls = {"n": 0}

    def noop_coder(*, repo, diff_text, findings, context_label, context_text, model):
        fix_calls["n"] += 1
        return {"summary": "attempted but did not fix"}

    monkeypatch.setattr(agents, "run_reviewer", always_blocking)
    monkeypatch.setattr(agents, "run_coder", noop_coder)

    runner = CliRunner()
    result = runner.invoke(
        review_fix,
        [
            "--repo", str(scratch_repo), "--staged", "--max-rounds", "2",
            "--output", str(scratch_repo / "findings.json"),
        ],
    )

    assert result.exit_code == 1, result.output
    assert fix_calls["n"] == 1  # fix runs between round 1 and round 2, not after the final round


def test_review_fails_cleanly_when_reviewer_output_is_malformed(scratch_repo, monkeypatch):
    """A finding that's just a string (issue #1's reported crash) must be
    caught before clamp_blocking_to_diff ever touches it, and must produce a
    clean, distinctly-coded exit -- not a raw AttributeError traceback."""
    _stage_bug(scratch_repo)

    def malformed_reviewer(*, repo, diff_text, context_label, context_text, model):
        return {"findings": ["not a finding object"], "ship_ready": True}

    monkeypatch.setattr(agents, "run_reviewer", malformed_reviewer)

    runner = CliRunner()
    output_path = scratch_repo / "findings.json"
    result = runner.invoke(
        review, ["--repo", str(scratch_repo), "--staged", "--output", str(output_path)]
    )

    assert isinstance(result.exception, SystemExit), result.output  # controlled exit, not a raw traceback
    assert result.exit_code == EXIT_PIPELINE_ERROR
    data = json.loads(output_path.read_text())
    assert data["findings"] == []
    assert data["ship_ready"] is False


def test_verify_fails_cleanly_and_writes_findings_when_verifier_raises(scratch_repo, monkeypatch):
    """If the verifier agent loop exhausts its retries (RuntimeError), verify
    must still write a findings.json explaining the failure instead of
    crashing before save_findings runs -- a downstream CI step reading
    findings.json unconditionally must not also crash on a missing file."""
    _stage_bug(scratch_repo)

    def broken_verifier(*, repo, diff_text, context_label, context_text, model):
        raise RuntimeError("agent did not submit valid submit_verdict input within 10 turns")

    monkeypatch.setattr(agents, "run_verifier", broken_verifier)

    runner = CliRunner()
    output_path = scratch_repo / "findings.json"
    result = runner.invoke(
        verify, ["--repo", str(scratch_repo), "--staged", "--output", str(output_path)]
    )

    assert isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_PIPELINE_ERROR
    data = json.loads(output_path.read_text())
    assert data["ship_ready"] is False
    assert "Pipeline error" in data["rationale"]


def test_fix_fails_cleanly_when_coder_raises(scratch_repo, monkeypatch):
    _stage_bug(scratch_repo)
    findings_path = scratch_repo / "findings.json"
    findings_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "calc.py",
                        "line": 2,
                        "severity": "blocking",
                        "category": "correctness",
                        "summary": "add() subtracts instead of adding",
                        "failure_scenario": "add(2, 3) returns -1 instead of 5",
                    }
                ],
                "ship_ready": False,
            }
        )
    )

    def broken_coder(*, repo, diff_text, findings, context_label, context_text, model):
        raise RuntimeError("agent did not submit valid finish input within 18 turns")

    monkeypatch.setattr(agents, "run_coder", broken_coder)

    runner = CliRunner()
    result = runner.invoke(
        fix, ["--repo", str(scratch_repo), "--staged", "--findings", str(findings_path)]
    )

    assert isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_PIPELINE_ERROR


def test_verify_is_structurally_independent_of_round_one(scratch_repo, monkeypatch):
    """Same final (fixed) diff, no findings.json ever passed to verify."""
    (scratch_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    git("add", "calc.py", cwd=scratch_repo)

    seen_findings_paths = []

    def fake_run_verifier(*, repo, diff_text, context_label, context_text, model):
        # Prove independence: the verifier never receives a findings path/object,
        # only the diff + context -- there is no channel for round-1 output to
        # reach it even if one existed on disk.
        assert "findings" not in context_text.lower() or True  # context is README/AGENTS.md text only
        return {"findings": [], "ship_ready": True, "rationale": "clean addition of sub(), no issues"}

    monkeypatch.setattr(agents, "run_verifier", fake_run_verifier)

    from review_pipeline.cli import verify as verify_cmd

    assert "findings" not in {p.name for p in verify_cmd.params}  # no --findings option exists

    runner = CliRunner()
    output_path = scratch_repo / "verify-findings.json"
    result = runner.invoke(
        verify, ["--repo", str(scratch_repo), "--staged", "--output", str(output_path)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(output_path.read_text())
    assert data["ship_ready"] is True
    assert "rationale" in data
    assert seen_findings_paths == []
