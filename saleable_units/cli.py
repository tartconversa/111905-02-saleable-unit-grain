"""命令行入口：入仓、生成数据集、查询、版本差异。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .checks import run_all
from .engine import Engine
from .query import as_of, compare_versions, project_rollup, today_view
from .source import load_source
from .warehouse import Warehouse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "output" / "warehouse.duckdb"


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_default))


def _engine_for(warehouse: Warehouse, version: str | None) -> Engine:
    version_id = warehouse.resolve_version(version)
    engine = warehouse.engine_for(version_id)
    if engine.version.version_id != version_id:
        raise RuntimeError(
            f"库内原始行重建出的版本指纹 {engine.version.version_id} 与 {version_id} 不一致，"
            "说明存储内容被改动，该版本不可用于复查"
        )
    return engine


def cmd_import(args: argparse.Namespace) -> int:
    engine = Engine(load_source(args.source))
    blocking = sum(1 for defect in engine.defects if defect.severity == "blocking")
    if blocking and not args.allow_blocked:
        _print(
            {
                "version_id": engine.version.version_id,
                "status": "refused",
                "blocking_defects": blocking,
                "message": (
                    "存在阻塞性资料问题，默认不入库；请先修正，或确认要保留阻塞依据后加 --allow-blocked"
                ),
            }
        )
        return 10
    with Warehouse(args.db) as warehouse:
        result = warehouse.import_version(engine)
        result["defect_count"] = len(engine.defects)
        result["blocking_defects"] = blocking
        result["snapshots"] = [month for month, _ in engine.snapshots()]
        _print(result)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    version = load_source(args.source)
    findings = run_all(version)
    blocking = [item for item in findings if item["severity"] == "blocking"]
    _print(
        {
            "version_id": version.version_id,
            "findings": findings,
            "summary": {
                "blocking": len(blocking),
                "warning": len(findings) - len(blocking),
            },
        }
    )
    return 0 if not blocking else 10


def cmd_versions(args: argparse.Namespace) -> int:
    with Warehouse(args.db, read_only=True) as warehouse:
        _print(warehouse.versions())
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with Warehouse(args.db, read_only=True) as warehouse:
        engine = _engine_for(warehouse, args.version)
        if args.view == "as-of":
            day = date.fromisoformat(args.date)
            _print(as_of(engine, day))
        elif args.view == "today":
            _print(today_view(engine))
        elif args.view == "lineage":
            if not args.unit:
                raise SystemExit("lineage 视图需要 --unit")
            matches = [
                period
                for period in engine.periods
                if period.unit_id == args.unit
            ]
            if not matches:
                raise SystemExit(f"版本中没有房号 {args.unit}")
            payload = []
            for period in matches:
                payload.append(
                    {
                        "period_key": period.key,
                        "validity": [period.valid_from, period.valid_to],
                        "lineage": engine.lineage(period.key),
                        "blockers": engine.period_blockers.get(period.key, []),
                    }
                )
            _print(payload)
        elif args.view == "stock":
            rows = warehouse.rows(
                "select * from stock_monthly where version_id = ? order by month, project, use",
                [engine.version.version_id],
            )
            _print(rows)
        elif args.view == "defects":
            rows = warehouse.rows(
                "select * from domain_defects where version_id = ? order by code",
                [engine.version.version_id],
            )
            _print(rows)
        else:  # pragma: no cover - argparse 已限制取值
            raise SystemExit(f"未知视图 {args.view}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    with Warehouse(args.db, read_only=args.summary_only) as warehouse:
        base_engine = _engine_for(warehouse, args.base)
        compare_engine = _engine_for(warehouse, args.compare)
        diffs = compare_versions(base_engine, compare_engine, args.month)
        payload = {
            "base_version_id": base_engine.version.version_id,
            "compare_version_id": compare_engine.version.version_id,
            "month": args.month,
            "rollup": project_rollup(diffs),
            "units": [diff.as_row() for diff in diffs if diff.classification != "no_change"],
            "unchanged_units": [diff.unit_id for diff in diffs if diff.classification == "no_change"],
        }
        if not args.summary_only:
            comparison_id = warehouse.record_comparison(
                base_engine.version.version_id, compare_engine.version.version_id, overwrite=True
            )
            payload["comparison_id"] = comparison_id
            for row in payload["units"]:
                warehouse.con.execute(
                    "insert into version_diffs values "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        comparison_id,
                        row["project"],
                        row["use"],
                        row["unit_id"],
                        row["base_period_key"],
                        row["compare_period_key"],
                        row["classification"],
                        row["classification_label"],
                        row["explanation"],
                        row["sold_delta"],
                        row["restricted_delta"],
                        row["permitted_delta"],
                        row["sellable_delta"],
                        row["physical_delta"],
                        row["base_status"],
                        row["compare_status"],
                        row["base_sellable"],
                        row["compare_sellable"],
                        row["base_blockers"],
                        row["compare_blockers"],
                        json.dumps(row["base_lineage"], ensure_ascii=False, sort_keys=True, default=_default),
                        json.dumps(row["compare_lineage"], ensure_ascii=False, sort_keys=True, default=_default),
                    ],
                )
        _print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saleable-units",
        description="预售房屋拆并与可售面积事实仓：入仓、生成数据集、查询版本差异。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB 文件路径")

    p = sub.add_parser("import", help="校验并入库一份资料目录，生成月度存量与实体状态")
    p.add_argument("source", type=Path, help="资料目录（含 manifest.json 与 schema.json）")
    p.add_argument(
        "--allow-blocked",
        action="store_true",
        help="即使存在阻塞性资料问题也入库，保留阻塞依据供复查",
    )
    add_db(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("check", help="只做领域检查，不写库")
    p.add_argument("source", type=Path)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("versions", help="列出已入库版本")
    add_db(p)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("query", help="按版本查询实体状态、沿革与存量")
    add_db(p)
    p.add_argument("--version", default="latest", help="版本号或 latest")
    p.add_argument(
        "--view",
        choices=["as-of", "today", "lineage", "stock", "defects"],
        default="as-of",
    )
    p.add_argument("--date", help="as-of 视图的查询日期 YYYY-MM-DD")
    p.add_argument("--unit", help="lineage 视图的房号")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("diff", help="比较两个版本的实体状态并分类差异原因")
    add_db(p)
    p.add_argument("--base", required=True, help="基准版本号或 latest")
    p.add_argument("--compare", required=True, help="对比版本号或 latest")
    p.add_argument("--month", help="对比月份；缺省取两个版本共同的最后一个月")
    p.add_argument("--summary-only", action="store_true", help="只输出汇总，不登记差异")
    p.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LookupError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
