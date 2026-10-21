# -*- coding: utf-8 -*-
"""更新.cmd の貯水率集計が、標準地図（app.js の pinValueInfo）の表示と一致することの検証。"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rate_coverage as rc  # noqa: E402


def m(value=None, status="ok", reason=None):
    return {"value": value, "status": status, "reason": reason}


def dam(irr=None, calc=None, **kw):
    d = {"id": "x", "data_status": "ok", "rate_irrigation": irr if irr is not None else m(None, "not_provided"),
         "rate_irrigation_calculated": calc}
    d.update(kw)
    return d


class TestClassify(unittest.TestCase):
    def test_official_value(self):
        self.assertEqual(rc.classify(dam(m(58.4))), ("official", None))

    def test_zero_percent_is_shown_not_missing(self):
        self.assertEqual(rc.classify(dam(m(0))), ("official", None))
        self.assertEqual(rc.classify(dam(m(0.0))), ("official", None))

    def test_over_100_is_still_shown(self):
        self.assertEqual(rc.classify(dam(m(109.4))), ("official", None))

    def test_official_wins_over_reference_and_no_double_count(self):
        ds = [dam(m(50), calc=m(70))]
        s = rc.summarize(ds)
        self.assertEqual((s["shown"], s["official"], s["reference"], s["dash"]), (1, 1, 0, 0))

    def test_reference_used_only_when_official_absent(self):
        d = dam(m(None, "not_provided", "未提供"), calc=m(47.0))
        self.assertEqual(rc.classify(d), ("reference", None))

    def test_reference_with_bad_status_is_not_adopted(self):
        d = dam(m(None, "not_provided"), calc=m(47.0, "missing"))
        self.assertEqual(rc.classify(d)[0], "none")

    def test_data_status_ok_does_not_mean_shown(self):
        d = dam(m(None, "missing"), data_status="ok")
        self.assertEqual(rc.classify(d), ("none", "missing"))

    def test_data_status_partial_with_official_value_is_shown(self):
        d = dam(m(12.5), data_status="partial")
        self.assertEqual(rc.classify(d), ("official", None))

    def test_ok_status_without_number_is_dash(self):
        for bad in (m(None, "ok"), m("12", "ok"), m(True, "ok"), m(float("nan"), "ok")):
            self.assertEqual(rc.classify(dam(bad))[0], "none")

    def test_reasons(self):
        for st in ("not_provided", "missing", "closed", "no_source", "fetch_failed"):
            self.assertEqual(rc.classify(dam(m(None, st))), ("none", st))
        self.assertEqual(rc.classify(dam(m(None, "weird"))), ("none", "other"))
        self.assertEqual(rc.classify({"rate_irrigation": None}), ("none", "other"))

    def test_effective_rate_is_not_substituted(self):
        d = dam(m(None, "not_provided"), rate_effective=m(62.4))
        self.assertEqual(rc.classify(d)[0], "none")

    def test_dash_with_other_values_counted_separately(self):
        ds = [dam(m(None, "closed"), storage_level_m=m(400.0)),
              dam(m(None, "no_source"), storage_level_m=m(None, "no_source"))]
        s = rc.summarize(ds)
        self.assertEqual((s["dash"], s["dash_with_other_values"]), (2, 1))

    def test_totals_add_up(self):
        ds = [dam(m(10)), dam(m(0)), dam(None, calc=m(5)), dam(m(None, "closed")), dam(m(None, "missing"))]
        s = rc.summarize(ds)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["shown"] + s["dash"], s["total"])
        self.assertEqual(s["official"] + s["reference"], s["shown"])
        self.assertEqual(sum(s["reasons"].values()), s["dash"])

    def test_messages(self):
        s = rc.summarize([dam(m(10)), dam(None, calc=m(5)), dam(m(None, "closed"), storage_level_m=m(1.0))])
        text = "\n".join(rc.result_lines(s))
        self.assertIn("公式値 1基／参考値 1基", text)
        self.assertIn("閉局（現在の取得先の観測所）  1基", text)
        self.assertNotIn("値が取れた", text)
        self.assertNotIn("値がない", text)
        self.assertIn("3基のうち、標準地図で貯水率が出るのは2基", rc.done_line(s))

    def test_reason_labels_do_not_imply_no_other_source(self):
        # 理由は「今の取得先での状態」。他の取得先まで無いと読める言い方はしない
        for label in rc.REASON_LABELS.values():
            for bad in ("公表していない", "公表されていない", "情報源に無い", "データ提供なし"):
                self.assertNotIn(bad, label)
        self.assertEqual(rc.REASON_LABELS["not_provided"], "現在の取得先で利水貯水率が未提供")
        self.assertEqual(rc.REASON_LABELS["no_source"], "取得元未設定")


class TestMatchesAppJs(unittest.TestCase):
    """node が無い環境でも、app.js の選択規則が本集計の前提から変わっていないことを確かめる。"""

    def test_app_js_rule_unchanged(self):
        src = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
        self.assertRegex(src, r'item && item\.status === "ok" && typeof item\.value === "number"')
        body = re.search(r"function pinValueInfo\(dam\) \{(.*?)\n  \}", src, re.S).group(1)
        order = [body.index(k) for k in ('def.field !== "rate_irrigation"',
                                         "val(dam.rate_irrigation)",
                                         "val(dam.rate_irrigation_calculated)")]
        self.assertEqual(order, sorted(order))
        self.assertIn('default_basis', (ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))
        data = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))
        self.assertEqual(data["thresholds"]["default_basis"], "irrigation")
        self.assertEqual(data["thresholds"]["bases"]["irrigation"]["field"], "rate_irrigation")


class TestUpdateToolUsesIt(unittest.TestCase):
    def test_no_old_wording_left(self):
        src = (ROOT / "scripts" / "update_and_publish.py").read_text(encoding="utf-8")
        self.assertNotIn("値が取れた", src)
        self.assertNotIn("n_nodata", src)
        self.assertIn("rate_coverage.summarize", src)
        self.assertIn("rate_coverage.done_line", src)


class TestProductionSnapshot(unittest.TestCase):
    def test_current_dams_json_adds_up(self):
        data = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))
        s = rc.summarize(data["dams"])
        self.assertEqual(s["total"], len(data["dams"]))
        self.assertEqual(s["shown"] + s["dash"], s["total"])


if __name__ == "__main__":
    unittest.main()
