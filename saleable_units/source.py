"""来源资料装载、校验与版本指纹。

版本由 *内容* 决定：同一资料目录重复导入得到同一 ``version_id``，任何字节变化
都会生成新的版本。版本一旦生成即不可修改，供日报与复查使用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .errors import SourceError
from .fractions import parse_decimal, parse_fraction, parse_interval

DATASETS = ("units", "changes", "edges", "rights", "occupations", "measurements")


@dataclass(frozen=True)
class SourceVersion:
    version_id: str
    source_dir: str
    manifest_sha256: str
    file_sha256: dict[str, str]
    rows: dict[str, list[dict[str, Any]]] = field(hash=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{path.name}:{line_no}: 不是合法 JSON") from exc
        if not isinstance(row, dict):
            raise SourceError(f"{path.name}:{line_no}: 每行必须是对象")
        rows.append(row)
    return rows


def _expect_fields(dataset: str, rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    fields = schema["datasets"][dataset]["fields"]
    expected = set(fields)
    for index, row in enumerate(rows, 1):
        if set(row) != expected:
            raise SourceError(
                f"{dataset}.ndjson:{index}: 字段集合不符，缺少 {sorted(expected - set(row))}，"
                f"多出 {sorted(set(row) - expected)}"
            )
        for name, kind in fields.items():
            value = row[name]
            if kind == "any":
                continue
            ok = {
                "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "array": isinstance(value, list),
                "object": isinstance(value, dict),
            }[kind]
            if not ok:
                raise SourceError(f"{dataset}.ndjson:{index}: 字段 {name} 应为 {kind}")


def _parse_date(text: object, *, label: str) -> date:
    if not isinstance(text, str):
        raise SourceError(f"{label}: 日期必须是字符串")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SourceError(f"{label}: 不是 ISO 日期 {text!r}") from exc


def _validate_semantics(rows: dict[str, list[dict[str, Any]]]) -> None:
    """只校验格式与身份键；跨记录完整性留给领域检查，作为来源事实记录。"""

    seen_periods: set[tuple[str, str]] = set()
    for index, row in enumerate(rows["units"], 1):
        label = f"units.ndjson:{index}"
        if not row["unit_id"]:
            raise SourceError(f"{label}: unit_id 不能为空")
        if not row["project"] or not row["building"]:
            raise SourceError(f"{label}: project/building 不能为空")
        if not row["use"]:
            raise SourceError(f"{label}: use 不能为空")
        start = _parse_date(row["valid_from"], label=label + " valid_from")
        if row["valid_to"] is not None:
            end = _parse_date(row["valid_to"], label=label + " valid_to")
            if end <= start:
                raise SourceError(f"{label}: valid_to 必须晚于 valid_from")
        identity = (row["unit_id"], row["valid_from"])
        if identity in seen_periods:
            raise SourceError(
                f"{label}: unit_id={row['unit_id']!r} 在 {row['valid_from']} 重复声明，无法区分实体"
            )
        seen_periods.add(identity)

    change_ids = [row["change_id"] for row in rows["changes"]]
    if len(change_ids) != len(set(change_ids)):
        raise SourceError("changes.ndjson: change_id 必须唯一")
    for index, row in enumerate(rows["changes"], 1):
        label = f"changes.ndjson:{index}"
        if not row["change_id"]:
            raise SourceError(f"{label}: change_id 不能为空")
        _parse_date(row["effective"], label=label + " effective")
        if row["expected_edges"] < 0:
            raise SourceError(f"{label}: expected_edges 不能为负")

    for index, row in enumerate(rows["edges"], 1):
        label = f"edges.ndjson:{index}"
        if not row["change_id"]:
            raise SourceError(f"{label}: change_id 不能为空")
        if not row["from_unit"] or not row["to_unit"]:
            raise SourceError(f"{label}: from_unit/to_unit 不能为空")
        parse_fraction(row["fraction"], label=label + " fraction")
        parse_interval(row["source_slice"], label=label + " source_slice")

    records = [row["record_id"] for row in rows["rights"]]
    if len(records) != len(set(records)):
        raise SourceError("rights.ndjson: record_id 必须唯一")
    for index, row in enumerate(rows["rights"], 1):
        label = f"rights.ndjson:{index}"
        if row["kind"] not in {"permit", "restriction", "release"}:
            raise SourceError(f"{label}: 未知 kind {row['kind']!r}")
        if not row["scope"] or not row["use"]:
            raise SourceError(f"{label}: scope/use 不能为空")
        parse_fraction(row["share"], label=label + " share")
        parse_interval(row["source_slice"], label=label + " source_slice")
        _parse_date(row["effective"], label=label + " effective")
        if row["kind"] == "release" and not row["reference"]:
            raise SourceError(f"{label}: release 必须引用被解除的限制编号")

    contracts = [row["contract_id"] for row in rows["occupations"]]
    if len(contracts) != len(set(contracts)):
        raise SourceError("occupations.ndjson: contract_id 必须唯一")
    for index, row in enumerate(rows["occupations"], 1):
        label = f"occupations.ndjson:{index}"
        if not row["unit_id"] or not row["measurement_id"]:
            raise SourceError(f"{label}: unit_id/measurement_id 不能为空")
        parse_fraction(row["share"], label=label + " share")
        parse_decimal(row["contract_area"], label=label + " contract_area")
        parse_interval(row["source_slice"], label=label + " source_slice")
        _parse_date(row["effective"], label=label + " effective")

    measurements = [row["measurement_id"] for row in rows["measurements"]]
    if len(measurements) != len(set(measurements)):
        raise SourceError("measurements.ndjson: measurement_id 必须唯一")
    for index, row in enumerate(rows["measurements"], 1):
        label = f"measurements.ndjson:{index}"
        if not row["unit_id"]:
            raise SourceError(f"{label}: unit_id 不能为空")
        area = parse_decimal(row["area_sqm"], label=label + " area_sqm")
        if area < 0:
            raise SourceError(f"{label}: 面积不能为负")
        if row["basis"] not in {"predicted", "surveyed"}:
            raise SourceError(f"{label}: 未知 basis {row['basis']!r}")
        for key in ("effective", "received"):
            if not row[key]:
                raise SourceError(f"{label}: {key} 不能为空")
            _parse_date(row[key], label=label + " " + key)


def load_source(directory: str | Path) -> SourceVersion:
    directory = Path(directory)
    schema_path = directory / "schema.json"
    manifest_path = directory / "manifest.json"
    for required in (schema_path, manifest_path):
        if not required.is_file():
            raise SourceError(f"缺少必要文件：{required}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest_sha = _sha256(manifest_path.read_bytes())
    file_sha: dict[str, str] = {}
    rows: dict[str, list[dict[str, Any]]] = {}

    seen: set[str] = set()
    for entry in manifest["files"]:
        dataset = entry["dataset"]
        if dataset not in DATASETS:
            raise SourceError(f"manifest.json: 未知数据集 {dataset!r}")
        if dataset in seen:
            raise SourceError(f"manifest.json: 数据集 {dataset!r} 重复声明")
        seen.add(dataset)
        path = directory / entry["path"]
        if not path.is_file():
            raise SourceError(f"缺少资料文件：{entry['path']}")
        data = path.read_bytes()
        digest = _sha256(data)
        if digest != entry["sha256"]:
            raise SourceError(f"资料哈希不符：{entry['path']}")
        loaded = _read_ndjson(path)
        if len(loaded) != entry["rows"]:
            raise SourceError(f"资料行数不符：{entry['path']}")
        _expect_fields(dataset, loaded, schema)
        file_sha[dataset] = digest
        rows[dataset] = loaded

    missing = set(DATASETS) - seen
    if missing:
        raise SourceError(f"manifest.json 缺少数据集：{sorted(missing)}")
    _validate_semantics(rows)

    normalized = json.dumps(
        {
            "files": {name: [_row_fingerprint(r) for r in rows[name]] for name in DATASETS},
            "manifest_sha256": manifest_sha,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    version_id = _sha256(normalized.encode("utf-8"))
    return SourceVersion(
        version_id=version_id,
        source_dir=str(directory.resolve()),
        manifest_sha256=manifest_sha,
        file_sha256=file_sha,
        rows=rows,
    )


def _row_fingerprint(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
