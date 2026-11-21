"""评分降分依据接口：贡献视图、版本重放、日报复查项与采集证据核对。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import dq_deduction

router = APIRouter(prefix="/api", tags=["评分降分依据"])


class Payload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/scorecard/{record_id}/contribution")
def scorecard_contribution(record_id: int, version: str | None = None) -> dict[str, Any]:
    detail = dq_deduction.serve_scorecard_detail(record_id, version)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"评分记录 {record_id} 不存在或没有该评分版本")
    return detail


@router.get("/scorecard/{record_id}/replay")
def scorecard_replay(record_id: int, version: str | None = None,
                     revision: int | None = None) -> dict[str, Any]:
    return dq_deduction.replay_version(record_id, {"version": version, "revision": revision})


@router.get("/scorecard/{record_id}/verify")
def scorecard_verify(record_id: int, version: str | None = None,
                     expected: float | None = None) -> dict[str, Any]:
    return dq_deduction.verify_version(record_id, {"version": version, "expected": expected})


@router.get("/scorecard-scenario")
def scorecard_scenario() -> dict[str, Any]:
    return dq_deduction.serve_scenario()


@router.post("/scorecard/{record_id}/calculation/start")
def start_calculation(record_id: int, payload: Payload) -> dict[str, Any]:
    return dq_deduction.start_calculation(record_id, payload.values)


@router.post("/scorecard/{record_id}/calculation/resume")
def resume_calculation(record_id: int, payload: Payload) -> dict[str, Any]:
    return dq_deduction.resume_calculation(record_id, payload.values)


@router.post("/scorecard/{record_id}/corrections")
def accept_correction(record_id: int, payload: Payload) -> dict[str, Any]:
    return dq_deduction.accept_correction(record_id, payload.values)


@router.post("/scorecard/{record_id}/confirm")
def confirm_version(record_id: int, payload: Payload) -> dict[str, Any]:
    return dq_deduction.confirm_version(record_id, payload.values)


@router.post("/evidence")
def add_evidence(payload: Payload) -> dict[str, Any]:
    return dq_deduction.add_evidence(payload.values)


@router.get("/evidence")
def list_evidence(report_no: str | None = None) -> list[dict[str, Any]]:
    return dq_deduction.list_evidence(report_no)


@router.post("/daily-report")
def create_daily_report(payload: Payload) -> dict[str, Any]:
    values = payload.values
    record_id = values.get("record_id")
    if record_id is None:
        return {"ok": False, "message": "缺少 record_id"}
    return dq_deduction.generate_daily_report(int(record_id), values)


@router.post("/daily-report/{report_no}/dispatch")
def dispatch_daily_report(report_no: str, payload: Payload) -> dict[str, Any]:
    return dq_deduction.dispatch_report(report_no, payload.values)


@router.post("/daily-report/{report_no}/rebase")
def rebase_daily_report(report_no: str, payload: Payload) -> dict[str, Any]:
    return dq_deduction.rebase_report(report_no, payload.values)


@router.post("/daily-report/{report_no}/reopen")
def reopen_daily_report(report_no: str, payload: Payload) -> dict[str, Any]:
    return dq_deduction.reopen_report(report_no, payload.values)


@router.get("/daily-report/{report_no}/verify")
def verify_daily_report(report_no: str) -> dict[str, Any]:
    return dq_deduction.verify_report(report_no)


@router.get("/daily-report/{report_no}")
def get_daily_report(report_no: str) -> dict[str, Any]:
    report = next(
        (row for row in dq_deduction.list_reports() if row.get("报告编号") == report_no),
        None,
    )
    if report is None:
        raise HTTPException(status_code=404, detail=f"报告 {report_no} 不存在")
    return {
        "report": report,
        "review_items": dq_deduction.list_review_items(report_no),
    }
