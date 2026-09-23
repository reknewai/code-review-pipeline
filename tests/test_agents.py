from copy import deepcopy
from types import SimpleNamespace

import pytest

from review_pipeline import agents


def _terminal_response(tool_input, call_id):
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_verdict",
                input=tool_input,
                id=call_id,
            )
        ]
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
