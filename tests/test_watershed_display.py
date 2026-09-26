#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域の表示（app.js の実コード）: 地図に収める余白、面積の桁、導水が未確認のときの表示。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_watershed_pipeline import _run_jscript  # noqa: E402

APP_JS = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "docs" / "style.css").read_text(encoding="utf-8")


def cut(start: str, end: str) -> str:
    a = APP_JS.index(start)
    return APP_JS[a:APP_JS.index(end, a)]


def rect(left, top, right, bottom):
    return {"left": left, "top": top, "right": right, "bottom": bottom}


@unittest.skipUnless(os.name == "nt" and shutil.which("cscript"), "Windows の cscript がある環境のみ")
class TestFitPadding(unittest.TestCase):
    """fitPadding(mr, pr): 実際の地図の表示領域とパネルの位置から、fitBounds の余白を決める。"""

    def pad(self, mr, pr):
        fn = cut("function fitPadding(mr, pr) {", "function inRing(lng, lat, ring)")
        prog = (fn + chr(10) + "var p=fitPadding(" + json.dumps(mr) + "," + json.dumps(pr) + ");"
                "WScript.Echo([p.top,p.bottom,p.left,p.right,p.sheet].join(','));")
        t, b, l, r, s = _run_jscript(prog).strip().split(",")
        return {"top": int(t), "bottom": int(b), "left": int(l), "right": int(r), "sheet": s == "true"}

    def test_pc_with_side_panel_keeps_previous_values(self):
        # 1280×860、地図は上端 110 から、右パネル幅 400（従来: top80/bottom60/left60/右=パネル幅+40）
        p = self.pad(rect(0, 110, 1280, 860), rect(880, 58, 1280, 860))
        self.assertEqual(p, {"top": 80, "bottom": 60, "left": 60, "right": 440, "sheet": False})

    def test_pc_without_panel_keeps_previous_values(self):
        p = self.pad(rect(0, 110, 1280, 860), None)
        self.assertEqual(p, {"top": 80, "bottom": 60, "left": 60, "right": 60, "sheet": False})

    def test_phone_bottom_sheet_goes_to_bottom_padding(self):
        # 390×844、地図は上端 175 から、集水域表示中のシートは下 44%（上端 473）
        mr, pr = rect(0, 175, 390, 844), rect(0, 473, 390, 844)
        p = self.pad(mr, pr)
        self.assertTrue(p["sheet"])
        self.assertEqual((p["left"], p["right"]), (16, 16))            # 幅は画面いっぱいを使う
        self.assertEqual(p["bottom"], 16 + (844 - 473))                   # シートの高さ分だけ下を空ける
        free_h = (844 - 175) - p["top"] - p["bottom"]
        self.assertGreaterEqual(free_h, 80)
        self.assertLessEqual(175 + p["top"] + free_h, 473)                # 輪を置く枠はシートより上

    def test_phone_full_sheet_still_leaves_room(self):
        # シートが高いまま（76%）でも、余白の合計が地図より大きくならない（従来は fitBounds が動かなかった）
        mr, pr = rect(0, 175, 390, 844), rect(0, 203, 390, 844)
        p = self.pad(mr, pr)
        self.assertTrue(p["sheet"])
        self.assertGreaterEqual((844 - 175) - p["top"] - p["bottom"], 80)
        self.assertGreaterEqual(390 - p["left"] - p["right"], 80)

    def test_previous_bug_is_not_reproduced(self):
        # 従来の式（右 = パネル幅 + 40）はスマホで地図の幅を超えていた
        p = self.pad(rect(0, 175, 390, 844), rect(0, 473, 390, 844))
        self.assertLess(p["left"] + p["right"], 390)


class TestCompactSheetCss(unittest.TestCase):
    def test_compact_sheet_only_on_narrow_screens(self):
        a = STYLE.index("@media (max-width: 720px)")
        block_end = STYLE.index("\n}\n", a)
        self.assertIn("#panel.is-ws-compact", STYLE[a:block_end])       # スマホ幅の中にだけある
        self.assertNotIn("#panel.is-ws-compact", STYLE[:a])
        self.assertNotIn("#panel.is-ws-compact", STYLE[block_end:])


