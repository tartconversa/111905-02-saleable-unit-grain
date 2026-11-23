"""DuckDB 持久化：按资料版本不可变存储，并生成事实/存量/差异数据集。

入库幂等：同一 ``version_id`` 重复入库不会产生重复行。已入库版本只读，
修正后的资料生成新的 ``version_id``；旧版本保留供复查与对照。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .engine import Engine, EntityState

SCHEMA_VERSION = "3"

DDL = """
create table if not exists meta (
    schema_version varchar not null,
    created_at varchar not null
);

create table if not exists source_versions (
    version_id varchar primary key,
    source_dir varchar not null,
    manifest_sha256 varchar not null,
    row_counts varchar not null,
    file_sha256 varchar not null,
    imported_at varchar not null
);

create table if not exists unit_periods (
    version_id varchar not null,
    period_key varchar not null,
    unit_id varchar not null,
    project varchar not null,
    building varchar not null,
    use varchar not null,
    valid_from date not null,
    valid_to date,
    content_complete boolean not null,
    primary key (version_id, period_key)
);

create table if not exists evolution_groups (
    version_id varchar not null,
    change_id varchar not null,
    effective date not null,
    expected_edges integer not null,
    status varchar not null,
    reasons varchar not null,
    primary key (version_id, change_id)
);

create table if not exists evolution_edges (
    version_id varchar not null,
    change_id varchar not null,
    from_period_key varchar not null,
    to_period_key varchar not null,
    from_unit varchar not null,
    to_unit varchar not null,
    fraction_num bigint not null,
    fraction_den bigint not null,
    slice_start_num bigint not null,
    slice_start_den bigint not null,
    slice_end_num bigint not null,
    slice_end_den bigint not null
);

create table if not exists space_atoms (
    version_id varchar not null,
    period_key varchar not null,
    root_period_key varchar not null,
    root_unit_id varchar not null,
    root_project varchar not null,
    root_building varchar not null,
    root_start_num bigint not null,
    root_start_den bigint not null,
    root_end_num bigint not null,
    root_end_den bigint not null,
    coord_start_num bigint not null,
    coord_start_den bigint not null,
    coord_end_num bigint not null,
    coord_end_den bigint not null,
    measure_num bigint not null,
    measure_den bigint not null
);

create table if not exists rights (
    version_id varchar not null,
    record_id varchar not null,
    kind varchar not null,
    scope varchar not null,
    share_num bigint not null,
    share_den bigint not null,
    use varchar not null,
    effective date not null,
    reference varchar,
    slice_start_num bigint not null,
    slice_start_den bigint not null,
    slice_end_num bigint not null,
    slice_end_den bigint not null
);

create table if not exists occupations (
    version_id varchar not null,
    contract_id varchar not null,
    unit_id varchar not null,
    period_key varchar,
    share_num bigint not null,
    share_den bigint not null,
    contract_area decimal(24,6) not null,
    measurement_id varchar not null,
    effective date not null,
    slice_start_num bigint not null,
    slice_start_den bigint not null,
    slice_end_num bigint not null,
    slice_end_den bigint not null
);

create table if not exists measurements (
    version_id varchar not null,
    measurement_id varchar not null,
    unit_id varchar not null,
    basis varchar not null,
    area_sqm decimal(24,6) not null,
    effective date not null,
    received date not null
);

create table if not exists source_rows (
    version_id varchar not null,
    dataset varchar not null,
    row_index integer not null,
    payload varchar not null,
    primary key (version_id, dataset, row_index)
);

create table if not exists domain_defects (
    version_id varchar not null,
    code varchar not null,
    severity varchar not null,
    entity_type varchar not null,
    entity_key varchar not null,
    project varchar,
    building varchar,
    message varchar not null,
    detail varchar not null
);

create sequence if not exists entity_state_id start 1;

