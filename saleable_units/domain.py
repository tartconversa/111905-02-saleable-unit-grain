"""来源行到强类型领域对象的转换，以及房号有效期的解析规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .errors import SourceError
from fractions import Fraction

from .fractions import parse_decimal, parse_fraction, parse_interval

FAR_FUTURE = date.max


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SourceError(f"不是 ISO 日期：{text!r}") from exc


@dataclass(frozen=True)
class UnitPeriod:
    unit_id: str
    project: str
    building: str
    use: str
    valid_from: date
    valid_to: date | None

    @property
    def key(self) -> str:
        return f"{self.unit_id}@{self.valid_from.isoformat()}"

    @property
    def end(self) -> date:
        return self.valid_to if self.valid_to is not None else FAR_FUTURE

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day < self.end

    def matches_reference_day(self, day: date) -> bool:
        """引用解析：允许在有效期末日当天引用（变更生效日即旧房号末日之后）。"""

        return self.valid_from <= day <= self.end


@dataclass(frozen=True)
class Change:
    change_id: str
    effective: date
    expected_edges: int


@dataclass(frozen=True)
class Edge:
    change_id: str
    from_unit: str
    to_unit: str
    fraction: Fraction
    source_slice: tuple[Fraction, Fraction]
    effective: date


@dataclass(frozen=True)
class Right:
    record_id: str
    kind: str
    scope: str
    share: Fraction
    use: str
    effective: date
    reference: str | None
    source_slice: tuple[Fraction, Fraction]


@dataclass(frozen=True)
class Occupation:
    contract_id: str
    unit_id: str
    share: Fraction
    contract_area: Fraction
    measurement_id: str
    effective: date
    source_slice: tuple[Fraction, Fraction]


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    unit_id: str
    basis: str
    area_sqm: Fraction
    effective: date
    received: date


@dataclass
class SourceModel:
    units: list[UnitPeriod]
    changes: list[Change]
    edges: list[Edge]
    rights: list[Right]
    occupations: list[Occupation]
    measurements: list[Measurement]
    data_as_of: date

    def periods_for(self, unit_id: str) -> list[UnitPeriod]:
        return sorted(
            (period for period in self.units if period.unit_id == unit_id),
            key=lambda period: period.valid_from,
        )

    def resolve_unit(self, unit_id: str, day: date) -> tuple[UnitPeriod | None, list[UnitPeriod]]:
        """按引用日期解析房号到有效期。

        返回 (唯一命中的有效期或 None, 全部候选)。房号只在有效期内有意义；
        区间末端的变更引用允许落在 valid_to 当天。
        """

        periods = self.periods_for(unit_id)
        strict = [p for p in periods if p.active_on(day)]
        if len(strict) == 1:
            return strict[0], periods
        if strict:
            return None, strict
        at_end = [p for p in periods if p.valid_from <= day == p.end]
        if len(at_end) == 1:
            return at_end[0], periods
        return None, periods


def build_source_model(rows: dict[str, list[dict[str, Any]]]) -> SourceModel:
    units = [
        UnitPeriod(
            unit_id=row["unit_id"],
            project=row["project"],
            building=row["building"],
            use=row["use"],
            valid_from=parse_date(row["valid_from"]),
            valid_to=parse_date(row["valid_to"]) if row["valid_to"] is not None else None,
        )
        for row in rows["units"]
    ]
    changes = [
        Change(
            change_id=row["change_id"],
            effective=parse_date(row["effective"]),
            expected_edges=row["expected_edges"],
        )
        for row in rows["changes"]
    ]
    change_dates = {c.change_id: c.effective for c in changes}
    edges = [
        Edge(
            change_id=row["change_id"],
            from_unit=row["from_unit"],
            to_unit=row["to_unit"],
            fraction=parse_fraction(row["fraction"], label="edges.fraction"),
            source_slice=parse_interval(row["source_slice"], label="edges.source_slice"),
            effective=change_dates[row["change_id"]],
        )
        for row in rows["edges"]
    ]
    rights = [
        Right(
            record_id=row["record_id"],
            kind=row["kind"],
            scope=row["scope"],
            share=parse_fraction(row["share"], label="rights.share"),
            use=row["use"],
            effective=parse_date(row["effective"]),
            reference=row["reference"],
            source_slice=parse_interval(row["source_slice"], label="rights.source_slice"),
        )
        for row in rows["rights"]
    ]
    occupations = [
        Occupation(
            contract_id=row["contract_id"],
            unit_id=row["unit_id"],
            share=parse_fraction(row["share"], label="occupations.share"),
            contract_area=Fraction(parse_decimal(row["contract_area"], label="occupations.contract_area")),
            measurement_id=row["measurement_id"],
            effective=parse_date(row["effective"]),
            source_slice=parse_interval(row["source_slice"], label="occupations.source_slice"),
        )
        for row in rows["occupations"]
    ]
    measurements = [
        Measurement(
            measurement_id=row["measurement_id"],
            unit_id=row["unit_id"],
            basis=row["basis"],
            area_sqm=Fraction(parse_decimal(row["area_sqm"], label="measurements.area_sqm")),
            effective=parse_date(row["effective"]),
            received=parse_date(row["received"]),
        )
        for row in rows["measurements"]
    ]

    observed = [p.valid_from for p in units]
    observed += [c.effective for c in changes]
    observed += [r.effective for r in rights]
    observed += [o.effective for o in occupations]
    observed += [m.effective for m in measurements]
    observed += [m.received for m in measurements]
    if not observed:
        raise SourceError("资料为空：units/changes/rights/occupations/measurements 均无记录，无法建立快照")
    data_as_of = max(observed)

    return SourceModel(
        units=units,
        changes=changes,
        edges=edges,
        rights=rights,
        occupations=occupations,
        measurements=measurements,
        data_as_of=data_as_of,
    )


def month_snapshots(model: SourceModel) -> list[tuple[str, date]]:
    """月库存快照：自然月末；数据截止所在月取截止日，避免用未来状态。"""

    starts = [p.valid_from for p in model.units]
    starts += [c.effective for c in model.changes]
    starts += [r.effective for r in model.rights]
    starts += [o.effective for o in model.occupations]
    if not starts:
        return []
    first = min(starts)
    snapshots: list[tuple[str, date]] = []
    cursor = date(first.year, first.month, 1)
    as_of = model.data_as_of
    while cursor <= as_of:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        snapshot = min(month_end, as_of)
        snapshots.append((cursor.strftime("%Y-%m"), snapshot))
        cursor = next_month
    return snapshots

