"""Scope-safe upkeep measurement and an explicitly synthetic correctness harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any


class MaintenanceError(ValueError):
    """Raised when upkeep measurements are malformed or would overclaim."""


SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
SCOPE_MAX_SENSITIVITY = {"public": 0, "personal": 1, "private": 2}


def _scope(scope: object) -> int:
    if not isinstance(scope, str):
        raise MaintenanceError("scope must be a string")
    try:
        return SCOPE_MAX_SENSITIVITY[scope]
    except KeyError as error:
        raise MaintenanceError(f"unknown scope: {scope}") from error


def _rows(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], table: str
) -> list[dict[str, Any]]:
    if not isinstance(rows, Mapping):
        raise MaintenanceError("rows must be a mapping")
    value = rows.get(table, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MaintenanceError(f"{table} must be a sequence of records")
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise MaintenanceError(f"{table} contains a non-record")
        result.append(dict(row))
    return result


def _visible(row: Mapping[str, Any], maximum: int, table: str) -> bool:
    sensitivity = row.get("sensitivity")
    if not isinstance(sensitivity, str):
        raise MaintenanceError(f"{table} has invalid sensitivity")
    try:
        return SENSITIVITY_RANK[sensitivity] <= maximum
    except KeyError as error:
        raise MaintenanceError(f"{table} has invalid sensitivity") from error


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaintenanceError(f"{field} must be a non-empty string")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MaintenanceError(f"{field} must be a non-negative integer")
    return value


def _canonical_date(value: object, field: str) -> date:
    text = _required_text(value, field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise MaintenanceError(f"{field} must be a canonical ISO date") from error
    if parsed.isoformat() != text:
        raise MaintenanceError(f"{field} must be a canonical ISO date")
    return parsed


def _aware_datetime(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise MaintenanceError(f"{field} must be an ISO timestamp with timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaintenanceError(f"{field} must be an ISO timestamp with timezone")
    return parsed


def measured_elapsed_seconds(
    *,
    started_at: object,
    started_monotonic_ns: object,
    finished_at: object,
    finished_monotonic_ns: object,
    explicit_elapsed_seconds: object = None,
) -> int:
    """Resolve one integer duration, detecting reboot or material wall-clock drift."""
    if explicit_elapsed_seconds is not None:
        return _nonnegative_integer(explicit_elapsed_seconds, "explicit_elapsed_seconds")
    start_wall = _aware_datetime(started_at, "started_at")
    finish_wall = _aware_datetime(finished_at, "finished_at")
    start_tick = _nonnegative_integer(started_monotonic_ns, "started_monotonic_ns")
    finish_tick = _nonnegative_integer(finished_monotonic_ns, "finished_monotonic_ns")
    wall_seconds = (finish_wall - start_wall).total_seconds()
    monotonic_seconds = (finish_tick - start_tick) / 1_000_000_000
    if wall_seconds < 0 or monotonic_seconds < 0:
        raise MaintenanceError(
            "maintenance clock continuity was lost; provide explicit_elapsed_seconds"
        )
    if abs(wall_seconds - monotonic_seconds) > 5:
        raise MaintenanceError(
            "maintenance clocks disagree; provide explicit_elapsed_seconds"
        )
    return int(round(monotonic_seconds))


def _week_start(performed_on: date) -> str:
    return (performed_on - timedelta(days=performed_on.weekday())).isoformat()


def maintenance_report(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], *, scope: str = "private"
) -> dict[str, object]:
    """Aggregate visible session-level effort without multiplying it by item links."""
    maximum = _scope(scope)
    sessions = _rows(rows, "maintenance_sessions")
    links = _rows(rows, "maintenance_session_items")
    items = _rows(rows, "items")
    evidence = _rows(rows, "evidence")

    visible_item_ids = {
        item["item_id"]
        for item in items
        if isinstance(item.get("item_id"), str) and _visible(item, maximum, "items")
    }
    visible_evidence_ids = {
        record["evidence_id"]
        for record in evidence
        if isinstance(record.get("evidence_id"), str)
        and _visible(record, maximum, "evidence")
    }
    item_ids_by_session: dict[str, set[str]] = {}
    for link in links:
        session_id, item_id = link.get("maintenance_session_id"), link.get("item_id")
        if isinstance(session_id, str) and item_id in visible_item_ids:
            item_ids_by_session.setdefault(session_id, set()).add(str(item_id))

    report_sessions: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in sessions:
        session_id = _required_text(row.get("maintenance_session_id"), "maintenance_session_id")
        if session_id in seen:
            raise MaintenanceError("maintenance_session_id values must be unique")
        seen.add(session_id)
        if not _visible(row, maximum, "maintenance_sessions"):
            continue
        evidence_id = row.get("evidence_id")
        if evidence_id not in visible_evidence_ids:
            continue
        performed_on = _canonical_date(row.get("performed_on"), "performed_on")
        record = {
            "maintenance_session_id": session_id,
            "performed_on": performed_on.isoformat(),
            "week_start": _week_start(performed_on),
            "activity": _required_text(row.get("activity"), "activity"),
            "elapsed_seconds": _nonnegative_integer(
                row.get("elapsed_seconds"), "elapsed_seconds"
            ),
            "correction_count": _nonnegative_integer(
                row.get("correction_count"), "correction_count"
            ),
            "review_count": _nonnegative_integer(row.get("review_count"), "review_count"),
            "evidence_id": evidence_id,
            "item_ids": sorted(item_ids_by_session.get(session_id, set())),
        }
        report_sessions.append(record)
    report_sessions.sort(
        key=lambda record: (
            str(record["performed_on"]),
            str(record["maintenance_session_id"]),
        )
    )

    weeks: dict[str, dict[str, int | str]] = {}
    for session in report_sessions:
        week = str(session["week_start"])
        bucket = weeks.setdefault(
            week,
            {
                "week_start": week,
                "session_count": 0,
                "elapsed_seconds": 0,
                "correction_count": 0,
                "review_count": 0,
                "item_link_count": 0,
            },
        )
        bucket["session_count"] += 1  # type: ignore[operator]
        for field in ("elapsed_seconds", "correction_count", "review_count"):
            bucket[field] += session[field]  # type: ignore[operator]
        bucket["item_link_count"] += len(session["item_ids"])  # type: ignore[arg-type,operator]
    weekly = [weeks[key] for key in sorted(weeks)]
    summary = {
        "session_count": len(report_sessions),
        "elapsed_seconds": sum(int(row["elapsed_seconds"]) for row in report_sessions),
        "correction_count": sum(int(row["correction_count"]) for row in report_sessions),
        "review_count": sum(int(row["review_count"]) for row in report_sessions),
        "item_link_count": sum(len(row["item_ids"]) for row in report_sessions),
        "observed_week_count": len(weekly),
    }
    return {
        "scope": scope,
        "claim": "observed-records-only-not-longitudinal-proof",
        "summary": summary,
        "weeks": weekly,
        "sessions": report_sessions,
        "meaning_if_empty": "no visible recorded upkeep sessions; not proof that no upkeep occurred",
    }


def run_synthetic_four_week_harness(fixture: Mapping[str, Any]) -> dict[str, object]:
    """Exercise aggregation on four declared synthetic weeks, never real-world evidence."""
    if not isinstance(fixture, Mapping) or set(fixture) != {
        "corpus_label",
        "rows",
        "expected_summary",
        "expected_weeks",
    }:
        raise MaintenanceError("synthetic upkeep fixture has an invalid schema")
    if fixture["corpus_label"] != "synthetic":
        raise MaintenanceError("upkeep harness accepts only an explicitly synthetic corpus")
    rows = fixture["rows"]
    if not isinstance(rows, Mapping):
        raise MaintenanceError("synthetic upkeep rows must be a mapping")
    report = maintenance_report(rows, scope="private")
    expected_summary = fixture["expected_summary"]
    expected_weeks = fixture["expected_weeks"]
    if report["summary"] != expected_summary or report["weeks"] != expected_weeks:
        raise MaintenanceError("synthetic upkeep aggregation disagrees with checked expectations")
    if len(report["weeks"]) != 4:
        raise MaintenanceError("synthetic upkeep harness requires exactly four distinct weeks")
    return {
        "status": "pass",
        "claim": "synthetic-fixture-only",
        "week_count": 4,
        "summary": report["summary"],
    }
