"""Reviewer / Coder / Verifier: three independent LLM roles built on a single
small tool-use agent loop.

Every call into the model goes through `_agentic_loop`, which is the entire
"agent" here: read files, (for the coder) write files, and terminate by
calling a role-specific structured-output tool. No third-party agent
framework, so swapping models/providers later means touching only this file.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
from jsonschema import ValidationError, validate

from review_pipeline.schema import finding_schema

DEFAULT_MODEL = os.environ.get("REVIEW_PIPELINE_MODEL", "claude-sonnet-5")
MAX_TURNS = 8
MAX_TERMINAL_RETRIES = 2
MAX_OUTPUT_TOKENS = 16384

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

# Reuse the canonical finding schema (schemas/findings.schema.json) rather
# than hand-duplicating it here -- a second, looser copy is how a finding
# with e.g. an empty `summary` or `line: 0` used to pass this tool's
# validation and only blow up later in save_findings.
_FINDING_SCHEMA = finding_schema()

READ_FILE_TOOL = {
    "name": "read_file",
    "description": "Read a UTF-8 text file from the repository, path relative to the repo root. Returns line-numbered content.",
    "input_schema": {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    },
}

LIST_DIR_TOOL = {
    "name": "list_dir",
    "description": "List files and directories at a path relative to the repo root.",
    "input_schema": {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    },
}

WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": (
        "Overwrite a file's full contents, path relative to the repo root. "
        "Creates the file (and parent directories) if it doesn't exist. "
        "Always pass the COMPLETE new file contents, never a diff or partial snippet."
    ),
    "input_schema": {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
    },
}

SUBMIT_FINDINGS_TOOL = {
    "name": "submit_findings",
    "description": (
        "Submit your final, complete list of review findings. Call this exactly once, "
        "after you've read whatever context you need. This ends the review."
    ),
    "input_schema": {
        "type": "object",
        "required": ["findings", "ship_ready"],
        "properties": {
            "findings": {"type": "array", "items": _FINDING_SCHEMA},
            "ship_ready": {
                "type": "boolean",
                "description": "Your own verdict: true if there are no blocking findings.",
            },
        },
    },
}

SUBMIT_VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": (
        "Submit your final, complete list of findings plus your independent ship_ready verdict. "
        "Call this exactly once. This ends the verification."
    ),
    "input_schema": {
        "type": "object",
        "required": ["findings", "ship_ready", "rationale"],
        "properties": {
            "findings": {"type": "array", "items": _FINDING_SCHEMA},
            "ship_ready": {"type": "boolean"},
            "rationale": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": "One sentence explaining the verdict. Must not be empty or whitespace-only.",
            },
        },
    },
}

FINISH_TOOL = {
    "name": "finish",
    "description": "Call this once you've applied all the fixes needed for the given findings.",
    "input_schema": {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string", "description": "What you changed, one sentence."}},
    },
}

# ---------------------------------------------------------------------------
# Sandboxed filesystem tool executors
# ---------------------------------------------------------------------------


class ToolError(RuntimeError):
    pass


def _safe_path(repo: str, rel_path: str) -> Path:
    root = Path(repo).resolve()
    candidate = (root / rel_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"path '{rel_path}' escapes the repository root")
    return candidate


def _tool_read_file(repo: str, tool_input: dict) -> str:
    path = _safe_path(repo, tool_input["path"])
    if not path.exists():
        return f"ERROR: no such file: {tool_input['path']}"
    if path.is_dir():
        return f"ERROR: '{tool_input['path']}' is a directory, use list_dir"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    truncated = len(lines) > 4000
    if truncated:
        lines = lines[:4000]
    numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    if truncated:
        numbered += "\n... [truncated at 4000 lines]"
    return numbered


def _tool_list_dir(repo: str, tool_input: dict) -> str:
    path = _safe_path(repo, tool_input["path"] or ".")
    if not path.exists():
        return f"ERROR: no such path: {tool_input['path']}"
    if not path.is_dir():
        return f"ERROR: '{tool_input['path']}' is not a directory"
    entries = sorted(
        p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if p.name != ".git"
    )
    return "\n".join(entries) if entries else "(empty directory)"


def _tool_write_file(repo: str, tool_input: dict) -> str:
    path = _safe_path(repo, tool_input["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tool_input["content"], encoding="utf-8")
    return f"wrote {tool_input['path']} ({len(tool_input['content'])} bytes)"


_READ_ONLY_EXECUTORS: dict[str, Callable[[str, dict], str]] = {
    "read_file": _tool_read_file,
    "list_dir": _tool_list_dir,
}

_CODER_EXECUTORS: dict[str, Callable[[str, dict], str]] = {
    **_READ_ONLY_EXECUTORS,
    "write_file": _tool_write_file,
}

# ---------------------------------------------------------------------------
# Generic agent loop
# ---------------------------------------------------------------------------


def _agentic_loop(
    *,
    system: str,
    user_message: str,
    tools: list[dict],
    executors: dict[str, Callable[[str, dict], str]],
    terminal_tool: str,
    repo: str,
    model: str,
    max_turns: int = MAX_TURNS,
) -> dict[str, Any]:
    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": user_message}]
    terminal_schema = next(
        (tool["input_schema"] for tool in tools if tool["name"] == terminal_tool), None
    )
    if terminal_schema is None:
        raise ValueError(f"terminal_tool {terminal_tool!r} is not in the tools list")

    total_turns = max_turns + MAX_TERMINAL_RETRIES
    for turn in range(total_turns):
        force_terminal = turn >= max_turns - 1
        create_kwargs: dict[str, Any] = {}
        if force_terminal:
            create_kwargs["tool_choice"] = {"type": "tool", "name": terminal_tool}

        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            **create_kwargs,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "max_tokens":
            # The response was cut off mid-generation -- any tool call in it
            # may be truncated/invalid JSON. Don't try to parse it; ask for a
            # shorter retry instead, same as a malformed-output retry. If a
            # tool_use block is open, the API requires a matching tool_result
            # in the very next turn -- a bare string reply there gets
            # rejected with a 400, so reply in kind for every open tool_use.
            truncated_tool_uses = [block for block in response.content if block.type == "tool_use"]
            error_text = (
                "ERROR: your previous response was cut off after hitting the "
                "token limit, so any tool call in it may be incomplete. "
                "Respond again, more concisely."
            )
            if truncated_tool_uses:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": error_text,
                                "is_error": True,
                            }
                            for tool_use in truncated_tool_uses
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": error_text})
            continue

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            messages.append(
                {
                    "role": "user",
                    "content": f"Continue. When finished, call {terminal_tool} exactly once.",
                }
            )
            continue

        terminal_call = next((tu for tu in tool_uses if tu.name == terminal_tool), None)
        if terminal_call is not None:
            try:
                validate(instance=terminal_call.input, schema=terminal_schema)
            except ValidationError as exc:
                error_text = (
                    f"ERROR: {terminal_tool} input does not match its schema: "
                    f"{exc.message}. Call {terminal_tool} again with corrected input."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": (
                                    error_text
                                    if tool_use.id == terminal_call.id
                                    else "ERROR: ignored because the terminal output was invalid."
                                ),
                                "is_error": True,
                            }
                            for tool_use in tool_uses
                        ],
                    }
                )
                continue
            return terminal_call.input

        tool_results = []
        for tool_use in tool_uses:
            executor = executors.get(tool_use.name)
            if executor is None:
                result_text = f"ERROR: unknown tool '{tool_use.name}'"
            else:
                try:
                    result_text = executor(repo, tool_use.input)
                except (ToolError, OSError, ValueError) as exc:
                    # OSError/ValueError: unusual paths (null bytes, writing
                    # to a directory, etc.) that _safe_path/read_text/
                    # write_text can raise but that aren't a sandbox escape
                    # -- degrade to a tool error instead of crashing the loop.
                    result_text = f"ERROR: {exc}"
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tool_use.id, "content": result_text}
            )
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(
        f"agent did not submit valid {terminal_tool} input within {total_turns} turns"
    )


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

SYSTEM_REVIEWER = """You are an independent, rigorous senior code reviewer.

