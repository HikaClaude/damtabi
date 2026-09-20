#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""証拠層（ws_evidence / ws_outlet_profile / ws_eval_summary）と、dam_basin_spec.csv の入口の試験。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_basins as bb  # noqa: E402
import watershed_qa as wq  # noqa: E402
import ws_eval_summary as es  # noqa: E402
import ws_evidence as we  # noqa: E402
import ws_outlet_profile as op  # noqa: E402

EVAL_DIR = ROOT / "data" / "watershed" / "evaluations"
HEAD = "dam_id,dam_name,binran_no,binran_name,basin_raw,total_km2,direct_km2,indirect_km2,all_direct,note,source,basin_status"


def spec_file(tmp: Path, header: str, rows: list[str]) -> Path:
    p = tmp / "spec.csv"
    p.write_text(header + chr(10) + chr(10).join(rows) + chr(10), encoding="utf-8")
    return p


class TestSpecEntrance(unittest.TestCase):
    """dam_basin_spec.csv: 直接・間接流域・出典・未確認状態を dam_id 単位で受け取る入口。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.saved = bb.SPEC_CSV
        self.addCleanup(setattr, bb, "SPEC_CSV", self.saved)
        self.ids = {"a-1", "b-2", "c-3"}

    def load(self, *rows, header=HEAD):
        bb.SPEC_CSV = spec_file(self.tmp, header, list(rows))
        return bb.load_spec(self.ids)

    def test_confirmed_row_with_source_and_numbers_loads(self):
        s = self.load("a-1,A,,,,100,60,40,,,河川管理者の資料(試験),confirmed")
        self.assertEqual(s["a-1"]["basin_status"], "confirmed")
        self.assertEqual(s["a-1"]["source"], "河川管理者の資料(試験)")

    def test_unconfirmed_row_carries_no_numbers(self):
        s = self.load("a-1,A,,,,,,,,備考,,unconfirmed")
        self.assertEqual(s["a-1"]["basin_status"], "unconfirmed")

    def test_guessed_numbers_cannot_be_entered_as_unconfirmed(self):
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,,12.3,,,,,unconfirmed")

    def test_confirmed_needs_numbers_and_source(self):
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,,,,,,ダム便覧,confirmed")                 # 数値なし
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,100,100,0,1,,,confirmed")                # 出典なし（便覧番号もなし）

    def test_binran_number_counts_as_a_source(self):
        s = self.load("a-1,A,0873,A,617.5km2,617.5,617.5,0,1,,,confirmed")
        self.assertEqual(s["a-1"]["basin_status"], "confirmed")

    def test_bad_status_and_bad_numbers_are_rejected(self):
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,100,100,0,,,ダム便覧,maybe")
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,abc,100,0,,,ダム便覧,confirmed")
        with self.assertRaises(bb.SpecError):
            self.load("a-1,A,,,,-5,-5,0,,,ダム便覧,confirmed")

    def test_legacy_header_without_new_columns_still_loads(self):
        legacy = "dam_id,dam_name,binran_no,binran_name,basin_raw,total_km2,direct_km2,indirect_km2,all_direct,note"
        s = self.load("a-1,A,0873,A,617.5km2,617.5,617.5,0,1,", "b-2,B,,,,,,,,ダム便覧に該当を確認できず", header=legacy)
        self.assertEqual((s["a-1"]["basin_status"], s["b-2"]["basin_status"]), ("confirmed", "unconfirmed"))

    def test_diversion_flag_from_entrance_numbers(self):
        s = self.load("a-1,A,,,,100,60,40,,,資料(試験),confirmed",
                      "b-2,B,,,,50,50,0,,,資料(試験),confirmed",
                      "c-3,C,,,,,50,,,,資料(試験),confirmed")
        self.assertEqual(bb.diversion_flag(s["a-1"])["status"], "yes")
        self.assertEqual(bb.diversion_flag(s["b-2"])["status"], "none")
        self.assertEqual(bb.diversion_flag(s["c-3"])["status"], "unknown")      # 内訳が入力されていない
        self.assertEqual(bb.diversion_flag(None)["status"], "unknown")

    def test_shipped_spec_is_dam_id_keyed_with_expected_states(self):
        bb.SPEC_CSV = ROOT / "dam_basin_spec.csv"
        data = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))
        s = bb.load_spec({d["id"] for d in data["dams"]})
        self.assertEqual(len(s), 23)
        conf = {i for i, r in s.items() if r["basin_status"] == "confirmed"}
        self.assertEqual(len(conf), 21)
        self.assertEqual({i for i in s if i not in conf}, {"toyama-funagawa", "toyama-otani"})
        self.assertTrue(all(s[i]["source"] == "ダム便覧" for i in conf))
        # 既存23行の導水判定は、この入口の追加で変わっていない
        st = {i: bb.diversion_flag(r)["status"] for i, r in s.items()}
        self.assertEqual({i for i, v in st.items() if v == "yes"}, {"toyama-muromaki", "toyama-kurobe", "toyama-arimine"})
        self.assertEqual({i for i, v in st.items() if v == "unknown"}, {"toyama-funagawa", "toyama-otani"})
        self.assertEqual(sum(v == "none" for v in st.values()), 18)


def meta_of(**kw):
    m = {"computed_area_km2": 10.0, "kawabou_area_km2": 10.0, "outlet_method": "snap", "lake_km2": 0.0,
         "diversion": {"status": "unknown"}, "snap_radius_m": 150.0, "outlet_distance_m": 140}
    m.update(kw)
    return m


def basin(status="unconfirmed", direct=None, total=None, div="unknown"):
    return {"spec_row_present": status != "unconfirmed", "status": status, "direct_km2": direct,
            "indirect_km2": None, "total_km2": total, "source": "ダム便覧" if status == "confirmed" else None,
            "binran_no": None, "diversion_status": div}


def evi(ref=10.0, err=0.0, **b):
    return {"reference": {"area_km2": ref, "error_pct": err, "source": "川の防災情報", "computed_km2": 10.0},
            "basin_info": basin(**b)}


class TestEvidenceValues(unittest.TestCase):
    def test_coordinate_identical_differs_and_missing(self):
        dam = {"lat": 36.0, "lon": 137.0, "observation": {"obs_fcd": "X1"}}
        same = we.coordinate_evidence(dam, {"X1": {"lat": 36.0, "lon": 137.0}})
        self.assertEqual((same["class"], same["offset_m"]), ("identical_to_station_master", 0.0))
        near = we.coordinate_evidence(dam, {"X1": {"lat": 36.001, "lon": 137.0}})
        self.assertEqual(near["class"], "differs_from_station_master")
        self.assertAlmostEqual(near["offset_m"], 111.3, delta=1.0)
        self.assertEqual(we.coordinate_evidence(dam, {})["class"], "no_station_master")
        self.assertEqual(we.coordinate_evidence({"lat": 1, "lon": 1, "observation": {}}, {"X": {}})["class"],
                         "no_station_master")

    def test_reference_error_is_recomputed_from_areas(self):
        r = we.reference_evidence(meta_of(computed_area_km2=8.81, kawabou_area_km2=10.4))
        self.assertEqual(r["error_pct"], -15.3)
        self.assertEqual(r["source"], "川の防災情報")
        n = we.reference_evidence(meta_of(kawabou_area_km2=None))
        self.assertEqual((n["area_km2"], n["error_pct"], n["source"]), (None, None, None))

    def test_basin_info_records_presence_source_and_status(self):
        row = {"direct_km2": "85.2", "indirect_km2": "43.1", "total_km2": "128.3", "source": "ダム便覧",
               "binran_no": "0846", "basin_status": "confirmed"}
        b = we.basin_info_evidence(row, meta_of(diversion={"status": "yes"}))
        self.assertEqual((b["direct_km2"], b["indirect_km2"], b["source"], b["diversion_status"]),
                         (85.2, 43.1, "ダム便覧", "yes"))
        self.assertTrue(we.has_official(b))
        none = we.basin_info_evidence(None, meta_of())
        self.assertEqual((none["spec_row_present"], none["status"], none["direct_km2"]), (False, "unconfirmed", None))
        self.assertFalse(we.has_official(none))

    def test_lake_ratio_is_recorded_only(self):
        lk = we.lake_evidence(meta_of(outlet_method="reservoir", lake_km2=0.01, computed_area_km2=9.54))
        self.assertTrue(lk["detected"])
        self.assertAlmostEqual(lk["ratio_pct"], 0.105, places=3)
        self.assertIn("記録のみ", lk["note"])
        self.assertIsNone(we.lake_evidence(meta_of())["ratio_pct"])

    def test_profile_and_method_agreement(self):
        prof = {"radii": [{"r_m": r, "dist_m": r - 5, "area_km2": a}
                          for r, a in zip(we.PROFILE_RADII_M, (0.0, 2.28, 2.35, 2.42, 17.46, 17.86))],
                "downstream_km2": [[0, 2.28], [250, 2.4]]}
        p = we.profile_evidence(prof, meta_of(computed_area_km2=2.28))
        self.assertTrue(p["collected"])
        self.assertEqual(p["area_km2"], [0.0, 2.28, 2.35, 2.42, 17.46, 17.86])
        self.assertEqual(p["max_over_min_150_400"], round(2.42 / 2.28, 3))
        self.assertEqual(p["max_over_min_150_1000"], round(17.86 / 2.28, 3))
        self.assertFalse(we.profile_evidence(None, meta_of())["collected"])
        # 河道スナップの基には貯水池法との比較が無い
        self.assertFalse(we.method_agreement_evidence(prof, meta_of())["applicable"])
        ma = we.method_agreement_evidence(prof, meta_of(outlet_method="reservoir", computed_area_km2=2.28))
        self.assertEqual(ma["diff_pct_by_radius"][1], 0.0)
        self.assertEqual(ma["diff_pct_by_radius"][-1], round((17.86 / 2.28 - 1) * 100, 1))
        self.assertFalse(we.method_agreement_evidence(None, meta_of(outlet_method="reservoir"))["collected"])


class TestEvaluationGroups(unittest.TestCase):
    """T1〜T4 相当の評価区分の規則。数値は既存の ±15% だけ。"""

    def test_rules(self):
        g = we.evaluation_group
        self.assertEqual(g("PASS", evi(status="confirmed", direct=10.0, div="none"))[0], "G1")
        self.assertEqual(g("WARN", evi(status="confirmed", direct=10.0, div="none"))[0], "G1")
        self.assertEqual(g("WARN", evi(status="confirmed", direct=10.0, div="yes"))[0], "G2")
        self.assertEqual(g("WARN", evi(status="confirmed", direct=None, total=12.0, div="none"))[0], "G1")
        # 便覧があっても、参考面積が乖離していても、便覧が優先（臼中・朝日小川の型）
        self.assertEqual(g("WARN", evi(ref=48.2, err=-72.8, status="confirmed", direct=13.5, div="none"))[0], "G1")
        self.assertEqual(g("WARN", evi(err=-15.0))[0], "G3")
        self.assertEqual(g("WARN", evi(err=15.0))[0], "G3")
        self.assertEqual(g("WARN", evi(err=15.1)), ("G4", ["reference_divergent"]))
        self.assertEqual(g("WARN", evi(err=-15.3)), ("G4", ["reference_divergent"]))
        self.assertEqual(g("WARN", evi(ref=None, err=None)), ("G4", ["no_verification_material"]))
        for c in ("HOLD", "FAIL", "UNEVALUABLE"):
            self.assertEqual(g(c, evi(status="confirmed", direct=10.0, div="none")), ("G4", ["qa_not_passed"]))

    def test_unconfirmed_spec_row_is_not_official(self):
        self.assertEqual(we.evaluation_group("WARN", evi(err=1.0, status="unconfirmed"))[0], "G3")

    def test_no_new_thresholds(self):
        # ±15% は変えていない。区分の規則が使う数値は、この既存の値だけ
        self.assertEqual(we.REF_OK_PCT, 15.0)
        self.assertEqual(we.REF_OK_PCT, wq.THRESHOLDS["area_error_pct"])
        self.assertEqual(es.REF_OK_PCT, we.REF_OK_PCT)
        self.assertEqual(wq.THRESHOLDS, {
            "area_error_pct": 15.0, "grid_drift_pct": 15.0, "unreached_pct": 10.0, "tol_cells": 2,
            "mask_far_outside_pct": 2.0, "ring_far_only_pct": 5.0, "path_outside_pct": 2.0,
            "ring_self_intersections": 0, "mask_min_largest_pct": 95.0})

    def test_recorded_only_evidence_never_changes_the_group(self):
        base = evi(err=3.0)
        want = we.evaluation_group("WARN", dict(base))[0]
        for lake_ratio in (0.0, 0.01, 0.2, 5.0):                                 # 0.2% 水面比は条件にしない
            e = dict(base, lake={"ratio_pct": lake_ratio})
            self.assertEqual(we.evaluation_group("WARN", e)[0], want)
        for cls in ("identical_to_station_master", "differs_from_station_master", "no_station_master"):
            e = dict(base, coordinate={"class": cls, "offset_m": 700.0})
            self.assertEqual(we.evaluation_group("WARN", e)[0], want)
        e = dict(base, outlet_profile={"max_over_min_150_1000": 50.0}, method_agreement={"diff_pct_by_radius": [99.0]})
        self.assertEqual(we.evaluation_group("WARN", e)[0], want)


class TestOutletProfile(unittest.TestCase):
    def test_profile_on_a_synthetic_valley(self):
        # 谷底（中央の列）へ集まり、下へ流れる斜面。出口は谷底の下端付近
        n, mpp = 121, 10.0
        ii, jj = np.mgrid[0:n, 0:n]
        dem = (0.05 * np.abs(jj - 60) + 0.02 * (n - 1 - ii)).astype(np.float32)
        fd = bb.flow_dir(bb.fill_sinks(dem))
        acc = bb.flow_accum(fd)
        p = op.profile_one({"id": "t-1"}, {}, dem, fd, acc, mpp, 100, 60)
        self.assertEqual([x["r_m"] for x in p["radii"]], list(we.PROFILE_RADII_M))
        areas = [x["area_km2"] for x in p["radii"]]
        self.assertTrue(all(a > 0 for a in areas))
        self.assertLessEqual(areas[0], areas[-1])                # 候補の範囲が広がれば、より下流の大きな面積が見つかる
        down = p["downstream_km2"]
        self.assertEqual(down[0][0], 0)
        self.assertTrue(all(b[1] >= a[1] for a, b in zip(down, down[1:])))       # 下流ほど上流面積は増える
        self.assertEqual(p["mpp_m"], 10.0)


def cls_row(i, cls, codes=None):
    return {"id": i, "name": i.upper(), "pref": i.split("-")[0], "cls": cls, "codes": codes or []}


class TestSummaryEvidenceReport(unittest.TestCase):
    def test_report_is_keyed_by_dam_id_and_counts_groups(self):
        dams = [{"id": "p-a", "name": "A", "lat": 1.0, "lon": 1.0, "observation": {"obs_fcd": "F1"}},
                {"id": "p-b", "name": "B", "lat": 2.0, "lon": 2.0, "observation": {}}]
        metas = {"p-a": meta_of(computed_area_km2=10.0, kawabou_area_km2=10.5),
                 "p-b": meta_of(computed_area_km2=5.0, kawabou_area_km2=None)}
        cls = [cls_row("p-a", "WARN", ["no_official_direct_area"]), cls_row("p-b", "WARN", ["no_official_direct_area"])]
        rep = es.build_evidence_report(cls, dams, metas, {}, {"F1": {"lat": 1.0, "lon": 1.0}}, None)
        by = {o["id"]: o for o in rep["dams"]}
        self.assertEqual(by["p-a"]["evaluation_group"], "G3")
        self.assertEqual(by["p-b"]["evaluation_group"], "G4")
        self.assertEqual(by["p-b"]["group_reasons"], ["no_verification_material"])
        self.assertEqual(by["p-a"]["evidence"]["coordinate"]["class"], "identical_to_station_master")
        self.assertEqual({k: v["count"] for k, v in rep["groups"].items()}, {"G1": 0, "G2": 0, "G3": 1, "G4": 1})
        self.assertFalse(rep["profiles_collected"])
        for key in ("reference", "coordinate", "outlet_profile", "method_agreement", "lake", "basin_info"):
            self.assertIn(key, by["p-a"]["evidence"])

    def test_uncomputed_dam_is_g4_without_evidence(self):
        dams = [{"id": "p-a", "name": "A", "lat": 1.0, "lon": 1.0, "observation": {}}]
        rep = es.build_evidence_report([cls_row("p-a", "UNEVALUABLE", ["no_basin"])], dams, {}, {}, None, None)
        self.assertEqual((rep["dams"][0]["evaluation_group"], rep["dams"][0]["evidence"]), ("G4", None))


@unittest.skipUnless((EVAL_DIR / "national80_outline-exact.evidence.json").exists(), "証拠層の評価証跡が未生成")
class TestCommittedEvidence80(unittest.TestCase):
    """コミット済みの80基の証拠値・評価区分が、規則から再現でき、監査済みの 17/3/50/10 と一致する。"""

    rep = json.loads((EVAL_DIR / "national80_outline-exact.evidence.json").read_text(encoding="utf-8")) \
        if (EVAL_DIR / "national80_outline-exact.evidence.json").exists() else None
    summary = json.loads((EVAL_DIR / "national80_outline-exact.summary.json").read_text(encoding="utf-8"))

    def test_counts_match_the_audited_t1_to_t4(self):
        self.assertEqual({k: v["count"] for k, v in self.rep["groups"].items()},
                         {"G1": 17, "G2": 3, "G3": 50, "G4": 10})

    def test_g4_members(self):
        g4 = {o["id"] for o in self.rep["dams"] if o["evaluation_group"] == "G4"}
        self.assertEqual(g4, {"toyama-shiraiwagawa", "ishikawa-hakkagawa", "ishikawa-oya", "fukui-kuzuryu",
                              "nagano-uchimura", "nagano-toyooka", "nagano-onikuma",
                              "fukui-takinami", "fukui-kaitani", "fukui-yoshinosegawa"})
        g2 = {o["id"] for o in self.rep["dams"] if o["evaluation_group"] == "G2"}
        self.assertEqual(g2, {"toyama-muromaki", "toyama-kurobe", "toyama-arimine"})

    def test_all_80_dams_have_all_evidence_and_profiles(self):
        dams = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))["dams"]
        self.assertEqual([o["id"] for o in self.rep["dams"]], [d["id"] for d in dams])      # 順序も dam_id 基準
        self.assertTrue(self.rep["profiles_collected"])
        for o in self.rep["dams"]:
            ev = o["evidence"]
            self.assertEqual(set(ev), {"reference", "coordinate", "outlet_profile", "method_agreement", "lake", "basin_info"})
            self.assertTrue(ev["outlet_profile"]["collected"], o["id"])
            self.assertEqual(len(ev["outlet_profile"]["area_km2"]), len(we.PROFILE_RADII_M))
            self.assertEqual(ev["method_agreement"]["applicable"], ev["lake"]["detected"])

    def test_groups_are_reproducible_from_stored_evidence(self):
        for o in self.rep["dams"]:
            self.assertEqual(we.evaluation_group(o["qa_class"], o["evidence"]), (o["evaluation_group"], o["group_reasons"]))

    def test_qa_classes_are_the_audited_ones(self):
        self.assertEqual(self.summary["counts"], {"PASS": 6, "WARN": 73, "HOLD": 1, "FAIL": 0, "UNEVALUABLE": 0})
        by = {o["id"]: o["qa_class"] for o in self.rep["dams"]}
        self.assertEqual({d["id"]: d["cls"] for d in self.summary["dams"]}, by)
        self.assertEqual(by["toyama-shiraiwagawa"], "HOLD")                            # 白岩川は保留のまま

    def test_recorded_facts_match_the_audit(self):
        by = {o["id"]: o["evidence"] for o in self.rep["dams"]}
        self.assertEqual(sum(e["coordinate"]["class"] == "identical_to_station_master" for e in by.values()), 43)
        self.assertEqual(sum(e["coordinate"]["class"] == "no_station_master" for e in by.values()), 6)
        self.assertEqual(sum(e["basin_info"]["status"] == "confirmed" for e in by.values()), 21)
        self.assertEqual(by["fukui-kuzuryu"]["reference"]["error_pct"], -38.6)
        self.assertEqual(sum(e["lake"]["detected"] for e in by.values()), 27)


if __name__ == "__main__":
    unittest.main()
