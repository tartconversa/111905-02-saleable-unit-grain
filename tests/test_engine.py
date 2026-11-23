from pathlib import Path
from datetime import date
from fractions import Fraction
import tempfile
import unittest

from saleable_units.domain import build_source_model
from saleable_units.engine import Engine
from saleable_units.fractions import parse_fraction
from saleable_units.source import load_source

from helpers import ROOT, full, unit, change, edge, right, occupation, measurement, write_source


def states_by_unit(engine: Engine, day: date) -> dict:
    return {state.period.unit_id: state for state in engine.entity_states(day)}


class SampleSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(load_source(ROOT / "data"))

    def test_no_domain_defects(self):
        self.assertEqual(self.engine.defects, [])

    def test_split_preserves_space_and_signed_sale(self):
        # U1 在拆分前已售 1/2，限制 1/4；拆分后签约与限制应沿根空间落到新单元。
        before = states_by_unit(self.engine, date(2026, 10, 25))["U1"]
        self.assertEqual(before.sold_measure, Fraction(1, 2))
        self.assertEqual(before.restricted_measure, Fraction(1, 4))
        self.assertEqual(before.sellable_measure, Fraction(1, 4))
        after = states_by_unit(self.engine, date(2026, 11, 10))
        u2, u3 = after["U2"], after["U3"]
        # U2 承接 U1 空间 [0,3/5)：已售 [0,1/2) 全部在内，限制区间在 U3。
        # 份额按实体自身的 0..1 计量：1/2 ÷ 3/5 = 5/6。
        self.assertEqual(u2.sold_measure, Fraction(5, 6))
        self.assertEqual(u2.restricted_measure, Fraction(0))
        self.assertEqual(u2.area(u2.sold_measure), Fraction(51))
        # U3 承接 [3/5,1)，含限制区间 [3/4,1)：1/4 ÷ 2/5 = 5/8。
        self.assertEqual(u3.sold_measure, Fraction(0))
        self.assertEqual(u3.restricted_measure, Fraction(5, 8))
        self.assertEqual(u3.area(u3.restricted_measure), parse_fraction("51/2"))
        # 合同面积只随空间分摊到 U2，未复制到 U3。
        self.assertEqual(u2.contract_area, Fraction(50))
        self.assertEqual(u3.contract_area, Fraction(0))

    def test_areas_use_current_measurement_and_contract_history(self):
        after = states_by_unit(self.engine, date(2026, 11, 10))
        u2 = after["U2"]
        self.assertEqual(u2.physical_area, parse_fraction("306/5"))
        self.assertEqual(u2.measurement_id, "M2")
        self.assertEqual(u2.contract_area, Fraction(50))
        self.assertEqual(u2.area(u2.sold_measure), Fraction(51))
        self.assertEqual(u2.area(u2.sellable_measure), parse_fraction("51/5"))

    def test_measurement_received_after_effective_not_visible_early(self):
        # 实测 M2 的 effective 是 11-01，但 received 是 11-09；11-05 尚不可见。
        early = states_by_unit(self.engine, date(2026, 11, 5))
        self.assertEqual(early["U2"].physical_area, None)
        self.assertEqual(early["U2"].status, "blocked")
        self.assertIn("measurement_missing", [b.code for b in early["U2"].blockers])
        # 到达后可见，旧预测面积不适用于新单元。
        late = states_by_unit(self.engine, date(2026, 11, 10))
        self.assertEqual(late["U2"].physical_area, parse_fraction("306/5"))

    def test_existing_measurement_visible_before_survey(self):
        # 预测面积 M1 在 1-2 收到，11-05 前仍然有效
        state = states_by_unit(self.engine, date(2026, 10, 1))["U1"]
        self.assertEqual(state.measurement_id, "M1")
        self.assertEqual(state.physical_area, Fraction(100))

    def test_lineage_of_new_unit_points_to_old_space(self):
        lineage = self.engine.lineage("U2@2026-11-01")
        self.assertEqual(len(lineage), 1)
        self.assertEqual(lineage[0]["root_unit_id"], "U1")
        self.assertEqual((lineage[0]["root_start"], lineage[0]["root_end"]), ("0/1", "3/5"))
        self.assertEqual(lineage[0]["share_of_entity"], Fraction(1))
        composite = self.engine.lineage("U3@2026-11-01")
        self.assertEqual(composite[0]["root_start"], "3/5")

    def test_unrelated_building_isolated(self):
        # C1 属 B2，不因 B1 的沿革或限制被阻塞；只缺测绘，且缺口有明确依据。
        c1 = states_by_unit(self.engine, date(2026, 10, 25))["C1"]
        self.assertEqual([b.code for b in c1.blockers], ["measurement_missing"])
        self.assertEqual(c1.permitted_measure, Fraction(1))
        self.assertEqual(c1.sellable_measure, Fraction(0))


