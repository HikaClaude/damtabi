#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実際のブラウザ（ヘッドレス Chrome）で、詳細パネルの観測値表示を確かめる回帰試験。

1. 選んでいる基準で貯水率を出せるダムは従来どおり貯水率が主役。出せないダムは
   取得できている観測値（貯水量 → 流入・放流 → 水位）を先に出し、大きな「—」は出さない
2. 観測値は上下に重複して出さない。0 と欠測を区別し、理由は status ごとに言い分ける
3. 集水域の説明に、正常な流入量と観測時刻があるときだけ流入量を添える。
   ダム切替・閉じる・非対応ダムで前のダムの値を残さない

データは保存済みの docs/data/dams.json。足りない条件（水位のみ、1万m³未満、取得失敗）だけは
?synth=1 のときに配信直前のコピーへ合成し、ファイルは書き換えない。

MapLibre を CDN から読むためネットワークが要る。DAMTABI_BROWSER_TEST=1 のときだけ動く。
実行: set DAMTABI_BROWSER_TEST=1 && python -m unittest tests.test_browser_panel_obs -v
"""

from __future__ import annotations

import copy
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

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_browser_pin_ws import _kill_own_chrome, chrome_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
HARNESS = Path(__file__).resolve().parent / "browser" / "panel_obs_harness.js"
DAMS = json.loads((DOCS / "data" / "dams.json").read_text(encoding="utf-8"))
PHONE, PC = (390, 844), (1280, 860)
TIMEOUT_S = 180

FRIENDLY = ["貯まっている水の量", "ダムに入ってくる水", "ダムから出ていく水", "水面の高さ（標高）"]
KEYS = ["storage_capacity_1000m3", "inflow_m3s", "outflow_m3s", "storage_level_m"]
PHRASE = {
    "not_provided": "現在の取得先では提供されていません",
    "missing": "欠測です",
    "closed": "「閉局」扱い",
    "no_source": "取得元が未設定です",
    "fetch_failed": "取得に失敗しました",
}


def ok(m) -> bool:
    return isinstance(m, dict) and m.get("status") == "ok" and isinstance(m.get("value"), (int, float)) \
        and not isinstance(m.get("value"), bool)


def synth(data: dict) -> dict:
    """足りない条件だけを合成する（配信用のコピーにだけ適用）。"""
    d = copy.deepcopy(data)
    by = {x["id"]: x for x in d["dams"]}
    miss = {"value": None, "status": "missing", "reason": "欠測"}
    a = by["gifu-atagi"]                       # 水位のみ
    for k in ("storage_capacity_1000m3", "inflow_m3s", "outflow_m3s"):
        a[k] = dict(miss)
    i = by["gifu-iwamura"]                     # 1万m³未満の貯水量・流入量の取得失敗
    i["storage_capacity_1000m3"] = {"value": 0.5, "status": "ok", "reason": None}
    i["inflow_m3s"] = {"value": None, "status": "fetch_failed", "reason": "取得失敗"}
    by["fukui-eiheiji"]["storage_capacity_1000m3"] = {"value": 12345.6, "status": "ok", "reason": None}
    by["fukui-eiheiji"]["rate_effective"] = dict(miss)     # 利水は未提供・有効は欠測（理由が違う）
    return d


SYNTH = synth(DAMS)
BY_REAL = {x["id"]: x for x in DAMS["dams"]}
BY_SYNTH = {x["id"]: x for x in SYNTH["dams"]}


class _Handler(SimpleHTTPRequestHandler):
    sink = None
    use_synth = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html"):
            html = (DOCS / "index.html").read_text(encoding="utf-8")
            html, n = re.subn(r'<script>\s*if \("serviceWorker".*?</script>',
                              '<script src="/__harness.js"></script>', html, flags=re.S)
            assert n == 1
            return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/__frame":
            # ヘッドレス Chrome はウィンドウ幅を 512px 未満にできないので、
            # スマホ幅は iframe の幅で作る（メディアクエリは iframe の幅で効く）
            q = self.path.split("?", 1)[1]
            w, h = re.search(r"pw=(\d+)", q).group(1), re.search(r"ph=(\d+)", q).group(1)
            html = (f'<!doctype html><meta charset="utf-8"><body style="margin:0">'
                    f'<iframe src="/?{q}" style="border:0;width:{w}px;height:{h}px"></iframe>')
            return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/__harness.js":
            return self._send(HARNESS.read_bytes(), "text/javascript; charset=utf-8")
        if path == "/data/dams.json" and self.use_synth:
            return self._send(json.dumps(SYNTH, ensure_ascii=False).encode("utf-8"), "application/json")
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


def run_page(query: str, size: tuple[int, int], use_synth: bool = False) -> dict:
    got, done = {}, threading.Event()

    def sink(data):
        got.update(data)
        done.set()

    handler = type("H", (_Handler,), {"sink": staticmethod(sink), "use_synth": use_synth})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(DOCS)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    profile = tempfile.mkdtemp(prefix="damtabi-browser-test-")
    url = f"{base}/?{query}&sink={base}/__sink"
    window = size
    if size[0] < 600:
        url = f"{base}/__frame?{query}&pw={size[0]}&ph={size[1]}&sink={base}/__sink"
        window = (1000, size[1] + 40)
    proc = subprocess.Popen(
        [chrome_path(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         f"--user-data-dir={profile}", f"--window-size={window[0]},{window[1]}", url],
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


REAL_IDS = ["toyama-unazuki", "toyama-tori", "toyama-kumanogawa", "toyama-usunaka", "toyama-otani",
            "toyama-kurobe", "ishikawa-akase", "ishikawa-dainichigawa", "gifu-agigawa", "fukui-otsuro",
            "nagano-asakawa", "toyama-goi"]
SYNTH_IDS = ["gifu-atagi", "gifu-iwamura", "fukui-eiheiji"]


@unittest.skipUnless(os.environ.get("DAMTABI_BROWSER_TEST") == "1", "DAMTABI_BROWSER_TEST=1 のときだけ実行")
@unittest.skipUnless(os.name == "nt" and chrome_path(), "Windows の Chrome がある環境のみ")
class _Browser(unittest.TestCase):
    results: dict = {}

    @classmethod
    def page(cls, key, query, size, use_synth=False):
        if key not in cls.results:
            cls.results[key] = run_page(query, size, use_synth)
            dump = os.environ.get("DAMTABI_BROWSER_DUMP")
            if dump:
                Path(dump).mkdir(parents=True, exist_ok=True)
                (Path(dump) / f"{key}.json").write_text(
                    json.dumps(cls.results[key], ensure_ascii=False, indent=1), encoding="utf-8")
        r = cls.results[key]
        if any("maplibre" in e or "timeout" in e for e in r.get("errors", [])) and not r.get("cases"):
            raise unittest.SkipTest("地図を読み込めなかった（ネットワーク）: " + "; ".join(r["errors"]))
        return r


class TestPanelLayout(_Browser):

    def shown(self, dam, basis):
        if basis == "effective":
            return ok(dam.get("rate_effective"))
        return ok(dam.get("rate_irrigation")) or ok(dam.get("rate_irrigation_calculated"))

    def check_one(self, dam, s, basis, msg):
        order = s["order"]
        self.assertFalse(s["overflowX"], msg + " 横にはみ出さない")
        if self.shown(dam, basis):
            # 従来どおり: 貯水率 → 観測時刻。観測値は下の表（わかりやすい項目名）
            self.assertNotIn("obs", order, msg)
            self.assertLess(order.index("rates"), order.index("panel-freshness"), msg)
            self.assertEqual(s["facts"][2:6], FRIENDLY, msg)
            return
        self.assertEqual(s["bigDash"], 0, msg + " 大きな「—」を出さない")
        flow_keys = KEYS[:3]
        has_flow = any(ok(dam.get(k)) for k in flow_keys)
        for f in FRIENDLY:
            self.assertNotIn(f, s["facts"], msg + " 観測値を下の表に重ねて出さない")
        if has_flow:
            # 水の出入り → 集水域ボタン → 観測時刻・出典 の順
            self.assertNotIn("obs", order, msg)
            self.assertLess(order.index("flow"), order.index("ws-entry"), msg)
            self.assertLess(order.index("ws-entry"), order.index("panel-freshness"), msg)
            cells = s["flow"]
            self.assertEqual([c["badge"] for c in cells], ["貯まる", "入る", "出る"], msg)
            for c, k in zip(cells, flow_keys):
                self.assertFalse(c["clipped"], msg + " 文字切れなし")
                item = dam.get(k)
                if ok(item):
                    self.assertNotIn("is-lack", c["cls"], msg)
                else:
                    self.assertIn("is-lack", c["cls"], msg)
                    self.assertEqual(c["v"], {"not_provided": "未提供", "missing": "欠測", "closed": "閉局",
                                              "no_source": "取得元未設定", "fetch_failed": "取得失敗"}.get(
                        (item or {}).get("status"), "値なし"), msg + " 欠測を0にしない")
            # 貯水量が上、流入・放流は同じ高さで左右に並ぶ
            self.assertLess(cells[0]["y"], cells[1]["y"], msg)
            self.assertEqual(cells[1]["y"], cells[2]["y"], msg)
            self.assertLess(cells[1]["x"], cells[2]["x"], msg)
            # ボタンは出入りのすぐ下、観測時刻はボタンの直下
            self.assertLess(s["wsGoY"] - s["flowBottom"], 40, msg + " ボタンが出入りの直後")
            self.assertLess(s["freshY"] - s["wsGoBottom"], 40, msg + " 観測時刻がボタン直下")
            lv = dam.get("storage_level_m")
            self.assertEqual("水面の標高" in s["facts"], ok(lv), msg)
            self.assertNotIn("貯水位", s["facts"], msg)
        else:
            self.assertLess(order.index("obs"), order.index("panel-freshness"), msg)
            self.assertNotIn("flow", order, msg)
            have = [FRIENDLY[i] for i, k in enumerate(KEYS) if ok(dam.get(k))]
            self.assertEqual([next((f for f in FRIENDLY if o["k"].startswith(f)), o["k"]) for o in s["obs"]],
                             have, msg)
            lack = [(FRIENDLY[i], dam.get(k)) for i, k in enumerate(KEYS) if not ok(dam.get(k))]
            self.assertEqual(len(s["lack"]), len(lack), msg)
            for (label, item), line in zip(lack, s["lack"]):
                self.assertTrue(line.startswith(label), msg)
                st = (item or {}).get("status")
                if st in PHRASE:
                    self.assertIn(PHRASE[st], line, msg)
            self.assertEqual(bool(s["obsNone"]), not have, msg)
        # 率: 値がある率は自分の名前のカードで、値が無い率は1行の理由で
        for label, key in (("利水貯水率", "rate_irrigation"), ("有効貯水率", "rate_effective")):
            if ok(dam.get(key)):
                self.assertTrue(any(c.startswith(label + "|") for c in s["rateCards"]), msg)
            else:
                # 対象の率を明記した行（同じ理由なら「利水貯水率・有効貯水率」にまとまる）
                line = [x for x in s["rateLack"] if label in x or x.startswith("利水貯水率・有効貯水率")]
                self.assertGreaterEqual(len(line), 1, msg)
                st = (dam.get(key) or {}).get("status")
                if st in PHRASE:
                    self.assertTrue(any(PHRASE[st] in x for x in line), msg)

    def check_all(self, r, by, ids):
        self.assertEqual(r["errors"], [])
        for i in ids:
            c = r["cases"][i]
            dam = by[i]
            self.check_one(dam, c["irrigation"], "irrigation", f"{i} 利水")
            self.check_one(dam, c["effective"], "effective", f"{i} 有効")
            self.assertEqual(c["back"]["order"], c["irrigation"]["order"], f"{i} 基準を戻すと元どおり")
            self.assertEqual(c["irrigation"]["fresh"], c["back"]["fresh"])
            # 観測時刻は従来の文言・位置のまま
            if dam.get("obs_time"):
                self.assertIn("観測の値です", c["irrigation"]["fresh"])
            else:
                self.assertIn("観測値の配信がありません", c["irrigation"]["fresh"])

    def values(self, r, i, basis="irrigation"):
        c = r["cases"][i][basis]
        return [o["v"] for o in (c["flow"] or c["obs"])]

    def test_real_phone(self):
        r = self.page("real_phone", "mode=panel&ids=" + ",".join(REAL_IDS), PHONE)
        self.assertEqual(r["width"], PHONE[0], "スマホ幅で測れている")
        self.check_all(r, BY_REAL, REAL_IDS)
        self.assertEqual(self.values(r, "toyama-tori"),
                         ["378.1万 m³", "毎秒 3.45 m³", "毎秒 0 m³"])
        self.assertIn("水面の標高", r["cases"]["toyama-tori"]["irrigation"]["facts"])
        self.assertIn("329.15 m", r["cases"]["toyama-tori"]["irrigation"]["factsText"])
        self.assertEqual(self.values(r, "ishikawa-akase")[0], "0 m³", "0 は 0 として出す")
        self.assertIn("毎秒 0 m³", self.values(r, "fukui-otsuro"))
        # 阿木川: 利水基準では観測値が先、有効率は「有効貯水率」のまま（利水率に読み替えない）
        ag = r["cases"]["gifu-agigawa"]
        self.assertIn("有効貯水率|62.4%", ag["irrigation"]["rateCards"])
        self.assertNotIn("obs", ag["effective"]["order"])
        # 大日川: 利水基準は参考貯水率で主役、有効基準では観測値が先
        dn = r["cases"]["ishikawa-dainichigawa"]
        self.assertNotIn("obs", dn["irrigation"]["order"])
        self.assertIn("flow", dn["effective"]["order"])
        # 宇奈月: 貯水率あり。下の表に観測値（わかりやすい項目名・単位）
        un = r["cases"]["toyama-unazuki"]["irrigation"]["factsText"]
        self.assertIn("毎秒 39.51 m³", un)
        self.assertIn("標高 240.61 m", un)
        self.assertIn("300.2万 m³", un)
        # 閉局はダム全体の停止と読ませない
        self.assertTrue(all("ダムの運用状況を示すものではありません" in x
                            for x in r["cases"]["toyama-otani"]["irrigation"]["lack"]))

    def test_real_pc(self):
        r = self.page("real_pc", "mode=panel&ids=" + ",".join(REAL_IDS), PC)
        self.check_all(r, BY_REAL, REAL_IDS)
        flows = r["cases"]["toyama-tori"]["irrigation"]["flow"][1:]
        self.assertEqual(len(flows), 2)
        self.assertEqual(flows[0]["w"], flows[1]["w"], "流入・放流は同じ幅で横並び")

    def test_synth_phone(self):
        r = self.page("synth_phone", "mode=panel&ids=" + ",".join(SYNTH_IDS), PHONE, use_synth=True)
        self.check_all(r, BY_SYNTH, SYNTH_IDS)
        self.assertEqual(self.values(r, "gifu-atagi"), ["標高 571.48 m"], "水位のみ")
        self.assertIn("水の深さではありません", r["cases"]["gifu-atagi"]["irrigation"]["obs"][0]["sub"])
        self.assertEqual(r["cases"]["gifu-atagi"]["irrigation"]["flow"], [], "出入りが無ければ従来の観測値欄")
        self.assertEqual(self.values(r, "gifu-iwamura"), ["500 m³", "取得失敗", "毎秒 0.11 m³"], "小さい値を 0 に丸めず、失敗を 0 にしない")
        self.assertEqual(self.values(r, "fukui-eiheiji")[0], "1,234.56万 m³", "元データ以上の精度を付けない")
        # 率ごとに理由が違うときは、詳細で区別できる（タップで開ける details）
        e = r["cases"]["fukui-eiheiji"]["irrigation"]
        self.assertTrue(e["rateLackDiff"], "理由が違うときは details にまとめる")
        joined = " ".join(e["rateLack"])
        self.assertIn("現在の取得先では提供されていません", joined)
        self.assertIn("欠測です", joined)


def header_text(basis: str, dams) -> str:
    """サイト上部の文言を、Python 側の集計（rate_coverage.py）から組み立てる。"""
    import rate_coverage as rc
    c = rc.summarize(dams, basis)
    name = "有効貯水率" if basis == "effective" else "利水貯水率"
    out = f"{name}を表示：{c['shown']}／{c['total']}基" + (f"（参考値{c['reference']}基を含む）" if c["reference"] else "")
    if c["dash"]:
        short = {"not_provided": "未提供", "missing": "欠測", "closed": "閉局", "no_source": "取得元未設定",
                 "fetch_failed": "取得失敗"}
        parts = [f"{short[k]}{c['reasons'][k]}" for k in short if c["reasons"].get(k)]
        if c["reasons"].get("other"):
            parts.append(f"その他{c['reasons']['other']}")
        out += f"／表示なし {c['dash']}基（{name}の内訳：{'・'.join(parts)}）"
    return out


class TestHeaderCoverage(_Browser):
    """サイト上部の件数が、基準を切り替えても実際のピンの数字・Python 側の集計と一致する。"""

    def check(self, r, dams):
        import rate_coverage as rc
        self.assertEqual(r["errors"], [])
        c = r["cases"]
        self.assertEqual(c["irrigation"], header_text("irrigation", dams))
        self.assertEqual(c["effective"], header_text("effective", dams))
        self.assertEqual(c["back"], c["irrigation"], "基準を戻すと元の件数に戻る")
        self.assertEqual(c["irrigation_pins"], rc.summarize(dams)["shown"], "ピンの数字が出ている数と同じ")
        self.assertEqual(c["irrigation_ref"], rc.summarize(dams)["reference"], "参考値の印の数と同じ")
        self.assertEqual(c["effective_pins"], rc.summarize(dams, "effective")["shown"])
        self.assertEqual(c["back_pins"], c["irrigation_pins"])
        self.assertNotIn("取得済み", c["irrigation"])
        self.assertNotIn("数値なし", c["irrigation"])

    def test_real_pc(self):
        self.check(self.page("hdr_pc", "mode=header", PC), DAMS["dams"])

    def test_real_phone(self):
        self.check(self.page("hdr_phone", "mode=header", PHONE), DAMS["dams"])

    def test_synthetic_counts_follow_data(self):
        r = self.page("hdr_synth", "mode=header", PC, use_synth=True)
        self.check(r, SYNTH["dams"])


class TestStaticMatchesMap(_Browser):
    """刀利など: 個別ページ（生成HTML）の水の出入りが、地図パネルと同じ数値・同じ単位。"""

    def test_same_values(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_site as bs
        r = self.page("real_pc", "mode=panel&ids=" + ",".join(REAL_IDS), PC)
        checked = 0
        for i in REAL_IDS:
            d = BY_REAL[i]
            flow = r["cases"][i]["irrigation"]["flow"]
            if not flow:
                continue
            html = bs.dam_page(d, DAMS, "https://damtabi.com", "v")
            sec = re.search(r'<section class="flow">.*?</section>', html, re.S).group(0)
            static = [re.sub(r"<[^>]+>", "", v) for v in re.findall(r'<div class="v">(.*?)</div>', sec)]
            self.assertEqual(static, [c["v"] for c in flow], i)
            checked += 1
        self.assertGreaterEqual(checked, 5)


class TestWatershedInflow(_Browser):
    Q = "mode=ws&a=toyama-tori&b=toyama-goi&c=toyama-unazuki&x=ishikawa-oya"

    def check(self, r):
        self.assertEqual(r["errors"], [])
        c = r["cases"]
        self.assertIsNone(c["a_before"]["inflow"], "集水域を開く前は出さない")
        a = c["a_open"]
        self.assertTrue(a["wsStop"])
        self.assertIn("観測時点でダムに入ってくる水：毎秒 3.45 m³", a["inflow"])
        self.assertIn("2026年10月21日 01:40 観測", a["inflow"])
        self.assertIn("連動しておらず", a["inflow"])
        self.assertIn("概略", a["wsText"], "既存の注記を残す")
        self.assertTrue(c["a_open_effective"]["wsStop"], "基準を切り替えても集水域は開いたまま")
        self.assertEqual(c["a_open_effective"]["inflow"], a["inflow"])
        self.assertFalse(c["b_switched"]["wsStop"])
        self.assertTrue(c["b_switched"]["wsGo"])
        self.assertIsNone(c["b_switched"]["inflow"], "前のダムの流入量を残さない")
        self.assertTrue(c["b_open"]["wsStop"], "流入量が無くても集水域は使える")
        self.assertIsNone(c["b_open"]["inflow"])
        self.assertTrue(c["b_closed"]["wsGo"])
        self.assertIsNone(c["b_closed"]["inflow"])
        self.assertIn("毎秒 39.51 m³", c["c_open"]["inflow"])
        self.assertTrue(c["c_closed_panel"]["hidden"])
        self.assertFalse(c["x_unsupported"]["wsGo"])
        self.assertIsNone(c["x_unsupported"]["inflow"])
        self.assertTrue(c["a_again"]["wsGo"])
        self.assertIsNone(c["a_again"]["inflow"])

    def test_phone(self):
        self.check(self.page("ws_phone", self.Q, PHONE))

    def test_pc(self):
        self.check(self.page("ws_pc", self.Q, PC))


if __name__ == "__main__":
    unittest.main()
