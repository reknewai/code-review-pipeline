# How: implementation details

This page covers the mechanics inside [`src/review_pipeline/`](../../src/review_pipeline/).
For the higher-level component picture, see [Architecture](04-architecture.md).

## The agent loop (`agents.py`)

All three roles — Reviewer, Coder, Verifier — run through one function,
`_agentic_loop`. It's a plain `while`-style turn loop against the Anthropic
Messages API:

1. Send the conversation so far, with a fixed tool list, to the model.
2. If the model calls a *terminal* tool (`submit_findings`, `submit_verdict`,
   or `finish`), stop and return its input — that's the role's structured
   output.
3. Otherwise, execute whatever tools it called (`read_file`, `list_dir`,
   `write_file`), feed the results back as `tool_result` blocks, and loop.
4. On the last allowed turn, `tool_choice` is forced to the terminal tool, so
   the loop always terminates with valid output instead of running out of
   turns mid-thought.

The three roles differ only in their system prompt, their tool list (the
Coder additionally gets `write_file`), and their terminal tool's schema
(the Verifier's `submit_verdict` additionally requires `rationale`). All
three reuse the exact same loop and the exact same sandboxing.

**Sandboxing:** every filesystem tool resolves its path against the repo
root and rejects anything that escapes it (`_safe_path` in `agents.py`), so
a model-suggested path like `../../etc/passwd` fails instead of reading
outside the repo. This isn't a defense against a malicious model — it's
cheap insurance against an ordinary path-construction mistake.

## Changed-line filtering (`diff.py`)

`parse_changed_lines` walks a unified diff and builds, per file, the set of
line numbers that exist as added/modified lines *in the new version* of the
file. It tracks a running line counter per hunk (`@@ -a,b +c,d @@` gives the
starting line `c`), incrementing on context and added lines, holding steady
on removed lines (they don't exist in the new file, so they can't be
findings targets).

`clamp_blocking_to_diff` then walks every finding and, if its
`(file, line)` isn't in that changed-line set, forces `blocking`/`major`
down to `minor` — regardless of what the model decided. This runs after
every Reviewer and Verifier call, before anything is validated or written
to disk. See [why this lives in code](02-solution.md#why-severity-filtering-happens-in-code-not-in-the-prompt).

## Schema enforcement (`schema.py`)

`findings.json`'s shape is defined once, in
[`schemas/findings.schema.json`](../../schemas/findings.schema.json), and
loaded from the package at runtime via `importlib.resources` (so it works
the same whether the package is installed editable or from a wheel).
`validate_findings` runs on every write (`save_findings`) and every read
(`load_findings`) — a hand-edited or model-malformed findings.json fails
loudly instead of silently propagating a shape mismatch into `fix` or into
CI's gating logic.

## The `--staged` restage step

One implementation detail worth calling out because it wasn't obvious until
testing surfaced it: the Coder writes fixes to the **working tree**, not the
git index. If you're operating on `--staged` diffs (which is exactly what
the pre-commit hook does), a naive re-review after `fix` would still diff
the *stale, unfixed* index against `HEAD` — the fix would exist on disk but
be invisible to the next round. `cli.py` calls
`diff.restage_tracked_changes(root, touched_files)` (`git add -u -- <paths>`,
scoped to the files the diff being fixed touched) after every Coder run when
`--staged` is active, so the index reflects the fix before the loop
re-diffs — without sweeping in unrelated unstaged changes elsewhere in the
repo. This only re-stages already-tracked files; a fix that needs a
genuinely new file is outside this loop's scope.

## Orchestration (`cli.py`)

`review-fix` is a plain loop over `review` → check whether the round still
needs action → `fix` → repeat, capped at `--max-rounds` (default 3). "Needs
action" means either a blocking finding survived `clamp_blocking_to_diff`,
or the Reviewer's own `ship_ready` verdict came back false (`cli.py`'s
`_needs_action` helper, shared with `review`'s exit code) — a clean-looking
findings list doesn't pass if the model itself says it isn't ready. Each
round recomputes the diff from scratch (rather than reusing the previous
round's diff text), because the Coder may have changed line numbers, file
contents, or even which files are touched. The loop exits early the moment a
round no longer needs action; it only exits non-zero if the final round (the
cap) still needs it — intermediate rounds always get a fix attempt first.

Next: [the component-level architecture and diagram](04-architecture.md).
