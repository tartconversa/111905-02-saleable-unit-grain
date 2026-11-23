"""空间沿革几何、权利/占用映射与可售状态计算。

坐标模型
--------
* 每个 ``UnitPeriod``（房号 × 有效期）拥有内部坐标 ``[0,1)``，只用于解释该
  期间资料里的 ``source_slice``。
* 每个物理片段 ``Atom`` 记录它在沿革根空间中的区间 ``root``。根空间是所有
  后续拆并都无法再细分的原始物理空间；同一片空间在任何房号下都共享同一个
  ``root_key`` 与 ``root`` 区间。
* 拆并只做相似变换：面积（区间长度）在根空间中保持，内部坐标按变更份额
  归一化。签约和权利因此随物理空间延续，不会因改名复制或消失。

计量口径
--------
* 沿革：``fraction`` 必须等于 ``source_slice`` 长度；同一来源单元在本变更组
  内的来源区间必须恰好铺满 ``[0,1)``。
* 权利/占用：``share`` 是作用在 scope 上的权益比例，``source_slice`` 只负责
  定位。一个实体分到的量 = ``share × 该实体覆盖的来源区间长度 / 来源区间总长``。
  这样拆分后各新单元分摊之和仍等于原 ``share``，不会复制成交面积。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Any, Iterable

from .domain import (
    Change,
    Edge,
    Measurement,
    Occupation,
    Right,
    SourceModel,
    UnitPeriod,
    build_source_model,
    month_snapshots,
)
from .fractions import (
    format_fraction,
    format_interval,
    interval_intersection,
    interval_measure,
)
from .source import SourceVersion

BLOCKING = "blocking"
WARNING = "warning"


@dataclass(frozen=True)
class Atom:
    root_key: str
    root: tuple[Fraction, Fraction]
    coord: tuple[Fraction, Fraction]

    @property
    def root_width(self) -> Fraction:
        return self.root[1] - self.root[0]

    @property
    def coord_width(self) -> Fraction:
        return self.coord[1] - self.coord[0]


@dataclass
class Defect:
    code: str
    severity: str
    entity_type: str
    entity_key: str
    project: str | None
    building: str | None
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedEdge:
    edge: Edge
    src: UnitPeriod
    dst: UnitPeriod


@dataclass
class GroupState:
    change: Change
    status: str = "valid"
    reasons: list[tuple[str, str]] = field(default_factory=list)
    edges: list[ResolvedEdge] = field(default_factory=list)
    involved: dict[str, UnitPeriod] = field(default_factory=dict)
    shape: str = "valid"
    shape_reasons: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Blocker:
    code: str
    severity: str
    measure: Fraction
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityState:
    period: UnitPeriod
    snapshot_date: date
    status: str
    physical_area: Fraction | None
    measure_total: Fraction
    permitted_measure: Fraction
    sold_measure: Fraction
    restricted_measure: Fraction
    sellable_measure: Fraction
    blocked_measure: Fraction
    contract_area: Fraction
    blockers: list[Blocker]
    occupancy: list[dict[str, Any]]
    measurement_id: str | None
    measurement_basis: str | None

    def area(self, measure: Fraction) -> Fraction | None:
        if self.physical_area is None or self.measure_total == 0:
            return None
        return self.physical_area * measure / self.measure_total


class Engine:
    def __init__(self, version: SourceVersion):
        self.version = version
        self.model: SourceModel = build_source_model(version.rows)
        self.periods: list[UnitPeriod] = sorted(
            self.model.units, key=lambda p: (p.project, p.building, p.unit_id, p.valid_from)
        )
        self.period_by_key: dict[str, UnitPeriod] = {p.key: p for p in self.periods}
        self.periods_by_unit: dict[str, list[UnitPeriod]] = defaultdict(list)
        for period in self.periods:
            self.periods_by_unit[period.unit_id].append(period)
        for values in self.periods_by_unit.values():
            values.sort(key=lambda p: p.valid_from)
        self.building_scopes: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for period in self.periods:
            self.building_scopes[period.building].add((period.project, period.building))

        self.defects: list[Defect] = []
        self.groups: dict[str, GroupState] = {}
        self.incoming: dict[str, list[ResolvedEdge]] = defaultdict(list)
        self.outgoing: dict[str, list[ResolvedEdge]] = defaultdict(list)
        self.content: dict[str, list[Atom]] = {}
        self.content_root_total: dict[str, Fraction] = {}
        self.period_blockers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.unit_blockers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.target_units: set[str] = set()
        self.source_periods: set[str] = set()

        self._validate_unit_periods()
        self._resolve_groups()
        self._detect_cycles()
        self._cascade_invalid_groups()
        self._build_content()
        self._resolve_rights()
        self._resolve_occupations()

    # ------------------------------------------------------------------ 通用辅助
    def _add_defect(
        self,
        code: str,
        severity: str,
        entity_type: str,
        entity_key: str,
        message: str,
        *,
        period: UnitPeriod | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.defects.append(
            Defect(
                code=code,
                severity=severity,
                entity_type=entity_type,
                entity_key=entity_key,
                project=period.project if period else None,
                building=period.building if period else None,
                message=message,
                detail=detail or {},
            )
        )

    def _block_period(self, period: UnitPeriod, code: str, message: str) -> None:
        existing = self.period_blockers[period.key]
        if (code, message) not in existing:
            existing.append((code, message))

    def _block_unit(self, unit_id: str, code: str, message: str) -> None:
        existing = self.unit_blockers[unit_id]
        if (code, message) not in existing:
            existing.append((code, message))

    def _all_blockers(self, period: UnitPeriod) -> list[tuple[str, str]]:
        return list(self.period_blockers.get(period.key, [])) + list(
            self.unit_blockers.get(period.unit_id, [])
        )

    def _resolve_period(
        self, unit_id: str, day: date, *, context: str
    ) -> tuple[UnitPeriod | None, str | None]:
        period, candidates = self.model.resolve_unit(unit_id, day)
        if period is not None:
            return period, None
        if not candidates:
            return None, f"{context}: 房号 {unit_id!r} 在 {day} 没有任何有效期记录"
        windows = ", ".join(f"[{p.valid_from},{p.valid_to or '∞'})" for p in candidates)
        return None, f"{context}: 房号 {unit_id!r} 在 {day} 无法唯一解析，候选有效期 {windows}"

    # -------------------------------------------------------------- 有效期唯一性
    def _validate_unit_periods(self) -> None:
        for unit_id, periods in self.periods_by_unit.items():
            for earlier, later in zip(periods, periods[1:]):
                if later.valid_from < earlier.end:
                    message = (
                        f"房号 {unit_id!r} 的有效期重叠："
                        f"[{earlier.valid_from},{earlier.valid_to or '∞'}) 与 "
                        f"[{later.valid_from},{later.valid_to or '∞'})"
                    )
                    for period in (earlier, later):
                        self._block_period(period, "unit_period_overlap", message)
                    self._add_defect(
                        "unit_period_overlap",
                        BLOCKING,
                        "unit_period",
                        earlier.key + "|" + later.key,
                        message,
                        period=earlier,
                    )

    # ------------------------------------------------------------------ 变更组
    def _resolve_groups(self) -> None:
        declared = {change.change_id: change for change in self.model.changes}
        edges_by_change: dict[str, list[Edge]] = defaultdict(list)
        for edge in self.model.edges:
            if edge.change_id not in declared:
                self._add_defect(
                    "edge_unknown_change",
                    BLOCKING,
                    "edge",
                    f"{edge.change_id}:{edge.from_unit}->{edge.to_unit}",
                    f"变更明细引用了未声明的 change_id {edge.change_id!r}，该明细不参与沿革",
                )
                continue
            self.source_periods.add(edge.from_unit)
            self.target_units.add(edge.to_unit)
            edges_by_change[edge.change_id].append(edge)

        for change_id in sorted(declared):
            change = declared[change_id]
            state = GroupState(change=change)
            self.groups[change_id] = state
            group_edges = edges_by_change.get(change_id, [])
            if len(group_edges) != change.expected_edges:
                self._group_reason(
                    state,
                    "change_edges_incomplete",
                    f"声明 expected_edges={change.expected_edges}，实际 {len(group_edges)} 条",
                )

            slices_by_source: dict[str, list[tuple[Fraction, Fraction, Edge]]] = defaultdict(list)
            for edge in sorted(
                group_edges, key=lambda e: (e.from_unit, e.source_slice, e.to_unit)
            ):
                src, src_error = self._resolve_period(
                    edge.from_unit, change.effective, context=f"变更 {change_id} 的来源单元"
                )
                dst, dst_error = self._resolve_period(
                    edge.to_unit, change.effective, context=f"变更 {change_id} 的目标单元"
                )
                if src is None:
                    self._group_reason(state, "change_source_unresolved", src_error or "")
                    continue
                if dst is None:
                    self._group_reason(state, "change_target_unresolved", dst_error or "")
                    self._touch_pair(state, src, None)
                    continue
                self._touch_pair(state, src, dst)
                if edge.fraction <= 0:
                    self._group_reason(
                        state, "fraction_invalid", f"份额必须为正：{edge.fraction}"
                    )
                    continue
                slice_measure = interval_measure(edge.source_slice)
                if edge.fraction != slice_measure:
                    self._group_reason(
                        state,
                        "fraction_slice_mismatch",
                        f"{edge.from_unit}->{edge.to_unit} 份额 {format_fraction(edge.fraction)} "
                        f"与来源区间长度 {format_fraction(slice_measure)} 不一致",
                    )
                    continue
                resolved = ResolvedEdge(edge=edge, src=src, dst=dst)
                state.edges.append(resolved)
                slices_by_source[src.key].append(
                    (edge.source_slice[0], edge.source_slice[1], edge)
                )
                self._check_timeline(state, edge, src, dst)

            for src_key, slices in slices_by_source.items():
                ordered = sorted((start, end, edge) for start, end, edge in slices)
                for (a_start, a_end, _), (b_start, b_end, _) in zip(ordered, ordered[1:]):
                    if b_start < a_end:
                        message = (
                            f"来源 {src_key} 的沿革区间重叠："
                            f"{format_interval(a_start, a_end)} 与 {format_interval(b_start, b_end)}"
                        )
                        self._group_reason(state, "source_overlap", message)
                        self._add_defect(
                            "source_overlap",
                            BLOCKING,
                            "change_group",
                            state.change.change_id,
                            message,
                        )
                total = sum((end - start for start, end, _ in ordered), Fraction(0))
                if total < 1:
                    self._group_reason(
                        state,
                        "source_share_insufficient",
                        f"来源 {src_key} 只交割了 {format_fraction(total)}，不足 1",
                    )
                elif total > 1:
                    self._group_reason(
                        state,
                        "source_share_overflow",
                        f"来源 {src_key} 交割份额合计 {format_fraction(total)}，超过 1",
                    )

            if state.status != "valid":
                self._add_defect(
                    "change_group_invalid",
                    BLOCKING,
                    "change_group",
                    change_id,
                    f"变更组 {change_id} 不成立：" + "；".join(reason for _, reason in state.reasons),
                    detail={"reasons": [code for code, _ in state.reasons]},
                )
                continue
            if state.shape != "valid":
                # 结构成立但时间边界不一致：仍保留拓扑以便识别成环等更深问题，
                # 但不建立空间内容，相关期间由 _build_content 阻塞。
                self._add_defect(
                    "change_timeline_inconsistent",
                    BLOCKING,
                    "change_group",
                    change_id,
                    f"变更组 {change_id} 的时间边界不一致："
                    + "；".join(reason for _, reason in state.shape_reasons),
                    detail={"reasons": [code for code, _ in state.shape_reasons]},
                )
            for resolved in state.edges:
                self.incoming[resolved.dst.key].append(resolved)
                self.outgoing[resolved.src.key].append(resolved)

        self._invalidate_multi_created_targets()
        self._invalidate_shared_sources()

    def _check_timeline(
        self, state: GroupState, edge: Edge, src: UnitPeriod, dst: UnitPeriod
    ) -> None:
        def warn(code: str, message: str) -> None:
            state.shape = "inconsistent"
            if (code, message) not in state.shape_reasons:
                state.shape_reasons.append((code, message))

        if src.key == dst.key:
            warn(
                "evolution_cycle",
                f"变更 {state.change.change_id} 的来源与目标是同一期间 {src.key}，沿革自指成环",
            )
            return
        if src.valid_to != state.change.effective:
            warn(
                "change_source_validity",
                f"来源单元 {src.unit_id} 有效期止于 {src.valid_to or '∞'}，"
                f"与变更生效日 {state.change.effective} 不一致，无法确定交割边界",
            )
        if not (dst.valid_from <= state.change.effective < dst.end):
            warn(
                "change_target_validity",
                f"目标单元 {dst.unit_id} 的有效期 "
                f"[{dst.valid_from},{dst.valid_to or '∞'}) 不覆盖生效日 {state.change.effective}",
            )
        elif dst.valid_from != state.change.effective:
            warn(
                "change_target_start_mismatch",
                f"目标单元 {dst.unit_id} 自 {dst.valid_from} 起有效，"
                f"与变更生效日 {state.change.effective} 不一致",
            )

    def _group_reason(self, state: GroupState, code: str, message: str) -> None:
        state.status = "invalid"
        if (code, message) not in state.reasons:
            state.reasons.append((code, message))

    def _touch_pair(
        self, state: GroupState, src: UnitPeriod, dst: UnitPeriod | None
    ) -> None:
        """记录一份变更涉及其中的期间，即使明细因其他原因不成立。"""

        state.involved[src.key] = src
        if dst is not None:
            state.involved[dst.key] = dst

    def _invalidate_multi_created_targets(self) -> None:
        for dst_key, resolved_edges in list(self.incoming.items()):
            change_ids = sorted({item.edge.change_id for item in resolved_edges})
            if len(change_ids) <= 1:
                continue
            for change_id in change_ids:
                state = self.groups[change_id]
                self._group_reason(
                    state,
                    "change_target_multiple_groups",
                    f"目标期间 {dst_key} 同时被变更组 {change_ids} 创建",
                )
            del self.incoming[dst_key]

    def _invalidate_shared_sources(self) -> None:
        """一个来源有效期只能由一个变更组完整交割。"""

        by_source: dict[str, set[str]] = defaultdict(set)
        for resolved in (item for items in self.incoming.values() for item in items):
            by_source[resolved.src.key].add(resolved.edge.change_id)
        for src_key, change_ids in by_source.items():
            if len(change_ids) <= 1:
                continue
            for change_id in change_ids:
                state = self.groups[change_id]
                self._group_reason(
                    state,
                    "source_overlap",
                    f"来源 {src_key} 被多个变更组 {sorted(change_ids)} 重复交割",
                )
                self._add_defect(
                    "source_overlap",
                    BLOCKING,
                    "change_group",
                    src_key,
                    f"来源 {src_key} 被多个变更组重复交割：{sorted(change_ids)}",
                )
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self.incoming = defaultdict(list)
        self.outgoing = defaultdict(list)
        for state in self.groups.values():
            if state.status != "valid":
                continue
            for resolved in state.edges:
                self.incoming[resolved.dst.key].append(resolved)
                self.outgoing[resolved.src.key].append(resolved)

    # ------------------------------------------------------------------ 成环
    def _detect_cycles(self) -> None:
        # 先对结构成立的组做拓扑排序；环上的组即使时间边界不一致也要报成环。
        graph: dict[str, set[str]] = defaultdict(set)
        for state in self.groups.values():
            if state.status != "valid":
                continue
            for resolved in state.edges:
                graph[resolved.src.key].add(resolved.dst.key)
        nodes: set[str] = set(graph)
        for targets in graph.values():
            nodes |= targets
        indegree: dict[str, int] = defaultdict(int)
        for node in nodes:
            for target in graph.get(node, ()):
                indegree[target] += 1
        queue = [node for node in nodes if indegree[node] == 0]
        removed: set[str] = set()
        while queue:
            node = queue.pop()
            removed.add(node)
            for target in graph.get(node, ()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        cyclic = nodes - removed
        if not cyclic:
            return
        for change_id in sorted(self.groups):
            state = self.groups[change_id]
            if state.status != "valid":
                continue
            if any(r.src.key in cyclic or r.dst.key in cyclic for r in state.edges):
                self._group_reason(
                    state,
                    "evolution_cycle",
                    f"沿革成环或位于环的下游，涉及空间 {sorted(cyclic)}",
                )
                self._add_defect(
                    "evolution_cycle",
                    BLOCKING,
                    "change_group",
                    change_id,
                    f"变更组 {change_id} 位于沿革环上：{sorted(cyclic)}",
                )
        self._rebuild_indexes()

    # ------------------------------------------------------------ 无效组级联阻塞
    def _cascade_invalid_groups(self) -> None:
        """无效变更涉及的前后单元一起阻塞；依赖被阻塞来源的组继续失效。

        阻塞只沿沿革关系传播，不按楼栋或项目扩散，无关楼栋仍可出数。
        """

        for state in self.groups.values():
            if state.status != "valid":
                self._block_group_involved(state, "change_group_invalid", f"变更组 {state.change.change_id} 不成立")

        changed = True
        while changed:
            changed = False
            for change_id in sorted(self.groups):
                state = self.groups[change_id]
                if state.status != "valid":
                    continue
                blocked = [
                    resolved.src
                    for resolved in state.edges
                    if self._all_blockers(resolved.src)
                ]
                if not blocked:
                    continue
                self._group_reason(
                    state,
                    "change_lineage_blocked",
                    "来源空间已因其他资料问题暂不可用："
                    + ", ".join(sorted({period.key for period in blocked})),
                )
                self._add_defect(
                    "change_lineage_blocked",
                    BLOCKING,
                    "change_group",
                    change_id,
                    f"变更组 {change_id} 的来源空间已被阻塞",
                )
                self._block_group_involved(
                    state, "change_lineage_blocked", f"变更组 {change_id} 的来源空间被阻塞"
                )
                changed = True
        self._rebuild_indexes()

    def _block_group_involved(self, state: GroupState, code: str, message: str) -> None:
        for period in state.involved.values():
            self._block_period(period, code, message)

    # -------------------------------------------------------------- 空间内容计算
    def _slice_atoms(self, source_key: str, window: tuple[Fraction, Fraction]) -> list[Atom]:
        result: list[Atom] = []
        for atom in self.content.get(source_key, []):
            overlap = interval_intersection(atom.coord, window)
            if overlap is None:
                continue
            coord_width = atom.coord[1] - atom.coord[0]
            if coord_width == 0:
                continue
            root_width = atom.root_width
            rel_start = (overlap[0] - atom.coord[0]) / coord_width
            rel_end = (overlap[1] - atom.coord[0]) / coord_width
            root = (
                atom.root[0] + rel_start * root_width,
                atom.root[0] + rel_end * root_width,
            )
            result.append(Atom(root_key=atom.root_key, root=root, coord=overlap))
        return result

    def _build_content(self) -> None:
        # 时间边界不一致的组本身不可确定交割，相关期间一并阻塞
        for state in self.groups.values():
            if state.status == "valid" and state.shape != "valid":
                self._block_group_involved(
                    state,
                    "change_timeline_inconsistent",
                    f"变更组 {state.change.change_id} 的时间边界不一致",
                )

        # 出现在沿革明细中、但不是任何成立变更组目标的期间无法确定空间来源
        for period in self.periods:
            if period.unit_id not in self.target_units:
                continue
            if self.incoming.get(period.key):
                continue
            message = "该期间被沿革明细引用为目标，但没有成立的变更组创建它"
            self._block_period(period, "change_target_unresolved", message)
            self._add_defect(
                "change_target_unresolved",
                BLOCKING,
                "unit_period",
                period.key,
                f"目标期间 {period.key} 没有成立的变更组，无法确定空间来源",
                period=period,
            )

        # 拓扑序：只依赖成立的变更组
        indegree = {
            period.key: len(self.incoming.get(period.key, []))
            for period in self.periods
        }
        order: list[str] = []
        queue = sorted(key for key, degree in indegree.items() if degree == 0)
        while queue:
            key = queue.pop(0)
            order.append(key)
            for resolved in self.outgoing.get(key, []):
                indegree[resolved.dst.key] -= 1
                if indegree[resolved.dst.key] == 0:
                    queue.append(resolved.dst.key)
                    queue.sort()
        if len(order) != len(self.periods):
            unresolved = {p.key for p in self.periods} - set(order)
            for key in unresolved:
                period = self.period_by_key[key]
                self._block_period(period, "evolution_cycle", "沿革拓扑无法排序")
                self._add_defect(
                    "evolution_cycle",
                    BLOCKING,
                    "unit_period",
                    key,
                    f"期间 {key} 无法拓扑排序，沿革可能成环",
                    period=period,
                )
            order.extend(sorted(unresolved))

        for key in order:
            period = self.period_by_key[key]
            if self._all_blockers(period):
                continue
            incoming = self.incoming.get(key, [])
            if not incoming:
                self.content[key] = [
                    Atom(key, (Fraction(0), Fraction(1)), (Fraction(0), Fraction(1)))
                ]
                continue
            incoming = sorted(
                incoming,
                key=lambda item: (
                    item.edge.change_id,
                    item.edge.from_unit,
                    item.edge.source_slice[0],
                    item.edge.to_unit,
                ),
            )
            total_share = sum((item.edge.fraction for item in incoming), Fraction(0))
            if total_share <= 0:
                self._block_period(period, "content_incomplete", "变更份额合计不为正")
                continue
            cursor = Fraction(0)
            atoms: list[Atom] = []
            for index, item in enumerate(incoming):
                if index == len(incoming) - 1:
                    target_start, target_end = cursor, Fraction(1)
                else:
                    width = item.edge.fraction / total_share
                    target_start, target_end = cursor, cursor + width
                cursor = target_end
                target_width = target_end - target_start
                slice_start, slice_end = item.edge.source_slice
                slice_length = slice_end - slice_start
                source_atoms = self._slice_atoms(item.src.key, item.edge.source_slice)
                for source_atom in source_atoms:
                    local_start = source_atom.coord[0] - slice_start
                    local_end = source_atom.coord[1] - slice_start
                    mapped_start = target_start + local_start / slice_length * target_width
                    mapped_end = target_start + local_end / slice_length * target_width
                    atoms.append(
                        Atom(
                            root_key=source_atom.root_key,
                            root=source_atom.root,
                            coord=(mapped_start, mapped_end),
                        )
                    )
            atoms.sort(key=lambda atom: (atom.coord[0], atom.root_key, atom.root))
            coord_total = sum((atom.coord_width for atom in atoms), Fraction(0))
            if coord_total != 1:
                message = (
                    f"空间内容份额合计为 {format_fraction(coord_total)}，不等于 1"
                )
                self._block_period(period, "content_incomplete", message)
                self._add_defect(
                    "content_incomplete",
                    BLOCKING,
                    "unit_period",
                    key,
                    f"期间 {key} 的{message}",
                    period=period,
                )
                continue
            # 合并让一个期间的根空间总宽大于或小于 1；份额统一按根空间归一，
            # 内部坐标只保留给该期间自己的 source_slice 解释。
            root_total = sum((atom.root_width for atom in atoms), Fraction(0))
            if root_total <= 0:
                self._block_period(period, "content_incomplete", "根空间宽度不为正")
                continue
            self.content[key] = atoms
            self.content_root_total[key] = root_total

    # ------------------------------------------------------------------ 权利
    def _resolve_scope(
        self, scope: str, day: date, *, context: str
    ) -> tuple[list[UnitPeriod], str | None]:
        if scope in self.periods_by_unit:
            period, error = self._resolve_period(scope, day, context=context)
            if period is None:
                return [], error
            return [period], None
        if scope in self.building_scopes:
            options = self.building_scopes[scope]
            if len(options) > 1:
                return [], f"{context}: scope {scope!r} 同时匹配多个项目的楼栋，无法确定覆盖范围"
            project, building = next(iter(options))
            periods = [
                period
                for period in self.periods
                if period.project == project
                and period.building == building
                and period.valid_from <= day < period.end
            ]
            if not periods:
                return [], f"{context}: 楼栋 {building} 在 {day} 没有有效单元"
            return periods, None
        return [], f"{context}: scope {scope!r} 既不是房号也不是楼栋"

    def _map_window(
        self,
        periods: Iterable[UnitPeriod],
        window: tuple[Fraction, Fraction],
        *,
        label: str,
    ) -> tuple[list[Atom], str | None]:
        atoms: list[Atom] = []
        for period in periods:
            blockers = self._all_blockers(period)
            if blockers:
                return [], f"{label}: 引用的空间 {period.key} 因沿革问题不可用"
            atoms.extend(self._slice_atoms(period.key, window))
        if not atoms:
            return [], f"{label}: 来源区间 {format_interval(*window)} 未映射到任何物理空间"
        return atoms, None

    def _scope_unit_ids(self, scope: str) -> list[str]:
        if scope in self.periods_by_unit:
            return [scope]
        if scope in self.building_scopes:
            project, building = next(iter(self.building_scopes[scope]))
            return sorted(
                {
                    period.unit_id
                    for period in self.periods
                    if period.project == project and period.building == building
                }
            )
        return []

    def _resolve_rights(self) -> None:
        self.permit_atoms: list[dict[str, Any]] = []
        self.restriction_atoms: list[dict[str, Any]] = []
        self.releases: list[Right] = []
        for right in sorted(self.model.rights, key=lambda item: (item.effective, item.record_id)):
            if right.kind == "release":
                self.releases.append(right)
                continue
            periods, error = self._resolve_scope(
                right.scope, right.effective, context=f"权利 {right.record_id}"
            )
            if error:
                self._add_defect(
                    "right_scope_unresolved",
                    BLOCKING,
                    "right",
                    right.record_id,
                    error,
                    detail={"kind": right.kind, "scope": right.scope},
                )
                for unit_id in self._scope_unit_ids(right.scope):
                    self._block_unit(unit_id, "right_scope_unresolved", error)
                continue
            atoms, error = self._map_window(
                periods, right.source_slice, label=f"权利 {right.record_id}"
            )
            if error:
                self._add_defect(
                    "right_space_unresolved",
                    BLOCKING,
                    "right",
                    right.record_id,
                    error,
                    detail={"kind": right.kind, "scope": right.scope},
                )
                for unit_id in self._scope_unit_ids(right.scope):
                    self._block_unit(unit_id, "right_space_unresolved", error)
                continue
            slice_measure = interval_measure(right.source_slice)
            if right.share <= 0:
                message = f"权利 {right.record_id} 的 share 必须为正"
                self._add_defect(
                    "share_invalid", BLOCKING, "right", right.record_id, message,
                    detail={"kind": right.kind, "scope": right.scope},
                )
                for unit_id in self._scope_unit_ids(right.scope):
                    self._block_unit(unit_id, "share_invalid", message)
                continue
            record: dict[str, Any] = {"right": right, "atoms": atoms, "slice_measure": slice_measure}
            if right.kind == "permit":
                self.permit_atoms.append(record)
            else:
                self.restriction_atoms.append(record)

        restriction_ids = {record["right"].record_id for record in self.restriction_atoms}
        permit_ids = {record["right"].record_id for record in self.permit_atoms}
        for release in sorted(self.releases, key=lambda item: (item.effective, item.record_id)):
            if not release.reference:
                self._add_defect(
                    "release_reference_missing",
                    BLOCKING,
                    "right",
                    release.record_id,
                    f"解除记录 {release.record_id} 未引用限制编号",
                )
                continue
            if release.reference not in restriction_ids:
                if release.reference in permit_ids:
                    message = (
                        f"解除记录 {release.record_id} 引用了许可 {release.reference}；"
                        "来源约定只允许解除限制，许可不能随房号更名自动消失"
                    )
                    self._add_defect(
                        "release_targets_permit",
                        BLOCKING,
                        "right",
                        release.record_id,
                        message,
                    )
                    for unit_id in self._scope_unit_ids(release.scope):
                        self._block_unit(unit_id, "release_targets_permit", message)
                else:
                    message = f"解除记录 {release.record_id} 引用了不存在的限制 {release.reference}"
                    self._add_defect(
                        "release_reference_unresolved",
                        BLOCKING,
                        "right",
                        release.record_id,
                        message,
                    )
                    for unit_id in self._scope_unit_ids(release.scope):
                        self._block_unit(unit_id, "release_reference_unresolved", message)
                continue
            target = next(
                record
                for record in self.restriction_atoms
                if record["right"].record_id == release.reference
            )
            if release.effective < target["right"].effective:
                message = (
                    f"解除记录 {release.record_id} 早于被解除限制 {release.reference} 的生效日"
                )
                self._add_defect(
                    "release_before_restriction", BLOCKING, "right", release.record_id, message
                )
                for unit_id in self._scope_unit_ids(release.scope):
                    self._block_unit(unit_id, "release_before_restriction", message)
                continue
            if release.use != target["right"].use:
                message = (
                    f"解除记录 {release.record_id} 的用途 {release.use} 与被解除限制 "
                    f"{release.reference} 的用途 {target['right'].use} 不一致"
                )
                self._add_defect(
                    "release_use_mismatch", BLOCKING, "right", release.record_id, message
                )
                for unit_id in self._scope_unit_ids(release.scope):
                    self._block_unit(unit_id, "release_use_mismatch", message)
                continue
            target.setdefault("releases", []).append(release.effective)

        self.measurement_ids = {m.measurement_id for m in self.model.measurements}

    # ------------------------------------------------------------------ 占用
    def _resolve_occupations(self) -> None:
        self.occupation_atoms: list[dict[str, Any]] = []
        for occupation in sorted(
            self.model.occupations, key=lambda item: (item.effective, item.contract_id)
        ):
            period, error = self._resolve_period(
                occupation.unit_id, occupation.effective, context=f"签约 {occupation.contract_id}"
            )
            if error:
                self._add_defect(
                    "occupation_unit_unresolved",
                    BLOCKING,
                    "occupation",
                    occupation.contract_id,
                    error,
                )
                self._block_unit(occupation.unit_id, "occupation_unit_unresolved", error)
                continue
            if self._all_blockers(period):
                message = (
                    f"签约 {occupation.contract_id} 引用的空间 {period.key} 因沿革问题不可用，"
                    "无法确定其影响范围"
                )
                self._add_defect(
                    "occupation_on_blocked_space",
                    BLOCKING,
                    "occupation",
                    occupation.contract_id,
                    message,
                    period=period,
                )
                self._block_unit(period.unit_id, "occupation_on_blocked_space", message)
                continue
            atoms, error = self._map_window(
                [period], occupation.source_slice, label=f"签约 {occupation.contract_id}"
            )
            if error:
                self._add_defect(
                    "occupation_space_unresolved",
                    BLOCKING,
                    "occupation",
                    occupation.contract_id,
                    error,
                    period=period,
                )
                self._block_unit(period.unit_id, "occupation_space_unresolved", error)
                continue
            slice_measure = interval_measure(occupation.source_slice)
            if occupation.share <= 0:
                message = f"签约 {occupation.contract_id} 的 share 必须为正"
                self._add_defect(
                    "share_invalid", BLOCKING, "occupation", occupation.contract_id, message,
                    period=period,
                )
                self._block_unit(period.unit_id, "share_invalid", message)
                continue
            if occupation.measurement_id not in self.measurement_ids:
                self._add_defect(
                    "occupation_measurement_unresolved",
                    WARNING,
                    "occupation",
                    occupation.contract_id,
                    f"签约 {occupation.contract_id} 引用的测绘 {occupation.measurement_id} 不存在；"
                    "合同面积仍按签约记载保留",
                    period=period,
                )
            self.occupation_atoms.append(
                {
                    "occupation": occupation,
                    "atoms": atoms,
                    "slice_measure": slice_measure,
                    "period": period,
                }
            )

    # -------------------------------------------------------------- 测绘与快照
    def _measurement_for(self, period: UnitPeriod, day: date) -> Measurement | None:
        candidates = [
            measurement
            for measurement in self.model.measurements
            if measurement.unit_id == period.unit_id
            and period.valid_from <= measurement.effective <= day
            and measurement.received <= day
        ]
        if candidates:
            return max(
                candidates,
                key=lambda item: (item.effective, item.received, item.measurement_id),
            )
        # 纯改名：目标单元完整承接同一个前身空间（份额 1/1）且自身没有测绘时，
        # 可以沿用前身的物理面积；拆分或合并产生的子单元不沿用，避免面积错配。
        incoming = self.incoming.get(period.key, [])
        if len(incoming) == 1 and incoming[0].edge.fraction == 1:
            inherited = [
                measurement
                for measurement in self.model.measurements
                if measurement.unit_id == incoming[0].src.unit_id
                and measurement.effective <= day
                and measurement.received <= day
            ]
            if inherited:
                return max(
                    inherited,
                    key=lambda item: (item.effective, item.received, item.measurement_id),
                )
        return None

    def _active_periods(self, day: date) -> list[UnitPeriod]:
        return [period for period in self.periods if period.active_on(day)]

    def _physical_overlaps(self, periods: list[UnitPeriod]) -> dict[str, set[str]]:
        by_root: dict[str, list[tuple[Fraction, Fraction, str]]] = defaultdict(list)
        for period in periods:
            for atom in self.content.get(period.key, []):
                by_root[atom.root_key].append((atom.root[0], atom.root[1], period.key))
        overlaps: dict[str, set[str]] = defaultdict(set)
        for entries in by_root.values():
            entries.sort()
            active: list[tuple[Fraction, str]] = []
            for start, end, key in entries:
                active = [(cursor_end, active_key) for cursor_end, active_key in active if cursor_end > start]
                for _, other_key in active:
                    if other_key != key:
                        overlaps[key].add(other_key)
                        overlaps[other_key].add(key)
                active.append((end, key))
        return overlaps

    def _records_by_root(
        self, records: list[dict[str, Any]], day: date, *, honour_release: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            if "right" in record:
                right: Right = record["right"]
                if right.effective > day:
                    continue
                if honour_release and self._release_active(record, day):
                    continue
                share = right.share
            else:
                occupation: Occupation = record["occupation"]
                if occupation.effective > day:
                    continue
                share = occupation.share
            # share 是相对 scope 单元整体空间的权益比例；source_slice 只负责定位。
            # 权益在来源区间上的密度为 share / 区间长度（根空间单位）。
            # 拆分后各实体按自身覆盖的根空间长度分摊，全部后继分摊之和守恒。
            rate = share / record["slice_measure"]
            for atom in record["atoms"]:
                result[atom.root_key].append(
                    {"record": record, "start": atom.root[0], "end": atom.root[1], "rate": rate}
                )
        return result

    def _release_active(self, record: dict[str, Any], day: date) -> bool:
        return any(release <= day for release in record.get("releases", []))

    def _entity(self, period: UnitPeriod, day: date, overlaps: set[str]) -> EntityState:
        blockers: list[Blocker] = []
        occupancy: list[dict[str, Any]] = []
        measurement = self._measurement_for(period, day)
        physical_area = measurement.area_sqm if measurement else None
        atoms = self.content.get(period.key, [])
        # 份额指标（permitted/sold/restricted/sellable/blocked）统一按实体自身的
        # 0..1 计量：跨根空间合并时也能换算回面积，且不因份额总和而失真。
        measure_total = Fraction(1)
        zero = Fraction(0)

        if measurement is None:
            blockers.append(
                Blocker(
                    "measurement_missing",
                    BLOCKING,
                    measure_total,
                    "缺少截至该日期的有效测绘面积，可售面积无法落数",
                )
            )

        static_reasons = self._all_blockers(period)
        if static_reasons:
            for code, message in static_reasons:
                blockers.append(Blocker(code, BLOCKING, measure_total, message))
            return EntityState(
                period=period,
                snapshot_date=day,
                status="blocked",
                physical_area=physical_area,
                measure_total=measure_total,
                permitted_measure=zero,
                sold_measure=zero,
                restricted_measure=zero,
                sellable_measure=zero,
                blocked_measure=measure_total,
                contract_area=zero,
                blockers=blockers,
                occupancy=[],
                measurement_id=measurement.measurement_id if measurement else None,
                measurement_basis=measurement.basis if measurement else None,
            )

        if overlaps:
            blockers.append(
                Blocker(
                    "physical_overlap",
                    BLOCKING,
                    measure_total,
                    "同一物理空间同时被多个有效房号覆盖：" + ", ".join(sorted(overlaps)),
                )
            )
        if not atoms or measure_total == 0:
            blockers.append(Blocker("space_unresolved", BLOCKING, zero, "无法还原实体空间"))

        if overlaps or not atoms or measure_total == 0:
            return EntityState(
                period=period,
                snapshot_date=day,
                status="blocked",
                physical_area=physical_area,
                measure_total=measure_total,
                permitted_measure=zero,
                sold_measure=zero,
                restricted_measure=zero,
                sellable_measure=zero,
                blocked_measure=measure_total,
                contract_area=zero,
                blockers=blockers,
                occupancy=[],
                measurement_id=measurement.measurement_id if measurement else None,
                measurement_basis=measurement.basis if measurement else None,
            )

        permits = self._records_by_root(self.permit_atoms, day)
        restrictions = self._records_by_root(self.restriction_atoms, day, honour_release=True)
        occupations = self._records_by_root(self.occupation_atoms, day)

        root_atoms: dict[str, list[Atom]] = defaultdict(list)
        for atom in atoms:
            root_atoms[atom.root_key].append(atom)

        permitted = zero
        sold = zero
        sold_outside = zero
        restricted = zero
        restricted_outside = zero
        sellable = zero
        unpermitted = zero
        other_use = zero
        over_sold = zero
        over_restricted = zero
        deduction_over = zero
        permit_overlap = zero

        total_root_width = sum((atom.root_width for atom in atoms), Fraction(0))
        for root_key, entity_atoms in root_atoms.items():
            points: set[Fraction] = set()
            for atom in entity_atoms:
                points.add(atom.root[0])
                points.add(atom.root[1])
            for group in (permits.get(root_key, []), restrictions.get(root_key, []), occupations.get(root_key, [])):
                for item in group:
                    points.add(item["start"])
                    points.add(item["end"])
            ordered = sorted(points)
            permit_records = permits.get(root_key, [])
            restriction_records = restrictions.get(root_key, [])
            occupation_records = occupations.get(root_key, [])
            for start, end in zip(ordered, ordered[1:]):
                if start == end:
                    continue
                if not any(atom.root[0] <= start and end <= atom.root[1] for atom in entity_atoms):
                    continue
                width = (end - start) / total_root_width
                covering_permits = [
                    item for item in permit_records if item["start"] <= start and end <= item["end"]
                ]
                same = sum(
                    (
                        item["rate"]
                        for item in covering_permits
                        if item["record"]["right"].use == period.use
                    ),
                    zero,
                )
                other = sum(
                    (
                        item["rate"]
                        for item in covering_permits
                        if item["record"]["right"].use != period.use
                    ),
                    zero,
                )
                p_same = min(Fraction(1), same)
                p_other = min(Fraction(1), other)
                if p_same + p_other > 1:
                    permit_overlap += width * (p_same + p_other - 1)
                covering_occ = [
                    item for item in occupation_records if item["start"] <= start and end <= item["end"]
                ]
                s = sum((item["rate"] for item in covering_occ), zero)
                covering_res = [
                    item for item in restriction_records if item["start"] <= start and end <= item["end"]
                ]
                r = sum((item["rate"] for item in covering_res), zero)

                permitted += width * p_same
                sold_in = width * min(s, p_same)
                sold += sold_in
                restricted_in = width * min(r, p_same)
                restricted += restricted_in
                sellable += width * max(zero, p_same - s - r)
                unpermitted += width * max(zero, Fraction(1) - p_same)
                if p_other > 0 and p_same < 1:
                    other_use += width * p_other
                if s > 1:
                    over_sold += width * (s - 1)
                if r > 1:
                    over_restricted += width * (r - 1)
                if s + r > p_same:
                    deduction_over += width * (s + r - p_same)
                if p_same == 0:
                    sold_outside += width * min(s, Fraction(1))
                    restricted_outside += width * min(r, Fraction(1))

        contracted, occupancy = self._contract_allocation(period, day)

        if over_sold > 0:
            blockers.append(
                Blocker("occupation_overlap", BLOCKING, over_sold, "同一空间上的签约权益合计超过 1")
            )
        if over_restricted > 0:
            blockers.append(
                Blocker("restriction_overlap", BLOCKING, over_restricted, "同一空间上的限制权益合计超过 1")
            )
        if permit_overlap > 0:
            blockers.append(
                Blocker(
                    "permit_overlap",
                    BLOCKING,
                    permit_overlap,
                    "同一空间上住宅与商业许可合计超过 1，用途资格互相冲突",
                )
            )
        if unpermitted > 0:
            code = "use_conflict" if other_use > 0 else "permit_missing"
            blockers.append(
                Blocker(
                    code,
                    BLOCKING,
                    unpermitted,
                    "存在其他用途的许可，不能把本用途资格传给全部空间"
                    if other_use > 0
                    else "本用途许可未覆盖全部空间",
                    {"other_use_measure": format_fraction(other_use)},
                )
            )
        if sold_outside > 0:
            blockers.append(
                Blocker(
                    "sold_outside_permit",
                    BLOCKING,
                    sold_outside,
                    "已售空间不在本用途许可范围内，不能重新计入可售",
                )
            )
        if restricted_outside > 0:
            blockers.append(
                Blocker(
                    "restricted_outside_permit",
                    WARNING,
                    restricted_outside,
                    "限制作用在许可范围之外的空间",
                )
            )
        if deduction_over > 0:
            blockers.append(
                Blocker(
                    "deduction_exceeds_permit",
                    WARNING,
                    deduction_over,
                    "销售占用与权利限制的扣减合计超过许可范围，已按零可售处理",
                )
            )

        if measurement is None or over_sold > 0 or over_restricted > 0 or permit_overlap > 0:
            status = "blocked"
        elif permitted == 0:
            status = "blocked"
        elif sellable == 0:
            status = "sold_out"
        elif unpermitted > 0 or sold > 0 or restricted > 0:
            status = "partial"
        else:
            status = "available"
        if status == "blocked":
            sellable = zero

        return EntityState(
            period=period,
            snapshot_date=day,
            status=status,
            physical_area=physical_area,
            measure_total=measure_total,
            permitted_measure=permitted,
            sold_measure=sold,
            restricted_measure=restricted,
            sellable_measure=sellable,
            blocked_measure=measure_total - sellable,
            contract_area=contracted,
            blockers=blockers,
            occupancy=occupancy,
            measurement_id=measurement.measurement_id if measurement else None,
            measurement_basis=measurement.basis if measurement else None,
        )

    def _contract_allocation(
        self, period: UnitPeriod, day: date
    ) -> tuple[Fraction, list[dict[str, Any]]]:
        """合同面积按空间重叠分摊到当前实体；总额守恒，不随测绘改写。"""

        total = Fraction(0)
        details: list[dict[str, Any]] = []
        atoms = self.content.get(period.key, [])
        # 合同面积按来源区间切分；每个来源区间的合同面积密度为 contract_area / 区间长度。
        # 拆分后各实体分得的合同面积之和守恒，且不随新测绘改写。
        for record in self.occupation_atoms:
            occupation: Occupation = record["occupation"]
            if occupation.effective > day:
                continue
            overlap = Fraction(0)
            for atom in record["atoms"]:
                for entity_atom in atoms:
                    if atom.root_key != entity_atom.root_key:
                        continue
                    intersection = interval_intersection(atom.root, entity_atom.root)
                    if intersection is not None:
                        overlap += interval_measure(intersection)
            if overlap == 0:
                continue
            allocated = occupation.contract_area * overlap / record["slice_measure"]
            total += allocated
            details.append(
                {
                    "contract_id": occupation.contract_id,
                    "contract_area": occupation.contract_area,
                    "allocated_contract_area": allocated,
                    "measure": overlap / record["slice_measure"] * occupation.share,
                    "measurement_id": occupation.measurement_id,
                }
            )
        return total, details

    # ------------------------------------------------------------------ 对外接口
    def entity_states(self, day: date) -> list[EntityState]:
        active = self._active_periods(day)
        overlaps = self._physical_overlaps(active)
        return [self._entity(period, day, overlaps.get(period.key, set())) for period in active]

    def snapshots(self) -> list[tuple[str, date]]:
        return month_snapshots(self.model)

    def lineage(self, period_key: str) -> list[dict[str, Any]]:
        atoms = self.content.get(period_key, [])
        total = sum((atom.root_width for atom in atoms), Fraction(0))
        rows = []
        for atom in atoms:
            root = self.period_by_key[atom.root_key]
            rows.append(
                {
                    "period_key": period_key,
                    "root_period_key": atom.root_key,
                    "root_unit_id": root.unit_id,
                    "root_project": root.project,
                    "root_building": root.building,
                    "root_use": root.use,
                    "root_start": format_fraction(atom.root[0]),
                    "root_end": format_fraction(atom.root[1]),
                    "coord_start": format_fraction(atom.coord[0]),
                    "coord_end": format_fraction(atom.coord[1]),
                    "measure": atom.root_width,
                    "share_of_entity": atom.root_width / total if total else Fraction(0),
                }
            )
        return rows
