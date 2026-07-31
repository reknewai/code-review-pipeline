#!/usr/bin/env bash
# Wrapper invoked by the `review-pipeline` pre-commit hook (see
# .pre-commit-config.yaml / .pre-commit-hooks.yaml). Runs review-fix against
# staged changes and hard-blocks the commit on remaining blocking findings.
#
# This is a fast local convenience gate, NOT the real enforcement point --
# that's CI + branch protection (see .github/workflows/verify.yml). It can be
# bypassed on purpose:
#   - `git commit --no-verify` skips this (and every) pre-commit hook silently.
#   - `SKIP_REVIEW=1 git commit ...` skips just this hook, and logs a warning
#     so the bypass isn't silent.
set -euo pipefail

if [ "${SKIP_REVIEW:-}" = "1" ]; then
  echo "WARNING: SKIP_REVIEW=1 -- bypassing the review-pipeline pre-commit gate." >&2
  echo "WARNING: this is a local fast-feedback check only; CI (verify.yml) still runs." >&2
  exit 0
fi

exec review-pipeline review-fix --staged
