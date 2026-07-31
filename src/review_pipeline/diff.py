"""Git diff helpers and changed-line filtering.

The one invariant every caller relies on: a `blocking` finding must point at a
line that is actually part of the diff. That is enforced here, not left to the
LLM's discretion, so pre-existing repo issues can never gate a PR.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run_git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def repo_root(path: str = ".") -> str:
    try:
        return _run_git(["rev-parse", "--show-toplevel"], cwd=path).strip()
    except GitError as exc:
        raise GitError(f"'{path}' is not inside a git repository") from exc


def restage_tracked_changes(repo: str, paths: list[str]) -> None:
    """`git add -u -- <paths>`: re-stage the Coder's edits to the given,
    already-tracked files only.

    The Coder writes to the working tree, not the index. In --staged mode
    that means a follow-up `git diff --cached` wouldn't see the fix at all
    (it would still show the original, unfixed staged diff) unless we
    restage. Scoped to `paths` (the files the diff being fixed touched) so
    this doesn't sweep in unrelated unstaged changes elsewhere in the repo.
    Only touches already-tracked files -- if a fix needs a new file, it
    won't be picked up here.
    """
    if not paths:
        return
    _run_git(["add", "-u", "--", *paths], cwd=repo)


def get_diff(
    repo: str,
    *,
    base: str | None = None,
    commit_range: str | None = None,
    staged: bool = False,
) -> str:
    """Return a unified diff for the requested scope.

    Precedence: commit_range > base > staged > full working tree (HEAD).
    `base` diffs against the merge-base with HEAD (i.e. "what this branch
    introduced"), matching how a PR diff is computed in CI.
    """
    # Force the standard a/ b/ prefixes regardless of the user's/repo's
    # diff.noprefix or diff.mnemonicPrefix config -- parse_changed_lines
    # assumes "+++ b/..." headers, and a differently-configured prefix would
    # silently make every header fail to match.
    prefix_args = ["--src-prefix=a/", "--dst-prefix=b/"]
    if commit_range:
        args = ["diff", *prefix_args, commit_range]
    elif base:
        args = ["diff", *prefix_args, f"{base}...HEAD"]
    elif staged:
        args = ["diff", *prefix_args, "--cached"]
    else:
        args = ["diff", *prefix_args, "HEAD"]
    return _run_git(args, cwd=repo)


@dataclass
class ChangedLines:
    """Per-file sets of line numbers (in the *new* version of the file) that
    were added or modified by the diff."""

    files: dict[str, set[int]] = field(default_factory=dict)

    def contains(self, file: str, line: int) -> bool:
        lines = self.files.get(file)
        if lines is None:
            # Try matching by basename in case the model reported a
            # relative path with different leading components. Only trust
            # this if exactly one changed file matches -- if two changed
            # files share a basename, picking either one by chance would be
            # worse than not matching at all.
            candidates = [
                lset
                for fname, lset in self.files.items()
                if fname.endswith(f"/{file}") or file.endswith(f"/{fname}")
            ]
            if len(candidates) == 1:
                lines = candidates[0]
        return bool(lines) and line in lines

    def touched_files(self) -> list[str]:
        return sorted(self.files.keys())


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_FILE_HEADER_RE = re.compile(r"^\+\+\+ b/(.+)$")


def parse_changed_lines(diff_text: str) -> ChangedLines:
    changed = ChangedLines()
    current_file: str | None = None
    new_line_no: int | None = None

    for line in diff_text.splitlines():
        header_match = _FILE_HEADER_RE.match(line)
        if header_match:
            current_file = header_match.group(1)
            if current_file == "/dev/null":
                current_file = None
            else:
                changed.files.setdefault(current_file, set())
            new_line_no = None
            continue

        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            new_line_no = int(hunk_match.group(1))
            continue

        if new_line_no is None or current_file is None:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            changed.files[current_file].add(new_line_no)
            new_line_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass  # removed line: does not exist in the new file, no counter change
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            new_line_no += 1

    return changed


def clamp_blocking_to_diff(findings_data: dict, diff_text: str) -> dict:
    """Demote any `blocking`/`major` finding whose file+line falls outside the
    diff's changed lines to `minor`. Pre-existing issues can be surfaced for
    visibility, but they must never carry gating severity."""
    changed = parse_changed_lines(diff_text)
    for finding in findings_data.get("findings", []):
        if finding.get("severity") not in ("blocking", "major"):
            continue
        if "file" not in finding or "line" not in finding:
            # Malformed finding (missing a required key) -- leave it as-is
            # and let schema validation reject it with a clean error instead
            # of crashing here with a raw KeyError.
            continue
        if not changed.contains(finding["file"], finding["line"]):
            finding["severity"] = "minor"
            if not finding.get("summary", "").startswith("[outside diff]"):
                finding["summary"] = f"[outside diff] {finding.get('summary', '')}"
    return findings_data


def diff_touched_dirs(diff_text: str) -> list[str]:
    dirs: set[str] = set()
    for line in diff_text.splitlines():
        match = _FILE_HEADER_RE.match(line)
        if match and match.group(1) != "/dev/null":
            path = match.group(1)
            parts = path.rsplit("/", 1)
            dirs.add(parts[0] if len(parts) == 2 else ".")
    return sorted(dirs)


def gather_context(repo: str, diff_text: str) -> tuple[str, str]:
    """Find the context the model should read alongside the diff: the repo's
    AGENTS.md if present, otherwise README(s) in directories the diff touched."""
    agents_md = Path(repo) / "AGENTS.md"
    if agents_md.exists():
        return ("AGENTS.md", agents_md.read_text(encoding="utf-8", errors="replace"))

    sections = []
    for rel_dir in diff_touched_dirs(diff_text):
        for name in ("README.md", "README", "readme.md"):
            candidate = Path(repo) / rel_dir / name
            if candidate.exists():
                sections.append(
                    f"## {rel_dir}/{name}\n\n{candidate.read_text(encoding='utf-8', errors='replace')}"
                )
                break
    if sections:
        return ("directory README(s) of touched files", "\n\n".join(sections))
    return ("none found", "(no AGENTS.md or README found near touched files; reviewing the diff in isolation)")