class TestRestoreViewOnClose(unittest.TestCase):
    """「集水域を閉じる」でだけ開く直前の見え方へ戻す。実際の見え方は tests/test_browser_pin_ws.py で確かめる。"""

    def test_saved_at_press_and_kept_only_until_stop(self):
        start = cut("api.start = function (dam) {", "api.stop = function (rerender) {")
        # 押した時点（読み込みを待つ前）に覚え、読み込み後の api.stop(false) で消えないよう、その後に入れる
        self.assertLess(start.index("var before = panelView(dam.id);"), start.index("Promise.all("))
        self.assertLess(start.index("api.stop(false);"), start.index("restore = before;"))

    def test_only_explicit_close_restores(self):
        stop = cut("api.stop = function (rerender) {", "function panelView(id)")
        self.assertIn("restore = null;", stop)                   # どの終わり方でも持ち越さない
        a = stop.index("if (rerender !== false && was")
        self.assertIn("pnl.scrollTop += now.y - view.y", stop[a:])   # 戻すのは rerender（閉じる）のときだけ
        self.assertNotIn("setTimeout", stop)                      # 固定時間の待ちに頼らない
        self.assertNotIn("scrollTop = 0", APP_JS)                 # 先頭へ戻すだけの仕様にしない
        self.assertEqual(APP_JS.count("api.stop(true)"), 1)      # 呼ぶのは「集水域を閉じる」だけ


class TestPinSizeCss(unittest.TestCase):
    """ピンの大きさは CSS の変数だけで調整できる（JS は地図のズームを渡すだけ）。"""

    def test_pin_size_from_css_variables(self):
        for v in ("--pin-min: 48px", "--pin-max: 115px", "--pin-zoom-from: 11", "--pin-zoom-to: 14"):
            self.assertIn(v, STYLE)
        a = STYLE.index("@media (max-width: 720px)")
        self.assertIn("#map { --pin-max: 72px; }", STYLE[a:STYLE.index("\n}\n", a)])   # スマホ幅の上限
        self.assertIn("width: var(--pin-size);", STYLE)
        self.assertNotIn("PIN_SIZE", APP_JS)                      # 大きさを JS に直書きしない
        self.assertIn('style.setProperty("--map-zoom"', APP_JS)
        self.assertIn('map.on("zoom", syncPinZoom)', APP_JS)


@unittest.skipUnless(os.name == "nt" and shutil.which("cscript"), "Windows の cscript がある環境のみ")
class TestAreaText(unittest.TestCase):
    def test_km2_digits(self):
        fn = cut("function km2(v) {", "function fitPadding(mr, pr)")
        vals = [3.51, 3.44, 1.48, 1.5, 608.76, 13.12, 10.31, 9.94]
        prog = fn + chr(10) + "var v=" + json.dumps(vals) + ",o=[];for(var i=0;i<v.length;i++)o.push(km2(v[i]));WScript.Echo(o.join('|'));"
        self.assertEqual(_run_jscript(prog).strip().split("|"),
                         ["3.5", "3.4", "1.5", "1.5", "609", "13", "10", "9.9"])

    def test_no_reference_and_caution_notes(self):
        body = cut("var div = d.diversion || {};", "box.innerHTML =")
        self.assertIn("照合できる流域面積の資料も確認できていない", body)   # 便覧も参考値も無いとき
        self.assertIn("d.caution", body)                                     # notes.json の注記
        self.assertIn("esc(String(d.caution))", body)                        # 注記はエスケープして出す
        self.assertIn("原因は特定できていません", body)                       # 差の理由を決めつけない

    def test_unknown_diversion_is_shown(self):
        body = cut("var div = d.diversion || {};", "box.innerHTML =")
        self.assertIn('div.status === "unknown"', body)
        self.assertIn("導水の有無は未確認", body)
        # 「近い値」は実際に近いときだけ
        self.assertIn("refDiff <= 15", body)


if __name__ == "__main__":
    unittest.main()
