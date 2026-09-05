"""Strict bounded result report models and report_result behavior.

Phase 3 result reporting. Covers the strict ``ThreadResultReportCreate``
payload bounds, the draft insert/update/conflict/replay state machine, and
the hidden-not-found authorization surface.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_minimal_report_payload_defaults_tests_and_warnings() -> None:
    """One minimal report payload validates with empty tests and warnings."""
    from yinshi.models import ThreadResultReportCreate

    body = ThreadResultReportCreate(expected_version=0, summary="All checks passed.")
    assert body.expected_version == 0
    assert body.summary == "All checks passed."
    assert body.tests == []
    assert body.warnings == []


def test_report_summary_is_stripped_and_blank_is_rejected() -> None:
    """Summaries store trimmed and whitespace-only values fail validation."""
    from yinshi.models import ThreadResultReportCreate

    body = ThreadResultReportCreate(expected_version=0, summary="  done  ")
    assert body.summary == "done"
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(expected_version=0, summary="   ")


def test_report_summary_rejects_over_twenty_thousand_chars() -> None:
    """Summaries above 20,000 characters fail validation."""
    from yinshi.models import ThreadResultReportCreate

    with pytest.raises(ValidationError):
        ThreadResultReportCreate(expected_version=0, summary="x" * 20_001)
    body = ThreadResultReportCreate(expected_version=0, summary="x" * 20_000)
    assert len(body.summary) == 20_000


def test_report_expected_version_rejects_negative() -> None:
    """expected_version must be zero or a positive integer."""
    from yinshi.models import ThreadResultReportCreate

    with pytest.raises(ValidationError):
        ThreadResultReportCreate(expected_version=-1, summary="ok")
    body = ThreadResultReportCreate(expected_version=0, summary="ok")
    assert body.expected_version == 0


def test_report_tests_are_strict_bounded_objects() -> None:
    """Test entries validate command/status/summary strictly and forbid extras."""
    from yinshi.models import ThreadResultReportCreate, ThreadResultReportTest

    body = ThreadResultReportCreate(
        expected_version=1,
        summary="ok",
        tests=[
            {"command": "pytest -q", "status": "passed"},
            {"command": "pytest -q", "status": "failed", "summary": "boom"},
            {"command": "pytest -q", "status": "skipped"},
        ],
    )
    assert all(isinstance(test, ThreadResultReportTest) for test in body.tests)
    assert body.tests[1].summary == "boom"
    assert body.tests[2].summary is None
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(
            expected_version=1,
            summary="ok",
            tests=[{"command": "pytest -q", "status": "green"}],
        )
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(
            expected_version=1,
            summary="ok",
            tests=[{"command": "pytest -q", "status": "passed", "duration_ms": 5}],
        )
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(
            expected_version=1,
            summary="ok",
            tests=[{"command": "x" * 2_001, "status": "passed"}],
        )
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(
            expected_version=1,
            summary="ok",
            tests=[{"command": "pytest -q", "status": "passed", "summary": "x" * 5_001}],
        )


def test_report_tests_list_caps_at_fifty_entries() -> None:
    """The 51st test entry fails validation."""
    from yinshi.models import ThreadResultReportCreate

    with pytest.raises(ValidationError):
        ThreadResultReportCreate(
            expected_version=0,
            summary="s",
            tests=[{"command": "pytest -q", "status": "passed"} for _ in range(51)],
        )


def test_report_warnings_reject_fifty_one_entries() -> None:
    """A warnings list with 51 entries fails validation."""
    from yinshi.models import ThreadResultReportCreate

    warnings = [f"w{index}" for index in range(51)]
    with pytest.raises(ValidationError):
        ThreadResultReportCreate(expected_version=0, summary="s", warnings=warnings)


def test_report_rejects_warning_of_2001_chars() -> None:
    """A warning of 2,001 characters fails validation."""
    from yinshi.models import ThreadResultReportCreate

    with pytest.raises(ValidationError):
        ThreadResultReportCreate(expected_version=0, summary="s", warnings=["x" * 2001])
