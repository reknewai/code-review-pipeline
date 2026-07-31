# review-pipeline

An AI-driven replacement for the human "write → review → fix → ship" loop.
Three independent LLM roles, one fixed JSON contract between them, usable in
any git repository.

```
review   -> Reviewer LLM call on the current diff. Writes findings.json. No code changes.
fix      -> Coder LLM call that applies changes for blocking/major findings. No re-review.
review-fix -> review -> fix -> review, capped at 3 rounds. Convenience wrapper for local use.
verify   -> Verifier LLM call, CI-side. Independent second opinion. Cannot accept a findings.json input.
```

For the full design rationale, architecture diagrams, and a from-scratch
getting-started walkthrough, see [`docs/design/`](docs/design/README.md).

## Why two rounds, structurally isolated

`review`/`fix` (round 1) run locally, fast, on your machine or in a
pre-commit hook. `verify` (round 2) runs in CI as the real gate. If round 2
could read round 1's findings.json, a model that made a mistake in round 1
could simply repeat it in round 2 -- the second opinion wouldn't be
independent, it'd just be an echo.

So `verify` has no `--findings` flag anywhere in its argument parser, and its
code path never opens a file written by `review`/`fix`. This isn't a
convention that could be missed by accident -- it doesn't exist as an
input.

## Install

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

Requires Python 3.10+, `git` on PATH, and an Anthropic API key. All three
LLM calls (Reviewer, Coder, Verifier) go through the `anthropic` SDK
directly -- no third-party coding-agent framework. Each call is isolated in
[`agents.py`](src/review_pipeline/agents.py), so swapping in a different
model or provider later means editing that one file, not any caller.

## Quickstart

```bash
cd your-repo
# make some changes, then:
review-pipeline review              # writes findings.json from the working-tree diff
cat findings.json
review-pipeline fix                 # applies fixes for blocking/major findings
review-pipeline review              # confirm it's clean
```

Or let it loop for you:

```bash
review-pipeline review-fix          # review -> fix -> review, up to 3 rounds
echo $?                             # non-zero if blocking findings remain, or ship_ready is false, after the cap
```

Diff scope flags (shared by `review`, `fix`, `review-fix`, `verify`):

| Flag | Meaning |
| --- | --- |
| `--staged` | `git diff --cached` -- what's about to be committed |
| `--base BRANCH` | `git diff BRANCH...HEAD` -- what this branch introduced vs. its merge-base |
| `--commit-range A..B` | passed straight to `git diff` |
| (none) | `git diff HEAD` -- all uncommitted changes, staged + unstaged |

## The findings.json contract

```jsonc
{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 42,                              // line in the NEW version of the file
      "severity": "blocking",                  // blocking | major | minor | nit
      "category": "correctness",                // correctness | security | breaking-change | style | nit
      "summary": "one sentence",
      "failure_scenario": "concrete input/state -> wrong output or crash",
      "suggested_fix": "optional"
    }
  ],
  "ship_ready": false,
  "rationale": "one sentence -- only present on `verify` output"
}
```

Schema lives at [`schemas/findings.schema.json`](schemas/findings.schema.json)
and is enforced (not just documented) by [`schema.py`](src/review_pipeline/schema.py)
before anything is written to disk.

**A `blocking` finding must point at a line the diff actually touched.**
[`diff.py`](src/review_pipeline/diff.py) parses the unified diff into a
changed-line set per file and demotes any `blocking`/`major` finding outside
it down to `minor`, unconditionally, in code -- not by asking the model
nicely. Pre-existing repo issues can never gate a PR through this pipeline.

CI's gate (see below) additionally only counts `blocking` findings in the
`correctness`, `security`, or `breaking-change` categories -- a `blocking`
style nit doesn't fail the build.

## Context: AGENTS.md

Every LLM call reads the target repo's root `AGENTS.md` if one exists (see
[`docs/AGENTS.md`](docs/AGENTS.md) for an example to copy in). If there's no
AGENTS.md, it falls back to the README(s) in the directories the diff
touched. No AGENTS.md and no README means the model reviews the diff in
isolation -- still useful, just less context.

## Local enforcement: pre-commit (convenience, not the real gate)