You review only the CHANGES in a git diff -- not pre-existing code you happen \
to notice. For every finding, `line` must be a line that appears as an \
added/modified line in the diff, numbered in the new version of the file.

Be concrete, not generic. Every finding needs a `failure_scenario`: a \
specific input or state that produces a wrong result or a crash. If you \
can't state one, it's not `blocking` or `major` -- downgrade it to `minor` \
or `nit`, or drop it.

Severity guide:
- blocking: will break in a real, reachable scenario (bug, security hole, \
  breaking API/behavior change) -- must be fixed before shipping.
- major: a real but narrower correctness/robustness issue, or a breaking \
  change with a workaround.
- minor: worth fixing, not urgent.
- nit: style/preference.

You may call read_file / list_dir to see how a changed function is used \
elsewhere, or to read the file the diff is a fragment of, before you decide. \
Don't guess at code you haven't read. When you're done, call \
submit_findings exactly once with your complete finding list."""


def _reviewer_user_message(diff_text: str, context_label: str, context_text: str) -> str:
    return f"""# Repository context ({context_label})

{context_text}

# Diff to review

```diff
{diff_text}
```

Review only the changed lines above. Use read_file/list_dir if you need to \
see more of a file's surrounding code before judging. Then call \
submit_findings."""


def run_reviewer(
    *, repo: str, diff_text: str, context_label: str, context_text: str, model: str = DEFAULT_MODEL
) -> dict[str, Any]:
    result = _agentic_loop(
        system=SYSTEM_REVIEWER,
        user_message=_reviewer_user_message(diff_text, context_label, context_text),
        tools=[READ_FILE_TOOL, LIST_DIR_TOOL, SUBMIT_FINDINGS_TOOL],
        executors=_READ_ONLY_EXECUTORS,
        terminal_tool="submit_findings",
        repo=repo,
        model=model,
    )
    return {"findings": result.get("findings", []), "ship_ready": bool(result.get("ship_ready", False))}


