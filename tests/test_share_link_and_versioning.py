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


class TestAssetVersionIgnoresLineEndings(unittest.TestCase):
    """同じ内容なら LF / CRLF の違いだけで資産の版が変わらない（作業場所の core.autocrlf に依らない）。"""

    def copy_docs(self, crlf: bool) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for n in ("app.js", "style.css", "page.css", "sw.js"):
            t = (ROOT / "docs" / n).read_bytes().replace(b"\r\n", b"\n")
            (d / n).write_bytes(t.replace(b"\n", b"\r\n") if crlf else t)
        (d / "watershed").mkdir()
        shutil.copy(ROOT / "docs/watershed/index.json", d / "watershed/index.json")
        return d

    def version_of(self, docs: Path) -> str:
        saved = bs.DOCS
        bs.DOCS = docs
        try:
            return bs.asset_version()
        finally:
            bs.DOCS = saved

    def test_lf_and_crlf_give_the_same_version(self):
        lf, crlf = self.copy_docs(False), self.copy_docs(True)
        self.assertNotEqual((lf / "app.js").read_bytes(), (crlf / "app.js").read_bytes())
        self.assertEqual(self.version_of(lf), self.version_of(crlf))
        self.assertEqual(self.version_of(lf), bs.asset_version())       # リポジトリの作業場所とも同じ

    def test_real_change_changes_the_version(self):
        d = self.copy_docs(True)
        v0 = self.version_of(d)
        with open(d / "style.css", "ab") as f:
            f.write(b"\r\n/* change */\r\n")
        self.assertNotEqual(v0, self.version_of(d))

    def test_files_are_not_rewritten(self):
        d = self.copy_docs(True)
        before = (d / "app.js").read_bytes()
        self.version_of(d)
        self.assertEqual(before, (d / "app.js").read_bytes())


class TestUpdateToolPublishesSw(unittest.TestCase):
    """貯水率の定期更新（update_and_publish.py）が、作り直した sw.js をページと一緒に公開対象にする。"""

    def test_sw_is_in_publish_paths_and_change_is_detected(self):
        import subprocess
        import update_and_publish as up
        self.assertIn("docs/sw.js", up.PUBLISH_PATHS)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "docs/dam").mkdir(parents=True)
        for rel in ("docs/sw.js", "docs/index.html", "docs/dam/index.html", "docs/app.js"):
            (tmp / rel).write_text("v1\n", encoding="utf-8")
        g = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True, check=True)
        g("init", "-q"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        saved = up.ROOT
        up.ROOT = tmp
        try:
            before = up.hashes_of(up.PUBLISH_PATHS)
            for rel in ("docs/sw.js", "docs/index.html", "docs/app.js"):          # build_site が書き直した想定
                (tmp / rel).write_text("v2\n", encoding="utf-8")
            after = up.hashes_of(up.PUBLISH_PATHS)
        finally:
            up.ROOT = saved
        changed = sorted(n for n, h in after.items() if before.get(n) != h)
        self.assertEqual(changed, ["docs/index.html", "docs/sw.js"])       # app.js など許可リスト外は巻き込まない


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
