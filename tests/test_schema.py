import pytest

from review_pipeline.schema import (
    FindingsValidationError,
    has_blocking,
    validate_findings,
)


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


def test_has_blocking_respects_category_filter():
    data = {
        "findings": [_finding(severity="blocking", category="style")],
        "ship_ready": False,
    }
    assert has_blocking(data) is True
    assert has_blocking(data, categories=("correctness", "security", "breaking-change")) is False
