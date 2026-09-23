# Getting started

Five-minute walkthrough: install the tool, run it against a real bug, watch
it get fixed and re-verified.

## 1. Install

```bash
git clone https://github.com/reknewai/code-review-pipeline.git
cd code-review-pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Try it on a throwaway repo

```bash
mkdir /tmp/demo && cd /tmp/demo
git init -q && git config user.email you@example.com && git config user.name you
cat > calc.py <<'EOF'
def add(a, b):
    return a + b
EOF
git add calc.py && git commit -q -m "initial commit"
```

Introduce a bug and stage it:

```bash
cat > calc.py <<'EOF'
def add(a, b):
    return a - b
EOF
git add calc.py
```

Review it:

```bash
review-pipeline review --staged
cat findings.json
```

You should see a `blocking`/`correctness` finding pointing at the changed
line, with a concrete failure scenario (e.g. `add(2, 3)` returning `-1`).

## 3. Fix it, then confirm

```bash
review-pipeline fix --staged --findings findings.json
git diff --cached   # the fix is already re-staged
review-pipeline review --staged --output findings2.json
cat findings2.json  # findings: [], ship_ready: true
```

Or skip the manual dance and let the loop do both:

```bash
review-pipeline review-fix --staged
echo $?   # 0 if it converged, 1 if blocking findings survived 3 rounds
```

## 4. Add the pre-commit gate

```bash
pip install pre-commit
```

Copy this repo's [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)
and [`scripts/pre-commit-review.sh`](../../scripts/pre-commit-review.sh)
into your project (or reference this repo directly as a hook source — see
[Integration](05-integration.md#3-wire-up-the-pre-commit-hook-local-convenience)),
then:

```bash
pre-commit install
```

Now `git commit` runs `review-fix --staged` first. Try committing the buggy
`calc.py` above and watch it get blocked; `SKIP_REVIEW=1 git commit ...`
bypasses it with a warning, `git commit --no-verify` bypasses it silently.

## 5. Add the CI gate

Copy [`.github/workflows/verify.yml`](../../.github/workflows/verify.yml)
into your repo's `.github/workflows/`, add an `ANTHROPIC_API_KEY` repository
secret, and add a branch protection rule requiring the `ai-review` check.
Full details in [Integration](05-integration.md#4-wire-up-the-ci-gate-the-real-enforcement).

## 6. Add context for better reviews

Copy [`docs/AGENTS.md`](../AGENTS.md) to your repo's root and fill it in —
this is what turns a generic review into one that knows your project's
conventions and traps. See [Integration](05-integration.md#2-add-context-agentsmd).

## Where to go from here

- [Problem](01-problem.md) / [Solution](02-solution.md) if you want the
  reasoning before you customize anything.
- [Implementation](03-implementation.md) if you're modifying the agent loop,
  the diff filtering, or the schema.
- The top-level [README](../../README.md) for the full CLI flag reference.
