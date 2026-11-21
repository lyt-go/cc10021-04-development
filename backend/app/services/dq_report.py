"""质量报告服务：聚合巡检和质量事件数据，生成多维度质量分析报告。

domain_chain: 质量报告 (dq_report)
  └─ 读取 → 质量评分卡 (dq_scorecard) 降分依据视图
  └─ 派工 → 降分复核清单 (dq_deduction_review)
  └─ 核对 → 采集证据 (dq_collection_evidence)

日报不再只取评分卡上的综合结果，而是从评分卡的“降分依据视图”读取规则权重
和规则得分，按规则逐条生成复查项。报告落库时冻结评分版本、修订号、分项和
综合分，因此后续更正不会改写已经发出的报告；未齐项单独标注并可阻止派工。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.base import BaseModuleService
from app.services.dq_scorecard import (
    DqScorecardService,
    _now,
    composite_of,
)
from app.store import store

CHECKLIST_MODULE = "dq_deduction_review"


class DqReportService(BaseModuleService):
    MODULE = "dq_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间", "报告版本"]
    REQUIRED_FIELDS = ["报告编号", "报告周期", "覆盖表数"]
    STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
    ACTION_RULES = {"生成报告": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.scorecard = DqScorecardService()

    # ------------------------------------------------------------------
    # 日报：从降分依据视图取权重与得分，生成复查项
    # ------------------------------------------------------------------
    def generate_daily_report(
        self,
        score_record_id: int,
        *,
        version: str | None = None,
        period: str = "daily",
        report_no: str | None = None,
        dispatch: bool = True,
        reviewer: str = "质量平台",
    ) -> dict[str, Any]:
        """生成日报并按其降分依据生成复核清单。

        报告冻结生成时刻的评分版本、修订号、分项权重与得分。未齐项照常写入
        报告的“未齐项”列表并在复核项上单独标注；派工由 dispatch_review_items
        拦截，存在未齐项时不会把不完整的分项派给复核人。
        """
        view = self.scorecard.build_deduction_view(score_record_id, version)
        if view is None:
            return {"ok": False, "message": f"评分记录 {score_record_id} 或其评分版本不存在"}
        if not view.get("计算进度", {}).get("已完成", True):
            return {
                "ok": False,
                "未齐项": view.get("未齐项") or [],
                "message": "评分版本仍在计算中，日报不能派发不完整的分项",
            }
        version = view["评分版本"]

        pending_items = view.get("未齐项") or []

        rows = store.rows(self.MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        report_no = report_no or f"DQ_R-{new_id:04d}"
        report = {
            "id": new_id,
            "报告编号": report_no,
            "报告周期": period,
            "覆盖表数": "1",
            "通过规则数": str(sum(
                1 for item in view["规则贡献"]
                if item["纳入"] and item["结果"] == "通过")),
            "失败规则数": str(sum(
                1 for item in view["规则贡献"]
                if item["纳入"] and item["结果"] != "通过")),
            "趋势分析": "下降" if view["综合得分"] < 75 else "平稳",
            "生成时间": _now(),
            "报告版本": "v1.0",
            # 报告引用的是评分视图快照，不随后续更正漂移。
            "评分记录编号": view["记录编号"],
            "评分记录ID": score_record_id,
            "评分版本": version,
            "引用修订号": view["修订号"],
            "目标表名": view["目标表名"],
            "规则权重": view["规则权重与得分"],
            "分项明细": deepcopy(view["规则贡献"]),
            "降分依据": deepcopy(view["降分依据"]),
            "综合得分": view["综合得分"],
            "降分合计": view["降分合计"],
            "权重覆盖": view["权重覆盖"],
            "可复算": view["可复算"],
            "摘要": view.get("摘要"),
            "未齐项数": view["未齐项数"],
            "未齐项": deepcopy(pending_items),
            "复核状态": "待派工" if dispatch else "未派工",
            "status": "已完成",
            "pending": bool(dispatch),
            "abnormal": view["综合得分"] < 60,
        }
        rows.append(report)

        items: list[dict[str, Any]] = []
        if dispatch:
            items = self._create_review_items(report, view, reviewer)
            if pending_items:
                report["复核状态"] = "已生成（含未齐项，未派工）"
            else:
                report["复核状态"] = "已派工"
            report["pending"] = True

        return {
            "ok": True,
            "报告编号": report_no,
            "评分版本": version,
            "引用修订号": view["修订号"],
            "综合得分": view["综合得分"],
            "降分合计": view["降分合计"],
            "可复算": view["可复算"],
            "未齐项": pending_items,
            "复核项数": len(items),
            "复核项": items,
            "报告": report,
            "message": f"日报已生成，从降分依据视图生成 {len(items)} 条复查项",
        }

    def _create_review_items(self, report: dict[str, Any], view: dict[str, Any],
                             reviewer: str) -> list[dict[str, Any]]:
        """逐条规则生成降分复核项，权重和得分直接取自降分依据视图。"""
        items: list[dict[str, Any]] = []
        pending_map = {item["规则编号"]: item for item in view.get("未齐项") or []}
        for contribution in view["规则贡献"]:
            rule_id = contribution["规则编号"]
            pending = rule_id in pending_map or not contribution["纳入"]
            rows = store.rows(CHECKLIST_MODULE)
            new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            is_deduction = contribution["降分贡献"] > 0
            item = {
                "id": new_id,
                "复核项编号": f"DQV-{new_id:04d}",
                "记录编号": report.get("评分记录编号"),
                "评分记录ID": report.get("评分记录ID"),
                "报告编号": report["报告编号"],
                "目标表名": report.get("目标表名"),
                "评分版本": report.get("评分版本"),
                "引用修订号": report.get("引用修订号"),
                "规则编号": rule_id,
                "规则名称": contribution["规则名称"],
                "维度": contribution["维度"],
                "权重": contribution["权重"],
                "规则得分": contribution["得分"],
                "加权得分": contribution["加权得分"],
                "降分贡献": contribution["降分贡献"],
                "结果": contribution["结果"],
                "报告综合得分": report["综合得分"],
                "分项类型": "降分项" if is_deduction else "已通过项",
                "是否降分": is_deduction and not pending,
                "未齐": pending,
                "未齐原因": contribution.get("未齐原因"),
                "确认结果": contribution.get("确认状态"),
                "派工状态": "待派工",
                "派工时间": None,
                "派工人": None,
                "负责人": None,
                "状态": "待派工（版本未确认）" if pending else "待派工",
                "pending": True,
                "abnormal": pending,
            }
            rows.append(item)
            items.append(item)
        return items

    def rebase_report(self, report_no: str, *, reason: str = "评分版本已修订") -> dict[str, Any]:
        """把报告与其复核项重挂到评分版本的最新修订上。

        更正结果在派工前到达时，报告冻结的旧修订分项已经过期。此方法用最新的
        降分依据视图刷新报告分项、综合分和每个复核项的权重/得分，同时把旧快照
        归档，保证派工所用的分项与综合分仍然是同一口径、可复算。
        """
        report = next(
            (row for row in store.rows(self.MODULE) if row.get("报告编号") == report_no),
            None,
        )
        if report is None:
            return {"ok": False, "message": f"报告 {report_no} 不存在"}
        record_id = report.get("评分记录ID")
        if record_id is None:
            return {"ok": False, "message": f"报告 {report_no} 未关联评分记录，无法重挂"}
        view = self.scorecard.build_deduction_view(int(record_id), str(report.get("评分版本")))
        if view is None:
            return {"ok": False, "message": "评分版本不存在，无法重挂报告"}

        history = report.setdefault("快照历史", [])
        history.append({
            "引用修订号": report.get("引用修订号"),
            "综合得分": report.get("综合得分"),
            "摘要": report.get("摘要"),
            "分项明细": deepcopy(report.get("分项明细") or []),
            "归档时间": _now(),
            "归档原因": reason,
        })

        by_rule = {item["规则编号"]: item for item in view["规则贡献"]}
        report.update({
            "引用修订号": view["修订号"],
            "评分版本": view["评分版本"],
            "规则权重": deepcopy(view["规则权重与得分"]),
            "分项明细": deepcopy(view["规则贡献"]),
            "降分依据": deepcopy(view["降分依据"]),
            "综合得分": view["综合得分"],
            "降分合计": view["降分合计"],
            "权重覆盖": view["权重覆盖"],
            "可复算": view["可复算"],
            "未齐项数": view["未齐项数"],
            "未齐项": deepcopy(view["未齐项"]),
            "摘要": view["摘要"],
            "重挂时间": _now(),
            "重挂原因": reason,
        })

        refreshed = 0
        for item in store.rows(CHECKLIST_MODULE):
            if item.get("报告编号") != report_no:
                continue
            contribution = by_rule.get(item.get("规则编号"))
            if contribution is None:
                continue
            item.update({
                "引用修订号": view["修订号"],
                "权重": contribution["权重"],
                "规则得分": contribution["得分"],
                "加权得分": contribution["加权得分"],
                "降分贡献": contribution["降分贡献"],
                "结果": contribution["结果"],
                "报告综合得分": view["综合得分"],
                "分项类型": "降分项" if contribution["降分贡献"] > 0 else "已通过项",
                "是否降分": contribution["降分贡献"] > 0 and contribution["纳入"],
                "未齐": bool(contribution.get("未齐")),
                "未齐原因": contribution.get("未齐原因"),
                "确认结果": contribution.get("确认状态"),
                "证据编号": contribution.get("证据编号") or item.get("证据编号"),
            })
            refreshed += 1

        return {
            "ok": True,
            "报告编号": report_no,
            "评分版本": view["评分版本"],
            "新引用修订号": view["修订号"],
            "综合得分": view["综合得分"],
            "刷新复核项数": refreshed,
            "可复算": view["可复算"],
            "message": f"报告已重挂到修订 {view['修订号']}，刷新 {refreshed} 条复核项",
        }

    def dispatch_review_items(self, report_no: str,
                              reviewer: str = "质量平台",
                              *, allow_stale: bool = False) -> dict[str, Any]:
        """把恢复后的复核项正式派工；未齐项或过期修订都拒绝派工。

        如果报告生成后评分版本又修订过（更正结果在派工前到达），派工前必须先
        重挂报告，否则派工用的是旧修订的分项，无法与版本当前总分复算。
        """
        report = next(
            (row for row in store.rows(self.MODULE) if row.get("报告编号") == report_no),
            None,
        )
        items = [
            row for row in store.rows(CHECKLIST_MODULE)
            if row.get("报告编号") == report_no
        ]
        if not items:
            return {"ok": False, "message": f"报告 {report_no} 没有复核项"}

        if report is not None and not allow_stale:
            live = self.scorecard.recompute_version(
                int(report.get("评分记录ID") or 0),
                str(report.get("评分版本") or ""),
            )
            if live.get("ok") and live.get("修订号") not in (None, report.get("引用修订号")):
                return {
                    "ok": False,
                    "版本已修订": True,
                    "报告引用修订号": report.get("引用修订号"),
                    "版本当前修订号": live.get("修订号"),
                    "message": "评分版本已修订，请先重挂报告再派工，避免派工分项与综合分不可复算",
                }

        pending = [item for item in items if item.get("未齐")]
        if pending:
            return {
                "ok": False,
                "未齐项": pending,
                "message": "存在未齐项，不能派工：" + "、".join(
                    item["规则编号"] for item in pending),
            }
        now = _now()
        for item in items:
            item.update({
                "派工状态": "已派发",
                "派工时间": now,
                "派工人": reviewer,
                "负责人": item.get("负责人") or "数据工程组",
                "状态": "已派发",
                "pending": True,
            })
        if report is not None:
            report["复核状态"] = "已派工"
            report["dispatch_note"] = None
            report["派工时间"] = now
            report["派工人"] = reviewer
        return {
            "ok": True,
            "报告编号": report_no,
            "派工项数": len(items),
            "引用修订号": report.get("引用修订号") if report else None,
            "可复算": report.get("可复算") if report else None,
            "message": f"已派工 {len(items)} 条复查项",
        }

    def reopen_review_items(
        self,
        report_no: str,
        *,
        evidence_no: str | None = None,
        reason: str = "更正结果迟到，需按新证据重开复核",
        reviewer: str = "质量平台",
    ) -> dict[str, Any]:
        """补采集证据后重开已发出的复核项，等待重新派工。"""
        report = next(
            (row for row in store.rows(self.MODULE) if row.get("报告编号") == report_no),
            None,
        )
        if report is None:
            return {"ok": False, "message": f"报告 {report_no} 不存在"}
        items = [
            row for row in store.rows(CHECKLIST_MODULE)
            if row.get("报告编号") == report_no
        ]
        reopened = 0
        for item in items:
            if item.get("状态") in ("已重开", "已关闭"):
                continue
            item.update({
                "状态": "已重开",
                "重新派工状态": "待重新派工",
                "重开原因": reason,
                "重开时间": _now(),
                "重开人": reviewer,
                "证据编号": evidence_no or item.get("证据编号"),
                "pending": True,
                "abnormal": True,
            })
            reopened += 1
        report.update({
            "复核状态": "已重开",
            "重开时间": _now(),
            "重开证据": evidence_no,
            "重开原因": reason,
        })
        return {
            "ok": True,
            "报告编号": report_no,
            "重开复核项数": reopened,
            "证据编号": evidence_no,
            "报告版本": report.get("评分版本"),
            "引用修订号": report.get("引用修订号"),
            "message": f"报告 {report_no} 的 {reopened} 条复核项已重新打开",
        }

    # ------------------------------------------------------------------
    # 核对：派工所用分项与综合分是否可复算
    # ------------------------------------------------------------------
    def verify_dispatch_basis(self, report_no: str) -> dict[str, Any]:
        """核对派工所用分项及综合分是否可复算。

        同时做两层校验：
        1. 报告冻结的分项明细 → 按权重复算综合分，与报告综合分比对；
        2. 派工复核项上冻结的权重/得分 → 复算，与报告综合分比对；
        3. 与评分版本当前快照比对，说明报告是否仍引用可重放的旧修订。
        """
        report = next(
            (row for row in store.rows(self.MODULE) if row.get("报告编号") == report_no),
            None,
        )
        if report is None:
            return {"ok": False, "可复算": False, "message": f"报告 {report_no} 不存在"}

        report_items = deepcopy(report.get("分项明细") or [])
        report_computed = composite_of(report_items)
        report_score_raw = report.get("综合得分")
        report_score = float(report_score_raw) if report_score_raw is not None else None
        report_diff = round(report_computed - report_score, 2) if report_score is not None else None

        review_items = [
            row for row in store.rows(CHECKLIST_MODULE)
            if row.get("报告编号") == report_no
        ]
        item_computed = round(sum(
            float(item.get("权重", 0)) * float(item.get("规则得分", 0))
            for item in review_items
        ), 1)
        item_checks = []
        for item in review_items:
            expected = float(item.get("权重", 0)) * float(item.get("规则得分", 0))
            item_checks.append({
                "复核项编号": item.get("复核项编号"),
                "规则编号": item.get("规则编号"),
                "权重": item.get("权重"),
                "规则得分": item.get("规则得分"),
                "记录加权得分": item.get("加权得分"),
                "复算加权得分": round(expected, 2),
                "一致": abs(round(expected, 2) - float(item.get("加权得分") or 0)) < 0.05,
                "派工状态": item.get("派工状态"),
                "状态": item.get("状态"),
            })

        docked = [check for check in item_checks if not check["一致"]]
        live = self.scorecard.recompute_version(
            int(report.get("评分记录ID") or 0),
            str(report.get("评分版本") or ""),
            expected=float(report_score) if report_score is not None else None,
        )
        version_matches_report = (
            live.get("摘要") == report.get("摘要")
            if report.get("摘要") else None
        )
        reproducible = (
            report_diff is not None
            and abs(report_diff) < 0.05
            and abs(item_computed - report_score) < 0.05
            and not docked
        )
        return {
            "ok": True,
            "报告编号": report_no,
            "可复算": reproducible,
            "报告综合得分": report_score,
            "分项复算综合分": report_computed,
            "分项差异": report_diff,
            "复核项复算综合分": item_computed,
            "复核项差异": round(item_computed - float(report_score), 2) if report_score is not None else None,
            "复核项数": len(review_items),
            "不一致复核项": docked,
            "复核项核对": item_checks,
            "评分版本": report.get("评分版本"),
            "引用修订号": report.get("引用修订号"),
            "评分版本当前修订号": live.get("修订号"),
            "版本摘要": report.get("摘要"),
            "版本当前摘要": live.get("摘要"),
            "版本与报告一致": version_matches_report,
            "版本复算": live,
            "未齐项": report.get("未齐项") or [],
            "message": "派工分项与综合分可复算" if reproducible else "派工依据不可复算，请复核",
        }

    # ------------------------------------------------------------------
    # 旧能力：全量聚合报告
    # ------------------------------------------------------------------
    def aggregate_quality_report(self, period: str = "daily") -> dict[str, Any]:
        """聚合全量巡检结果和质量事件，生成多维度质量分析报告。

        查询巡检执行记录、质量事件、评分卡和趋势数据，
        聚合各维度指标计算结果，写入质量报告记录并导出趋势快照。
        """
        incident_rows = store.rows("dq_incident")
        check_rows = store.rows("dq_check_run")
        scorecard_rows = store.rows("dq_scorecard")
        trend_rows = store.rows("dq_trend")

        total_checks = len(check_rows)
        passed_checks = sum(1 for r in check_rows if not r.get("abnormal"))
        failed_checks = total_checks - passed_checks

        total_incidents = len(incident_rows)
        open_incidents = sum(1 for r in incident_rows if r.get("status") in ("待确认", "调查中"))
        resolved_incidents = sum(1 for r in incident_rows if r.get("status") in ("已修复", "已关闭"))

        quality_values = []
        for t in trend_rows:
            try:
                quality_values.append(float(t.get("综合质量", "0")))
            except (ValueError, TypeError):
                pass
        avg_quality = sum(quality_values) / len(quality_values) if quality_values else 0

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        report = {
            "id": new_id,
            "报告编号": f"DQ_R-{new_id:04d}",
            "报告周期": period,
            "覆盖表数": str(len(scorecard_rows)),
            "通过规则数": str(passed_checks),
            "失败规则数": str(failed_checks),
            "趋势分析": "上升" if avg_quality >= 85 else "平稳" if avg_quality >= 70 else "下降",
            "生成时间": _now(),
            "报告版本": "v1.0",
            "status": "已完成",
            "pending": False,
            "abnormal": failed_checks > passed_checks,
        }
        rows.append(report)

        dim_scores = self._compute_dimension_scores(scorecard_rows)

        return {
            "报告编号": report["报告编号"],
            "报告周期": period,
            "巡检总次数": total_checks,
            "巡检通过率": f"{passed_checks / total_checks * 100:.1f}%" if total_checks else "N/A",
            "质量事件总数": total_incidents,
            "未处理事件": open_incidents,
            "已修复事件": resolved_incidents,
            "修复率": f"{resolved_incidents / total_incidents * 100:.1f}%" if total_incidents else "N/A",
            "平均综合质量": round(avg_quality, 2),
            "维度得分": dim_scores,
            "生成时间": report["生成时间"],
            "趋势": report["趋势分析"],
        }

    def _compute_dimension_scores(self, scorecard_rows: list[dict]) -> dict[str, float]:
        """计算质量维度得分汇总：完整性、准确性、时效性。"""
        dims = {"完整性": [], "准确性": [], "时效性": []}
        for s in scorecard_rows:
            for dim_key, field in [("完整性", "完整性得分"), ("准确性", "准确性得分"), ("时效性", "时效性得分")]:
                try:
                    dims[dim_key].append(float(s.get(field, "0")))
                except (ValueError, TypeError):
                    pass
        return {
            k: round(sum(v) / len(v), 1) if v else 0.0
            for k, v in dims.items()
        }

    def export_report_csv(self, report_id: int) -> str | None:
        """导出质量报告为CSV格式，包含所有维度数据和趋势标记。"""
        report = store.find(self.MODULE, report_id)
        if not report:
            return None

        lines = [
            "报告编号,报告周期,覆盖表数,通过规则数,失败规则数,趋势,生成时间,版本",
            f"{report.get('报告编号')},{report.get('报告周期')},{report.get('覆盖表数')},{report.get('通过规则数')},{report.get('失败规则数')},{report.get('趋势分析')},{report.get('生成时间')},{report.get('报告版本')}",
        ]
        return "\n".join(lines)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合巡检和质量事件数据生成质量分析报告"
        snapshot["artifact"] = "质量报告数据集"
        snapshot["artifact_desc"] = "引用降分依据视图并生成复查项的质量报告数据集"
        return snapshot
