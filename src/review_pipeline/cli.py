"""review-pipeline CLI: review / fix / review-fix / verify.

Design invariant: `review`/`fix` (round 1, local/dev) and `verify` (round 2,
CI) share only the findings.json *schema*, never a findings.json *value*.
`verify` has no --findings option anywhere below -- that's enforced by the
argument parser, not by convention.
"""

from __future__ import annotations

import sys

import anthropic
import click

from review_pipeline import agents, diff as diffmod
from review_pipeline.schema import (
    FindingsValidationError,
    GATING_CATEGORIES,
    has_blocking,
    load_findings,
    save_findings,
    summarize,
    validate_findings,
)

# Distinct from click's own exit codes (0 = success, 2 = usage error) and
# from the gating exit code 1 (blocking findings present): 3 means the
# pipeline itself failed to produce a usable result (bad/truncated agent
# output, retries exhausted, a transient API error) -- a different condition
# than "review ran and found problems," so callers can tell the two apart.
EXIT_PIPELINE_ERROR = 3

# Failures from the agent loop that should exit cleanly with
# EXIT_PIPELINE_ERROR instead of crashing with a raw traceback:
# RuntimeError (retries exhausted), FindingsValidationError (agent output
# still doesn't satisfy the contract), and anthropic.APIError (rate limits,
# transient 5xx/overload, connection drops -- the most common real-world
# source of "intermittent" pipeline failures).
_PIPELINE_ERRORS = (RuntimeError, FindingsValidationError, anthropic.APIError)

DIFF_OPTIONS = [
    click.option("--repo", default=".", show_default=True, help="Path inside the target git repo."),
    click.option("--base", default=None, help="Diff against the merge-base with this branch (e.g. 'main')."),
    click.option("--commit-range", default=None, help="Explicit git range/revspec, e.g. 'abc123..def456'."),
    click.option("--staged", is_flag=True, help="Diff staged changes only (git diff --cached)."),
]


def _add_options(options):
    def decorator(func):
        for opt in reversed(options):
            func = opt(func)
        return func

    return decorator


def _require_api_key():
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo("ERROR: ANTHROPIC_API_KEY is not set.", err=True)
        sys.exit(2)


def _needs_action(result) -> bool:
    """True if the result has a blocking finding, or the agent's own
    ship_ready verdict says no -- matches the check `verify` applies."""
    return has_blocking(result) or not result.get("ship_ready", True)


def _resolve_diff(repo, base, commit_range, staged):
    try:
        root = diffmod.repo_root(repo)
        diff_text = diffmod.get_diff(root, base=base, commit_range=commit_range, staged=staged)
    except diffmod.GitError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)
    return root, diff_text


def _run_agent_stage(fn, *, output: str, require_rationale: bool):
    """Run an agent call (run_reviewer/run_verifier) and validate its output
    against the findings.json contract *before* anything else touches it --
    clamp_blocking_to_diff runs after this, not before.

    Turns a pipeline failure (the agent loop exhausting its retries, or
    returning output that still doesn't satisfy the contract) into a clean,
    distinctly-coded error instead of a raw traceback, and still writes
    `output` so a CI consumer always has a findings.json to read instead of
    crashing itself on a missing file.
    """
    try:
        result = fn()
        validate_findings(result, require_rationale=require_rationale)
    except _PIPELINE_ERRORS as exc:
        click.echo(f"ERROR: review pipeline failed: {exc}", err=True)
        error_result: dict = {"findings": [], "ship_ready": False}
        if require_rationale:
            error_result["rationale"] = f"Pipeline error: {exc}"
        save_findings(output, error_result, require_rationale=require_rationale)
        sys.exit(EXIT_PIPELINE_ERROR)
    return result


@click.group()
def main():
    """AI-driven review -> fix -> verify pipeline for any git repository."""


@main.command()
@_add_options(DIFF_OPTIONS)
@click.option("--output", default="findings.json", show_default=True, help="Where to write findings.")
@click.option("--model", default=None, help="Override the model used (default: env REVIEW_PIPELINE_MODEL or built-in default).")
def review(repo, base, commit_range, staged, output, model):
    """Run the independent Reviewer against the current diff. Makes no code changes."""
    root, diff_text = _resolve_diff(repo, base, commit_range, staged)

    if not diff_text.strip():
        click.echo("No changes to review.")
        save_findings(output, {"findings": [], "ship_ready": True})
        sys.exit(0)

    _require_api_key()
    context_label, context_text = diffmod.gather_context(root, diff_text)
    click.echo(f"Reviewing diff ({len(diff_text.splitlines())} lines) with context: {context_label}")

    result = _run_agent_stage(
        lambda: agents.run_reviewer(
            repo=root,
            diff_text=diff_text,
            context_label=context_label,
            context_text=context_text,
            model=model or agents.DEFAULT_MODEL,
        ),
        output=output,
        require_rationale=False,
    )
    result = diffmod.clamp_blocking_to_diff(result, diff_text)
    save_findings(output, result)

    click.echo(f"Wrote {output}: {summarize(result)}")
    sys.exit(1 if _needs_action(result) else 0)


