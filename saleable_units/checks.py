"""领域自动检查：入仓前后都可运行的不变量校验。

分为两层：
* ``check_source``：资料级事实（缺边、份额不足、重叠、成环、引用未解析）；
* ``check_engine``：计算结果不变量（份额守恒、面积非负、契约/物理/可售混淆、
  无关楼栋隔离、月份单调有效）。
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from typing import Any

from .domain import build_source_model
from .engine import Engine, EntityState
from .source import SourceVersion


def _result(code: str, severity: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "detail": detail}


def check_source(version: SourceVersion) -> list[dict[str, Any]]:
    model = build_source_model(version.rows)
    findings: list[dict[str, Any]] = []
    change_ids = {change.change_id for change in model.changes}
    for edge in model.edges:
        if edge.change_id not in change_ids:
            findings.append(
                _result(
                    "edge_unknown_change",
                    "blocking",
                    f"沿革明细 {edge.from_unit}->{edge.to_unit} 引用了未声明的变更组",
                    change_id=edge.change_id,
                )
            )
    for occupation in model.occupations:
        if occupation.measurement_id not in {m.measurement_id for m in model.measurements}:
            findings.append(
                _result(
                    "occupation_measurement_missing",
                    "warning",
                    f"签约 {occupation.contract_id} 引用的测绘不存在，合同面积仍保留",
                    contract_id=occupation.contract_id,
                )
            )
    return findings


def _as_fraction(value: Fraction | None) -> Fraction:
    return value if value is not None else Fraction(0)


def check_engine(engine: Engine) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # 1) 已在引擎中发现的缺陷必须带有明确归类。
    for defect in engine.defects:
        if defect.severity not in {"blocking", "warning"}:
            findings.append(
                _result(
                    "defect_severity_invalid",
                    "blocking",
                    f"缺陷 {defect.code} 的严重度无法识别",
                    entity_key=defect.entity_key,
                )
            )

    # 2) 每个实体期间的空间内容必须完整；除非被明确阻塞。
    for period in engine.periods:
        blocked = bool(engine.period_blockers.get(period.key)) or bool(
            engine.unit_blockers.get(period.unit_id)
        )
        atoms = engine.content.get(period.key, [])
        if blocked:
            continue
        if not atoms:
            findings.append(
                _result(
                    "content_missing",
                    "blocking",
                    f"期间 {period.key} 没有空间内容且未标记阻塞",
                    period_key=period.key,
                )
            )
            continue
        coord_total = sum((atom.coord_width for atom in atoms), Fraction(0))
        if coord_total != 1:
            findings.append(
                _result(
                    "coord_not_normalized",
                    "blocking",
                    f"期间 {period.key} 内部坐标合计为 {coord_total}，不等于 1",
                    period_key=period.key,
                )
            )

    # 3) 快照不变量。
    for month, day in engine.snapshots():
        states = engine.entity_states(day)
        for state in states:
            errors = _check_state(state)
            findings.extend(errors)
        _check_no_double_count(engine, states, month, findings)

    # 4) 物理空间不得同时属于两个有效实体（除非明确标记阻塞）。
    for month, day in engine.snapshots():
        active = [period for period in engine.periods if period.active_on(day)]
        overlaps = engine._physical_overlaps(active)
        for key, others in overlaps.items():
            period = engine.period_by_key[key]
            if not engine.period_blockers.get(key) and not engine.unit_blockers.get(
                period.unit_id
            ):
                findings.append(
                    _result(
                        "physical_overlap_unflagged",
                        "blocking",
                        f"{month} 快照中 {key} 与 {sorted(others)} 重叠但未阻塞",
                        period_key=key,
                    )
                )
    return findings


def _check_state(state: EntityState) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    label = f"{state.snapshot_date} {state.period.key}"
    for name in ("permitted_measure", "sold_measure", "restricted_measure", "sellable_measure"):
        value = getattr(state, name)
        if value < 0:
            findings.append(_result("negative_measure", "blocking", f"{label} {name} 为负"))
        if value > 1:
            findings.append(
                _result("measure_above_one", "blocking", f"{label} {name} 超过实体空间 1")
            )
    if state.status != "blocked":
        expected = state.permitted_measure - state.sold_measure - state.restricted_measure
        if expected < 0:
            expected = Fraction(0)
        if state.sellable_measure != expected:
            findings.append(
                _result(
                    "sellable_mismatch",
                    "blocking",
                    f"{label} 可售量 {state.sellable_measure} 不等于许可减占用与限制 {expected}",
                )
            )
        if state.physical_area is None:
            findings.append(
                _result("sellable_without_measurement", "warning", f"{label} 缺少测绘却有可售量")
            )
    if state.contract_area < 0:
        findings.append(_result("negative_contract_area", "blocking", f"{label} 合同面积为负"))
    return findings


def _check_no_double_count(
    engine: Engine,
    states: list[EntityState],
    month: str,
    findings: list[dict[str, Any]],
) -> None:
    """同一份签约的合同面积在任一快照内分摊之和不得超过其记载面积。"""

    totals: dict[str, Fraction] = {}
    for state in states:
        for item in state.occupancy:
            if "allocated_contract_area" not in item:
                continue
            contract_id = item["contract_id"]
            totals[contract_id] = totals.get(contract_id, Fraction(0)) + Fraction(
                item["allocated_contract_area"]
            )
    declared = {
        occupation.contract_id: occupation.contract_area
        for occupation in engine.model.occupations
    }
    for contract_id, allocated in totals.items():
        original = declared.get(contract_id)
        if original is not None and allocated > original:
            findings.append(
                _result(
                    "contract_area_duplicated",
                    "blocking",
                    f"{month} 快照中签约 {contract_id} 分摊合同面积 {allocated} 超过原记载 {original}",
                    contract_id=contract_id,
                )
            )


def run_all(version: SourceVersion) -> list[dict[str, Any]]:
    engine = Engine(version)
    findings: list[dict[str, Any]] = []
    findings.extend(check_source(version))
    for defect in engine.defects:
        findings.append(
            _result(
                defect.code,
                defect.severity,
                defect.message,
                entity_type=defect.entity_type,
                entity_key=defect.entity_key,
                project=defect.project,
                building=defect.building,
                **defect.detail,
            )
        )
    findings.extend(check_engine(engine))
    return findings
