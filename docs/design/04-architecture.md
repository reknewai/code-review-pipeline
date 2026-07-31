# Architecture overview

## Components

```mermaid
flowchart TB
    subgraph CLI["cli.py — click commands"]
        review["review"]
        fix["fix"]
        reviewfix["review-fix"]
        verify["verify"]
    end

    subgraph Core["shared core"]
        diffmod["diff.py\ngit diff, changed-line parsing,\nAGENTS.md/README context lookup"]
        agentsmod["agents.py\nReviewer / Coder / Verifier\n+ tool-use agent loop"]
        schemamod["schema.py\nfindings.json validation"]
    end

    git[("target repo\n(git working tree)")]
    anthropic[["Anthropic API"]]
    findingsjson[["findings.json"]]

    review --> diffmod
    fix --> diffmod
    reviewfix --> diffmod
    verify --> diffmod

    review --> agentsmod
    fix --> agentsmod
    reviewfix --> agentsmod
    verify --> agentsmod

    review --> schemamod
    fix --> schemamod
    reviewfix --> schemamod
    verify --> schemamod

    diffmod <--> git
    agentsmod <--> git
    agentsmod <--> anthropic
    schemamod <--> findingsjson

    classDef gate fill:#844,stroke:#c66,color:#fff
    class verify gate
```

`verify` is marked out because it's the only command wired into anything
that gates a merge (see the local-vs-CI split below). All four commands sit
on the same three core modules — there's no separate code path for "the CI
version" of review logic; the isolation between round 1 and round 2 is
enforced by `verify` never being given a findings.json to read, not by
duplicating logic.

## Local round 1 vs. CI round 2

```mermaid
sequenceDiagram
    participant Dev as Developer machine
    participant PC as pre-commit hook
    participant Repo as git repo
    participant GH as GitHub Actions

    Dev->>Repo: stage changes
    Dev->>PC: git commit
    PC->>PC: review-pipeline review-fix --staged
    Note over PC: Reviewer -> Coder -> Reviewer,<br/>capped at 3 rounds
    PC-->>Dev: block (blocking findings remain) or allow

    Dev->>Repo: git push, open PR
    GH->>Repo: checkout PR head, diff vs base
    GH->>GH: review-pipeline verify
    Note over GH: independent Verifier,<br/>no findings.json input, ever
    GH-->>Repo: ai-review check: success / failure
```

The pre-commit hook and the CI workflow never talk to each other and share
no state — the only thing they have in common is the findings.json *schema*
they both happen to produce. That's the isolation guarantee made visible as
a diagram: there is no arrow from "pre-commit hook" to "GitHub Actions" in
this picture, because there is no code path for one.

## The `review-fix` loop in detail

```mermaid
flowchart LR
    A["round 1: review"] -->|ship ready| Z["exit 0\nship ready"]
    A -->|needs action| B["fix"]
    B --> C["round 2: review"]
    C -->|ship ready| Z
    C -->|needs action| D["fix"]
    D --> E["round 3: review"]
    E -->|ship ready| Z
    E -->|needs action| Y["exit 1\nstill needs action"]
```

Each "review" box is a fresh Reviewer call against a freshly recomputed
diff — nothing carries over between rounds except what's actually on disk.

## Where each command runs

| Command | Typical caller | Writes code? | Exit code meaning |
| --- | --- | --- | --- |
| `review` | developer, CI (ad hoc) | No | non-zero if any blocking finding or `ship_ready` is false |
| `fix` | developer, `review-fix` | Yes | non-zero on error only |
| `review-fix` | pre-commit hook | Yes (via `fix`) | non-zero if action is still needed after the cap |
| `verify` | GitHub Actions only | No | non-zero if `ship_ready` is false |

Next: [how this plugs into an existing repository](05-integration.md).
