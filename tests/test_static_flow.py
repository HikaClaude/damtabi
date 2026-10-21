# -*- coding: utf-8 -*-
"""静的なダム個別ページの「水の出入り」と、一覧ページ・更新結果の集計の検証（ブラウザ不要）。

生成側（scripts/build_site.py）を保存済みの dams.json から直接呼び、地図パネル（docs/app.js）と
同じ見せ方・同じ数値になっていることを確かめる。足りない条件だけ合成データで補う。
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_site as bs  # noqa: E402
import rate_coverage as rc  # noqa: E402

DATA = json.loads((ROOT / "docs" / "data" / "dams.json").read_text(encoding="utf-8"))
BY = {d["id"]: d for d in DATA["dams"]}
BASE = "https://damtabi.com"


def page(dam, data=DATA):
    return bs.dam_page(dam, data, BASE, "vtest")


def flow_section(html: str) -> str:
    m = re.search(r'<section class="flow">.*?</section>', html, re.S)
    return m.group(0) if m else ""


def flow_values(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", v) for v in re.findall(r'<div class="v">(.*?)</div>', flow_section(html))]


def facts_labels(html: str) -> list[str]:
    t = re.search(r'<table class="facts">.*?</table>', html, re.S).group(0)
    return re.findall(r"<th>(.*?)</th>", t)


def synth():
    d = copy.deepcopy(DATA)
    by = {x["id"]: x for x in d["dams"]}
    miss = {"value": None, "status": "missing", "reason": "欠測"}
    for k in ("storage_capacity_1000m3", "inflow_m3s", "outflow_m3s"):
        by["gifu-atagi"][k] = dict(miss)                     # 水位のみ
    by["gifu-iwamura"]["storage_capacity_1000m3"] = {"value": 0.5, "status": "ok", "reason": None}
    by["gifu-iwamura"]["inflow_m3s"] = {"value": None, "status": "fetch_failed", "reason": "取得失敗"}
    by["fukui-eiheiji"]["storage_capacity_1000m3"] = {"value": 12345.6, "status": "ok", "reason": None}
    by["fukui-eiheiji"]["rate_effective"] = dict(miss)       # 利水は未提供・有効は欠測
    return d, by


class TestFormats(unittest.TestCase):
    def test_storage(self):
        f = bs.fmt_storage
        self.assertEqual(f(3781), "378.1万 m³")
        self.assertEqual(f(12345.6), "1,234.56万 m³")
        self.assertEqual(f(0.5), "500 m³")
        self.assertEqual(f(0), "0 m³")
        self.assertEqual(f(10), "1万 m³")
        self.assertEqual(f(9), "9,000 m³")
        self.assertEqual(f(27463), "2,746.3万 m³")
        self.assertEqual(f(7), "7,000 m³")

    def test_flow_and_level(self):
        self.assertEqual(bs.fmt_flow(3.45), "毎秒 3.45 m³")
        self.assertEqual(bs.fmt_flow(0), "毎秒 0 m³")
        self.assertEqual(bs.fmt_flow(8.0), "毎秒 8 m³", "JS の String(8) と同じ桁")
        self.assertEqual(bs.fmt_level(329.15), "標高 329.15 m")

    def test_bool_is_not_a_number(self):
        self.assertIsNone(bs.val({"status": "ok", "value": True}))


class TestStaticPages(unittest.TestCase):
    def test_tori_flow_then_cta_then_freshness(self):
        h = page(BY["toyama-tori"])
        self.assertEqual(flow_values(h), ["378.1万 m³", "毎秒 3.45 m³", "毎秒 0 m³"])
        i_flow, i_cta, i_fresh = h.index('class="flow"'), h.index('class="ws-cta"'), h.index('class="freshness"')
        self.assertLess(i_flow, i_cta)
        self.assertLess(i_cta, i_fresh)
        self.assertIn("地図で、このダムに水が集まる範囲を見る", h)
        self.assertIn(f'href="{BASE}/#dam_id=toyama-tori"', h)
        self.assertIn("（出典：川の防災情報）", h)
        self.assertIn("利水貯水率・有効貯水率", h)
        self.assertIn("現在の取得先では提供されていません", h)
        labels = facts_labels(h)
        self.assertIn("水面の標高", labels)
        for dup in ("貯水位", "貯まっている水の量", "ダムに入ってくる水", "ダムから出ていく水"):
            self.assertNotIn(dup, labels, "水の出入りと重複して出さない")
        self.assertIn("329.15 m", h)
        self.assertIn("元データの名前と単位", h)

    def test_unazuki_keeps_rate_layout(self):
        h = page(BY["toyama-unazuki"])
        self.assertNotIn('class="flow"', h)
        self.assertNotIn("ws-cta", h)
        self.assertIn("58.4", h)
        self.assertIn("19.2", h)
        labels = facts_labels(h)
        i = labels.index("水面の高さ（標高）")
        self.assertEqual(labels[i:i + 4], ["水面の高さ（標高）", "貯まっている水の量", "ダムに入ってくる水", "ダムから出ていく水"])
        self.assertIn("300.2万 m³", h)
        self.assertIn("標高 240.61 m", h)

    def test_akase_zero_storage_is_shown(self):
        h = page(BY["ishikawa-akase"])
        self.assertEqual(flow_values(h)[0], "0 m³")
        self.assertNotIn("is-lack", flow_section(h))

    def test_agigawa_keeps_effective_rate_name(self):
        h = page(BY["gifu-agigawa"])
        self.assertIn("有効貯水率", h)
        self.assertIn("62.4", h)
        self.assertEqual(flow_values(h)[0], "2,746.3万 m³")
        lack = re.search(r'<ul class="rate-lack">.*?</ul>', h, re.S).group(0)
        self.assertIn("利水貯水率", lack)
        self.assertNotIn("利水貯水率・有効貯水率", lack, "有効は値があるので、利水だけの理由にする")
        self.assertIn("「閉局」", lack)
        self.assertIn("ダムの運用状況を示すものではありません", lack)

    def test_dainichigawa_keeps_reference_rate(self):
        h = page(BY["ishikawa-dainichigawa"])
        self.assertIn("参考貯水率", h)
        self.assertIn("DAM TABI算出", h)
        self.assertNotIn('class="flow"', h)

    def test_no_values_and_no_source_keep_old_layout(self):
        for i in ("toyama-usunaka", "toyama-otani", "toyama-kurobe", "fukui-takinami"):
            h = page(BY[i])
            self.assertNotIn('class="flow"', h, i)
            self.assertNotIn("ws-cta", h, i)
            self.assertIn('class="rate is-nodata"', h, i)

    def test_unsupported_watershed_has_no_cta(self):
        d = BY["nagano-toyooka"]
        self.assertNotIn(d["id"], bs.WS_IDS)
        h = page(d)
        self.assertIn('class="flow"', h)
        self.assertNotIn("ws-cta", h)
        self.assertNotIn("水が集まる範囲", h)

    def test_cta_only_for_supported_flow_pages(self):
        for d in DATA["dams"]:
            has_cta = "ws-cta" in page(d)
            want = (not bs.rate_shown(d)) and bs.has_flow(d) and d["id"] in bs.WS_IDS
            self.assertEqual(has_cta, want, d["id"])

    def test_synthetic_cases(self):
        sd, sby = synth()
        h = page(sby["gifu-atagi"], sd)                       # 水位のみ
        self.assertNotIn('class="flow"', h)
        self.assertIn("標高 571.48 m", h)
        h = page(sby["gifu-iwamura"], sd)                     # 小さい値を 0 にしない・取得失敗を 0 にしない
        self.assertEqual(flow_values(h)[:2], ["500 m³", "取得失敗"])
        h = page(sby["fukui-eiheiji"], sd)
        self.assertEqual(flow_values(h)[0], "1,234.56万 m³")
        self.assertIn("rate-lack--diff", h)                   # 理由が違うので詳細で区別
        self.assertIn("欠測です", h)
        self.assertIn("現在の取得先では提供されていません", h)

    def test_meta_and_structured_data_unchanged(self):
        """canonical・OGP・description・構造化データは、変更前の生成物（HEAD）と同じ。"""
        for rel, i in (("toyama/tori", "toyama-tori"), ("toyama/unazuki", "toyama-unazuki"),
                       ("ishikawa/akase", "ishikawa-akase")):
            old = subprocess.run(["git", "show", f"HEAD:docs/dam/{rel}/index.html"], cwd=ROOT,
                                 capture_output=True, encoding="utf-8").stdout
            new = page(BY[i])
            pick = lambda t: sorted(re.findall(r'<link rel="canonical"[^>]*>|<meta property="og:[^>]*>|'
                                               r'<meta name="description"[^>]*>|<title>.*?</title>', t))
            self.assertEqual(pick(old), pick(new), i)
            ld = lambda t: [json.loads(x) for x in re.findall(r'application/ld\+json">(.*?)</script>', t, re.S)]
            self.assertEqual(ld(old), ld(new), i)

    def test_generated_files_match_generator(self):
        """docs/dam/ は手編集ではなく、生成側の再生成そのものであること。"""
        base = bs.SITE.get("base_url") or bs.DEFAULT_BASE_URL
        for d in DATA["dams"]:
            p = ROOT / "docs" / "dam" / d["pref"] / d["slug"] / "index.html"
            text = p.read_text(encoding="utf-8")
            ver = re.search(r"page\.css\?v=([0-9a-f]+)", text).group(1)
            self.assertEqual(text.replace("\r\n", "\n"), bs.dam_page(d, DATA, base, ver).replace("\r\n", "\n"), d["id"])


class TestCoverageMatchesSite(unittest.TestCase):
    """サイト上部（app.js）と更新結果（rate_coverage.py）は同じ規則。基準ごとの件数はここで確認する。"""

    def test_python_basis_effective(self):
        s = rc.summarize(DATA["dams"], "effective")
        self.assertEqual(s["reference"], 0, "有効基準では参考値を使わない")
        self.assertEqual(s["shown"], sum(1 for d in DATA["dams"] if rc.val(d.get("rate_effective")) is not None))
        self.assertEqual(s["shown"] + s["dash"], s["total"])

    def test_default_is_irrigation(self):
        self.assertEqual(rc.summarize(DATA["dams"]), rc.summarize(DATA["dams"], "irrigation"))

    def test_index_page_text(self):
        h = bs.index_page(DATA, BASE, "vtest")
        s = rc.summarize(DATA["dams"])
        self.assertIn(f"標準の利水貯水率を表示できるのは{s['shown']}基です", h)
        self.assertNotIn("数値を取得できています", h)


if __name__ == "__main__":
    unittest.main()
