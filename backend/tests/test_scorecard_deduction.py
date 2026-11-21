"""业务测试：评分规则贡献 → 降分依据视图 → 日报复查项 → 采集证据核对。

覆盖题目要求的全部约束：同版本贡献、并发修订、中断恢复、旧版本重放、
未齐项标注、版本确认回写复核清单、日报按降分依据派工、证据补录后重开核对。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dq_report import DqReportService
from app.services.dq_scorecard import (
    SCENARIO_RULES,
    DqScorecardService,
    composite_of,
    ensure_scorecard_scenario,
)
from app.store import store


def _new_record(table: str = "ods_test_deduction") -> int:
    rows = store.rows("dq_scorecard")
    new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": new_id,
        "记录编号": f"DQ_S-{new_id:04d}",
        "目标表名": table,
        "完整性得分": "—",
        "准确性得分": "—",
        "时效性得分": "—",
        "综合得分": "—",
        "评分时间": "2026-11-21",
        "评分版本": "v1",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })
    return new_id


@pytest.fixture()
def scenario_record() -> int:
    return int(ensure_scorecard_scenario()["id"])


class TestContributionLedger:
    """评分卡只保存结果，贡献账本说明“为什么降分”。"""

    def test_view_explains_each_deduction(self, scenario_record):
        svc = DqScorecardService()
        view = svc.build_deduction_view(scenario_record)
        assert view["综合得分"] == 72.0
        assert view["降分合计"] == 28.0
        # 两条降分规则已确认，第三条更正未到。
        confirmed = [d for d in view["降分依据"] if d["确认状态"] == "已确认"]
        assert [d["规则编号"] for d in confirmed] == ["DQ_R-1004", "DQ_R-1003"]
        assert [d["降分贡献"] for d in confirmed] == [14.0, 9.0]
        assert view["未齐项"][0]["规则编号"] == "DQ_R-1005"

    def test_each_rule_carries_weight_score_and_contribution(self, scenario_record):
        svc = DqScorecardService()
        view = svc.build_deduction_view(scenario_record)
        for rule in view["规则贡献"]:
            assert rule["权重"] > 0
            assert rule["加权得分"] == pytest.approx(rule["权重"] * rule["得分"], abs=0.01)
            assert rule["降分贡献"] == pytest.approx(rule["权重"] * (100 - rule["得分"]), abs=0.01)

    def test_total_recomputes_from_weights_and_results(self, scenario_record):
        svc = DqScorecardService()
        view = svc.build_deduction_view(scenario_record)
        assert composite_of(view["规则贡献"]) == view["综合得分"]
        verify = svc.recompute_version(scenario_record, "v1")
        assert verify["可复算"] is True
        assert verify["差异"] == 0
        assert verify["按权重复算"] == verify["按降分复算"]

    def test_new_calculation_writes_contribution_ledger(self):
        svc = DqScorecardService()
        result = svc.calculate_quality_score("ods_new_ledger", [
            {"规则类型": "空值检查", "结果": "失败", "得分": 50, "权重": 0.5},
            {"规则类型": "格式校验", "结果": "通过", "权重": 0.5},
        ])
        record = next(r for r in store.rows("dq_scorecard") if r["记录编号"] == result["记录编号"])
        view = svc.build_deduction_view(int(record["id"]))
        assert view is not None
        assert view["降分合计"] == 25.0
        assert view["规则总数"] == 2


class TestVersioning:
    """同版本贡献、并发修订、中断恢复与旧版本重放。"""

    def test_interrupted_calculation_resumes_from_checkpoint(self):
        rid = _new_record("ods_resume")
        svc = DqScorecardService()
        started = svc.start_calculation(rid, version="v1", rules=SCENARIO_RULES, stop_after=3)
        assert started["状态"] == "计算中"
        assert started["检查点"] == 3

        interrupted = svc.build_deduction_view(rid)
        assert interrupted["计算进度"]["已完成"] is False
        assert interrupted["可复算"] is False
        assert interrupted["规则总数"] == len(SCENARIO_RULES)
        assert interrupted["未齐项数"] >= len(SCENARIO_RULES) - 3

        resumed = svc.resume_calculation(rid, "v1")
        assert resumed["恢复自检查点"] == 3
        assert resumed["状态"] == "待确认"
        assert len(resumed["未齐项"]) == 1
        assert svc.recompute_version(rid, "v1")["可复算"] is True

    def test_start_on_unfinished_version_is_rejected(self):
        rid = _new_record("ods_resume_guard")
        svc = DqScorecardService()
        svc.start_calculation(rid, version="v1", rules=SCENARIO_RULES, stop_after=2)
        again = svc.start_calculation(rid, version="v1", rules=SCENARIO_RULES)
        assert again["ok"] is False
        assert "恢复接口" in again["message"]

    def test_only_same_version_contributions_accepted(self, scenario_record):
        svc = DqScorecardService()
        rejected = svc.accept_correction(
            scenario_record, version="v2", rule_id="DQ_R-1005", result="通过", score=80)
        assert rejected["ok"] is False
        assert rejected["版本冲突"] is True
        assert svc.build_deduction_view(scenario_record)["未齐项数"] == 1

    def test_concurrent_revision_uses_optimistic_lock(self):
        rid = _new_record("ods_concurrency")
        svc = DqScorecardService()
        svc.start_calculation(rid, version="v1", rules=SCENARIO_RULES)
        first = svc.accept_correction(
            rid, version="v1", rule_id="DQ_R-1003", result="通过", score=95,
            expected_revision=0)
        assert first["ok"] is True and first["修订号"] == 1

        stale = svc.accept_correction(
            rid, version="v1", rule_id="DQ_R-1004", result="通过", score=95,
            expected_revision=0)
        assert stale["ok"] is False
        assert stale["并发冲突"] is True
        assert stale["当前修订号"] == 1

        second = svc.accept_correction(
            rid, version="v1", rule_id="DQ_R-1004", result="通过", score=95,
            expected_revision=1)
        assert second["ok"] is True and second["修订号"] == 2

    def test_old_revision_replay_is_stable_after_correction(self):
        rid = _new_record("ods_replay")
        svc = DqScorecardService()
        svc.start_calculation(rid, version="v1", rules=SCENARIO_RULES)
        before = svc.replay_version(rid, "v1")
        before_score = before["快照综合得分"]

        svc.accept_correction(rid, version="v1", rule_id="DQ_R-1005", result="通过", score=80,
                              expected_revision=0)
        after = svc.replay_version(rid, "v1")
        old = svc.replay_version(rid, "v1", revision=0)

        assert before["可重放"] and old["可重放"]
        assert old["快照综合得分"] == before_score
        assert old["差异"] == 0
        assert after["快照综合得分"] > before_score
        assert after["修订号"] == 1

    def test_replay_unknown_revision_fails_cleanly(self, scenario_record):
        svc = DqScorecardService()
        result = svc.replay_version(scenario_record, "v1", revision=99)
        assert result["ok"] is False
        assert result["可重放"] is False


class TestDailyReportReview:
    """日报从降分依据视图读取权重和得分，生成可复算的复查项。"""

    def test_report_items_come_from_deduction_view(self, scenario_record):
        report_svc = DqReportService()
        scorecard_svc = DqScorecardService()
        view = scorecard_svc.build_deduction_view(scenario_record)
        report = report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T1")
        assert report["ok"] is True
        assert report["复核项数"] == len(view["规则贡献"])
        for item in report["复核项"]:
            source = next(r for r in view["规则贡献"] if r["规则编号"] == item["规则编号"])
            assert item["权重"] == source["权重"]
            assert item["规则得分"] == source["得分"]
            assert item["加权得分"] == source["加权得分"]

    def test_pending_item_is_flagged_and_blocks_dispatch(self, scenario_record):
        report_svc = DqReportService()
        report = report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T2")
        pending_items = [item for item in report["复核项"] if item["未齐"]]
        assert [item["规则编号"] for item in pending_items] == ["DQ_R-1005"]
        assert pending_items[0]["未齐原因"]

        blocked = report_svc.dispatch_review_items("DQ_R-T2")
        assert blocked["ok"] is False
        assert blocked["未齐项"][0]["规则编号"] == "DQ_R-1005"

    def test_confirmed_deductions_are_the_dispatch_scope(self, scenario_record):
        report_svc = DqReportService()
        report = report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T3")
        deductions = [item for item in report["复核项"] if item["是否降分"]]
        assert [item["规则编号"] for item in deductions] == ["DQ_R-1003", "DQ_R-1004"]

    def test_version_confirm_writes_back_review_checklist(self):
        rid = _new_record("ods_confirm_writeback")
        svc = DqScorecardService()
        report_svc = DqReportService()
        svc.start_calculation(rid, version="v1", rules=[
            {"规则编号": "DQ_R-2001", "权重": 0.5, "结果": "失败", "得分": 40},
            {"规则编号": "DQ_R-2002", "权重": 0.5, "结果": "通过", "得分": 100},
        ])
        report = report_svc.generate_daily_report(rid, report_no="DQ_R-T4")
        assert report["ok"] is True

        confirmed = svc.confirm_version(rid, "v1")
        assert confirmed["ok"] is True
        assert confirmed["回写复核项数"] == 2
        checklist = [
            row for row in store.rows("dq_deduction_review")
            if row["报告编号"] == "DQ_R-T4"
        ]
        assert all(row["确认结果"] == "已确认" for row in checklist)
        assert all(row["确认人"] for row in checklist)


class TestLateCorrectionAndEvidence:
    """第三条更正赶在派工前到达，补证据后重开并核对派工分项可复算。"""

    def test_correction_before_dispatch_requires_rebase(self, scenario_record):
        scorecard = DqScorecardService()
        report_svc = DqReportService()
        report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T5")

        correction = scorecard.accept_correction(
            scenario_record, version="v1", rule_id="DQ_R-1005",
            result="通过", score=80, expected_revision=0,
        )
        assert correction["ok"] is True
        assert correction["未齐项数"] == 0
        assert correction["综合得分"] == 75.0

        # 版本已修订：旧报告的分项过期，不能直接派工。
        stale = report_svc.dispatch_review_items("DQ_R-T5")
        assert stale["ok"] is False
        assert stale["版本已修订"] is True

        rebased = report_svc.rebase_report("DQ_R-T5")
        assert rebased["ok"] is True
        assert rebased["引用修订号" if "引用修订号" in rebased else "新引用修订号"] == 1
        assert rebased["综合得分"] == 75.0

        dispatched = report_svc.dispatch_review_items("DQ_R-T5")
        assert dispatched["ok"] is True
        assert dispatched["引用修订号"] == 1

    def test_evidence_reopen_and_verify(self, scenario_record):
        scorecard = DqScorecardService()
        report_svc = DqReportService()
        report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T6")
        scorecard.accept_correction(
            scenario_record, version="v1", rule_id="DQ_R-1005",
            result="通过", score=80, expected_revision=0)
        report_svc.rebase_report("DQ_R-T6")
        report_svc.dispatch_review_items("DQ_R-T6")

        evidence = scorecard.add_evidence(
            record_no="DQ_S-1001", version="v1", revision=1, report_no="DQ_R-T6",
            payload={"证据编号": "EV-TEST", "规则编号": "DQ_R-1005",
                     "样本行数": 1200, "失败行数": 0, "结论": "复核通过"},
        )
        assert evidence["报告编号"] == "DQ_R-T6"
        reopened = report_svc.reopen_review_items("DQ_R-T6", evidence_no="EV-TEST")
        assert reopened["ok"] is True
        assert reopened["重开复核项数"] >= 1

        verify = report_svc.verify_dispatch_basis("DQ_R-T6")
        assert verify["可复算"] is True
        assert verify["报告综合得分"] == 75.0
        assert verify["分项复算综合分"] == 75.0
        assert verify["复核项复算综合分"] == 75.0
        assert verify["不一致复核项"] == []
        assert verify["版本与报告一致"] is True
        assert all(check["一致"] for check in verify["复核项核对"])

    def test_verify_detects_tampered_review_item(self, scenario_record):
        report_svc = DqReportService()
        report_svc.generate_daily_report(scenario_record, report_no="DQ_R-T7")
        report_svc.rebase_report("DQ_R-T7")
        target = next(
            row for row in store.rows("dq_deduction_review")
            if row["报告编号"] == "DQ_R-T7" and row["规则编号"] == "DQ_R-1003"
        )
        target["规则得分"] = 90.0  # 篡改：与冻结的加权得分不再匹配

        verify = report_svc.verify_dispatch_basis("DQ_R-T7")
        assert verify["可复算"] is False
        assert verify["不一致复核项"][0]["规则编号"] == "DQ_R-1003"
