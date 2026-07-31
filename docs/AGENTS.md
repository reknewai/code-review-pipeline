# AGENTS.md

This file is context for AI coding agents (including review-pipeline's
Reviewer, Coder, and Verifier) working in this repository. It's plain
Markdown, no required schema -- write whatever a new human contributor would
need on day one. Copy this file to your repo root and edit it for your
project; delete the sections that don't apply.

## What this project is

One or two sentences: what it does, who it's for.

## How to build and test

```bash
# install
# run tests
# run the app locally
```

## Conventions worth knowing

- Language/framework versions that matter (e.g. "Python 3.11+, no walrus in
  the compat shim module").
- Patterns to follow (e.g. "all DB access goes through `repo/`, never raw
  SQL in handlers").
- Patterns to avoid, and why (e.g. "don't add new global state -- we're
  mid-migration off it, see ADR-0007").

## Things that look like bugs but aren't

Anything a reviewer (human or AI) would flag as suspicious but is actually
intentional. Save everyone the round-trip.

## Security-sensitive areas

Call out code paths that need extra scrutiny: auth, payment, anything
handling user-supplied input that reaches a shell/SQL/template, secrets
handling. review-pipeline's Reviewer and Verifier both read this file --
this is the highest-leverage place to raise their bar on the parts of the
codebase where a missed bug actually hurts.

## Where to look for more context

- Directory READMEs, ADRs, design docs, wherever they live.
- If there isn't an AGENTS.md in a subdirectory, review-pipeline falls back
  to README(s) in the directories the diff touches -- so keeping those
  accurate helps too.