@main.command()
@_add_options(DIFF_OPTIONS)
@click.option("--findings", "findings_path", default="findings.json", show_default=True, help="Findings file to act on.")
@click.option("--model", default=None)
def fix(repo, base, commit_range, staged, findings_path, model):
    """Apply changes for blocking/major findings from an existing findings.json. Does not re-review."""
    root, diff_text = _resolve_diff(repo, base, commit_range, staged)

    try:
        data = load_findings(findings_path)
    except (OSError, ValueError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)
    actionable = [f for f in data["findings"] if f["severity"] in ("blocking", "major")]
    if not actionable:
        click.echo("No blocking/major findings to fix.")
        sys.exit(0)

    _require_api_key()
    context_label, context_text = diffmod.gather_context(root, diff_text)
    click.echo(f"Fixing {len(actionable)} finding(s)...")

    try:
        outcome = agents.run_coder(
            repo=root,
            diff_text=diff_text,
            findings=actionable,
            context_label=context_label,
            context_text=context_text,
            model=model or agents.DEFAULT_MODEL,
        )
    except _PIPELINE_ERRORS as exc:
        click.echo(f"ERROR: coder pipeline failed: {exc}", err=True)
        sys.exit(EXIT_PIPELINE_ERROR)
    if staged:
        touched = diffmod.parse_changed_lines(diff_text).touched_files()
        diffmod.restage_tracked_changes(root, touched)
    click.echo(f"Coder finished: {outcome.get('summary', '(no summary)')}")


@main.command(name="review-fix")
@_add_options(DIFF_OPTIONS)
@click.option("--output", default="findings.json", show_default=True)
@click.option("--max-rounds", default=3, show_default=True)
@click.option("--model", default=None)
def review_fix(repo, base, commit_range, staged, output, max_rounds, model):
    """review -> fix -> review, capped at --max-rounds. Exits non-zero if blocking findings remain."""
    root, _ = _resolve_diff(repo, base, commit_range, staged)

    for round_num in range(1, max_rounds + 1):
        _, diff_text = _resolve_diff(repo, base, commit_range, staged)
        if not diff_text.strip():
            click.echo("No changes to review.")
            save_findings(output, {"findings": [], "ship_ready": True})
            sys.exit(0)

        _require_api_key()
        context_label, context_text = diffmod.gather_context(root, diff_text)
        click.echo(f"[round {round_num}/{max_rounds}] reviewing...")
        result = _run_agent_stage(
            lambda: agents.run_reviewer(
                repo=root,
                diff_text=diff_text,
                context_label=context_label,
                context_text=context_text,
                model=model or agents.DEFAULT_MODEL,
            ),
            output=output,
            require_rationale=False,
        )
        result = diffmod.clamp_blocking_to_diff(result, diff_text)
        save_findings(output, result)
        click.echo(f"[round {round_num}/{max_rounds}] {summarize(result)}")

        if not _needs_action(result):
            click.echo("No blocking findings. Ship ready.")
            sys.exit(0)

        if round_num == max_rounds:
            click.echo(f"Blocking findings remain after {max_rounds} round(s). Failing.", err=True)
            sys.exit(1)

        blocking_and_major = [f for f in result["findings"] if f["severity"] in ("blocking", "major")]
        click.echo(f"[round {round_num}/{max_rounds}] fixing {len(blocking_and_major)} finding(s)...")
        try:
            agents.run_coder(
                repo=root,
                diff_text=diff_text,
                findings=blocking_and_major,
                context_label=context_label,
                context_text=context_text,
                model=model or agents.DEFAULT_MODEL,
            )
        except _PIPELINE_ERRORS as exc:
            click.echo(f"ERROR: coder pipeline failed: {exc}", err=True)
            sys.exit(EXIT_PIPELINE_ERROR)
        if staged:
            touched = diffmod.parse_changed_lines(diff_text).touched_files()
            diffmod.restage_tracked_changes(root, touched)

    sys.exit(1)  # unreachable, but keeps mypy/readers honest


@main.command()
@_add_options(DIFF_OPTIONS)
@click.option("--output", default="findings.json", show_default=True)
@click.option("--model", default=None)
def verify(repo, base, commit_range, staged, output, model):
    """Independent second-round Verifier. Structurally cannot accept a findings.json input."""
    root, diff_text = _resolve_diff(repo, base, commit_range, staged)

    if not diff_text.strip():
        result = {"findings": [], "ship_ready": True, "rationale": "No changes in this diff."}
        save_findings(output, result, require_rationale=True)
        click.echo("No changes to verify. ship_ready=true")
        sys.exit(0)

    _require_api_key()
    context_label, context_text = diffmod.gather_context(root, diff_text)
    click.echo(f"Verifying diff ({len(diff_text.splitlines())} lines) with context: {context_label}")

    result = _run_agent_stage(
        lambda: agents.run_verifier(
            repo=root,
            diff_text=diff_text,
            context_label=context_label,
            context_text=context_text,
            model=model or agents.DEFAULT_MODEL,
        ),
        output=output,
        require_rationale=True,
    )
    result = diffmod.clamp_blocking_to_diff(result, diff_text)

    gate_blocking = has_blocking(result, categories=GATING_CATEGORIES)
    result["ship_ready"] = bool(result["ship_ready"]) and not gate_blocking
    save_findings(output, result, require_rationale=True)

    click.echo(f"Wrote {output}: {summarize(result)}")
    click.echo(f"ship_ready={result['ship_ready']} rationale={result.get('rationale', '')}")
    sys.exit(0 if result["ship_ready"] else 1)


if __name__ == "__main__":
    main()