create table if not exists entity_states (
    state_id bigint primary key default nextval('entity_state_id'),
    version_id varchar not null,
    snapshot_date date not null,
    period_key varchar not null,
    unit_id varchar not null,
    project varchar not null,
    building varchar not null,
    use varchar not null,
    status varchar not null,
    measurement_id varchar,
    measurement_basis varchar,
    physical_area decimal(24,6),
    measure_total_num bigint not null,
    measure_total_den bigint not null,
    permitted_num bigint not null,
    permitted_den bigint not null,
    sold_num bigint not null,
    sold_den bigint not null,
    restricted_num bigint not null,
    restricted_den bigint not null,
    sellable_num bigint not null,
    sellable_den bigint not null,
    blocked_num bigint not null,
    blocked_den bigint not null,
    contract_area decimal(24,6) not null,
    sellable_area decimal(24,6),
    sold_area decimal(24,6),
    restricted_area decimal(24,6),
    blocked_area decimal(24,6),
    permit_area decimal(24,6)
);

create table if not exists state_blockers (
    version_id varchar not null,
    snapshot_date date not null,
    period_key varchar not null,
    code varchar not null,
    severity varchar not null,
    measure_num bigint not null,
    measure_den bigint not null,
    message varchar not null,
    detail varchar not null
);

create table if not exists contract_allocations (
    version_id varchar not null,
    snapshot_date date not null,
    period_key varchar not null,
    contract_id varchar not null,
    contract_area decimal(24,6) not null,
    allocated_area decimal(24,6) not null
);

create table if not exists stock_monthly (
    version_id varchar not null,
    month varchar not null,
    snapshot_date date not null,
    project varchar not null,
    use varchar not null,
    unit_count bigint not null,
    measure_count bigint not null,
    physical_area decimal(24,6) not null,
    permitted_area decimal(24,6) not null,
    sold_area decimal(24,6) not null,
    restricted_area decimal(24,6) not null,
    sellable_area decimal(24,6) not null,
    blocked_area decimal(24,6) not null,
    blocked_unit_count bigint not null,
    contract_area decimal(24,6) not null
);

create table if not exists version_comparisons (
    comparison_id varchar not null,
    base_version_id varchar not null,
    compare_version_id varchar not null,
    created_at varchar not null,
    primary key (comparison_id)
);

