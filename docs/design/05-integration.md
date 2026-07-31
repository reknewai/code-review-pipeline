# Integrating with an existing repository

`review-pipeline` isn't tied to this repo — it's meant to be installed
alongside *any* git project and pointed at that project's diffs. This page
is the map of what actually needs to change in a consumer repo.

## 1. Install the package where it needs to run

Two places need it: wherever developers run `review`/`fix`/`review-fix`
locally (their machines, via pre-commit), and CI (for `verify`).

```bash
pip install review-pipeline   # once published; until then, pip install
                               # "git+https://github.com/<org>/review-pipeline.git@<rev>"
```

Nothing in the consumer repo needs to vendor this project's source — it's a
regular Python dependency with a console-script entry point
(`review-pipeline`).

## 2. Add context: `AGENTS.md`

Copy [`docs/AGENTS.md`](../AGENTS.md) into the consumer repo's root and fill
it in. This is the single highest-leverage integration step — every
Reviewer/Coder/Verifier call reads it, and it's where you tell the model
about conventions, false-positive traps ("this looks like a bug but isn't"),
and which code paths deserve extra scrutiny. No `AGENTS.md` still works
(falls back to touched-directory READMEs, then the diff alone) but produces
a more generic review.

## 3. Wire up the pre-commit hook (local, convenience)

Either reference this repo directly as a hook source:

```yaml
# consumer-repo/.pre-commit-config.yaml
repos:
  - repo: https://github.com/<org>/review-pipeline
    rev: v0.1.0
    hooks:
      - id: review-pipeline
```

or vendor [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)'s
`local` hook block plus [`scripts/pre-commit-review.sh`](../../scripts/pre-commit-review.sh)
directly into the consumer repo. Either way, developers still need
`pip install pre-commit && pre-commit install` once, and `ANTHROPIC_API_KEY`
set in their own shell.

## 4. Wire up the CI gate (the real enforcement)

Copy [`.github/workflows/verify.yml`](../../.github/workflows/verify.yml)
into the consumer repo's `.github/workflows/`. Three things need to be true
for it to run:

1. `pip install review-pipeline` resolves to something real — swap the
   placeholder install line for a published version or a pinned source URL.
2. An `ANTHROPIC_API_KEY` repository secret exists.
3. A branch protection rule on the default branch requires the `ai-review`
   check to pass before merge — this is the step that actually makes any of
   this a gate rather than a suggestion. Without it, `verify` still runs and
   reports, but nothing stops a merge on failure.

## 5. Swap the model or provider, if needed

Every LLM call is isolated in [`agents.py`](../../src/review_pipeline/agents.py)
behind `run_reviewer`/`run_coder`/`run_verifier`. The model is
`--model`, or `REVIEW_PIPELINE_MODEL`, or a built-in default — no caller
outside `agents.py` knows or cares which model or provider is behind those
three functions. A consumer repo that wants a different model just sets
`REVIEW_PIPELINE_MODEL`; a maintainer who wants a different provider's SDK
entirely changes `agents.py` and nothing else, because the CLI/diff/schema
layers only depend on the `{findings, ship_ready, rationale?}` contract, not
on how it was produced.

## What does *not* need to change per-repo

The `findings.json` schema, the CLI's command surface, and the isolation
guarantee between `review`/`fix` and `verify` are fixed points — they're the
same regardless of language, framework, or which repo this runs in. That's
the whole point of keeping them a stable contract (see
[the solution rationale](02-solution.md)).

Next: [a concrete getting-started walkthrough](06-getting-started.md).