class SplitScenarios(unittest.TestCase):
    def _engine(self, rows):
        with tempfile.TemporaryDirectory() as temp:
            path = write_source(Path(temp) / "src", rows)
            return Engine(load_source(path))

    def test_insufficient_share_isolates_building(self):
        rows = full(
            units=[
                unit("X1", building="B9", end="2026-07-01"),
                unit("X2", building="B9", start="2026-07-01"),
                unit("OK1", building="B1"),
            ],
            changes=[change("bad", "2026-07-01", 1)],
            edges=[edge("bad", "X1", "X2", "1/2", ["0/1", "1/2"])],
            rights=[
                right("PX1", "permit", "X1"),
                right("PX2", "permit", "X2", effective="2026-07-01"),
                right("POK", "permit", "OK1"),
            ],
            measurements=[
                measurement("MX1", "X1", "80.00"),
                measurement("MX2", "X2", "80.00", effective="2026-07-01"),
                measurement("MOK", "OK1", "70.00"),
            ],
        )
        engine = self._engine(rows)
        codes = {defect.code for defect in engine.defects}
        self.assertIn("change_group_invalid", codes)
        snapped = states_by_unit(engine, date(2026, 8, 1))
        self.assertEqual(snapped["X2"].status, "blocked")
        self.assertNotEqual(snapped["OK1"].status, "blocked")
        self.assertEqual(snapped["OK1"].sellable_measure, Fraction(1))

    def test_source_overlap_blocks_only_related_space(self):
        rows = full(
            units=[
                unit("X1", building="B9", end="2026-07-01"),
                unit("X2", building="B9", start="2026-07-01"),
                unit("X3", building="B9", start="2026-07-01"),
            ],
            changes=[change("bad", "2026-07-01", 2)],
            edges=[
                edge("bad", "X1", "X2", "1/2", ["0/1", "1/2"]),
                edge("bad", "X1", "X3", "1/2", ["1/4", "3/4"]),
            ],
            rights=[],
            measurements=[
                measurement("MX1", "X1", "10.00"),
                measurement("MX2", "X2", "10.00", effective="2026-07-01"),
                measurement("MX3", "X3", "10.00", effective="2026-07-01"),
            ],
        )
        engine = self._engine(rows)
        self.assertIn("source_overlap", {d.code for d in engine.defects})
        self.assertIn("change_group_invalid", {d.code for d in engine.defects})
        snapped = states_by_unit(engine, date(2026, 8, 1))
        self.assertEqual(snapped["X2"].status, "blocked")
        self.assertEqual(snapped["X3"].status, "blocked")

    def test_merge_shares_are_conserved(self):
        rows = full(
            units=[
                unit("A1", end="2026-06-01"),
                unit("A2", end="2026-06-01"),
                unit("M1", start="2026-06-01"),
            ],
            changes=[change("merge", "2026-06-01", 2)],
            edges=[
                edge("merge", "A1", "M1", "1/1", ["0/1", "1/1"]),
                edge("merge", "A2", "M1", "1/1", ["0/1", "1/1"]),
            ],
            rights=[
                right("PA1", "permit", "A1"),
                right("PA2", "permit", "A2"),
                right("PM1", "permit", "M1", effective="2026-06-01"),
            ],
            occupations=[
                occupation("S1", "A1", share="1/2", contract_area="40.00", measurement_id="MA1"),
                occupation("S2", "A2", share="1/2", contract_area="30.00", measurement_id="MA2"),
            ],
            measurements=[
                measurement("MA1", "A1", "50.00"),
                measurement("MA2", "A2", "50.00"),
                measurement("MM1", "M1", "100.00", effective="2026-06-01"),
            ],
        )
        engine = self._engine(rows)
        self.assertEqual(engine.defects, [])
        merged = states_by_unit(engine, date(2026, 6, 15))["M1"]
        self.assertEqual(merged.permitted_measure, Fraction(1))
        # 两份 1/2 签约各占合并后空间的 1/4，合计 1/2；合同面积合计 70。
        self.assertEqual(merged.sold_measure, Fraction(1, 2))
        self.assertEqual(merged.contract_area, Fraction(70))
        self.assertEqual(merged.sellable_measure, Fraction(1, 2))
        self.assertEqual(merged.area(merged.sold_measure), Fraction(50))

    def test_cycle_blocks_related_periods(self):
        rows = full(
            units=[
                unit("P1", end="2026-12-01"),
                unit("P2", start="2026-06-01", end="2026-12-01"),
                unit("OK", building="B2"),
            ],
            changes=[
                change("a", "2026-06-01", 1),
                change("b", "2026-12-01", 1),
            ],
            edges=[
                edge("a", "P1", "P2", "1/1", ["0/1", "1/1"]),
                edge("b", "P2", "P1", "1/1", ["0/1", "1/1"]),
            ],
            rights=[right("POK", "permit", "OK")],
            measurements=[
                measurement("M1", "P1", "10.00"),
                measurement("M2", "P2", "10.00", effective="2026-06-01"),
                measurement("MOK", "OK", "10.00"),
            ],
        )
        engine = self._engine(rows)
        self.assertIn("evolution_cycle", {d.code for d in engine.defects})
        snapped = states_by_unit(engine, date(2026, 7, 1))
        self.assertEqual(snapped["P2"].status, "blocked")
        self.assertNotEqual(snapped["OK"].status, "blocked")

    def test_restriction_requires_release_to_end(self):
        rows = full(
            units=[unit("H1")],
            rights=[
                right("PH1", "permit", "H1"),
                right("RH1", "restriction", "H1", share="1/2",
                      effective="2026-03-01", source_slice=["0/1", "1/2"]),
                right("REL1", "release", "H1", effective="2026-05-01",
                      reference="RH1", source_slice=["0/1", "1/2"]),
            ],
            measurements=[measurement("MH1", "H1", "100.00")],
        )
        engine = self._engine(rows)
        self.assertEqual(engine.defects, [])
        before = states_by_unit(engine, date(2026, 4, 1))["H1"]
        self.assertEqual(before.restricted_measure, Fraction(1, 2))
        after = states_by_unit(engine, date(2026, 6, 1))["H1"]
        self.assertEqual(after.restricted_measure, Fraction(0))
        self.assertEqual(after.sellable_measure, Fraction(1))

    def test_release_cannot_target_permit(self):
        rows = full(
            units=[unit("H1")],
            rights=[
                right("PH1", "permit", "H1"),
                right("REL1", "release", "H1", effective="2026-05-01", reference="PH1"),
            ],
            measurements=[measurement("MH1", "H1", "100.00")],
        )
        engine = self._engine(rows)
        self.assertIn("release_targets_permit", {d.code for d in engine.defects})
        state = states_by_unit(engine, date(2026, 6, 1))["H1"]
        self.assertEqual(state.status, "blocked")


