#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実際のブラウザ（ヘッドレス Chrome）で、地図画面の2つの見え方を確かめる回帰試験。

1. ピンの大きさがズームに合わせて連続的に変わり、押せる範囲が見た目と一致する
2. 「集水域を閉じる」で、集水域を開く直前のパネルの見え方へ戻る
   （別のダム・パネルを閉じる・共有リンク・見つかりません では持ち越さない）

MapLibre を CDN から読むためネットワークが要る。ほかの試験（ネットワークを使わない）と分けるため、
環境変数 DAMTABI_BROWSER_TEST=1 のときだけ動く。

実行: set DAMTABI_BROWSER_TEST=1 && python -m unittest tests.test_browser_pin_ws -v

Chrome はこの試験が起動したもの（専用の一時プロファイル）だけを終了する。
ふだん使っている Chrome には触れない。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
HARNESS = Path(__file__).resolve().parent / "browser" / "pin_ws_harness.js"
STYLE = (DOCS / "style.css").read_text(encoding="utf-8")

DAM_A, DAM_B, DAM_B_NAME = "toyama-unazuki", "toyama-muromaki", "室牧ダム"
PHONE, PC = (390, 844), (1280, 860)
TIMEOUT_S = 180


def chrome_path() -> str | None:
    for p in (shutil.which("chrome"), shutil.which("google-chrome"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if p and Path(p).exists():
            return p
    return None


def css_px(name: str, block: str = STYLE) -> float:
    return float(re.search(re.escape(name) + r":\s*([\d.]+)", block).group(1))


def narrow_block() -> str:
    a = STYLE.index("@media (max-width: 720px)")
    return STYLE[a:STYLE.index("\n}\n", a)]


PIN_MIN = css_px("--pin-min")
PIN_MAX_PC = css_px("--pin-max")
PIN_MAX_PHONE = css_px("--pin-max", narrow_block())
Z_FROM = css_px("--pin-zoom-from")
Z_TO = css_px("--pin-zoom-to")


def expected_pin(z: float, pin_max: float) -> float:
    t = min(1.0, max(0.0, (z - Z_FROM) / (Z_TO - Z_FROM)))
    return PIN_MIN + (pin_max - PIN_MIN) * t


class _Handler(SimpleHTTPRequestHandler):
    """docs/ をそのまま配る（読むだけ）。index.html にだけ計測スクリプトを差し込み、SW は登録させない。"""

    sink = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html"):
            html = (DOCS / "index.html").read_text(encoding="utf-8")
            html, n = re.subn(r'<script>\s*if \("serviceWorker".*?</script>',
                              '<script src="/__harness.js"></script>', html, flags=re.S)
            assert n == 1, "index.html の Service Worker 登録部分が見つからない"
            return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/__harness.js":
            return self._send(HARNESS.read_bytes(), "text/javascript; charset=utf-8")
        if path == "/sw.js":
            self.send_error(404)
            return
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        type(self).sink(json.loads(self.rfile.read(n).decode("utf-8")))
        self._send(b"ok", "text/plain")

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _kill_own_chrome(proc: subprocess.Popen, profile: str) -> None:
    """この試験が起動した Chrome だけを終了する（名前での一括終了はしない）。"""
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # 念のため、この試験専用のプロファイルを使っている残りだけを探して止める
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*" + profile.replace("'", "''") + "*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_page(query: str, size: tuple[int, int], hash_: str = "") -> dict:
    got, done = {}, threading.Event()

    def sink(data):
        got.update(data)
        done.set()

    handler = type("H", (_Handler,), {"sink": staticmethod(sink)})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(DOCS)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    profile = tempfile.mkdtemp(prefix="damtabi-browser-test-")
    url = f"{base}/?{query}&sink={base}/__sink{hash_}"
    proc = subprocess.Popen(
        [chrome_path(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         f"--user-data-dir={profile}", f"--window-size={size[0]},{size[1]}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not done.wait(TIMEOUT_S):
            raise AssertionError(f"ブラウザから結果が返らない（{TIMEOUT_S}秒）: {query}")
    finally:
        _kill_own_chrome(proc, profile)
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(profile, ignore_errors=True)
    return got


@unittest.skipUnless(os.environ.get("DAMTABI_BROWSER_TEST") == "1", "DAMTABI_BROWSER_TEST=1 のときだけ実行")
@unittest.skipUnless(os.name == "nt" and chrome_path(), "Windows の Chrome がある環境のみ")
class _Browser(unittest.TestCase):
    results: dict = {}

    @classmethod
    def page(cls, key, query, size, hash_=""):
        if key not in cls.results:
            cls.results[key] = run_page(query, size, hash_)
            dump = os.environ.get("DAMTABI_BROWSER_DUMP")   # 計測値を見たいときの書き出し先（任意）
            if dump:
                Path(dump).mkdir(parents=True, exist_ok=True)
                (Path(dump) / f"{key}.json").write_text(
                    json.dumps(cls.results[key], ensure_ascii=False, indent=1), encoding="utf-8")
        r = cls.results[key]
        if any("maplibre" in e or "timeout" in e for e in r.get("errors", [])) and not r.get("cases"):
            raise unittest.SkipTest("地図を読み込めなかった（ネットワーク）: " + "; ".join(r["errors"]))
        return r


class TestPinSize(_Browser):
    """ズーム 11 以下は 48px、11→14 で連続的に大きくなり、14 以上は上限（PC 72px / スマホ 64px）。"""

    def check(self, key, size, pin_max):
        r = self.page(key, "mode=pin", size)
        self.assertEqual(r["errors"], [])
        c = r["cases"]
        for z, info in c["table"].items():
            exp = expected_pin(float(z), pin_max)
            self.assertAlmostEqual(info["w"], exp, delta=0.05, msg=f"zoom {z}")
            self.assertAlmostEqual(info["h"], exp, delta=0.05, msg=f"zoom {z}")
            self.assertAlmostEqual(info["gaugeW"], info["w"], delta=0.05, msg=f"見た目とボタンの大きさ zoom {z}")
            self.assertTrue(info["allSame"], f"zoom {z}")
            self.assertIn(round(info["font"] / info["w"] * 48, 2), (13.0, 16.0), f"数字もピンに比例 zoom {z}")
        self.assertAlmostEqual(c["table"]["8"]["w"], PIN_MIN, delta=0.05)
        self.assertAlmostEqual(c["table"]["16"]["w"], pin_max, delta=0.05)
        # 起動時の大きさは地図の実際のズームで決まっている
        self.assertAlmostEqual(c["initial"]["w"], expected_pin(float(c["initial"]["zoomVar"]), pin_max), delta=0.05)
        # 実際に「+」「−」で拡大縮小している間: 毎フレーム式どおり、途中で飛ばない
        frames = c["frames"]
        zooms = [f[0] for f in frames]
        self.assertGreaterEqual(max(zooms), Z_TO, "上限に届くズームまで拡大できている")
        for zv, w in frames:
            self.assertAlmostEqual(w, expected_pin(zv, pin_max), delta=0.05)
        jumps = [abs(b[1] - a[1]) for a, b in zip(frames, frames[1:])]
        self.assertLess(max(jumps), 4.0, "1フレームで 4px 以上変わらない")
        self.assertAlmostEqual(c["afterZoom"]["w"], c["initial"]["w"], delta=0.05)
        # 押せる範囲 = 見た目の円
        # 選択中は従来どおり 1.16 倍（最大のときの見た目の大きさ）
        self.assertAlmostEqual(c["activeBaseW"], pin_max, delta=0.05)
        self.assertAlmostEqual(c["activeMaxW"], pin_max * 1.16, delta=0.5)
        for k in ("hitInitial", "hitMax"):
            self.assertGreater(c[k]["tried"], 0, k)
            self.assertEqual(c[k]["ok"], c[k]["tried"], k)

    def test_pc(self):
        self.check("pin-pc", PC, PIN_MAX_PC)

    def test_phone(self):
        self.check("pin-phone", PHONE, PIN_MAX_PHONE)


class TestWatershedRestore(_Browser):
    """「集水域を閉じる」で開く直前の見え方へ戻る。ほかの終わり方では持ち越さない。"""

    RESTORE_CASES = ("top", "pre120", "top_mid", "pre120_mid", "again",
                     "switchDam_thenB", "closePanel_thenA", "shareLink_thenB", "notFound_thenA")

    def run_ws(self, key, size, mid):
        q = f"mode=ws&a={DAM_A}&b={DAM_B}&bname={quote(DAM_B_NAME)}&mid={mid}"
        return self.page(key, q, size, f"#dam_id={DAM_A}")

    def assert_restored(self, c, name):
        b, cl = c["steps"]["before"], c["steps"]["close"]
        for k in ("scrollTop", "nameY", "entryY"):
            self.assertLessEqual(abs(cl[k] - b[k]), 1, f"{name}: {k} {b[k]} → {cl[k]}")
        if "closeSync" in c["steps"]:
            self.assertLessEqual(abs(c["steps"]["closeSync"]["scrollTop"] - b["scrollTop"]), 1,
                                 f"{name}: 待たずに戻っている")
        self.assertFalse(cl["compact"], name)
        self.assertFalse(cl["wsActive"], name)
        self.assertEqual(cl["dimmed"], 0, name)

    def check(self, key, size, mid, phone):
        r = self.run_ws(key, size, mid)
        self.assertEqual(r["errors"], [])
        c = r["cases"]
        for name in self.RESTORE_CASES:
            self.assert_restored(c[name], name)
            op = c[name]["steps"]["open"]
            self.assertTrue(op["wsActive"], name)
            self.assertGreater(op["dimmed"], 0, name)
        if phone:
            # スマホでは開くと説明までシートが送られる（従来どおり）。閉じると戻る
            self.assertGreater(c["top"]["steps"]["open"]["scrollTop"], 0)
            self.assertTrue(c["top"]["steps"]["open"]["compact"])
        # 表示中にスクロールしても、閉じると「開く直前」へ戻る
        self.assertNotEqual(c["top_mid"]["steps"]["scrolled"]["scrollTop"],
                            c["top_mid"]["steps"]["before"]["scrollTop"])
        # 別のダム・パネルを閉じる・共有リンク・見つかりません: 集水域は止まり、古い位置を使わない
        sw = c["switchDam"]["steps"]["switched"]
        self.assertEqual(sw["name"], DAM_B_NAME)
        self.assertFalse(sw["wsActive"])
        self.assertEqual(sw["dimmed"], 0)
        self.assertTrue(c["closePanel"]["steps"]["closed"]["hidden"])
        self.assertEqual(c["closePanel"]["steps"]["closed"]["dimmed"], 0)
        self.assertEqual(c["shareLink"]["steps"]["switched"]["name"], DAM_B_NAME)
        self.assertFalse(c["shareLink"]["steps"]["switched"]["wsActive"])
        self.assertEqual(c["notFound"]["steps"]["notFound"]["dimmed"], 0)

    def test_phone(self):
        self.check("ws-phone", PHONE, 200, phone=True)

    def test_pc(self):
        self.check("ws-pc", PC, 300, phone=False)


if __name__ == "__main__":
    unittest.main()
