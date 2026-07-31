from review_pipeline.diff import clamp_blocking_to_diff, parse_changed_lines
from tests.conftest import git


def test_parse_changed_lines_marks_added_lines_only(scratch_repo):
    (scratch_repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    # a comment\n"
        "    return a - b\n"  # bug: should be a + b
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    diff_text = git("diff", cwd=scratch_repo)
    changed = parse_changed_lines(diff_text)

    assert "calc.py" in changed.files
    # line 2 ("# a comment") and 3 (the buggy return) are new/changed lines
    assert changed.contains("calc.py", 2)
    assert changed.contains("calc.py", 3)
    # lines 5-6 (new function) are also added
    assert changed.contains("calc.py", 5)
    assert changed.contains("calc.py", 6)


def test_clamp_demotes_blocking_finding_outside_diff(scratch_repo):
    (scratch_repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    diff_text = git("diff", cwd=scratch_repo)

    data = {
        "findings": [
            {
                "file": "calc.py",
                "line": 2,  # inside the diff (unchanged context line though -- still not "added")
                "severity": "blocking",
                "category": "correctness",
                "summary": "pre-existing issue, not part of this diff",
                "failure_scenario": "n/a",
            },
            {
                "file": "calc.py",
                "line": 5,  # inside the diff, an actually-added line
                "severity": "blocking",
                "category": "correctness",
                "summary": "real bug in new code",
                "failure_scenario": "mul(2, 3) misbehaves",
            },
        ],
        "ship_ready": False,
    }
    result = clamp_blocking_to_diff(data, diff_text)

    by_summary = {f["summary"].split("] ")[-1]: f for f in result["findings"]}
    assert by_summary["pre-existing issue, not part of this diff"]["severity"] == "minor"
    assert by_summary["real bug in new code"]["severity"] == "blocking"


def test_clamp_leaves_minor_and_nit_alone(scratch_repo):
    (scratch_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    diff_text = git("diff", cwd=scratch_repo)
    data = {
        "findings": [
            {
                "file": "nonexistent.py",
                "line": 999,
                "severity": "nit",
                "category": "style",
                "summary": "stylistic nit far outside the diff",
                "failure_scenario": "n/a",
            }
        ],
        "ship_ready": True,
    }
    result = clamp_blocking_to_diff(data, diff_text)
    assert result["findings"][0]["severity"] == "nit"