create table if not exists version_diffs (
    comparison_id varchar not null,
    project varchar not null,
    use varchar not null,
    unit_id varchar not null,
    base_period_key varchar,
    compare_period_key varchar,
    classification varchar not null,
    classification_label varchar not null,
    explanation varchar not null,
    sold_delta decimal(24,6),
    restricted_delta decimal(24,6),
    permitted_delta decimal(24,6),
    sellable_delta decimal(24,6),
    physical_delta decimal(24,6),
    base_status varchar,
    compare_status varchar,
    base_sellable decimal(24,6),
    compare_sellable decimal(24,6),
    base_blockers varchar,
    compare_blockers varchar,
    base_lineage varchar not null,
    compare_lineage varchar not null
);
"""


def _fraction_columns(value: Fraction | None, prefix: str) -> list[Any]:
    if value is None:
        return [None, None]
    return [value.numerator, value.denominator]


def _decimal(value: Fraction | None) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.000001"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class Warehouse:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            self.con = duckdb.connect(self.path, read_only=True)
        else:
            self.con = duckdb.connect(self.path)
            self.con.execute(DDL)
            rows = self.con.execute("select schema_version from meta limit 1").fetchall()
            if not rows:
                self.con.execute(
                    "insert into meta values (?, ?)",
                    [SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()],
                )
            elif rows[0][0] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"资料库结构版本为 {rows[0][0]}，程序需要 {SCHEMA_VERSION}；请使用新建路径"
                )

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 入仓
    def version_exists(self, version_id: str) -> bool:
        return bool(
            self.con.execute(
                "select 1 from source_versions where version_id = ?", [version_id]
            ).fetchone()
        )

    def import_version(self, engine: Engine) -> dict[str, Any]:
        version = engine.version
        if self.version_exists(version.version_id):
            return {
                "version_id": version.version_id,
                "status": "already_present",
                "entity_state_rows": 0,
            }
        self.con.execute("begin")
        try:
            self._insert_source(engine)
            state_rows = self._insert_snapshots(engine)
            self.con.execute("commit")
        except Exception:
            self.con.execute("rollback")
            raise
        return {
            "version_id": version.version_id,
            "status": "imported",
            "entity_state_rows": state_rows,
        }

    def _executemany(self, sql: str, rows: list[list[Any]]) -> None:
        if rows:
            self.con.executemany(sql, rows)

    def _insert_source(self, engine: Engine) -> None:
        version = engine.version
        row_values = []
        for dataset in ("units", "changes", "edges", "rights", "occupations", "measurements"):
            for index, row in enumerate(version.rows.get(dataset, [])):
                row_values.append([version.version_id, dataset, index, _json(row)])
        self._executemany(
            "insert into source_rows values (?,?,?,?)", row_values
        )
        self.con.execute(
            "insert into source_versions values (?,?,?,?,?,?)",
            [
                version.version_id,
                version.source_dir,
                version.manifest_sha256,
                _json({name: len(rows) for name, rows in version.rows.items()}),
                _json(version.file_sha256),
                datetime.now(timezone.utc).isoformat(),
            ],
        )
        self._executemany(
            "insert into unit_periods values (?,?,?,?,?,?,?,?,?)",
            [
                [
                    version.version_id,
                    period.key,
                    period.unit_id,
                    period.project,
                    period.building,
                    period.use,
                    period.valid_from,
                    period.valid_to,
                    bool(engine.content.get(period.key))
                    and not engine.period_blockers.get(period.key),
                ]
                for period in engine.periods
            ],
        )
        group_rows = []
        for change_id, state in sorted(engine.groups.items()):
            group_rows.append(
                [
                    version.version_id,
                    change_id,
                    state.change.effective,
                    state.change.expected_edges,
                    state.status,
                    _json([code for code, _ in state.reasons]),
                ]
            )
        self._executemany(
            "insert into evolution_groups values (?,?,?,?,?,?)", group_rows
        )
        edge_rows = []
        for change_id, state in sorted(engine.groups.items()):
            for resolved in state.edges:
                edge = resolved.edge
                edge_rows.append(
                    [
                        version.version_id,
                        change_id,
                        resolved.src.key,
                        resolved.dst.key,
                        edge.from_unit,
                        edge.to_unit,
                        edge.fraction.numerator,
                        edge.fraction.denominator,
                        edge.source_slice[0].numerator,
                        edge.source_slice[0].denominator,
                        edge.source_slice[1].numerator,
                        edge.source_slice[1].denominator,
                    ]
                )
        self._executemany(
            "insert into evolution_edges values (?,?,?,?,?,?,?,?,?,?,?,?)", edge_rows
        )
        atom_rows = []
        for period in engine.periods:
            for atom in engine.content.get(period.key, []):
                root = engine.period_by_key[atom.root_key]
                atom_rows.append(
                    [
                        version.version_id,
                        period.key,
                        atom.root_key,
                        root.unit_id,
                        root.project,
                        root.building,
                        atom.root[0].numerator,
                        atom.root[0].denominator,
                        atom.root[1].numerator,
                        atom.root[1].denominator,
                        atom.coord[0].numerator,
                        atom.coord[0].denominator,
                        atom.coord[1].numerator,
                        atom.coord[1].denominator,
                        atom.root_width.numerator,
                        atom.root_width.denominator,
                    ]
                )
        self._executemany(
            "insert into space_atoms values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", atom_rows
        )
        self._executemany(
            "insert into rights values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                [
                    version.version_id,
                    right.record_id,
                    right.kind,
                    right.scope,
                    right.share.numerator,
                    right.share.denominator,
                    right.use,
                    right.effective,
                    right.reference,
                    right.source_slice[0].numerator,
                    right.source_slice[0].denominator,
                    right.source_slice[1].numerator,
                    right.source_slice[1].denominator,
                ]
                for right in engine.model.rights
            ],
        )
        self._executemany(
            "insert into occupations values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                [
                    version.version_id,
                    occupation.contract_id,
                    occupation.unit_id,
                    None,
                    occupation.share.numerator,
                    occupation.share.denominator,
                    _decimal(occupation.contract_area),
                    occupation.measurement_id,
                    occupation.effective,
                    occupation.source_slice[0].numerator,
                    occupation.source_slice[0].denominator,
                    occupation.source_slice[1].numerator,
                    occupation.source_slice[1].denominator,
                ]
                for occupation in engine.model.occupations
            ],
        )
        self._executemany(
            "insert into measurements values (?,?,?,?,?,?,?)",
            [
                [
                    version.version_id,
                    measurement.measurement_id,
                    measurement.unit_id,
                    measurement.basis,
                    _decimal(measurement.area_sqm),
                    measurement.effective,
                    measurement.received,
                ]
                for measurement in engine.model.measurements
            ],
        )
        self._executemany(
            "insert into domain_defects values (?,?,?,?,?,?,?,?,?)",
            [
                [
                    version.version_id,
                    defect.code,
                    defect.severity,
                    defect.entity_type,
                    defect.entity_key,
                    defect.project,
                    defect.building,
                    defect.message,
                    _json(defect.detail),
                ]
                for defect in engine.defects
            ],
        )

    def load_stored_version(self, version_id: str) -> "SourceVersion":
        """从库内原始行重建版本，保证复查不受来源目录后续变化影响。"""

        from .source import SourceVersion

        record = self.con.execute(
            "select source_dir, manifest_sha256, file_sha256 from source_versions where version_id = ?",
            [version_id],
        ).fetchone()
        if record is None:
            raise LookupError(f"资料库中没有版本 {version_id}")
        rows: dict[str, list[Any]] = {name: [] for name in (
            "units", "changes", "edges", "rights", "occupations", "measurements"
        )}
        for dataset, payload in self.con.execute(
            "select dataset, payload from source_rows where version_id = ? order by dataset, row_index",
            [version_id],
        ).fetchall():
            rows[dataset].append(json.loads(payload))
        return SourceVersion(
            version_id=version_id,
            source_dir=record[0],
            manifest_sha256=record[1],
            file_sha256=json.loads(record[2]),
            rows=rows,
        )

    def engine_for(self, version_id: str) -> Engine:
        return Engine(self.load_stored_version(version_id))

    def _insert_snapshots(self, engine: Engine) -> int:
        version_id = engine.version.version_id
        total = 0
        stock_rows: list[list[Any]] = []
        for month, day in engine.snapshots():
            states = engine.entity_states(day)
            state_rows: list[list[Any]] = []
            blocker_rows: list[list[Any]] = []
            allocation_rows: list[list[Any]] = []
            for state in states:
                physical = state.physical_area
                sellable_area = state.area(state.sellable_measure)
                sold_area = state.area(state.sold_measure)
                restricted_area = state.area(state.restricted_measure)
                blocked_area = state.area(state.blocked_measure)
                permit_area = state.area(state.permitted_measure)
                state_rows.append(
                    [
                        version_id,
                        day,
                        state.period.key,
                        state.period.unit_id,
                        state.period.project,
                        state.period.building,
                        state.period.use,
                        state.status,
                        state.measurement_id,
                        state.measurement_basis,
                        _decimal(physical),
                        *_fraction_columns(state.measure_total, "measure_total"),
                        *_fraction_columns(state.permitted_measure, "permitted"),
                        *_fraction_columns(state.sold_measure, "sold"),
                        *_fraction_columns(state.restricted_measure, "restricted"),
                        *_fraction_columns(state.sellable_measure, "sellable"),
                        *_fraction_columns(state.blocked_measure, "blocked"),
                        _decimal(state.contract_area),
                        _decimal(sellable_area),
                        _decimal(sold_area) if sold_area is not None else None,
                        _decimal(restricted_area) if restricted_area is not None else None,
                        _decimal(blocked_area) if blocked_area is not None else None,
                        _decimal(permit_area) if permit_area is not None else None,
                    ]
                )
                for blocker in state.blockers:
                    blocker_rows.append(
                        [
                            version_id,
                            day,
                            state.period.key,
                            blocker.code,
                            blocker.severity,
                            blocker.measure.numerator,
                            blocker.measure.denominator,
                            blocker.message,
                            _json(blocker.detail),
                        ]
                    )
                for item in state.occupancy:
                    if "allocated_contract_area" in item:
                        allocation_rows.append(
                            [
                                version_id,
                                day,
                                state.period.key,
                                item["contract_id"],
                                _decimal(Fraction(item["contract_area"])),
                                _decimal(Fraction(item["allocated_contract_area"])),
                            ]
                        )
            self._executemany(
                "insert into entity_states values (default,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                state_rows,
            )
            self._executemany(
                "insert into state_blockers values (?,?,?,?,?,?,?,?,?)", blocker_rows
            )
            self._executemany(
                "insert into contract_allocations values (?,?,?,?,?,?)", allocation_rows
            )
            total += len(state_rows)
            stock_rows.extend(self._stock_rows(version_id, month, day, states))
        self._executemany(
            "insert into stock_monthly values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", stock_rows
        )
        return total

    def _stock_rows(
        self, version_id: str, month: str, day: date, states: list[EntityState]
    ) -> list[list[Any]]:
        groups: dict[tuple[str, str], list[EntityState]] = {}
        for state in states:
            groups.setdefault((state.period.project, state.period.use), []).append(state)
        rows = []
        for (project, use), items in sorted(groups.items()):
            def total(attr: str) -> Decimal:
                """份额列换算为面积后求和；没有测绘的实体不进入面积口径。"""

                value = Fraction(0)
                for item in items:
                    measure = getattr(item, attr)
                    if measure is not None:
                        value += measure
                return _decimal(value) or Decimal(0)

            def area_total(attr: str) -> Decimal:
                total_area = Decimal(0)
                for item in items:
                    area = item.area(getattr(item, attr))
                    if area is not None:
                        total_area += _decimal(area) or Decimal(0)
                return total_area

            blocked_units = sum(1 for item in items if item.status == "blocked")
            measured = [item for item in items if item.physical_area is not None]
            rows.append(
                [
                    version_id,
                    month,
                    day,
                    project,
                    use,
                    len(items),
                    len(measured),
                    _decimal(sum((item.physical_area for item in measured), Fraction(0))),
                    area_total("permitted_measure"),
                    area_total("sold_measure"),
                    area_total("restricted_measure"),
                    area_total("sellable_measure"),
                    area_total("blocked_measure"),
                    blocked_units,
                    _decimal(sum((item.contract_area for item in items), Fraction(0))),
                ]
            )
        return rows

    # ------------------------------------------------------------------ 查询辅助
    def versions(self) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "select version_id, source_dir, imported_at, row_counts from source_versions order by imported_at"
        ).fetchall()
        return [
            {
                "version_id": row[0],
                "source_dir": row[1],
                "imported_at": row[2],
                "row_counts": json.loads(row[3]),
            }
            for row in rows
        ]

    def latest_version(self) -> str | None:
        row = self.con.execute(
            "select version_id from source_versions order by imported_at desc limit 1"
        ).fetchone()
        return row[0] if row else None

    def resolve_version(self, version_id: str | None) -> str:
        if version_id == "latest" or version_id is None:
            latest = self.latest_version()
            if latest is None:
                raise LookupError("资料库中还没有任何版本；请先执行 import")
            return latest
        if not self.version_exists(version_id):
            raise LookupError(f"资料库中没有版本 {version_id}")
        return version_id

    def rows(self, sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self.con.execute(sql, list(params or []))
        names = [description[0] for description in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def record_comparison(self, base: str, compare: str, *, overwrite: bool = False) -> str:
        comparison_id = f"{base[:12]}..{compare[:12]}"
        if overwrite:
            self.con.execute(
                "delete from version_diffs where comparison_id = ?", [comparison_id]
            )
            self.con.execute(
                "delete from version_comparisons where comparison_id = ?", [comparison_id]
            )
        self.con.execute(
            "insert into version_comparisons values (?,?,?,?)",
            [comparison_id, base, compare, datetime.now(timezone.utc).isoformat()],
        )
        return comparison_id
