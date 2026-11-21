"""质量评分卡服务：计算表级数据质量多维度评分，并保留每条规则的降分贡献。

domain_chain: 质量评分卡 (dq_scorecard)
  └─ 计算 → 质量趋势 (dq_trend)
  └─ 降分依据 → 质量报告 (dq_report)
  └─ 确认 → 降分复核清单 (dq_deduction_review)

评分卡记录本身只保存结果，无法解释“这张表为什么降分”。本模块在评分结果之外
补充“规则贡献”账本：每个评分版本逐条记录规则权重、执行结果、规则得分、
加权得分和降分贡献，最终生成可复算的降分依据视图。

围绕这套账本同时提供：
- 版本化：同一张表的每次评分对应一个版本，版本内可修订，修订历史保留快照；
- 并发修订：只接纳与目标版本一致的更正，并通过修订号做乐观并发校验；
- 中断恢复：计算按规则逐条推进，检查点落在版本行上，可从中断处继续；
- 版本重放：历史快照与摘要不随新修订变化，旧报告引用的版本仍可重放；
- 未齐项：更正结果未到达的规则单独标记，既不静默丢弃也不当作已确认；
- 确认回写：版本确认结果回写降分复核清单。
"""

from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

VERSION_MODULE = "dq_score_version"
CHECKLIST_MODULE = "dq_deduction_review"
EVIDENCE_MODULE = "dq_collection_evidence"
REVISION_LOG_MODULE = "dq_score_revision_log"

DIMENSION_BY_RULE_TYPE = {
    "空值检查": "完整性",
    "值域范围": "准确性",
    "格式校验": "准确性",
    "唯一性检查": "唯一性",
    "时效性检查": "时效性",
    "参照完整性": "一致性",
}

SCENARIO_RECORD_NO = "DQ_S-1001"
SCENARIO_TABLE = "dws_daily_summary"
SCENARIO_VERSION = "v1"

