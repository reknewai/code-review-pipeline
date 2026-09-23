from pathlib import Path

import pytest

from review_pipeline.schema import (
    FindingsValidationError,
    finding_schema,
    has_blocking,
    validate_findings,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _finding(**overrides):
    base = {
        "file": "a.py",
        "line": 1,
        "severity": "blocking",
        "category": "correctness",
        "summary": "s",
        "failure_scenario": "f",
    }
    base.update(overrides)
    return base


def test_validate_findings_accepts_minimal_valid_doc():
    validate_findings({"findings": [], "ship_ready": True})


def test_validate_findings_accepts_full_finding():
    validate_findings({"findings": [_finding(suggested_fix="do x")], "ship_ready": False})


def test_validate_findings_rejects_bad_severity():
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": [_finding(severity="catastrophic")], "ship_ready": False})


def test_validate_findings_rejects_missing_required_field():
    bad = _finding()
    del bad["failure_scenario"]
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": [bad], "ship_ready": False})


def test_validate_findings_requires_ship_ready():
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": []})


def test_validate_findings_rejects_unknown_top_level_field():
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": [], "ship_ready": True, "extra_field": 1})


def test_validate_findings_requires_non_empty_rationale():
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": [], "ship_ready": False, "rationale": ""}, require_rationale=True)


def test_validate_findings_rejects_whitespace_only_rationale():
    with pytest.raises(FindingsValidationError):
        validate_findings({"findings": [], "ship_ready": False, "rationale": "   "}, require_rationale=True)


def test_validate_findings_accepts_non_empty_rationale():
    validate_findings({"findings": [], "ship_ready": True, "rationale": "clean"}, require_rationale=True)


def test_finding_schema_matches_the_one_findings_json_is_validated_against():
    # agents.py's tool schemas reuse this so the LLM's structured output
    # can't drift from the contract save_findings actually enforces.
    schema = finding_schema()
    assert schema["required"] == [
        "file",
        "line",
        "severity",
        "category",
        "summary",
        "failure_scenario",
    ]
    assert schema["properties"]["file"]["minLength"] == 1
    assert schema["properties"]["line"]["minimum"] == 1


def test_root_and_packaged_findings_schema_copies_stay_in_sync():
    # schemas/findings.schema.json (linked from README.md/docs) and
    # src/review_pipeline/schemas/findings.schema.json (the one actually
    # loaded at runtime via importlib.resources) are two independently
    # edited copies of the same contract -- nothing else catches drift
    # between them if only one gets updated.
    root_copy = (_REPO_ROOT / "schemas" / "findings.schema.json").read_text()
    packaged_copy = (_REPO_ROOT / "src" / "review_pipeline" / "schemas" / "findings.schema.json").read_text()
    assert root_copy == packaged_copy


def test_has_blocking_respects_category_filter():
    data = {
        "findings": [_finding(severity="blocking", category="style")],
        "ship_ready": False,
    }
    assert has_blocking(data) is True
    assert has_blocking(data, categories=("correctness", "security", "breaking-change")) is False