class RenameAndInheritance(unittest.TestCase):
    def _engine(self, rows):
        with tempfile.TemporaryDirectory() as temp:
            path = write_source(Path(temp) / "src", rows)
            return Engine(load_source(path))

    def test_rename_inherits_measurement(self):
        rows = full(
            units=[unit("OLD", end="2026-05-01"), unit("NEW", start="2026-05-01")],
            changes=[change("rename", "2026-05-01", 1)],
            edges=[edge("rename", "OLD", "NEW", "1/1", ["0/1", "1/1"])],
            rights=[
                right("P1", "permit", "OLD"),
                right("P2", "permit", "NEW", effective="2026-05-01"),
            ],
            measurements=[measurement("M1", "OLD", "120.00")],
        )
        engine = self._engine(rows)
        self.assertEqual(engine.defects, [])
        state = states_by_unit(engine, date(2026, 6, 1))["NEW"]
        self.assertEqual(state.physical_area, Fraction(120))
        self.assertEqual(state.status, "available")

    def test_split_does_not_inherit_measurement(self):
        rows = full(
            units=[unit("OLD", end="2026-05-01"), unit("N1", start="2026-05-01"), unit("N2", start="2026-05-01")],
            changes=[change("split", "2026-05-01", 2)],
            edges=[
                edge("split", "OLD", "N1", "1/2", ["0/1", "1/2"]),
                edge("split", "OLD", "N2", "1/2", ["1/2", "1/1"]),
            ],
            rights=[
                right("P1", "permit", "N1", effective="2026-05-01"),
                right("P2", "permit", "N2", effective="2026-05-01"),
            ],
            measurements=[measurement("M1", "OLD", "120.00")],
        )
        engine = self._engine(rows)
        for unit_id in ("N1", "N2"):
            state = states_by_unit(engine, date(2026, 6, 1))[unit_id]
            self.assertIsNone(state.physical_area)
            self.assertEqual(state.status, "blocked")


class CrossUse(unittest.TestCase):
    def _engine(self, rows):
        with tempfile.TemporaryDirectory() as temp:
            path = write_source(Path(temp) / "src", rows)
            return Engine(load_source(path))

    def test_merged_unit_does_not_inherit_use_qualification(self):
        # A1(residential) 与 A2(commercial) 合并为 M1(residential)：
        # 商业许可不能自动转成住宅资格；无法确定部分保留阻塞依据。
        rows = full(
            units=[
                unit("A1", end="2026-06-01", use="residential"),
                unit("A2", end="2026-06-01", use="commercial"),
                unit("M1", start="2026-06-01", use="residential"),
            ],
            changes=[change("merge", "2026-06-01", 2)],
            edges=[
                edge("merge", "A1", "M1", "1/1", ["0/1", "1/1"]),
                edge("merge", "A2", "M1", "1/1", ["0/1", "1/1"]),
            ],
            rights=[
                right("PA1", "permit", "A1", use="residential"),
                right("PA2", "permit", "A2", use="commercial"),
                right("PM1", "permit", "M1", share="1/1", use="residential",
                      effective="2026-06-01", source_slice=["0/1", "1/2"]),
            ],
            measurements=[
                measurement("MA1", "A1", "50.00"),
                measurement("MA2", "A2", "50.00"),
                measurement("MM1", "M1", "100.00", effective="2026-06-01"),
            ],
        )
        engine = self._engine(rows)
        state = states_by_unit(engine, date(2026, 6, 15))["M1"]
        codes = {blocker.code for blocker in state.blockers}
        self.assertIn("use_conflict", codes)
        self.assertLess(state.sellable_measure, Fraction(1))
        self.assertEqual(state.status, "partial")


if __name__ == "__main__":
    unittest.main()
