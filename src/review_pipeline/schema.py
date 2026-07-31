"""Validation for the findings.json contract shared by review/fix/verify."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import jsonschema

SEVERITIES = ("blocking", "major", "minor", "nit")
CATEGORIES = ("correctness", "security", "breaking-change", "style", "nit")
GATING_CATEGORIES = ("correctness", "security", "breaking-change")


class FindingsValidationError(ValueError):
    pass


def _load_schema() -> dict[str, Any]:
    raw = resources.files("review_pipeline.schemas").joinpath("findings.schema.json").read_text()
    return json.loads(raw)


_SCHEMA = _load_schema()


def validate_findings(data: dict[str, Any], *, require_rationale: bool = False) -> None:
    """Raise FindingsValidationError if `data` does not match the findings.json contract."""
    try:
        jsonschema.validate(instance=data, schema=_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise FindingsValidationError(f"findings.json failed schema validation: {exc.message}") from exc

    if require_rationale and "rationale" not in data:
        raise FindingsValidationError("verify output must include a top-level 'rationale' string")


def load_findings(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    validate_findings(data)
    return data


def save_findings(path: str, data: dict[str, Any], *, require_rationale: bool = False) -> None:
    validate_findings(data, require_rationale=require_rationale)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def has_blocking(data: dict[str, Any], categories: tuple[str, ...] | None = None) -> bool:
    for finding in data.get("findings", []):
        if finding["severity"] != "blocking":
            continue
        if categories is not None and finding["category"] not in categories:
            continue
        return True
    return False


def summarize(data: dict[str, Any]) -> str:
    counts: dict[str, int] = {sev: 0 for sev in SEVERITIES}
    for finding in data.get("findings", []):
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    parts = [f"{sev}={counts[sev]}" for sev in SEVERITIES]
    return ", ".join(parts)
