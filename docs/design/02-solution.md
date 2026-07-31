# Why: this shape, not another

## Three roles, not one

It would be simpler to build a single "review this diff and fix what's
wrong" command. We deliberately split it into three instead:

| Role | Runs | Makes code changes? | Sees prior findings? |
| --- | --- | --- | --- |
| Reviewer (`review`) | locally / pre-commit | No | No |
| Coder (`fix`) | locally / pre-commit | Yes | Yes (the findings it's fixing) |
| Verifier (`verify`) | CI only | No | **No, structurally** |

A single "look at this and fix it" command collapses reviewing and fixing
into one call, which means the same model pass is both deciding what's wrong
*and* deciding whether its own fix is good — there's no independent check
in between. Splitting Reviewer from Coder means the fix is judged against
findings a separate call produced, and splitting both from the Verifier
means there's a second, independently-formed opinion before merge. This
mirrors why human review works in two passes in the first place: a second,
fresh set of eyes catches what the first pass's blind spots miss.

## Why isolation is enforced in code, not convention

The most important design decision here: `verify`'s argument parser has no
`--findings` flag. Not "please don't pass one," not "we discard it if you
do" — it does not exist as an input. Why so strict?

Because "the CI reviewer shouldn't be influenced by the local reviewer" is
exactly the kind of rule that erodes under normal engineering pressure. A
future contributor debugging a flaky verify result might reasonably think
"let me just pass the findings.json to verify so it can cross-check" —
that's a plausible-sounding change that would quietly destroy the entire
value of round two. Making it structurally impossible (no code path reads a
findings.json inside `verify`) means that erosion can't happen by accident,
and a deliberate attempt to break it shows up as an obvious diff to
`cli.py`, not a quiet parameter addition.

## Why severity filtering happens in code, not in the prompt

Similarly, `blocking`/`major` findings outside the diff's changed lines are
demoted to `minor` by [`diff.py`](../../src/review_pipeline/diff.py)'s
`clamp_blocking_to_diff`, unconditionally, after every Reviewer/Verifier
call. We *also* tell the model this in its system prompt — but the prompt
is a request, and the clamp is a guarantee. Models are good at concrete
tasks like "does this failure scenario make sense" and unreliable at
consistently self-enforcing structural rules like "never let a line number
outside this specific line-set carry this specific severity" across every
response. Put the guarantee where it can't be skipped: in code that runs on
every finding, every time.

## Why `anthropic` directly, not a coding-agent framework

The Coder's job here is narrow: given a short list of findings, read the
affected files and write corrected versions. That's a handful of tool
definitions and a loop with a forced termination condition — maybe 150 lines
(see [`agents.py`](../../src/review_pipeline/agents.py)). Pulling in a
general-purpose coding-agent framework would mean inheriting its assumptions
about project structure, its own prompt strategy, and a much larger surface
area to reason about for something this scoped. Writing the loop directly
also means the three LLM calls all go through one file — swapping models or
providers later touches `agents.py` and nothing else, which matters for a
tool meant to outlive any one model generation.

## Why enforcement is two-tiered (pre-commit *and* CI)

Pre-commit is fast feedback on the author's machine, using the author's API
key, before the code ever leaves their laptop. But anything that runs
client-side, using client-side credentials, can be skipped by the client —
that's not a flaw to fix, it's just what "local" means. So pre-commit is
explicitly documented as *convenience*, and the actual gate is CI + branch
protection, which GitHub enforces server-side regardless of whose machine or
API key produced the last push. Two tiers, two different jobs: catch things
fast locally, guarantee things centrally.

Next: [how each piece is actually implemented](03-implementation.md).
