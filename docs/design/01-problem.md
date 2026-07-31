# What: the problem

## The loop we're replacing

Every PR goes through the same human loop: an author writes code, a reviewer
reads the diff and leaves comments, the author fixes what's flagged, and
(ideally) a second reviewer signs off before merge. That loop works, but it
has three structural weaknesses:

- **It's slow and bursty.** A PR can sit for hours or days waiting for a
  reviewer's attention, independent of how hard the actual review is.
- **It's inconsistent.** Two reviewers looking at the same diff apply
  different bars, on different days, depending on how busy or careful they
  happen to be. The same bug pattern gets caught this week and missed next
  week.
- **The two rounds aren't actually independent.** In practice, a second
  reviewer often anchors on the first reviewer's comments ("oh, X already
  looked at this") instead of forming their own opinion — so "two reviews"
  quietly degrades into "one review plus a rubber stamp."

None of this is a knock on human reviewers — it's just what happens when
review capacity is scarce and asynchronous. The question this project
answers is narrower: **can an LLM-driven review/fix/verify loop do the fast,
mechanical part of this well enough to be a real gate, not just a
suggestion?**

## What "well enough to be a real gate" requires

For an automated reviewer to be trustworthy enough to block a merge, three
properties have to hold, or it just becomes noise people route around:

1. **Findings must be concrete.** "This could be a problem" is not
   actionable and erodes trust fast. Every finding needs a specific failure
   scenario: an input or state that produces a wrong result or a crash.
2. **Severity must be scoped to the diff.** An automated reviewer that flags
   pre-existing issues elsewhere in the file as blocking will hold PRs
   hostage for problems the author didn't create. Only what the diff
   actually changed can gate it.
3. **The second opinion must be a real second opinion.** If round two can
   see round one's output, a systematic blind spot in round one (a bug
   pattern the model doesn't recognize, a prompt weakness) reproduces itself
   in round two instead of getting caught. The two rounds need to be
   structurally incapable of leaking into each other, not just conventionally
   discouraged from it.

## Scope

This project is deliberately narrow: it's the review/fix/verify mechanics,
not a full CI platform, not a replacement for human review on anything that
actually matters (security-sensitive code, architectural decisions,
anything with real consequences), and not tied to any one repository or
language. It has to work by cloning it into *any* git repo and pointing it
at a diff.

Next: [why this specific shape solves it](02-solution.md).