# ---------------------------------------------------------------------------
# Verifier (independent second round -- must never see round-1 artifacts)
# ---------------------------------------------------------------------------

SYSTEM_VERIFIER = """You are the independent second-opinion reviewer, running \
in CI as the final gate before merge. You have not seen, and must assume \
nothing about, any prior review or fix that may have happened on this branch \
-- you are judging this diff completely fresh, as if it were the first and \
only review it will ever get.

You review only the CHANGES in a git diff -- not pre-existing code you \
happen to notice. For every finding, `line` must be a line that appears as \
an added/modified line in the diff, numbered in the new version of the file.

Be concrete, not generic. Every finding needs a `failure_scenario`: a \
specific input or state that produces a wrong result or a crash. If you \
can't state one, it's not `blocking` or `major`.

Severity guide:
- blocking: will break in a real, reachable scenario (bug, security hole, \
  breaking API/behavior change) -- must be fixed before shipping.
- major: a real but narrower correctness/robustness issue, or a breaking \
  change with a workaround.
- minor: worth fixing, not urgent.
- nit: style/preference.

Set `ship_ready` to your own honest, independent verdict: false if there is \
any `blocking` finding in the `correctness`, `security`, or `breaking-change` \
categories; true otherwise. Give a one-sentence `rationale` for that verdict.

You may call read_file / list_dir to see how a changed function is used \
elsewhere before you decide. When done, call submit_verdict exactly once."""


def _verifier_user_message(diff_text: str, context_label: str, context_text: str) -> str:
    return f"""# Repository context ({context_label})

{context_text}

# Diff to verify

```diff
{diff_text}
```

Judge this diff independently. Use read_file/list_dir if needed, then call \
submit_verdict."""


def run_verifier(
    *, repo: str, diff_text: str, context_label: str, context_text: str, model: str = DEFAULT_MODEL
) -> dict[str, Any]:
    result = _agentic_loop(
        system=SYSTEM_VERIFIER,
        user_message=_verifier_user_message(diff_text, context_label, context_text),
        tools=[READ_FILE_TOOL, LIST_DIR_TOOL, SUBMIT_VERDICT_TOOL],
        executors=_READ_ONLY_EXECUTORS,
        terminal_tool="submit_verdict",
        repo=repo,
        model=model,
    )
    return {
        "findings": result.get("findings", []),
        "ship_ready": bool(result.get("ship_ready", False)),
        "rationale": result.get("rationale", ""),
    }


# ---------------------------------------------------------------------------
# Coder
# ---------------------------------------------------------------------------

SYSTEM_CODER = """You are a careful, minimal-diff coder. You are given a list \
of code review findings (already filtered to `blocking` and `major` only) \
and must fix the underlying problems -- nothing else.

Rules:
- Fix only what the findings describe. Do not refactor, rename, reformat, or \
  "improve" unrelated code.
- Before editing a file, read_file it first so you edit the real current \
  contents, not what you assume is there.
- write_file always takes the COMPLETE new file contents.
- Do not add speculative error handling, comments, or abstractions beyond \
  what's needed to fix the finding.
- When every finding has been addressed, call finish with a one-sentence \
  summary of what you changed."""


def _coder_user_message(diff_text: str, findings: list[dict], context_label: str, context_text: str) -> str:
    findings_lines = []
    for f in findings:
        findings_lines.append(
            f"- [{f['severity']}/{f['category']}] {f['file']}:{f['line']} -- {f['summary']}\n"
            f"  failure scenario: {f['failure_scenario']}"
            + (f"\n  suggested fix: {f['suggested_fix']}" if f.get("suggested_fix") else "")
        )
    findings_block = "\n".join(findings_lines)
    return f"""# Repository context ({context_label})

{context_text}

# Original diff (for reference -- files may have shifted since)

```diff
{diff_text}
```

# Findings to fix (blocking/major only)

{findings_block}

Read each affected file, apply the minimal fix, then call finish."""


def run_coder(
    *,
    repo: str,
    diff_text: str,
    findings: list[dict],
    context_label: str,
    context_text: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    return _agentic_loop(
        system=SYSTEM_CODER,
        user_message=_coder_user_message(diff_text, findings, context_label, context_text),
        tools=[READ_FILE_TOOL, LIST_DIR_TOOL, WRITE_FILE_TOOL, FINISH_TOOL],
        executors=_CODER_EXECUTORS,
        terminal_tool="finish",
        repo=repo,
        model=model,
        max_turns=16,
    )
