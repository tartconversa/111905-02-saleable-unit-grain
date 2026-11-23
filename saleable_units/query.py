"""版本差异与下钻查询。

差异按“版本 × 月份 × 房号”对齐，并给出可核对的原因分类，使使用者能从项目
汇总下钻到具体空间份额，判断减少来自销售占用、权利限制、许可、测绘修订，
还是拆并资料不完整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .engine import Engine, EntityState
from .fractions import format_fraction

CLASSIFICATION_LABELS = {
    "lineage_incomplete": "拆并资料不完整/空间不可用",
    "evolution_change": "拆并沿革变化",
    "sales_occupation": "销售占用变化",
    "rights_restriction": "权利限制变化",
    "permit_change": "许可变化",
    "measurement_revision": "测绘修订",
    "measurement_missing": "缺少测绘",
    "unit_removed": "房号退出",
    "unit_added": "房号新增",
    "no_change": "无变化",
}

LINEAGE_CODES = {
    "change_group_invalid",
    "change_target_unresolved",
    "evolution_cycle",
    "change_lineage_blocked",
    "source_share_insufficient",
    "source_share_overflow",
    "source_overlap",
    "content_incomplete",
    "fraction_slice_mismatch",
    "physical_overlap",
}


@dataclass
class UnitDiff:
    project: str
    use: str
    unit_id: str
    base_period_key: str | None
    compare_period_key: str | None
    classification: str
    sold_delta: Decimal | None
    restricted_delta: Decimal | None
    permitted_delta: Decimal | None
    sellable_delta: Decimal | None
    physical_delta: Decimal | None
    base_status: str | None
    compare_status: str | None
    base_sellable: Decimal | None
    compare_sellable: Decimal | None
    base_blockers: list[str] = field(default_factory=list)
    compare_blockers: list[str] = field(default_factory=list)
    explanation: str = ""
    base_lineage: list[dict[str, Any]] = field(default_factory=list)
    compare_lineage: list[dict[str, Any]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "use": self.use,
            "unit_id": self.unit_id,
            "base_period_key": self.base_period_key,
            "compare_period_key": self.compare_period_key,
            "classification": self.classification,
            "classification_label": CLASSIFICATION_LABELS.get(self.classification, self.classification),
            "sold_delta": _decimal_text(self.sold_delta),
            "restricted_delta": self.restricted_delta,
            "permitted_delta": self.permitted_delta,
            "sellable_delta": self.sellable_delta,
            "physical_delta": self.physical_delta,
            "base_status": self.base_status,
            "compare_status": self.compare_status,
            "base_sellable": _decimal_text(self.base_sellable),
            "compare_sellable": _decimal_text(self.compare_sellable),
            "base_blockers": ",".join(sorted(set(self.base_blockers))),
            "compare_blockers": ",".join(sorted(set(self.compare_blockers))),
            "explanation": self.explanation,
            "base_lineage": self.base_lineage,
            "compare_lineage": self.compare_lineage,
        }


def _measure_area(state: EntityState, measure: Fraction) -> Decimal | None:
    area = state.area(measure)
    if area is None:
        return None
    return Decimal(area.numerator) / Decimal(area.denominator)


def _snapshot_day(engine: Engine, month: str | None) -> tuple[str, date] | None:
    snapshots = engine.snapshots()
    if not snapshots:
        return None
    if month is None:
        return snapshots[-1]
    for snapshot in snapshots:
        if snapshot[0] == month:
            return snapshot
    return None


def _states_by_unit(engine: Engine, day: date) -> dict[str, EntityState]:
    """同一房号在同一天应只有一个有效实体；若出现多个则分别保留以暴露冲突。"""

    result: dict[str, EntityState] = {}
    for state in engine.entity_states(day):
        key = state.period.unit_id
        if key in result:
            key = f"{key}#{state.period.key}"
        result[key] = state
    return result


def _blocker_codes(state: EntityState | None) -> list[str]:
    if state is None:
        return []
    return [blocker.code for blocker in state.blockers]


def _is_lineage_block(code: str) -> bool:
    return code in LINEAGE_CODES


def _classify(
    base: EntityState | None,
    compare: EntityState | None,
    lineage_changed: bool,
) -> tuple[str, str]:
    compare_codes = set(_blocker_codes(compare))
    base_codes = set(_blocker_codes(base))
    new_lineage = {code for code in compare_codes - base_codes if _is_lineage_block(code)}
    if new_lineage:
        return (
            "lineage_incomplete",
            "新版本出现拆并/空间完整性阻塞：" + ", ".join(sorted(new_lineage)),
        )
    if base is None and compare is not None:
        return "unit_added", "新版本出现该房号，请结合沿革确认来源空间"
    if compare is None and base is not None:
        return "unit_removed", "该房号在新版本不再有效，空间应由沿革后继承接"
    if base is None and compare is None:
        return "no_change", "两个版本均无该房号"
    assert base is not None and compare is not None

    def delta(attr: str) -> Decimal:
        before = _measure_area(base, getattr(base, attr))
        after = _measure_area(compare, getattr(compare, attr))
        if before is None or after is None:
            return Decimal(0)
        return after - before

    sold = delta("sold_measure")
    restricted = delta("restricted_measure")
    permitted = delta("permitted_measure")
    sellable = delta("sellable_measure")
    physical = _fraction_delta(base.physical_area, compare.physical_area)
    new_measurement_missing = "measurement_missing" in compare_codes and (
        "measurement_missing" not in base_codes
    )
    if new_measurement_missing:
        return "measurement_missing", "新版本缺少有效测绘，面积无法落数"
    if lineage_changed:
        return "evolution_change", "该房号的沿革组成发生变化，需按空间份额重建口径"
    if sold != 0:
        return "sales_occupation", "销售额度变化：" + f"{sold:+.6f} 平方米"
    if restricted != 0:
        return "rights_restriction", "权利限制变化：" + f"{restricted:+.6f} 平方米"
    if permitted != 0:
        return "permit_change", "许可覆盖变化：" + f"{permitted:+.6f} 平方米"
    if physical != 0:
        return "measurement_revision", "测绘面积修订：" + f"{physical:+.6f} 平方米"
    if sellable != 0:
        return "sales_occupation", "可售量变化但占用/限制未变，请核对许可与限制"
    return "no_change", "实体与计量均无变化"


def compare_versions(
    base_engine: Engine, compare_engine: Engine, month: str | None = None
) -> list[UnitDiff]:
    base_snapshot = _snapshot_day(base_engine, month)
    compare_snapshot = _snapshot_day(compare_engine, month)
    if base_snapshot is None or compare_snapshot is None:
        raise LookupError(f"月份 {month} 不在两个版本的可比快照中")
    # 用同一个截止日评估两个版本：取两者快照日期的较小值，避免把资料截止日
    # 的差异误读成业务变化。若一方在该日尚不可见某测绘，会如实体现为差异。
    day = min(base_snapshot[1], compare_snapshot[1])
    base_states = _states_by_unit(base_engine, day)
    compare_states = _states_by_unit(compare_engine, day)
    keys = sorted(set(base_states) | set(compare_states))
    diffs: list[UnitDiff] = []
    for key in keys:
        base_state = base_states.get(key)
        compare_state = compare_states.get(key)
        base_lineage = (
            base_engine.lineage(base_state.period.key) if base_state is not None else []
        )
        compare_lineage = (
            compare_engine.lineage(compare_state.period.key) if compare_state is not None else []
        )
        lineage_changed = _lineage_fingerprint(base_lineage) != _lineage_fingerprint(compare_lineage)
        classification, explanation = _classify(base_state, compare_state, lineage_changed)
        sold_delta = _delta(base_state, compare_state, "sold_measure")
        restricted_delta = _delta(base_state, compare_state, "restricted_measure")
        permitted_delta = _delta(base_state, compare_state, "permitted_measure")
        sellable_delta = _delta(base_state, compare_state, "sellable_measure")
        physical_delta = _physical_delta(base_state, compare_state)
        diffs.append(
            UnitDiff(
                project=(compare_state or base_state).period.project,  # type: ignore[union-attr]
                use=(compare_state or base_state).period.use,  # type: ignore[union-attr]
                unit_id=(compare_state or base_state).period.unit_id,  # type: ignore[union-attr]
                base_period_key=base_state.period.key if base_state else None,
                compare_period_key=compare_state.period.key if compare_state else None,
                classification=classification,
                sold_delta=sold_delta,
                restricted_delta=restricted_delta,
                permitted_delta=permitted_delta,
                sellable_delta=sellable_delta,
                physical_delta=physical_delta,
                base_status=base_state.status if base_state else None,
                compare_status=compare_state.status if compare_state else None,
                base_sellable=_measure_area(base_state, base_state.sellable_measure) if base_state else None,
                compare_sellable=_measure_area(compare_state, compare_state.sellable_measure) if compare_state else None,
                base_blockers=_blocker_codes(base_state),
                compare_blockers=_blocker_codes(compare_state),
                explanation=explanation,
                base_lineage=base_lineage,
                compare_lineage=compare_lineage,
            )
        )
    return diffs


def _fraction_delta(before: Fraction | None, after: Fraction | None) -> Decimal:
    from .fractions import fraction_to_decimal

    value = (after or Fraction(0)) - (before or Fraction(0))
    return fraction_to_decimal(value)


def _lineage_fingerprint(lineage: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            item["root_period_key"],
            item["root_start"],
            item["root_end"],
            item["coord_start"],
            item["coord_end"],
        )
        for item in lineage
    )


def _delta(base: EntityState | None, compare: EntityState | None, attr: str) -> Decimal | None:
    if base is None or compare is None:
        return None
    before = _measure_area(base, getattr(base, attr))
    after = _measure_area(compare, getattr(compare, attr))
    return (after or Decimal(0)) - (before or Decimal(0))


def _physical_delta(base: EntityState | None, compare: EntityState | None) -> Decimal | None:
    if base is None or compare is None:
        return None
    return _fraction_delta(base.physical_area, compare.physical_area)


def project_rollup(diffs: list[UnitDiff]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for diff in diffs:
        key = (diff.project, diff.use)
        bucket = groups.setdefault(
            key,
            {
                "project": diff.project,
                "use": diff.use,
                "units": 0,
                "sellable_delta": Decimal(0),
                "sold_delta": Decimal(0),
                "restricted_delta": Decimal(0),
                "permitted_delta": Decimal(0),
                "classifications": {},
            },
        )
        bucket["units"] += 1
        bucket["sellable_delta"] += diff.sellable_delta or Decimal(0)
        bucket["sold_delta"] += diff.sold_delta or Decimal(0)
        bucket["restricted_delta"] += diff.restricted_delta or Decimal(0)
        bucket["permitted_delta"] += diff.permitted_delta or Decimal(0)
        bucket["classifications"].setdefault(diff.classification, 0)
        bucket["classifications"][diff.classification] += 1
    return [groups[key] for key in sorted(groups)]


def _decimal_text(value: Decimal | Fraction | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return format(
            (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.0001")),
            "f",
        )
    return format(value.quantize(Decimal("0.0001")), "f")


def _area_text(state: EntityState, measure: Fraction) -> str | None:
    return _decimal_text(_measure_area(state, measure))


def as_of(engine: Engine, day: date) -> list[dict[str, Any]]:
    rows = []
    for state in engine.entity_states(day):
        rows.append(
            {
                "period_key": state.period.key,
                "unit_id": state.period.unit_id,
                "project": state.period.project,
                "building": state.period.building,
                "use": state.period.use,
                "valid_from": state.period.valid_from.isoformat(),
                "valid_to": state.period.valid_to.isoformat() if state.period.valid_to else None,
                "status": state.status,
                "measurement_id": state.measurement_id,
                "measurement_basis": state.measurement_basis,
                "physical_area": _decimal_text(state.physical_area),
                "contract_area": _decimal_text(state.contract_area),
                "measure_total": format_fraction(state.measure_total),
                "permitted_measure": format_fraction(state.permitted_measure),
                "sold_measure": format_fraction(state.sold_measure),
                "restricted_measure": format_fraction(state.restricted_measure),
                "sellable_measure": format_fraction(state.sellable_measure),
                "blocked_measure": format_fraction(state.blocked_measure),
                "sellable_area": _area_text(state, state.sellable_measure),
                "sold_area": _area_text(state, state.sold_measure),
                "restricted_area": _area_text(state, state.restricted_measure),
                "blocked_area": _area_text(state, state.blocked_measure),
                "permit_area": _area_text(state, state.permitted_measure),
                "blockers": [blocker.code for blocker in state.blockers],
                "blocker_detail": [
                    {
                        "code": blocker.code,
                        "severity": blocker.severity,
                        "measure": format_fraction(blocker.measure),
                        "message": blocker.message,
                    }
                    for blocker in state.blockers
                ],
                "occupancy": [
                    {
                        "contract_id": item.get("contract_id"),
                        "allocated_contract_area": _decimal_text(
                            Fraction(item["allocated_contract_area"])
                        ),
                        "measure": format_fraction(item["measure"]),
                    }
                    for item in state.occupancy
                    if "allocated_contract_area" in item
                ],
                "lineage": engine.lineage(state.period.key),
            }
        )
    return rows


def today_view(engine: Engine) -> list[dict[str, Any]]:
    """今天看到的新单元由哪些旧空间组成。"""

    rows = []
    for state in engine.entity_states(engine.model.data_as_of):
        for atom in engine.lineage(state.period.key):
            rows.append(
                {
                    "period_key": state.period.key,
                    "unit_id": state.period.unit_id,
                    "project": state.period.project,
                    "use": state.period.use,
                    "status": state.status,
                    "root_unit_id": atom["root_unit_id"],
                    "root_project": atom["root_project"],
                    "root_building": atom["root_building"],
                    "root_start": atom["root_start"],
                    "root_end": atom["root_end"],
                    "measure": format_fraction(atom["measure"]),
                    "share_of_entity": format_fraction(atom["share_of_entity"]),
                }
            )
    return rows
