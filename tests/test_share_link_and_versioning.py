#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A2（#dam_id= 共有リンク・資産の版付け）と集水域の統合部分の試験。ネットワークは使わない。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_site as bs  # noqa: E402

APP_JS = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")


def fn_body(name: str) -> str:
    a = APP_JS.index(f"function {name}(")
    return APP_JS[a:APP_JS.index("\n  }\n", a)]


class TestWatershedStopsOnHashScreens(unittest.TestCase):
    """closePanel を通らない A2 の画面でも、集水域の輪と雨を止める。"""

    def test_not_found_and_candidates_stop_watershed(self):
        for name in ("showDamNotFound", "showDamCandidates", "closePanel"):
            self.assertIn("ws.stop(false)", fn_body(name), name)

    def test_hashchange_is_wired_once_and_map_click_keeps_watershed_tap(self):
        self.assertEqual(APP_JS.count('addEventListener("hashchange", selectFromHash)'), 1)
        self.assertIn("ws.tap(ev.lngLat)", APP_JS)
        self.assertNotIn('map.on("click", closePanel)', APP_JS)

    def test_write_hash_uses_dam_id(self):
        body = fn_body("writeHash")
        self.assertIn('params.set("dam_id"', body)
        self.assertNotIn('"dam="', body)


class TestAssetVersionIncludesWatershed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for n in ("app.js", "style.css", "page.css", "sw.js"):
            shutil.copy(ROOT / "docs" / n, self.tmp / n)
        self.saved = bs.DOCS
        bs.DOCS = self.tmp
        self.addCleanup(setattr, bs, "DOCS", self.saved)

    def write_index(self, version):
        (self.tmp / "watershed").mkdir(exist_ok=True)
        (self.tmp / "watershed" / "index.json").write_text(
            json.dumps({"version": version, "generated": "2026-09-24", "dams": []}), encoding="utf-8")

    def test_stable_and_follows_watershed_version(self):
        v_none = bs.asset_version()
        self.write_index("aaaaaaaaaaaa")
        v_a = bs.asset_version()
        self.assertEqual(v_a, bs.asset_version())                 # 繰り返しても同じ（循環しない）
        self.assertNotEqual(v_none, v_a)
        self.write_index("bbbbbbbbbbbb")
        self.assertNotEqual(v_a, bs.asset_version())              # 集水域を作り直すと版が変わる
        # generated（日付）だけが変わっても版は変わらない
        p = self.tmp / "watershed" / "index.json"
        d = json.loads(p.read_text(encoding="utf-8")); d["generated"] = "2030-01-01"
        v_b = bs.asset_version()
        p.write_text(json.dumps(d), encoding="utf-8")
        self.assertEqual(v_b, bs.asset_version())

    def test_writing_the_version_into_sw_does_not_change_it(self):
        self.write_index("cccccccccccc")
        v = bs.asset_version()
        self.assertTrue(bs.write_sw_version(v))
        self.assertEqual(v, bs.asset_version())


class TestRepoIsBuilt(unittest.TestCase):
    """リポジトリの docs が build_site の版付けと揃っている（生成し忘れの検出）。"""

    def test_sw_and_index_carry_current_asset_version(self):
        v = bs.asset_version()
        sw = (ROOT / "docs" / "sw.js").read_text(encoding="utf-8")
        self.assertIn(f'var VERSION = "v{v}";', sw)
        idx = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"./app.js?v={v}", idx)
        self.assertIn(f"./style.css?v={v}", idx)

    def test_dam_pages_link_with_dam_id(self):
        pages = list((ROOT / "docs" / "dam").glob("*/*/index.html"))
        self.assertTrue(pages)
        for p in pages:
            s = p.read_text(encoding="utf-8")
            self.assertRegex(s, r'#dam_id=[a-z0-9-]+"', p)
            self.assertNotRegex(s, r"#dam=", p)


if __name__ == "__main__":
    unittest.main()