```bash
pip install pre-commit
pre-commit install
```

`.pre-commit-config.yaml` in this repo runs
[`scripts/pre-commit-review.sh`](scripts/pre-commit-review.sh), which calls
`review-pipeline review-fix --staged` and blocks the commit if blocking
findings remain, or the reviewer's own `ship_ready` verdict is still false,
after 3 rounds. Copy `.pre-commit-config.yaml` (or the
`local` hook block inside it) into your own repo, or reference this repo
directly as a hook source once it's published:

```yaml
repos:
  - repo: https://github.com/<org>/review-pipeline
    rev: v0.1.0
    hooks:
      - id: review-pipeline
```

**This is a fast-feedback convenience, not enforcement.** It runs on the
committer's machine, with the committer's API key, and can be skipped:

- `git commit --no-verify` -- skips *all* pre-commit hooks, silently, by
  design of git itself. There's no hook code to print a warning from.
- `SKIP_REVIEW=1 git commit ...` -- skips just this hook, and prints a
  warning to stderr so the bypass isn't invisible in the commit's terminal
  output.

The actual gate is CI + branch protection, below.

## CI enforcement: the real gate

`.github/workflows/verify.yml`:

- Triggers on `pull_request` events `ready_for_review` and `synchronize`,
  gated by `if: github.event.pull_request.draft == false` -- so it runs once
  when a PR leaves draft, and again on every push while non-draft, but never
  on draft pushes.
- Checks out the PR head, diffs it against the PR base
  (`base.sha...head.sha`), and runs `review-pipeline verify` -- no
  findings.json from round 1 is ever passed in.
- Sets a Check Run named `ai-review`: `success` only if `ship_ready == true`
  *and* there are no `blocking` findings in `correctness`/`security`/
  `breaking-change`; `failure` otherwise.
- Posts (and updates in place on re-runs) a PR comment with the findings
  table.

To make this the actual gate, add a branch protection rule on your default
branch requiring the `ai-review` check to pass before merge. That's the
step pre-commit can't replace: branch protection is enforced by GitHub
server-side, for everyone, regardless of whose machine or API key ran
locally.

Before this workflow will run in your repo:

1. Publish/install `review-pipeline` somewhere CI can `pip install` it (swap
   the placeholder in the workflow's install step for a PyPI release or a
   pinned `git+https://...` URL).
2. Add an `ANTHROPIC_API_KEY` repository secret.
3. Add the branch protection rule requiring `ai-review`.

## Model / provider swapping

Every LLM call is a single function in `agents.py`
(`run_reviewer`/`run_coder`/`run_verifier`) built on one small tool-use loop
(`_agentic_loop`). The model name is `--model`, or `REVIEW_PIPELINE_MODEL`,
or a built-in default. CLI, schema, and diff-filtering logic don't know or
care what's inside `agents.py` -- a different model, or a different
provider's SDK entirely, is a change scoped to that one file.

## Repo layout

```
src/review_pipeline/
├── cli.py      # review / fix / review-fix / verify subcommands
├── agents.py   # Reviewer / Coder / Verifier prompts + anthropic SDK calls + tool loop
├── diff.py     # git diff helpers, changed-line parsing, AGENTS.md/README context lookup
└── schema.py   # findings.json validation (backed by schemas/findings.schema.json)
.pre-commit-config.yaml     # copy into a consumer repo
.pre-commit-hooks.yaml      # manifest for referencing this repo as a hook source
scripts/pre-commit-review.sh
.github/workflows/verify.yml
schemas/findings.schema.json
docs/AGENTS.md               # example for consumers to copy
```

## Known limitations

- LLM calls are non-deterministic. `review-fix`'s 3-round cap and `verify`'s
  independent judgment are both best-effort, not proofs -- treat this as a
  strong additional check, not a replacement for human review on anything
  that actually matters.
- Every command that calls a model needs `ANTHROPIC_API_KEY` set and costs
  real tokens; `review`/`verify` on an empty diff and `fix` on an empty
  finding list both short-circuit before making a call.
- The Coder tool loop can read/write anywhere under the target repo root
  (path-traversal-checked, but not sandboxed beyond that) -- run it against
  code you trust the way you'd trust any local dev tool.
