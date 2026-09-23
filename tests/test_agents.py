from copy import deepcopy
from types import SimpleNamespace

import pytest

from review_pipeline import agents


def _terminal_response(tool_input, call_id, *, stop_reason="tool_use"):
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_verdict",
                input=tool_input,
                id=call_id,
            )
        ],
        stop_reason=stop_reason,
    )


class _FakeClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return next(self._responses)


def test_agentic_loop_retries_malformed_terminal_input(monkeypatch):
    malformed = {
        "findings": ["not a finding object"],
        "ship_ready": True,
        "rationale": "No issues.",
    }
    valid = {"findings": [], "ship_ready": True, "rationale": "No issues."}
    client = _FakeClient(
        [
            _terminal_response(malformed, "invalid-call"),
            _terminal_response(valid, "valid-call"),
        ]
    )
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    result = agents._agentic_loop(
        system="test",
        user_message="review",
        tools=[agents.SUBMIT_VERDICT_TOOL],
        executors={},
        terminal_tool="submit_verdict",
        repo=".",
        model="test-model",
        max_turns=1,
    )

    assert result == valid
    retry_message = client.calls[1]["messages"][-1]["content"][0]
    assert retry_message["is_error"] is True
    assert "does not match its schema" in retry_message["content"]


def test_agentic_loop_fails_after_malformed_terminal_retry_limit(monkeypatch):
    malformed = {
        "findings": ["not a finding object"],
        "ship_ready": True,
        "rationale": "No issues.",
    }
    attempts = 1 + agents.MAX_TERMINAL_RETRIES
    client = _FakeClient(
        [_terminal_response(malformed, f"invalid-{index}") for index in range(attempts)]
    )
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    with pytest.raises(RuntimeError, match="did not submit valid submit_verdict input"):
        agents._agentic_loop(
            system="test",
            user_message="review",
            tools=[agents.SUBMIT_VERDICT_TOOL],
            executors={},
            terminal_tool="submit_verdict",
            repo=".",
            model="test-model",
            max_turns=1,
        )

    assert len(client.calls) == attempts


def test_agentic_loop_retries_on_truncated_tool_call(monkeypatch):
    """A response cut off mid tool-call still has an open tool_use block --
    the API requires a matching tool_result in the very next turn, so the
    retry must reply in kind (a bare string there gets a 400 on the real
    API)."""
    valid = {"findings": [], "ship_ready": True, "rationale": "No issues."}
    client = _FakeClient(
        [
            _terminal_response({}, "cut-off-call", stop_reason="max_tokens"),
            _terminal_response(valid, "valid-call"),
        ]
    )
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    result = agents._agentic_loop(
        system="test",
        user_message="review",
        tools=[agents.SUBMIT_VERDICT_TOOL],
        executors={},
        terminal_tool="submit_verdict",
        repo=".",
        model="test-model",
        max_turns=2,
    )

    assert result == valid
    retry_message = client.calls[1]["messages"][-1]
    assert retry_message["role"] == "user"
    tool_result = retry_message["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "cut-off-call"
    assert tool_result["is_error"] is True
    assert "cut off" in tool_result["content"]


def test_agentic_loop_retries_on_truncated_text_response(monkeypatch):
    """A response cut off before any tool call has no dangling tool_use, so
    a plain-text retry message is valid."""
    valid = {"findings": [], "ship_ready": True, "rationale": "No issues."}
    truncated_text_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="thinking out loud...")],
        stop_reason="max_tokens",
    )
    client = _FakeClient([truncated_text_response, _terminal_response(valid, "valid-call")])
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    result = agents._agentic_loop(
        system="test",
        user_message="review",
        tools=[agents.SUBMIT_VERDICT_TOOL],
        executors={},
        terminal_tool="submit_verdict",
        repo=".",
        model="test-model",
        max_turns=2,
    )

    assert result == valid
    retry_message = client.calls[1]["messages"][-1]
    assert retry_message["role"] == "user"
    assert "cut off" in retry_message["content"]


def test_agentic_loop_rejects_empty_rationale(monkeypatch):
    empty_rationale = {"findings": [], "ship_ready": True, "rationale": ""}
    valid = {"findings": [], "ship_ready": True, "rationale": "No issues."}
    client = _FakeClient(
        [
            _terminal_response(empty_rationale, "invalid-call"),
            _terminal_response(valid, "valid-call"),
        ]
    )
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    result = agents._agentic_loop(
        system="test",
        user_message="review",
        tools=[agents.SUBMIT_VERDICT_TOOL],
        executors={},
        terminal_tool="submit_verdict",
        repo=".",
        model="test-model",
        max_turns=1,
    )

    assert result == valid
    retry_message = client.calls[1]["messages"][-1]["content"][0]
    assert retry_message["is_error"] is True


def test_agentic_loop_rejects_whitespace_only_rationale(monkeypatch):
    whitespace_rationale = {"findings": [], "ship_ready": True, "rationale": "   "}
    valid = {"findings": [], "ship_ready": True, "rationale": "No issues."}
    client = _FakeClient(
        [
            _terminal_response(whitespace_rationale, "invalid-call"),
            _terminal_response(valid, "valid-call"),
        ]
    )
    monkeypatch.setattr(agents.anthropic, "Anthropic", lambda: client)

    result = agents._agentic_loop(
        system="test",
        user_message="review",
        tools=[agents.SUBMIT_VERDICT_TOOL],
        executors={},
        terminal_tool="submit_verdict",
        repo=".",
        model="test-model",
        max_turns=1,
    )

    assert result == valid
    retry_message = client.calls[1]["messages"][-1]["content"][0]
    assert retry_message["is_error"] is True
