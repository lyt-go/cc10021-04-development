"""评分降分依据与日报复查项的服务编排。

把“评分卡记录 → 降分依据视图 → 日报 → 复核清单/派工 → 采集证据 → 重开核对”
串成一条可执行的链路，供 REST 与前端工作台复用。所有状态变化仍然落在
`app/services/dq_scorecard.py` 与 `app/services/dq_report.py`。
"""

from __future__ import annotations

from typing import Any

from app.services.dq_report import DqReportService
from app.services.dq_scorecard import DqScorecardService, SCENARIO_RULES, ensure_scorecard_scenario
from app.store import store


def _scorecard() -> DqScorecardService:
    return DqScorecardService()


def _report() -> DqReportService:
    return DqReportService()


def _scenario_record() -> dict[str, Any]:
    return ensure_scorecard_scenario()


def serve_scorecard_detail(record_id: int, version: str | None = None) -> dict[str, Any] | None:
    svc = _scorecard()
    view = svc.build_deduction_view(record_id, version)
    if view is None:
        return None
    return {
        "view": view,
        "versions": svc.list_versions(record_id),
        "evidence": svc.list_evidence(),
    }


def serve_scenario() -> dict[str, Any]:
    """演示场景：两条降分规则已确认，第三条更正尚未到达。"""
    record = _scenario_record()
    view = _scorecard().build_deduction_view(int(record["id"]))
    return {
        "record_id": record["id"],
        "记录编号": record.get("记录编号"),
        "目标表名": record.get("目标表名"),
        "评分版本": record.get("评分版本"),
        "view": view,
        "scenario_rules": SCENARIO_RULES,
        "confirmed_deductions": [
            item for item in (view or {}).get("降分依据", [])
            if item.get("确认状态") == "已确认"
        ],
        "pending_items": (view or {}).get("未齐项", []),
    }


def start_calculation(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().start_calculation(
        record_id,
        version=payload.get("version"),
        rules=payload.get("rules"),
        stop_after=payload.get("stop_after"),
        restart=bool(payload.get("restart")),
    )


def resume_calculation(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().resume_calculation(record_id, payload.get("version"))


def accept_correction(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().accept_correction(
        record_id,
        version=payload.get("version"),
        rule_id=str(payload.get("rule_id") or payload.get("规则编号") or ""),
        result=str(payload.get("result") or payload.get("结果") or ""),
        score=payload.get("score", payload.get("得分")),
        evidence_id=payload.get("evidence_id") or payload.get("证据编号"),
        expected_revision=payload.get("expected_revision"),
        operator=str(payload.get("operator") or "质量平台"),
    )


def confirm_version(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().confirm_version(
        record_id, payload.get("version"),
        reviewer=str(payload.get("reviewer") or "质量平台"),
    )


def replay_version(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().replay_version(
        record_id, payload.get("version"), revision=payload.get("revision"),
    )


def verify_version(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _scorecard().recompute_version(
        record_id, payload.get("version"),
        expected=payload.get("expected"),
    )


def add_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    record = _scenario_record()
    return _scorecard().add_evidence(
        record_no=str(payload.get("record_no") or record.get("记录编号")),
        version=str(payload.get("version") or record.get("评分版本")),
        revision=payload.get("revision"),
        report_no=payload.get("report_no"),
        evidence_type=str(payload.get("evidence_type") or "采集证据"),
        payload=payload,
    )


def list_evidence(report_no: str | None = None) -> list[dict[str, Any]]:
    return _scorecard().list_evidence(report_no)


def generate_daily_report(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return _report().generate_daily_report(
        record_id,
        version=payload.get("version"),
        period=str(payload.get("period") or "daily"),
        report_no=payload.get("report_no"),
        dispatch=bool(payload.get("dispatch", True)),
        reviewer=str(payload.get("reviewer") or "质量平台"),
    )


def dispatch_report(report_no: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _report().dispatch_review_items(
        report_no,
        reviewer=str(payload.get("reviewer") or "质量平台"),
        allow_stale=bool(payload.get("allow_stale")),
    )


def rebase_report(report_no: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _report().rebase_report(
        report_no, reason=str(payload.get("reason") or "评分版本已修订"),
    )


def reopen_report(report_no: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _report().reopen_review_items(
        report_no,
        evidence_no=payload.get("evidence_no") or payload.get("证据编号"),
        reason=str(payload.get("reason") or "更正结果迟到，需按新证据重开复核"),
        reviewer=str(payload.get("reviewer") or "质量平台"),
    )


def verify_report(report_no: str) -> dict[str, Any]:
    return _report().verify_dispatch_basis(report_no)


def list_review_items(report_no: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("dq_deduction_review")
    if report_no:
        rows = [row for row in rows if row.get("报告编号") == report_no]
    return [dict(row) for row in rows]


def list_reports(record_no: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("dq_report")
    if record_no:
        rows = [row for row in rows if row.get("评分记录编号") == record_no]
    return [dict(row) for row in rows]
