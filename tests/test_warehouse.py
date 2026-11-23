from pathlib import Path
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from decimal import Decimal
import json
import tempfile
import unittest

from saleable_units.checks import run_all
from saleable_units.cli import main as cli_main
from saleable_units.engine import Engine
from saleable_units.fractions import parse_fraction
from saleable_units.query import compare_versions, project_rollup, today_view
from saleable_units.source import load_source
from saleable_units.warehouse import Warehouse

from helpers import ROOT, full, unit, change, edge, right, occupation, measurement, write_source, copy_sample


def import_rows(db: Path, rows) -> str:
    with tempfile.TemporaryDirectory() as temp:
        path = write_source(Path(temp) / "src", rows)
        engine = Engine(load_source(path))
        with Warehouse(db) as warehouse:
            result = warehouse.import_version(engine)
        return result["version_id"]


class WarehouseRepeatability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "w.duckdb"

    def tearDown(self):
        self.temp.cleanup()

    def test_same_source_same_version_and_idempotent_import(self):
        first = import_rows(self.db, full(
            units=[unit("A1"), unit("A2")],
            rights=[right("P1", "permit", "A1"), right("P2", "permit", "A2")],
            measurements=[measurement("M1", "A1", "10.00"), measurement("M2", "A2", "20.00")],
        ))
        second = import_rows(self.db, full(
            units=[unit("A1"), unit("A2")],
            rights=[right("P1", "permit", "A1"), right("P2", "permit", "A2")],
            measurements=[measurement("M1", "A1", "10.00"), measurement("M2", "A2", "20.00")],
        ))
        self.assertEqual(first, second)
        with Warehouse(self.db, read_only=True) as warehouse:
            self.assertEqual(len(warehouse.versions()), 1)
            count = warehouse.con.execute("select count(*) from entity_states").fetchone()[0]
            self.assertGreater(count, 0)
            warehouse.close()
        # 再次导入同一版本不新增行
        with tempfile.TemporaryDirectory() as temp:
            path = write_source(Path(temp) / "src", full(
                units=[unit("A1"), unit("A2")],
                rights=[right("P1", "permit", "A1"), right("P2", "permit", "A2")],
                measurements=[measurement("M1", "A1", "10.00"), measurement("M2", "A2", "20.00")],
            ))
            with Warehouse(self.db) as warehouse:
                result = warehouse.import_version(Engine(load_source(path)))
                self.assertEqual(result["status"], "already_present")
                count_after = warehouse.con.execute(
                    "select count(*) from entity_states"
                ).fetchone()[0]
                warehouse.close()
        self.assertEqual(count, count_after)

    def test_different_bytes_create_new_version(self):
        rows = full(
            units=[unit("A1")],
            rights=[right("P1", "permit", "A1")],
            measurements=[measurement("M1", "A1", "10.00")],
        )
        first = import_rows(self.db, rows)
        rows2 = full(
            units=[unit("A1")],
            rights=[right("P1", "permit", "A1"), right("P2", "permit", "A1")],
            measurements=[measurement("M1", "A1", "10.00")],
        )
        second = import_rows(self.db, rows2)
        self.assertNotEqual(first, second)
        with Warehouse(self.db, read_only=True) as warehouse:
            self.assertEqual(len(warehouse.versions()), 2)
            warehouse.close()

    def test_stored_rows_reconstruct_same_version(self):
        version_id = import_rows(self.db, full(
            units=[unit("A1")],
            rights=[right("P1", "permit", "A1")],
            measurements=[measurement("M1", "A1", "10.00")],
        ))
        with Warehouse(self.db, read_only=True) as warehouse:
            engine = warehouse.engine_for(version_id)
            self.assertEqual(engine.version.version_id, version_id)
            warehouse.close()

    def test_stock_rows_are_area_based(self):
        import_rows(self.db, full(
            units=[unit("A1", building="B1"), unit("C1", building="B2", use="commercial")],
            rights=[
                right("P1", "permit", "A1"),
                right("P2", "permit", "C1", use="commercial"),
            ],
            measurements=[
                measurement("M1", "A1", "100.00"),
                measurement("M2", "C1", "30.00"),
            ],
        ))
        with Warehouse(self.db, read_only=True) as warehouse:
            rows = warehouse.rows(
                "select use, physical_area, sellable_area from stock_monthly where month='2026-01' order by use"
            )
            self.assertEqual(
                [(row["use"], row["physical_area"], row["sellable_area"]) for row in rows],
                [
                    ("commercial", Decimal("30.000000"), Decimal("30.000000")),
                    ("residential", Decimal("100.000000"), Decimal("100.000000")),
                ],
            )
            warehouse.close()