# 演示场景：两条降分规则已确认，第三条规则的更正结果尚未到达。
SCENARIO_RULES: list[dict[str, Any]] = [
    {"规则编号": "DQ_R-1001", "规则名称": "订单主键空值检查", "维度": "完整性", "权重": 0.25,
     "结果": "通过", "得分": 100, "确认状态": "已确认"},
    {"规则编号": "DQ_R-1002", "规则名称": "金额值域范围检查", "维度": "准确性", "权重": 0.20,
     "结果": "通过", "得分": 100, "确认状态": "已确认"},
    {"规则编号": "DQ_R-1003", "规则名称": "日期格式校验", "维度": "准确性", "权重": 0.15,
     "结果": "失败", "得分": 40, "确认状态": "已确认"},
    {"规则编号": "DQ_R-1004", "规则名称": "订单号唯一性检查", "维度": "唯一性", "权重": 0.20,
     "结果": "失败", "得分": 30, "确认状态": "已确认"},
    {"规则编号": "DQ_R-1005", "规则名称": "分区时效性检查", "维度": "时效性", "权重": 0.10,
     "结果": "失败", "得分": 50, "确认状态": "待更正",
     "未齐原因": "更正结果未到达，按当前结果暂计",
     "待更正": {"结果": "通过", "得分": 80}},
    {"规则编号": "DQ_R-1006", "规则名称": "维表参照完整性检查", "维度": "一致性", "权重": 0.10,
     "结果": "通过", "得分": 100, "确认状态": "已确认"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _digest(contributions: list[dict[str, Any]]) -> str:
    """对贡献账本做内容摘要，用于判断快照是否被改写。"""
    payload = [
        {
            "规则编号": item.get("规则编号"),
            "权重": item.get("权重"),
            "结果": item.get("结果"),
            "得分": item.get("得分"),
            "确认状态": item.get("确认状态"),
            "纳入": item.get("纳入"),
            "修订号": item.get("修订号"),
        }
        for item in contributions
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def composite_of(contributions: list[dict[str, Any]]) -> float:
    """按规则权重和结果复算综合分：Σ(权重 × 规则得分)，只统计纳入项。"""
    total = sum(
        float(item.get("权重", 0)) * float(item.get("得分", 0))
        for item in contributions
        if item.get("纳入", True)
    )
    return round(total, 1)


def deduction_of(contributions: list[dict[str, Any]]) -> float:
    total = sum(
        float(item.get("降分贡献", 0))
        for item in contributions
        if item.get("纳入", True)
    )
    return round(total, 2)


def weight_coverage_of(contributions: list[dict[str, Any]]) -> float:
    return round(sum(
        float(item.get("权重", 0))
        for item in contributions
        if item.get("纳入", True)
    ), 4)


def _normalise_rule(rule: dict[str, Any], index: int) -> dict[str, Any]:
    rule_type = str(rule.get("规则类型") or "").strip()
    result = str(rule.get("结果") or "失败")
    score = _as_float(rule.get("得分"), None)
    if score is None:
        score = 100.0 if result == "通过" else 40.0
    pending = rule.get("确认状态") not in (None, "", "已确认")
    if rule.get("待更正") and not rule.get("确认状态"):
        pending = True
    return {
        "规则编号": str(rule.get("规则编号") or f"RULE-{index + 1:02d}"),
        "规则名称": str(rule.get("规则名称") or rule_type or f"规则{index + 1}"),
        "维度": str(rule.get("维度") or DIMENSION_BY_RULE_TYPE.get(rule_type) or "其他"),
        "权重": _as_float(rule.get("权重"), 0.0) or 0.0,
        "结果": result,
        "得分": round(float(score), 2),
        "确认状态": str(rule.get("确认状态") or ("待更正" if pending else "已确认")),
        "纳入": bool(rule.get("纳入", True)),
        "未齐原因": rule.get("未齐原因"),
        "证据编号": rule.get("证据编号"),
        "修订号": int(rule.get("修订号") or 0),
        "待更正": deepcopy(rule.get("待更正")),
    }


def _build_contributions(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把规则结果展开为可复算的贡献账本，并单独标记未齐项。"""
    contributions: list[dict[str, Any]] = []
    for index, raw in enumerate(rules):
        rule = _normalise_rule(raw, index)
        pending = rule["确认状态"] != "已确认"
        included = rule["纳入"]
        if not included:
            reason = rule["未齐原因"] or "未纳入计算"
        elif pending:
            reason = rule["未齐原因"] or "更正结果未到达，按当前结果暂计"
        else:
            reason = None
        weight = rule["权重"]
        score = rule["得分"]
        weighted = round(weight * score, 4) if included else 0.0
        deduction = round(weight * (100 - score), 4) if included else 0.0
        contributions.append({
            "规则编号": rule["规则编号"],
            "规则名称": rule["规则名称"],
            "维度": rule["维度"],
            "权重": weight,
            "结果": rule["结果"],
            "得分": score,
            "确认状态": rule["确认状态"],
            "纳入": included,
            "未齐": bool((pending or not included) and included),
            "未齐原因": reason,
            "证据编号": rule["证据编号"],
            "修订号": rule["修订号"],
            "加权得分": round(weighted, 2),
            "降分贡献": round(deduction, 2),
        })
    return contributions


def _dimension_scores(contributions: list[dict[str, Any]]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for item in contributions:
        buckets.setdefault(item["维度"], []).append(float(item["得分"]))
    result = {
        dim: round(sum(values) / len(values), 1) if values else 85.0
        for dim, values in buckets.items()
    }
    result.setdefault("完整性", 85.0)
    result.setdefault("准确性", 85.0)
    result.setdefault("时效性", 85.0)
    return result


def _totals(contributions: list[dict[str, Any]]) -> dict[str, Any]:
    included = [item for item in contributions if item.get("纳入", True)]
    return {
        "综合得分": composite_of(contributions),
        "降分合计": deduction_of(contributions),
        "权重覆盖": weight_coverage_of(contributions),
        "规则总数": len(contributions),
        "纳入规则数": len(included),
        "未齐项数": sum(
            1 for item in contributions
            if item.get("未齐") or not item.get("纳入", True)
        ),
    }


def _pending_items(contributions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "规则编号": item["规则编号"],
            "规则名称": item["规则名称"],
            "未齐原因": item.get("未齐原因") or "未纳入计算",
            "当前结果": item["结果"],
            "当前得分": item["得分"],
        }
        for item in contributions
        if item.get("未齐") or not item.get("纳入", True)
    ]


class DqScorecardService(BaseModuleService):
    MODULE = "dq_scorecard"
    ENTITY = "评分记录"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["记录编号", "目标表名", "完整性得分", "准确性得分", "时效性得分", "综合得分", "评分时间", "评分版本"]
    REQUIRED_FIELDS = ["记录编号", "目标表名", "完整性得分"]
    STATUS_ORDER = ["草稿", "计算中", "已完成", "已归档"]
    ACTION_RULES = {"发起评分": "计算中", "确认完成": "已完成", "归档记录": "已归档"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 评分计算：旧入口补齐贡献账本，新入口支持中断与恢复
    # ------------------------------------------------------------------
    def calculate_quality_score(self, table_name: str, rule_results: list[dict] | None = None) -> dict[str, Any]:
        """计算表级综合质量评分，并为这次评分写入规则贡献账本。

        聚合该表所有质量规则执行结果，分别计算各维度得分，加权汇总为综合得分。
        评分结果写入质量趋势记录，同时生成对应评分版本的降分依据视图，
        使报告可以追溯到“哪条规则、多少权重、扣了多少分”。
        """
        if rule_results is None:
            rule_results = [
                {"规则类型": "空值检查", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.25},
                {"规则类型": "值域范围", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "格式校验", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.15},
                {"规则类型": "唯一性检查", "结果": random.choice(["通过", "通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "时效性检查", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.10},
                {"规则类型": "参照完整性", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.10},
            ]

        scored_rules: list[dict[str, Any]] = []
        passed = 0
        for index, rule in enumerate(rule_results):
            result = str(rule.get("结果") or "失败")
            explicit = _as_float(rule.get("得分"), None) if "得分" in rule else None
            if explicit is not None:
                score = explicit
            elif result == "通过":
                score = 100.0
            else:
                score = random.uniform(20, 60)
            if result == "通过":
                passed += 1
            scored_rules.append({
                "规则编号": str(rule.get("规则编号") or f"DQ_R-{index + 1:04d}"),
                "规则名称": str(rule.get("规则名称") or rule.get("规则类型") or f"规则{index + 1}"),
                "规则类型": str(rule.get("规则类型") or ""),
                "维度": str(rule.get("维度") or DIMENSION_BY_RULE_TYPE.get(
                    str(rule.get("规则类型") or ""), "其他")),
                "权重": _as_float(rule.get("权重"), 0.0) or 0.0,
                "结果": result,
                "得分": round(score, 2),
                "确认状态": "已确认",
            })

        contributions = _build_contributions(scored_rules)
        totals = _totals(contributions)
        dimensions = _dimension_scores(contributions)
        completeness = dimensions["完整性"]
        accuracy = dimensions["准确性"]
        timeliness = dimensions["时效性"]
        overall = totals["综合得分"]

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "记录编号": f"DQ_S-{new_id:04d}",
            "目标表名": table_name,
            "完整性得分": str(round(completeness, 1)),
            "准确性得分": str(round(accuracy, 1)),
            "时效性得分": str(round(timeliness, 1)),
            "综合得分": str(round(overall, 1)),
            "评分时间": _now(),
            "评分版本": "v1.0",
            "status": "已完成",
            "pending": False,
            "abnormal": overall < 60,
        }
        rows.append(entry)

        # 每次评分都生成贡献账本与降分依据快照。
        self._finalise_version(
            entry, "v1.0", scored_rules, contributions,
            status="已确认", save_record=False,
        )
        self._write_trend_record(table_name, completeness, accuracy, timeliness, overall)

        return {
            "目标表名": table_name,
            "规则总数": len(rule_results),
            "通过数": passed,
            "通过率": f"{passed / len(rule_results) * 100:.1f}%" if rule_results else "N/A",
            "完整性得分": round(completeness, 1),
            "准确性得分": round(accuracy, 1),
            "时效性得分": round(timeliness, 1),
            "综合得分": round(overall, 1),
            "降分合计": totals["降分合计"],
            "等级": "A" if overall >= 90 else "B" if overall >= 75 else "C" if overall >= 60 else "D",
            "记录编号": entry["记录编号"],
        }

    def start_calculation(
        self,
        record_id: int,
        *,
        version: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        stop_after: int | None = None,
        restart: bool = False,
    ) -> dict[str, Any]:
        """启动一次评分计算；stop_after 用于模拟计算中断并留下检查点。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        version = version or str(record.get("评分版本") or "v1")

        row = self._find_version(record.get("记录编号"), version)
        if row is not None and row.get("状态") == "计算中" and not restart:
            return {"ok": False, "message": f"评分版本 {version} 已有未完成计算，请调用恢复接口"}
        if row is not None and row.get("状态") != "计算中" and not restart:
            return {"ok": False, "message": f"评分版本 {version} 已完成计算，如需重算请显式 restart"}

        if rules is None:
            if row is not None and row.get("规则数据"):
                rules = row["规则数据"]
            else:
                return {"ok": False, "message": "缺少规则权重与结果，无法计算贡献"}
        rules = [_normalise_rule(rule, index) for index, rule in enumerate(rules)]
        if not rules:
            return {"ok": False, "message": "规则列表为空，无法计算贡献"}

        if row is not None:
            row.update({
                "规则数据": deepcopy(rules),
                "贡献快照": [],
                "修订号": 0,
                "修订历史": [],
                "检查点": 0,
                "计划规则数": len(rules),
            })
        else:
            row = self._new_version_row(record, version, rules)
            store.rows(VERSION_MODULE).append(row)

        total = len(rules)
        stop = total if stop_after is None else max(0, min(int(stop_after), total))
        contributions = _build_contributions(rules[:stop])

        if stop < total:
            # 检查点只推进贡献快照，规则数据始终保留完整规则集，恢复时才不会丢规则。
            self._save_version(row, "计算中", rules, contributions,
                               checkpoint=stop, total_rules=total)
            self._mark_record(record, version, contributions, status="计算中", pending=True,
                              abnormal=False, score=None)
            return {
                "ok": True,
                "记录编号": record.get("记录编号"),
                "评分版本": version,
                "状态": "计算中",
                "检查点": stop,
                "已处理规则数": stop,
                "规则总数": total,
                "综合得分": _totals(contributions)["综合得分"],
                "未齐项": _pending_items(contributions),
                "message": f"计算在 {stop}/{total} 条规则处中断，可从检查点恢复",
            }

        self._finalise_version(record, version, rules, contributions, status="待确认")
        return self._version_summary(record, version, row, contributions)

    def resume_calculation(self, record_id: int, version: str | None = None) -> dict[str, Any]:
        """从检查点恢复中断的评分计算，只继续处理剩余规则。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        version = version or str(record.get("评分版本") or "v1")
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            return {"ok": False, "message": f"评分版本 {version} 不存在"}
        if row.get("状态") != "计算中":
            return {
                "ok": True,
                "记录编号": record.get("记录编号"),
                "评分版本": version,
                "状态": row.get("状态"),
                "检查点": row.get("检查点"),
                "综合得分": row.get("综合得分"),
                "message": "该版本计算已结束，无需恢复",
            }

        rules = deepcopy(row.get("规则数据") or [])
        checkpoint = int(row.get("检查点") or 0)
        contributions = _build_contributions(rules)
        self._finalise_version(record, version, rules, contributions, status="待确认")
        return {
            **self._version_summary(record, version, row, contributions),
            "恢复自检查点": checkpoint,
            "message": f"已从检查点 {checkpoint} 恢复并完成计算",
        }

    # ------------------------------------------------------------------
    # 贡献与降分依据视图
    # ------------------------------------------------------------------
    def build_deduction_view(self, record_id: int, version: str | None = None) -> dict[str, Any] | None:
        """读取评分版本的贡献账本，生成降分依据视图。

        视图同时给出规则权重、规则得分、加权得分和降分贡献，并按降分贡献降序
        排列降分依据；未齐项单独成列表，不混入已确认的降分明细。
        """
        record = store.find(self.MODULE, record_id)
        if record is None:
            return None
        version = version or str(record.get("评分版本") or "v1")
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            return None

        contributions = deepcopy(row.get("贡献快照") or [])
        totals = _totals(contributions)
        uncomputed = self._uncomputed_items(row, contributions)
        if uncomputed:
            totals["未齐项数"] += len(uncomputed)
        complete = row.get("状态") != "计算中"
        recompute = self.recompute_version(record_id, version)
        deductions = sorted(
            [item for item in contributions if item.get("纳入", True) and item["降分贡献"] > 0],
            key=lambda item: item["降分贡献"],
            reverse=True,
        )
        return {
            "记录编号": record.get("记录编号"),
            "目标表名": record.get("目标表名"),
            "评分版本": version,
            "版本状态": row.get("状态"),
            "修订号": row.get("修订号", 0),
            "综合得分": totals["综合得分"],
            "降分合计": totals["降分合计"],
            "权重覆盖": totals["权重覆盖"],
            "规则总数": max(totals["规则总数"], len(row.get("规则数据") or [])),
            "纳入规则数": totals["纳入规则数"],
            "未齐项数": totals["未齐项数"],
            "规则贡献": contributions,
            "降分依据": deductions,
            "规则权重与得分": [
                {
                    "规则编号": item["规则编号"],
                    "规则名称": item["规则名称"],
                    "权重": item["权重"],
                    "结果": item["结果"],
                    "得分": item["得分"],
                    "确认状态": item["确认状态"],
                }
                for item in contributions
            ],
            "未齐项": _pending_items(contributions) + uncomputed,
            "计算进度": {
                "检查点": row.get("检查点", 0),
                "计划规则数": row.get("计划规则数", totals["规则总数"]),
                "已完成": complete,
            },
            # 中断中的版本只覆盖了部分规则，不能宣称“可复算”。
            "可复算": bool(recompute.get("可复算")) and complete,
            "复算": recompute,
            "摘要": row.get("摘要"),
            "更新时间": row.get("更新时间"),
        }

    def _uncomputed_items(self, row: dict[str, Any],
                          contributions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """计算中断时，尚未进入贡献账本的规则要单独标明是“未计算”而不是“通过”。"""
        if row.get("状态") != "计算中":
            return []
        done = {item["规则编号"] for item in contributions}
        return [
            {
                "规则编号": rule.get("规则编号"),
                "规则名称": rule.get("规则名称"),
                "未齐原因": "计算中断，尚未计入贡献账本",
                "当前结果": rule.get("结果"),
                "当前得分": rule.get("得分"),
            }
            for rule in row.get("规则数据") or []
            if rule.get("规则编号") not in done
        ]

    def recompute_version(
        self,
        record_id: int,
        version: str | None = None,
        *,
        contributions: list[dict[str, Any]] | None = None,
        expected: float | None = None,
    ) -> dict[str, Any]:
        """按规则权重和结果复算综合分，并与版本记录（或外部期望值）比对。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "可复算": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        version = version or str(record.get("评分版本") or "v1")
        row = self._find_version(record.get("记录编号"), version)

        if contributions is None:
            if row is None:
                return {"ok": False, "可复算": False, "message": f"评分版本 {version} 不存在"}
            contributions = deepcopy(row.get("贡献快照") or [])

        computed = composite_of(contributions)
        by_deduction = round(
            100 * weight_coverage_of(contributions) - deduction_of(contributions), 1)
        target = expected
        if target is None and row is not None and row.get("综合得分") is not None:
            target = _as_float(row.get("综合得分"), None)
        difference = None if target is None else round(computed - float(target), 2)
        reproducible = difference is not None and abs(difference) < 0.05
        return {
            "ok": True,
            "记录编号": record.get("记录编号"),
            "评分版本": version,
            "修订号": row.get("修订号") if row is not None else None,
            "状态": row.get("状态") if row is not None else None,
            "可复算": reproducible,
            "综合得分": computed,
            "按权重复算": computed,
            "按降分复算": by_deduction,
            "版本记录综合得分": target,
            "差异": difference,
            "权重覆盖": weight_coverage_of(contributions),
            "降分合计": deduction_of(contributions),
            "摘要": _digest(contributions),
            "分项": [
                {
                    "规则编号": item["规则编号"],
                    "权重": item["权重"],
                    "结果": item["结果"],
                    "得分": item["得分"],
                    "加权得分": item["加权得分"],
                    "降分贡献": item["降分贡献"],
                    "纳入": item["纳入"],
                    "未齐": item.get("未齐", False),
                }
                for item in contributions
            ],
        }

    def replay_version(
        self,
        record_id: int,
        version: str | None = None,
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """重放某个评分版本（可指定历史修订号），不读取后续修订内容。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "可重放": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        version = version or str(record.get("评分版本") or "v1")
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            return {"ok": False, "可重放": False, "message": f"评分版本 {version} 不存在"}

        snapshot = deepcopy(row.get("贡献快照") or [])
        stored_score = row.get("综合得分")
        stored_digest = row.get("摘要")
        revision_no = row.get("修订号", 0)
        if revision is not None and int(revision) != int(row.get("修订号", 0)):
            history = row.get("修订历史") or []
            matched = next(
                (item for item in history if int(item.get("修订号", -1)) == int(revision)),
                None,
            )
            if matched is None:
                return {
                    "ok": False,
                    "可重放": False,
                    "message": f"评分版本 {version} 不存在修订号 {revision} 的快照",
                }
            snapshot = deepcopy(matched.get("贡献快照") or [])
            stored_score = matched.get("综合得分")
            stored_digest = matched.get("摘要")
            revision_no = matched.get("修订号")

        computed = composite_of(snapshot)
        digest = _digest(snapshot)
        score_match = stored_score is not None and abs(computed - float(stored_score)) < 0.05
        digest_match = digest == stored_digest
        return {
            "ok": True,
            "可重放": bool(score_match and digest_match),
            "记录编号": record.get("记录编号"),
            "评分版本": version,
            "修订号": revision_no,
            "快照综合得分": _as_float(stored_score, None),
            "按权重复算": computed,
            "差异": None if stored_score is None else round(computed - float(stored_score), 2),
            "摘要一致": digest_match,
            "规则数": len(snapshot),
            "分项": snapshot,
        }

    def list_versions(self, record_id: int) -> list[dict[str, Any]]:
        record = store.find(self.MODULE, record_id)
        if record is None:
            return []
        return [
            deepcopy(row)
            for row in store.rows(VERSION_MODULE)
            if row.get("记录编号") == record.get("记录编号")
        ]

    # ------------------------------------------------------------------
    # 修订、确认与证据
    # ------------------------------------------------------------------
    def accept_correction(
        self,
        record_id: int,
        *,
        version: str | None = None,
        rule_id: str,
        result: str,
        score: float | None,
        evidence_id: str | None = None,
        expected_revision: int | None = None,
        operator: str = "质量平台",
    ) -> dict[str, Any]:
        """接纳一条规则更正：只接纳同一版本的贡献，并做修订号并发校验。

        更正只改动目标版本的规则项，随后按新的权重和结果复算同版本总分；
        旧修订的快照会归档到修订历史，供旧报告重放。
        """
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        current_version = str(record.get("评分版本") or "v1")
        version = version or current_version
        if version != current_version:
            return {
                "ok": False,
                "版本冲突": True,
                "message": f"版本不一致：只接纳当前评分版本 {current_version} 的贡献，"
                           f"拒绝来自 {version} 的贡献",
            }
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            return {"ok": False, "message": f"评分版本 {version} 不存在"}

        current_revision = int(row.get("修订号") or 0)
        if expected_revision is not None and int(expected_revision) != current_revision:
            return {
                "ok": False,
                "并发冲突": True,
                "当前修订号": current_revision,
                "message": f"并发修订冲突：期望修订号 {expected_revision}，当前为 {current_revision}",
            }

        rules = deepcopy(row.get("规则数据") or [])
        target = next((rule for rule in rules if rule.get("规则编号") == rule_id), None)
        if target is None:
            return {"ok": False, "message": f"评分版本 {version} 中不存在规则 {rule_id}"}

        previous_score = _as_float(target.get("得分"), None)
        previous_result = target.get("结果")
        self._archive_revision(row)
        target.update({
            "结果": result,
            "得分": round(float(score), 2) if score is not None else (
                100.0 if result == "通过" else 40.0),
            "确认状态": "已确认",
            "未齐原因": None,
            "待更正": None,
            "证据编号": evidence_id or target.get("证据编号"),
            "修订号": int(target.get("修订号") or 0) + 1,
        })
        row["规则数据"] = rules
        row["修订号"] = current_revision + 1
        contributions = _build_contributions(rules)
        self._save_version(row, "待确认", rules, contributions, checkpoint=len(rules))
        self._mark_record(record, version, contributions, status="已完成", pending=True,
                          abnormal=composite_of(contributions) < 60)
        self._log_revision(record, version, rule_id, previous_result, previous_score,
                           result, target["得分"], row["修订号"], operator, evidence_id)

        summary = self._version_summary(record, version, row, contributions)
        return {
            **summary,
            "规则编号": rule_id,
            "原结果": previous_result,
            "原得分": previous_score,
            "新结果": result,
            "新得分": target["得分"],
            "修订号": row["修订号"],
            "message": f"已接纳 {version} 的更正，同版本综合分已复算",
        }

    def confirm_version(
        self,
        record_id: int,
        version: str | None = None,
        *,
        reviewer: str = "质量平台",
    ) -> dict[str, Any]:
        """确认评分版本，并把确认结果回写降分复核清单。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "message": f"评分记录 {record_id} 不存在或已归档"}
        version = version or str(record.get("评分版本") or "v1")
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            return {"ok": False, "message": f"评分版本 {version} 不存在"}

        contributions = deepcopy(row.get("贡献快照") or [])
        pending = _pending_items(contributions) + self._uncomputed_items(row, contributions)
        if pending:
            return {
                "ok": False,
                "未齐项": pending,
                "message": "存在未齐项，不能确认评分版本：" + "、".join(
                    str(item["规则编号"]) for item in pending),
            }

        now = _now()
        row.update({"状态": "已确认", "确认时间": now, "确认人": reviewer,
                    "检查点": len(row.get("规则数据") or []), "更新时间": now})
        record.update({
            "评分版本": version,
            "综合得分": str(row.get("综合得分")),
            "status": "已完成",
            "pending": False,
            "abnormal": (_as_float(row.get("综合得分"), 0) or 0) < 60,
        })

        written = 0
        for item in store.rows(CHECKLIST_MODULE):
            if (item.get("记录编号") == record.get("记录编号")
                    and item.get("评分版本") == version):
                item["确认结果"] = "已确认"
                item["确认时间"] = now
                item["确认人"] = reviewer
                if item.get("派工状态") == "已派发":
                    item["状态"] = "已关闭"
                    item["pending"] = False
                else:
                    item["状态"] = "待派工（版本已确认）"
                    item["pending"] = True
                written += 1

        return {
            "ok": True,
            "记录编号": record.get("记录编号"),
            "评分版本": version,
            "状态": "已确认",
            "综合得分": row.get("综合得分"),
            "回写复核项数": written,
            "message": f"评分版本已确认，回写 {written} 条降分复核清单",
        }

    def add_evidence(
        self,
        *,
        record_no: str,
        version: str,
        revision: int | None = None,
        report_no: str | None = None,
        evidence_type: str = "采集证据",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一份采集证据，关联评分版本、规则和报告。"""
        payload = payload or {}
        rows = store.rows(EVIDENCE_MODULE)
        new_id = _next_id(EVIDENCE_MODULE)
        entry = {
            "id": new_id,
            "证据编号": str(payload.get("证据编号") or f"EV-{new_id:04d}"),
            "证据类型": evidence_type,
            "记录编号": record_no,
            "评分版本": version,
            "版本修订号": revision,
            "报告编号": report_no,
            "规则编号": payload.get("规则编号"),
            "采集批次": payload.get("采集批次") or f"BATCH-{new_id:04d}",
            "采集时间": payload.get("采集时间") or _now(),
            "样本行数": payload.get("样本行数"),
            "失败行数": payload.get("失败行数"),
            "结论": payload.get("结论") or "采集完成",
            "摘要": payload.get("摘要") or "更正结果采集证据",
            "登记时间": _now(),
            "status": "已登记",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)
        return entry

    def list_evidence(self, report_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(EVIDENCE_MODULE)
        if report_no:
            rows = [row for row in rows if row.get("报告编号") == report_no]
        return [deepcopy(row) for row in rows]

    # ------------------------------------------------------------------
    # 内部：版本行、复算摘要、修订历史
    # ------------------------------------------------------------------
    def _find_version(self, record_no: Any, version: str) -> dict[str, Any] | None:
        for row in store.rows(VERSION_MODULE):
            if row.get("记录编号") == record_no and row.get("评分版本") == version:
                return row
        return None

    def _new_version_row(self, record: dict[str, Any], version: str,
                         rules: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": _next_id(VERSION_MODULE),
            "记录编号": record.get("记录编号"),
            "目标表名": record.get("目标表名"),
            "评分版本": version,
            "状态": "计算中",
            "规则数据": deepcopy(rules),
            "贡献快照": [],
            "检查点": 0,
            "计划规则数": len(rules),
            "修订号": 0,
            "修订历史": [],
            "创建时间": _now(),
            "更新时间": _now(),
        }

    def _save_version(self, row: dict[str, Any], status: str,
                      rules: list[dict[str, Any]],
                      contributions: list[dict[str, Any]],
                      *, checkpoint: int,
                      total_rules: int | None = None) -> None:
        totals = _totals(contributions)
        row.update({
            "状态": status,
            "规则数据": deepcopy(rules),
            "贡献快照": deepcopy(contributions),
            "检查点": checkpoint,
            "规则总数": totals["规则总数"],
            # 计算中断时“规则总数”只覆盖已处理部分，用完整规则数单独记录进度分母。
            "计划规则数": total_rules if total_rules is not None else len(rules),
            "综合得分": totals["综合得分"],
            "降分合计": totals["降分合计"],
            "权重覆盖": totals["权重覆盖"],
            "纳入规则数": totals["纳入规则数"],
            "未齐项数": totals["未齐项数"],
            "摘要": _digest(contributions),
            "更新时间": _now(),
        })

    def _archive_revision(self, row: dict[str, Any]) -> None:
        """把当前修订快照归档，保证旧报告引用旧修订时可以重放。"""
        history = row.setdefault("修订历史", [])
        history.append({
            "修订号": row.get("修订号", 0),
            "综合得分": row.get("综合得分"),
            "降分合计": row.get("降分合计"),
            "摘要": row.get("摘要"),
            "归档时间": _now(),
            "贡献快照": deepcopy(row.get("贡献快照") or []),
        })

    def _finalise_version(self, record: dict[str, Any], version: str,
                          rules: list[dict[str, Any]],
                          contributions: list[dict[str, Any]],
                          *, status: str, save_record: bool = True) -> None:
        row = self._find_version(record.get("记录编号"), version)
        if row is None:
            row = self._new_version_row(record, version, rules)
            store.rows(VERSION_MODULE).append(row)
        self._save_version(row, status, rules, contributions, checkpoint=len(rules))
        if save_record:
            self._mark_record(record, version, contributions, status="已完成",
                              pending=status != "已确认",
                              abnormal=_totals(contributions)["综合得分"] < 60)

    def _mark_record(self, record: dict[str, Any], version: str,
                     contributions: list[dict[str, Any]], *, status: str,
                     pending: bool, abnormal: bool,
                     score: float | None = None) -> None:
        dimensions = _dimension_scores(contributions)
        totals = _totals(contributions)
        record.update({
            "评分版本": version,
            "完整性得分": str(round(dimensions["完整性"], 1)),
            "准确性得分": str(round(dimensions["准确性"], 1)),
            "时效性得分": str(round(dimensions["时效性"], 1)),
            "综合得分": str(round(totals["综合得分"] if score is None else score, 1)),
            "评分时间": _now(),
            "status": status,
            "pending": pending,
            "abnormal": abnormal,
        })

    def _version_summary(self, record: dict[str, Any], version: str,
                         row: dict[str, Any],
                         contributions: list[dict[str, Any]]) -> dict[str, Any]:
        totals = _totals(contributions)
        uncomputed = self._uncomputed_items(row, contributions)
        pending = _pending_items(contributions) + uncomputed
        return {
            "ok": True,
            "记录编号": record.get("记录编号"),
            "目标表名": record.get("目标表名"),
            "评分版本": version,
            "状态": row.get("状态"),
            "修订号": row.get("修订号", 0),
            "综合得分": totals["综合得分"],
            "降分合计": totals["降分合计"],
            "权重覆盖": totals["权重覆盖"],
            "纳入规则数": totals["纳入规则数"],
            "未齐项数": len(pending),
            "未齐项": pending,
            "摘要": row.get("摘要"),
        }

    def _log_revision(self, record: dict[str, Any], version: str, rule_id: str,
                      old_result: Any, old_score: Any, new_result: str, new_score: float,
                      revision: int, operator: str, evidence_id: str | None) -> None:
        rows = store.rows(REVISION_LOG_MODULE)
        rows.append({
            "id": _next_id(REVISION_LOG_MODULE),
            "记录编号": record.get("记录编号"),
            "评分版本": version,
            "修订号": revision,
            "规则编号": rule_id,
            "原结果": old_result,
            "原得分": old_score,
            "新结果": new_result,
            "新得分": new_score,
            "操作人": operator,
            "证据编号": evidence_id,
            "修订时间": _now(),
            "status": "已登记",
            "pending": False,
            "abnormal": False,
        })

    def _write_trend_record(self, table_name: str, completeness: float, accuracy: float,
                            timeliness: float, overall: float) -> None:
        """聚合评分数据写入质量趋势记录。"""
        trend_rows = store.rows("dq_trend")
        new_tid = max((int(r.get("id", 0)) for r in trend_rows), default=0) + 1
        trend_rows.append({
            "id": new_tid,
            "记录编号": f"DQ_T-{new_tid:04d}",
            "目标表名": table_name,
            "日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完备率": f"{completeness:.1f}%",
            "准确率": f"{accuracy:.1f}%",
            "及时率": f"{timeliness:.1f}%",
            "综合质量": f"{overall:.1f}",
            "趋势标记": "正常" if overall >= 75 else "下降趋势",
            "status": "正常" if overall >= 75 else "下降趋势",
            "pending": False,
            "abnormal": overall < 60,
        })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "计算"
        snapshot["desc"] = "计算表级多维度质量评分并聚合为评分卡"
        snapshot["artifact"] = "质量评分卡数据集"
        snapshot["artifact_desc"] = "聚合质量规则执行结果并保留规则贡献的评分卡数据集"
        snapshot["score_versions"] = len(store.rows(VERSION_MODULE))
        return snapshot


def ensure_scorecard_scenario() -> dict[str, Any]:
    """幂等准备演示场景：同一张表两条降分规则已确认，第三条更正尚未到达。"""
    rows = store.rows("dq_scorecard")
    existing = next(
        (row for row in rows if row.get("记录编号") == SCENARIO_RECORD_NO),
        None,
    )
    if existing is None:
        service = DqScorecardService()
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        existing = {
            "id": new_id,
            "记录编号": SCENARIO_RECORD_NO,
            "目标表名": SCENARIO_TABLE,
            "完整性得分": "—",
            "准确性得分": "—",
            "时效性得分": "—",
            "综合得分": "—",
            "评分时间": _now(),
            "评分版本": SCENARIO_VERSION,
            "status": "计算中",
            "pending": True,
            "abnormal": False,
        }
        rows.append(existing)
        service.start_calculation(
            new_id,
            version=SCENARIO_VERSION,
            rules=deepcopy(SCENARIO_RULES),
            restart=True,
        )
        return existing

    # 已有记录但没有贡献账本（例如种子数据被重建）：补建版本快照。
    if service_version_missing(existing):
        DqScorecardService().start_calculation(
            int(existing["id"]),
            version=str(existing.get("评分版本") or SCENARIO_VERSION),
            rules=deepcopy(SCENARIO_RULES),
            restart=True,
        )
    return existing


def service_version_missing(record: dict[str, Any]) -> bool:
    version = str(record.get("评分版本") or SCENARIO_VERSION)
    return not any(
        row.get("记录编号") == record.get("记录编号") and row.get("评分版本") == version
        for row in store.rows(VERSION_MODULE)
    )
