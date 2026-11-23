"""构造合成资料目录的测试辅助：写 NDJSON、schema 与 manifest。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ("units", "changes", "edges", "rights", "occupations", "measurements")
EMPTY_ROWS: dict[str, list[dict[str, Any]]] = {name: [] for name in DATASETS}


def write_source(directory: Path, rows: dict[str, list[dict[str, Any]]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    schema = json.loads((ROOT / "data" / "schema.json").read_text(encoding="utf-8"))
    (directory / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files = []
    for dataset in DATASETS:
        records = rows.get(dataset, [])
        path = directory / f"{dataset}.ndjson"
        text = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records
        )
        path.write_text(text, encoding="utf-8")
        files.append(
            {
                "path": path.name,
                "dataset": dataset,
                "rows": len(records),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (directory / "manifest.json").write_text(
        json.dumps({"description": "测试合成资料", "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return directory


def copy_sample(directory: Path) -> Path:
    """复制仓库自带的合成样本，便于就地修改后生成后续版本。"""

    target = directory / "sample"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT / "data", target)
    return target


def unit(
    unit_id: str,
    *,
    project: str = "P1",
    building: str = "B1",
    use: str = "residential",
    start: str = "2026-01-01",
    end: str | None = None,
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "project": project,
        "building": building,
        "use": use,
        "valid_from": start,
        "valid_to": end,
    }


def change(change_id: str, effective: str, expected_edges: int) -> dict[str, Any]:
    return {
        "change_id": change_id,
        "effective": effective,
        "expected_edges": expected_edges,
    }


def edge(
    change_id: str,
    from_unit: str,
    to_unit: str,
    fraction: str,
    source_slice: list[str],
) -> dict[str, Any]:
    return {
        "change_id": change_id,
        "from_unit": from_unit,
        "to_unit": to_unit,
        "fraction": fraction,
        "source_slice": source_slice,
    }


def right(
    record_id: str,
    kind: str,
    scope: str,
    *,
    share: str = "1/1",
    use: str = "residential",
    effective: str = "2026-01-01",
    reference: str | None = None,
    source_slice: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "kind": kind,
        "scope": scope,
        "share": share,
        "use": use,
        "effective": effective,
        "reference": reference,
        "source_slice": source_slice or ["0/1", "1/1"],
    }


def occupation(
    contract_id: str,
    unit_id: str,
    *,
    share: str = "1/1",
    contract_area: str = "10.00",
    measurement_id: str = "M1",
    effective: str = "2026-02-01",
    source_slice: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "contract_id": contract_id,
        "unit_id": unit_id,
        "share": share,
        "contract_area": contract_area,
        "measurement_id": measurement_id,
        "effective": effective,
        "source_slice": source_slice or ["0/1", "1/1"],
    }


def measurement(
    measurement_id: str,
    unit_id: str,
    area: str,
    *,
    basis: str = "surveyed",
    effective: str = "2026-01-01",
    received: str = "2026-01-02",
) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "unit_id": unit_id,
        "basis": basis,
        "area_sqm": area,
        "effective": effective,
        "received": received,
    }


def full(
    *,
    units: list[dict[str, Any]] | None = None,
    changes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    rights: list[dict[str, Any]] | None = None,
    occupations: list[dict[str, Any]] | None = None,
    measurements: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "units": units or [],
        "changes": changes or [],
        "edges": edges or [],
        "rights": rights or [],
        "occupations": occupations or [],
        "measurements": measurements or [],
    }