class VersionDiff(unittest.TestCase):
    def test_revision_produces_explained_difference(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "w.duckdb"
            base_dir = copy_sample(Path(temp))
            base = Engine(load_source(base_dir))
            with Warehouse(db) as warehouse:
                base_id = warehouse.import_version(base)["version_id"]

            # 修正：新增 C1 的实测面积，并释放 U3 上的限制。
            measurements = [
                json.loads(line) for line in (base_dir / "measurements.ndjson").read_text().splitlines()
            ]
            measurements.append(measurement("M4", "C1", "30.00"))
            rights = [
                json.loads(line) for line in (base_dir / "rights.ndjson").read_text().splitlines()
            ]
            # 解除日期早于资料截止日，使它在两个版本的可比快照中都可见。
            rights.append(right("REL1", "release", "U1", effective="2026-11-08",
                                reference="RES1", source_slice=["3/4", "1/1"]))
            from helpers import write_source
            revised = write_source(Path(temp) / "rev", full(
                units=[json.loads(line) for line in (base_dir / "units.ndjson").read_text().splitlines()],
                changes=[json.loads(line) for line in (base_dir / "changes.ndjson").read_text().splitlines()],
                edges=[json.loads(line) for line in (base_dir / "edges.ndjson").read_text().splitlines()],
                rights=rights,
                occupations=[json.loads(line) for line in (base_dir / "occupations.ndjson").read_text().splitlines()],
                measurements=measurements,
            ))
            revised_engine = Engine(load_source(revised))
            with Warehouse(db) as warehouse:
                revised_id = warehouse.import_version(revised_engine)["version_id"]
                self.assertNotEqual(base_id, revised_id)
                stored_base = warehouse.engine_for(base_id)
                stored_revised = warehouse.engine_for(revised_id)
                diffs = compare_versions(stored_base, stored_revised, "2026-11")
                by_unit = {diff.unit_id: diff for diff in diffs}
                self.assertEqual(by_unit["U3"].classification, "rights_restriction")
                self.assertEqual(by_unit["C1"].classification, "measurement_revision")
                self.assertEqual(by_unit["U2"].classification, "no_change")
                rollup = project_rollup(diffs)
                residential = [r for r in rollup if r["use"] == "residential"][0]
                self.assertEqual(residential["sellable_delta"], Decimal("25.5"))
                warehouse.close()

    def test_old_version_still_reviewable_after_source_dir_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "w.duckdb"
            source = copy_sample(Path(temp))
            with Warehouse(db) as warehouse:
                version_id = warehouse.import_version(Engine(load_source(source)))["version_id"]
                warehouse.close()
            # 修改来源目录：旧版本仍应能从库内原始行重建
            (source / "units.ndjson").write_text("", encoding="utf-8")
            with Warehouse(db, read_only=True) as warehouse:
                engine = warehouse.engine_for(version_id)
                self.assertEqual(engine.version.version_id, version_id)
                self.assertEqual(
                    {p.unit_id for p in engine.periods}, {"U1", "U2", "U3", "C1"}
                )
                warehouse.close()


def run_cli(args: list[str]) -> int:
    with redirect_stdout(StringIO()):
        return cli_main(args)


class CliEndToEnd(unittest.TestCase):
    def test_import_query_check_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "w.duckdb"
            self.assertEqual(run_cli(["import", str(ROOT / "data"), "--db", str(db)]), 0)
            self.assertEqual(run_cli(["check", str(ROOT / "data")]), 0)
            self.assertEqual(
                run_cli(["query", "--view", "as-of", "--date", "2026-11-10", "--db", str(db)]), 0
            )
            self.assertEqual(run_cli(["query", "--view", "today", "--db", str(db)]), 0)
            self.assertEqual(run_cli(["query", "--view", "stock", "--db", str(db)]), 0)
            self.assertEqual(run_cli(["versions", "--db", str(db)]), 0)

    def test_check_reports_blocking_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = full(
                units=[unit("A1", end="2026-06-01"), unit("A2", start="2026-06-01")],
                changes=[change("bad", "2026-06-01", 1)],
                edges=[edge("bad", "A1", "A2", "1/2", ["0/1", "1/2"])],
                measurements=[
                    measurement("M1", "A1", "10.00"),
                    measurement("M2", "A2", "10.00", effective="2026-06-01"),
                ],
            )
            from helpers import write_source
            path = write_source(Path(temp) / "src", rows)
            self.assertEqual(run_cli(["check", str(path)]), 10)


class Invariants(unittest.TestCase):
    def test_sample_passes_invariants(self):
        findings = run_all(load_source(ROOT / "data"))
        self.assertEqual(findings, [])

    def test_contract_area_never_duplicated(self):
        findings = run_all(load_source(ROOT / "data"))
        codes = {item["code"] for item in findings}
        self.assertNotIn("contract_area_duplicated", codes)

    def test_today_view_maps_new_to_old(self):
        engine = Engine(load_source(ROOT / "data"))
        rows = today_view(engine)
        mapping = {(row["unit_id"], row["root_unit_id"]) for row in rows}
        self.assertIn(("U2", "U1"), mapping)
        self.assertIn(("U3", "U1"), mapping)


if __name__ == "__main__":
    unittest.main()
