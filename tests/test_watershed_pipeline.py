#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域パイプラインの単体試験。ネットワーク・実DEM・標準外ライブラリ（numpy 以外）は使わない。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_basins as bb  # noqa: E402
import dem_tiles  # noqa: E402
import watershed_qa as wq  # noqa: E402
import ws_common as wc  # noqa: E402
import ws_pipeline as wp  # noqa: E402


# ------------------------------------------------------------------ 補助

def tile_text(base=100.0):
    return "\n".join(",".join(f"{base + (i + j) * 0.1:.1f}" for j in range(256)) for i in range(256)) + "\n"


class FakeNet:
    """地理院のふり。scripts に応じて 200 / 404 / 5xx / タイムアウト等を返す。"""

    def __init__(self, script):
        self.script = list(script)       # ("ok"|"404"|"500"|"429"|"timeout"|"reset"|"empty200"|"short"|"trunc")
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        act = self.script.pop(0) if self.script else "ok"
        if act == "ok":
            return 200, tile_text().encode()
        if act == "404":
            raise dem_tiles._Missing()
        if act in ("500", "429", "503"):
            raise urllib.error.HTTPError(url, int(act), "err", {}, None)
        if act == "timeout":
            raise TimeoutError("timed out")
        if act == "reset":
            raise ConnectionResetError("reset")
        if act == "empty200":
            return 200, b""
        if act == "short":
            return 200, ("1,2,3\n" * 10).encode()
        raise AssertionError(act)


def make_src(tmp, script=(), **kw):
    net = FakeNet(script)
    src = dem_tiles.TileSource(Path(tmp) / "dem", opener=net, sleep=lambda s: None, interval=0, **kw)
    return src, net


def files(d):
    d = Path(d)
    return sorted(p.name for p in d.glob("*")) if d.exists() else []


# ------------------------------------------------------------------ 詰め込み・座標・版

class TestCodec(unittest.TestCase):
    def test_pack_unpack_roundtrip_odd_and_even(self):
        rng = np.random.default_rng(1)
        for h, w in ((7, 9), (10, 10), (1, 1), (33, 17)):
            m = rng.random((h, w)) > 0.5
            d = rng.integers(0, 16, (h, w)).astype(np.uint8)
            np.testing.assert_array_equal(wc.unpack1(wc.pack1(m), h, w), m)
            np.testing.assert_array_equal(wc.unpack4(wc.pack4(d), h, w), d)

    def test_cell_lonlat_matches_tile_xy(self):
        z, x0, y0 = 14, 14000, 6400
        rec = {"zoom": z, "x0": x0, "y0": y0, "coarse": 2}
        lon, lat = wc.cell_lonlat(rec, np.array([10.0]), np.array([5.0]))
        # 逆変換: 経緯度 → 世界ピクセル → 粗セル
        world = 256.0 * 2 ** z
        px = (lon[0] + 180) / 360 * world
        r = math.radians(lat[0])
        py = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * world
        self.assertAlmostEqual((px - x0 * 256) / 2 - 0.5, 5.0, places=6)
        self.assertAlmostEqual((py - y0 * 256) / 2 - 0.5, 10.0, places=6)


class TestContentVersion(unittest.TestCase):
    base = {"id": "a", "x0": 1, "y0": 2, "zoom": 14, "coarse": 2, "outlets": [[1, 2]],
            "d8": "AAAA", "mask": "BBBB", "area_km2": 1.5, "version": "old"}

    def test_stable_and_key_order_independent(self):
        a = wc.content_version(self.base)
        b = wc.content_version(dict(reversed(list(self.base.items()))))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_version_field_itself_is_excluded(self):
        self.assertEqual(wc.content_version(self.base), wc.content_version(dict(self.base, version="new")))

    def test_every_content_field_changes_the_version(self):
        # 旧実装は d8・mask・面積だけをハッシュしていた。位置決め・出口・ポリゴンの変更も検出する
        v0 = wc.content_version(self.base)
        for k, new in (("x0", 2), ("y0", 3), ("zoom", 13), ("coarse", 3), ("outlets", [[1, 3]]),
                       ("d8", "AAAB"), ("mask", "BBBC"), ("area_km2", 1.51), ("id", "b")):
            self.assertNotEqual(v0, wc.content_version(dict(self.base, **{k: new})), k)

    def test_polygon_change_changes_basins_version(self):
        f1 = [{"type": "Feature", "properties": {"id": "a"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]
        f2 = json.loads(json.dumps(f1))
        f2[0]["geometry"]["coordinates"][0][1] = [1.00001, 0]
        self.assertNotEqual(wc.content_version({"features": f1}, exclude=()),
                            wc.content_version({"features": f2}, exclude=()))


class TestGeometry(unittest.TestCase):
    def test_self_intersections(self):
        square = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        bow = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
        self.assertEqual(wc.self_intersections(square), 0)
        self.assertEqual(wc.self_intersections(bow), 1)

    def test_points_in_ring_matches_scalar(self):
        ring = [[0, 0], [4, 0], [4, 3], [2, 5], [0, 3], [0, 0]]
        rng = np.random.default_rng(3)
        xs, ys = rng.random(200) * 5 - 0.5, rng.random(200) * 6 - 0.5
        vec = wc.points_in_ring(xs, ys, ring)
        for x, y, v in zip(xs, ys, vec):
            self.assertEqual(wc.point_in_poly(x, y, ring), bool(v))

    def test_smooth_ring_closed_and_drops_spike(self):
        n = 24
        circ = [[math.cos(2 * math.pi * k / n), math.sin(2 * math.pi * k / n)] for k in range(n)]
        spiked = circ[:6] + [[circ[6][0] * 3, circ[6][1] * 3]] + circ[7:]   # 1点だけ外へ突き出す
        spiked.append(spiked[0])
        out = wc.smooth_ring(spiked, 2)
        self.assertEqual(out[0], out[-1])
        # 鋭い突起（約30度より鋭い頂点）は落とされ、輪の最大半径は突起の3に達しない
        self.assertLess(max(math.hypot(x, y) for x, y in out), 2.0)

    def test_dilate(self):
        m = np.zeros((7, 7), bool)
        m[3, 3] = True
        self.assertEqual(int(wc.dilate(m, 1).sum()), 9)
        self.assertEqual(int(wc.dilate(m, 2).sum()), 25)


class TestOutlineExact(unittest.TestCase):
    @staticmethod
    def area(p):
        a = np.asarray(p, float)
        return 0.5 * abs(np.dot(a[:, 0], np.roll(a[:, 1], -1)) - np.dot(a[:, 1], np.roll(a[:, 0], -1)))

    def shapes(self):
        rect = np.zeros((10, 10), bool)
        rect[2:5, 3:8] = True
        L = np.zeros((10, 10), bool)
        L[1:8, 1:3] = True
        L[6:8, 1:8] = True
        U = np.zeros((10, 10), bool)
        U[1:9, 1:3] = True
        U[1:9, 6:8] = True
        U[7:9, 1:8] = True
        return {"rect": rect, "L": L, "U": U}

    def test_polygon_area_equals_cell_count_for_concave_shapes(self):
        for name, m in self.shapes().items():
            self.assertEqual(self.area(wc.outline_exact(m)), float(m.sum()), name)

    def test_legacy_angle_order_outline_misses_concavity(self):
        # 現行 outline()（角度順）は U 字の凹みを取りこぼす。exact との差が、空間QAで見つかったずれの原因
        U = self.shapes()["U"]
        self.assertNotEqual(self.area(bb.outline(U)), float(U.sum()))

    def test_hole_is_ignored_and_diagonal_touch_stays_one_loop(self):
        H = np.zeros((9, 9), bool)
        H[1:8, 1:8] = True
        H[3:6, 3:6] = False
        self.assertEqual(self.area(wc.outline_exact(H)), 49.0)
        D = np.zeros((6, 6), bool)
        D[1, 1] = D[2, 2] = True
        self.assertEqual(self.area(wc.outline_exact(D)), 2.0)


# ------------------------------------------------------------------ DEM: 通信障害と本当の欠損の分離

class TestTileSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_ok_is_cached_atomically_and_reused(self):
        src, net = make_src(self.tmp, ["ok"])
        a, k = src.get(14, 1, 2)
        self.assertEqual(k, "ok")
        self.assertEqual(a.shape, (256, 256))
        self.assertEqual(files(Path(self.tmp) / "dem"), ["dem_14_1_2.txt"])      # 一時ファイルが残らない
        _, k2 = src.get(14, 1, 2)
        self.assertEqual((k2, net.calls), ("ok", 1))

    def test_404_is_recorded_as_confirmed_missing_and_not_refetched(self):
        src, net = make_src(self.tmp, ["404"])
        a, k = src.get(14, 5, 5)
        self.assertEqual(k, "missing")
        self.assertTrue(np.isnan(a).all())
        self.assertEqual(files(Path(self.tmp) / "dem"), ["dem_14_5_5.missing"])
        self.assertEqual(src.get(14, 5, 5)[1], "missing")
        self.assertEqual(net.calls, 1)

    def test_transient_failures_are_not_cached_as_empty(self):
        # 旧実装: どんな失敗でも空ファイルを書き、次回から取りに行かなかった
        for act in ("500", "429", "503", "timeout", "reset", "empty200", "short"):
            with self.subTest(act=act):
                tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                src, net = make_src(tmp, [act] * 3)
                with self.assertRaises(dem_tiles.DemFetchError):
                    src.get(14, 7, 7)
                self.assertEqual(net.calls, 3)                                 # 再試行した
                self.assertEqual(files(Path(tmp) / "dem"), [])                 # 何も保存していない
                # 通信が回復したら、次回はちゃんと取れる（穴が固まっていない）
                src2 = dem_tiles.TileSource(Path(tmp) / "dem", opener=FakeNet(["ok"]), sleep=lambda s: None, interval=0)
                self.assertEqual(src2.get(14, 7, 7)[1], "ok")

    def test_network_request_cap_stops_runaway_fetching(self):
        # 想定外の大量通信を止める: 上限に達したら DemFetchError にして、それ以上は通信しない
        src, net = make_src(self.tmp, ["ok"] * 10, max_network_requests=2)
        src.get(14, 1, 1)
        src.get(14, 1, 2)
        with self.assertRaises(dem_tiles.DemFetchError):
            src.get(14, 1, 3)
        self.assertEqual(net.calls, 2)
        self.assertEqual(files(Path(self.tmp) / "dem"), ["dem_14_1_1.txt", "dem_14_1_2.txt"])

    def test_retry_then_success(self):
        src, net = make_src(self.tmp, ["500", "timeout", "ok"])
        self.assertEqual(src.get(14, 3, 3)[1], "ok")
        self.assertEqual(net.calls, 3)

    def test_offline_never_touches_network_or_disk(self):
        src, net = make_src(self.tmp, [], offline=True)
        with self.assertRaises(dem_tiles.DemUnavailable):
            src.get(14, 9, 9)
        self.assertEqual(net.calls, 0)
        self.assertEqual(files(Path(self.tmp) / "dem"), [])

    def test_missing_tile_promotes_grid_to_float64_like_legacy(self):
        # 旧実装との出力同一性のための挙動（dem_tiles._nan_tile の docstring）
        src, _ = make_src(self.tmp, ["404"])
        a, _k = src.get(14, 6, 6)
        self.assertEqual(a.dtype, np.float64)
        src2, _ = make_src(self.tmp, ["ok"])
        self.assertEqual(src2.get(14, 6, 7)[0].dtype, np.float32)

    def test_legacy_empty_is_flagged_not_silently_sea(self):
        d = Path(self.tmp) / "dem"
        d.mkdir(parents=True)
        (d / "dem_14_4_4.txt").write_text("")
        src, net = make_src(self.tmp, [])
        a, k = src.get(14, 4, 4)
        self.assertEqual(k, "legacy_empty")
        self.assertEqual(net.calls, 0)

    def test_revalidate_empty_404_becomes_missing_and_200_becomes_ok(self):
        d = Path(self.tmp) / "dem"
        d.mkdir(parents=True)
        (d / "dem_14_4_4.txt").write_text("")
        (d / "dem_14_4_5.txt").write_text("")
        src, net = make_src(self.tmp, ["404", "ok"], revalidate_empty=True)
        self.assertEqual(src.get(14, 4, 4)[1], "missing")
        self.assertEqual(src.get(14, 4, 5)[1], "ok")
        self.assertEqual(files(d), ["dem_14_4_4.missing", "dem_14_4_5.txt"])
        self.assertGreater((d / "dem_14_4_5.txt").stat().st_size, 0)

    def test_fallback_dir_is_read_only(self):
        fb = Path(self.tmp) / "fb"
        fb.mkdir()
        (fb / "dem_14_1_1.txt").write_text(tile_text())
        (fb / "dem_14_1_2.txt").write_text("")
        (fb / "dem_14_1_3.missing").write_text("404\n")
        before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in fb.iterdir()}
        src = dem_tiles.TileSource(Path(self.tmp) / "w", fallback_dirs=[fb], offline=True,
                                   sleep=lambda s: None, interval=0)
        self.assertEqual(src.get(14, 1, 1)[1], "ok")
        self.assertEqual(src.get(14, 1, 2)[1], "legacy_empty")
        self.assertEqual(src.get(14, 1, 3)[1], "missing")
        after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in fb.iterdir()}
        self.assertEqual(before, after)                           # 読み取り専用の置き場は変わらない
        self.assertEqual(files(Path(self.tmp) / "w"), [])

    def test_fallback_revalidation_writes_only_to_primary(self):
        fb = Path(self.tmp) / "fb"
        fb.mkdir()
        (fb / "dem_14_1_2.txt").write_text("")
        src = dem_tiles.TileSource(Path(self.tmp) / "w", fallback_dirs=[fb], opener=FakeNet(["404"]),
                                   revalidate_empty=True, sleep=lambda s: None, interval=0)
        self.assertEqual(src.get(14, 1, 2)[1], "missing")
        self.assertEqual(files(fb), ["dem_14_1_2.txt"])                       # フォールバックは無変更
        self.assertEqual(files(Path(self.tmp) / "w"), ["dem_14_1_2.missing"])
        # 以後は .missing が優先され、空 .txt があっても legacy_empty には戻らない
        src2 = dem_tiles.TileSource(Path(self.tmp) / "w", fallback_dirs=[fb], offline=True)
        self.assertEqual(src2.get(14, 1, 2)[1], "missing")

    def test_corrupt_cache_offline_is_an_error_not_a_hole(self):
        d = Path(self.tmp) / "dem"
        d.mkdir(parents=True)
        (d / "dem_14_2_2.txt").write_text("1,2,3\n")
        src, _ = make_src(self.tmp, [], offline=True)
        with self.assertRaises(dem_tiles.DemUnavailable):
            src.get(14, 2, 2)

    def test_build_grid_reports_tile_kinds_and_touching(self):
        src, _ = make_src(self.tmp, ["ok", "404", "ok", "ok"])
        rep = {}
        # 中心 (10.5, 20.5) → tx=10, ty=20。pad=0 の1枚で種別の記録を確認する
        dem, x0, y0 = src.build_grid(lambda la, lo, z: (10.5, 20.5), 0, 0, 14, 0, rep)
        self.assertEqual((x0, y0), (10, 20))
        self.assertEqual(rep["counts"], {"ok": 1, "missing": 0, "legacy_empty": 0})
        mask = np.zeros((256, 256), bool)
        mask[100, 100] = True
        tiles = [{"key": "14/1/1", "kind": "missing", "i0": 0, "j0": 0},
                 {"key": "14/1/2", "kind": "ok", "i0": 0, "j0": 0}]
        self.assertEqual(dem_tiles.tiles_touching(mask, tiles, dem), [{"key": "14/1/1", "kind": "missing"}])


# ------------------------------------------------------------------ 空間QA（描画される輪 × 雨マスク）

def synth_rec(n=48, cx=24, cy=24, r=14, coarse=1):
    """円形の集水域・中心が出口・全セルが中心へ流れる合成データ。"""
    rec = {"zoom": 14, "x0": 14000, "y0": 6400, "coarse": coarse, "w": n, "h": n, "cell_m": 9.5 * coarse}
    ii, jj = np.mgrid[0:n, 0:n]
    mask = (ii - cy) ** 2 + (jj - cx) ** 2 <= r * r
    codes = np.full((n, n), 15, np.uint8)
    for k, (di, dj) in enumerate(wc.D8):
        pass
    di = np.sign(cy - ii)
    dj = np.sign(cx - jj)
    for k, (a, b) in enumerate(wc.D8):
        codes[(di == a) & (dj == b)] = k
    codes[cy, cx] = 15
    rec["d8"] = wc.pack4(codes)
    rec["mask"] = wc.pack1(mask)
    rec["outlets"] = [[cy, cx]]
    rec["outlet"] = [cy, cx]
    return rec, mask


def ring_of(rec, cx=24, cy=24, r=14.5, m=90, dx=0.0, dy=0.0):
    pts = []
    for k in range(m):
        t = 2 * math.pi * k / m
        lon, lat = wc.cell_lonlat(rec, np.array([cy + dy + r * math.sin(t)]), np.array([cx + dx + r * math.cos(t)]))
        pts.append([float(lon[0]), float(lat[0])])
    pts.append(pts[0])
    return pts


class TestSpatialQA(unittest.TestCase):
    def test_matching_ring_and_mask_pass(self):
        rec, _ = synth_rec()
        m = wq.spatial_metrics(rec, ring_of(rec))
        self.assertGreater(m["iou"], 0.90)
        self.assertLess(m["mask_far_outside_pct"], 0.5)
        self.assertEqual(m["unreached_pct"], 0.0)
        self.assertEqual(m["path_outside_pct"], 0.0)
        res = wq.evaluate(m, {"official_area_km2": 1.0})
        self.assertEqual(res["status"], "pass", res)

    def test_shifted_ring_is_held(self):
        # 面積は同じでも位置がずれている輪。面積比較（旧 grid_drift）では見つからない
        rec, _ = synth_rec()
        base = wq.spatial_metrics(rec, ring_of(rec))
        m = wq.spatial_metrics(rec, ring_of(rec, dx=8))
        # 位置だけずらした輪は、ずらさない輪と面積がほとんど変わらない
        self.assertLess(abs(m["ring_vs_mask_area_pct"] - base["ring_vs_mask_area_pct"]), 3)
        res = wq.evaluate(m, {"grid_drift_pct": 0.0, "area_error_pct": 0.0, "official_area_km2": 1.0})
        self.assertEqual(res["status"], "hold")
        self.assertIn("rain_outside_ring", res["block_codes"])
        self.assertIn("ring_without_rain", res["block_codes"])

    def test_oversized_ring_is_held(self):
        rec, _ = synth_rec()
        m = wq.spatial_metrics(rec, ring_of(rec, r=21))
        res = wq.evaluate(m, {"official_area_km2": 1.0})
        self.assertIn("ring_without_rain", res["block_codes"])
        self.assertNotIn("rain_outside_ring", res["block_codes"])

    def test_undersized_ring_is_held(self):
        rec, _ = synth_rec()
        m = wq.spatial_metrics(rec, ring_of(rec, r=8))
        res = wq.evaluate(m, {"official_area_km2": 1.0})
        self.assertIn("rain_outside_ring", res["block_codes"])

    def test_rain_paths_that_leave_the_ring_are_detected(self):
        # 雨は輪の内側に降るが、流向が輪の外へ向かっている
        rec, mask = synth_rec()
        n = rec["h"]
        codes = np.full((n, n), 15, np.uint8)
        codes[:, :] = 2                                    # 全セル東向き（(0,1)）
        codes[24, 40] = 15
        rec["d8"] = wc.pack4(codes)
        rec["outlets"] = [[24, 40]]                        # 輪の外に出口
        m = wq.spatial_metrics(rec, ring_of(rec))
        self.assertGreater(m["path_outside_pct"] + m["unreached_pct"], 20)
        res = wq.evaluate(m, {"official_area_km2": 1.0})
        self.assertEqual(res["status"], "hold")

    def test_unreached_is_held(self):
        rec, _ = synth_rec()
        n = rec["h"]
        rec["d8"] = wc.pack4(np.full((n, n), 15, np.uint8))            # 誰も流れない
        m = wq.spatial_metrics(rec, ring_of(rec))
        self.assertGreater(m["unreached_pct"], 90)
        self.assertIn("unreached", wq.evaluate(m, {"official_area_km2": 1.0})["block_codes"])

    def test_pack_mismatch_is_detected_because_delivered_strings_are_read(self):
        # 詰め込みの取り違え（配信文字列を読み戻すから検出できる）
        rec, mask = synth_rec()
        rec["mask"] = wc.pack1(np.flipud(mask & (np.arange(mask.shape[0])[:, None] < 24)))
        m = wq.spatial_metrics(rec, ring_of(rec))
        self.assertEqual(wq.evaluate(m, {"official_area_km2": 1.0})["status"], "hold")

    def test_self_intersecting_ring_is_held(self):
        rec, _ = synth_rec()
        ring = ring_of(rec)
        ring[10], ring[40] = ring[40], ring[10]                        # 頂点を入れ替えて交差させる
        m = wq.spatial_metrics(rec, ring)
        self.assertGreater(m["ring_self_intersections_raw"], 0)

    def test_fragmented_mask_is_held(self):
        rec, mask = synth_rec()
        mask2 = mask.copy()
        mask2[:, 22:26] = False                                         # 中央を帯状に欠かして分断
        rec["mask"] = wc.pack1(mask2)
        m = wq.spatial_metrics(rec, ring_of(rec))
        self.assertGreater(m["mask_components"], 1)
        self.assertLess(m["mask_largest_pct"], 95)
        self.assertIn("mask_fragmented", wq.evaluate(m, {"official_area_km2": 1.0})["block_codes"])

    def test_gate_reasons(self):
        rec, _ = synth_rec()
        m = wq.spatial_metrics(rec, ring_of(rec))
        ok = {"official_area_km2": 1.0}
        self.assertEqual(wq.evaluate(m, ok)["status"], "pass")
        self.assertIn("area_error", wq.evaluate(m, dict(ok, area_error_pct=16.5))["block_codes"])
        self.assertIn("grid_drift", wq.evaluate(m, dict(ok, grid_drift_pct=-15.1))["block_codes"])
        self.assertIn("manual_hold", wq.evaluate(m, ok, manual_hold="原因不明")["block_codes"])
        # 警告は配信を止めない
        w = wq.evaluate(m, {"official_area_km2": None, "diversion_status": "unknown", "outlet_method": "snap"})
        self.assertEqual(w["status"], "pass")
        self.assertEqual({r["code"] for r in w["reasons"]},
                         {"no_official_direct_area", "diversion_unknown", "no_reservoir_surface"})

    def test_dem_facts_block(self):
        rec, _ = synth_rec()
        m = wq.spatial_metrics(rec, ring_of(rec))
        base = {"official_area_km2": 1.0}
        self.assertIn("dem_edge", wq.evaluate(m, dict(base, dem={"touches_edge": True}))["block_codes"])
        self.assertIn("dem_nodata_adjacent", wq.evaluate(m, dict(base, dem={"nodata_adjacent_cells": 3}))["block_codes"])
        r1 = wq.evaluate(m, dict(base, dem={"tiles_touching_catchment": [{"key": "14/1/1", "kind": "legacy_empty"}]}))
        self.assertIn("dem_unverified_empty_tile", r1["block_codes"])
        r2 = wq.evaluate(m, dict(base, dem={"tiles_touching_catchment": [{"key": "14/1/1", "kind": "missing"}]}))
        self.assertIn("dem_missing_tile", r2["block_codes"])
        # 集水域に接していない空タイルは止めない（tiles_touching に入らない）
        self.assertEqual(wq.evaluate(m, dict(base, dem={"tiles_touching_catchment": [], "tile_counts": {"legacy_empty": 59}}))["status"], "pass")


# ------------------------------------------------------------------ 1基単位の差分・配信ゲート・失敗時処理

def fake_dam(i, lat=36.0, lon=137.0):
    return {"id": i, "name": i.upper(), "lat": lat + int(i[-1]) * 0.1, "lon": lon}


def fake_gen(dam, seed=0, area=10.0):
    rec, mask = synth_rec()
    rec = dict(rec, id=dam["id"], name=dam["name"], area_km2=area + seed, fine_area_km2=area, seed=seed)
    idx = {"id": dam["id"], "name": dam["name"], "lat": dam["lat"], "lon": dam["lon"],
           "area_km2": area, "fine_area_km2": area, "official_area_km2": area}
    return {"rec": rec, "index": idx, "facts": {"official_area_km2": area, "dem": {}}}


class TestStoreAndAssemble(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = wp.Store(self.tmp)
        self.dams = [fake_dam("t-a1"), fake_dam("t-b2"), fake_dam("t-c3")]
        self.props = {}

    def publish(self, dam, seed=0, ring_shift=0.0):
        gen = fake_gen(dam, seed)
        ring = ring_of(gen["rec"], dx=ring_shift)
        judged = wp.judge(gen, ring, dam["id"])
        self.assertEqual(judged["gate"]["status"], "pass", judged["gate"]["reasons"])
        wp.apply_result(self.store, dam, judged, gen, ring, {"id": dam["id"], "name": dam["name"]})
        return judged

    def snapshot(self):
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file():
                out[p.relative_to(self.tmp).as_posix()] = p.read_bytes()
        return out

    def test_single_dam_update_touches_only_that_dam(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        before = self.snapshot()
        idx0 = json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))
        self.assertEqual([x["id"] for x in idx0["dams"]], ["t-a1", "t-b2", "t-c3"])

        self.publish(self.dams[1], seed=1)                       # b2 だけ作り直す
        plan = wp.assemble(self.store, self.dams)
        after = self.snapshot()
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k) and not k.endswith("index.local.json"))   # local は配信しない
        self.assertEqual(changed, ["data/watershed/dams/t-b2.json", "docs/watershed/flow/t-b2.json",
                                   "docs/watershed/index.json"])
        idx1 = json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))
        self.assertEqual([x["id"] for x in idx1["dams"]], ["t-a1", "t-b2", "t-c3"])      # 他のダムが消えない
        v0 = {x["id"]: x["v"] for x in idx0["dams"]}
        v1 = {x["id"]: x["v"] for x in idx1["dams"]}
        self.assertEqual(v0["t-a1"], v1["t-a1"])
        self.assertEqual(v0["t-c3"], v1["t-c3"])
        self.assertNotEqual(v0["t-b2"], v1["t-b2"])
        self.assertNotEqual(idx0["version"], idx1["version"])                  # 索引の版は内容が変われば変わる
        self.assertEqual(idx0["basins_version"], idx1["basins_version"])       # ポリゴンは変えていない
        self.assertNotIn("docs/watershed/basins.geojson", plan["written"])

    def test_no_change_means_no_write_and_same_version(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        idx_bytes = (self.tmp / "docs/watershed/index.json").read_bytes()
        bas_bytes = (self.tmp / "docs/watershed/basins.geojson").read_bytes()
        plan = wp.assemble(self.store, self.dams)
        self.assertEqual(plan["written"], [])
        self.assertEqual(idx_bytes, (self.tmp / "docs/watershed/index.json").read_bytes())
        self.assertEqual(bas_bytes, (self.tmp / "docs/watershed/basins.geojson").read_bytes())

    def test_polygon_only_change_changes_basins_and_index_version(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        i0 = json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))
        self.publish(self.dams[0], ring_shift=0.3)               # 格子は同じ内容、輪だけ少し違う
        wp.assemble(self.store, self.dams)
        i1 = json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))
        self.assertNotEqual(i0["basins_version"], i1["basins_version"])
        self.assertNotEqual(i0["version"], i1["version"])
        self.assertEqual({x["id"]: x["v"] for x in i0["dams"]}, {x["id"]: x["v"] for x in i1["dams"]})

    def test_hold_is_not_published_and_previous_version_is_kept(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        flow_before = (self.tmp / "docs/watershed/flow/t-a1.json").read_bytes()
        idx_before = (self.tmp / "docs/watershed/index.json").read_bytes()
        # a1 を、QA に落ちる結果（輪が大きくずれる）で作り直す
        gen = fake_gen(self.dams[0], seed=9)
        ring = ring_of(gen["rec"], dx=9)
        judged = wp.judge(gen, ring, "t-a1")
        self.assertEqual(judged["gate"]["status"], "hold")
        out = wp.apply_result(self.store, self.dams[0], judged, gen, ring, {"id": "t-a1"})
        self.assertIn("held", out["outcome"])
        wp.assemble(self.store, self.dams)
        self.assertEqual(flow_before, (self.tmp / "docs/watershed/flow/t-a1.json").read_bytes())   # 配信物は不変
        self.assertEqual(idx_before, (self.tmp / "docs/watershed/index.json").read_bytes())
        self.assertTrue((self.tmp / "docs/watershed/flow/_local/t-a1.json").exists())              # 退避先に残す
        st = self.store.load_state("t-a1")
        self.assertEqual(st["latest"]["status"], "hold")
        self.assertIsNotNone(st["published"])

    def test_new_dam_that_fails_qa_is_never_published(self):
        d = self.dams[0]
        gen = fake_gen(d)
        ring = ring_of(gen["rec"], dx=9)
        judged = wp.judge(gen, ring, d["id"])
        wp.apply_result(self.store, d, judged, gen, ring, {"id": d["id"]})
        plan = wp.assemble(self.store, self.dams)
        self.assertEqual(plan["published_ids"], [])
        self.assertFalse((self.tmp / "docs/watershed/flow" / f"{d['id']}.json").exists())

    def test_manual_hold_by_id(self):
        wp.EXCLUDED["t-a1"] = "原因不明のため保留（試験）"
        self.addCleanup(wp.EXCLUDED.pop, "t-a1", None)
        gen = fake_gen(self.dams[0])
        j = wp.judge(gen, ring_of(gen["rec"]), "t-a1")
        self.assertEqual(j["gate"]["status"], "hold")
        self.assertIn("manual_hold", j["gate"]["block_codes"])
        # 同名でも id が違えば影響しない
        j2 = wp.judge(fake_gen(self.dams[1]), ring_of(gen["rec"]), "t-b2")
        self.assertEqual(j2["gate"]["status"], "pass")

    def test_tampered_flow_file_aborts_without_writing(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        p = self.tmp / "docs/watershed/flow/t-b2.json"
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["area_km2"] = 999.0                                   # 版を更新せず中身だけ書き換え
        p.write_text(json.dumps(rec), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(wp.PipelineError):
            wp.assemble(self.store, self.dams)
        self.assertEqual(before, self.snapshot())

    def test_orphans_abort_unless_pruned(self):
        for d in self.dams:
            self.publish(d)
        wp.assemble(self.store, self.dams)
        (self.tmp / "docs/watershed/flow/legacy.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(wp.PipelineError):
            wp.assemble(self.store, self.dams)
        self.assertTrue((self.tmp / "docs/watershed/flow/legacy.json").exists())      # 勝手に消さない
        wp.assemble(self.store, self.dams, prune_orphans=True)
        self.assertFalse((self.tmp / "docs/watershed/flow/legacy.json").exists())

    def test_state_for_unknown_dam_id_aborts(self):
        self.publish(self.dams[0])
        with self.assertRaises(wp.PipelineError):
            wp.assemble(self.store, self.dams[1:])

    def test_downstream_is_verified_by_routing_not_only_containment(self):
        # a1 の輪の中に b2 のダムがあるが、b2 の格子をたどっても a1 の出口へは届かない → unverified
        a, b = self.dams[0], self.dams[1]
        rec, _ = synth_rec()
        lon, lat = wc.cell_lonlat(rec, np.array([24.0]), np.array([24.0]))
        b["lon"], b["lat"] = float(lon[0]) + 0.0005, float(lat[0])       # 出口の少し東
        for d in (a, b):
            self.publish(d)
        # b の位置は a の輪の中。a の D8 は全て中心へ向かうので、たどれば届く → downstream_reaches に入る
        wp.assemble(self.store, self.dams)
        idx = {x["id"]: x for x in json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))["dams"]}
        self.assertIn("t-a1", idx["t-b2"]["downstream_candidates"])
        self.assertEqual(idx["t-b2"]["downstream_reaches"] + idx["t-b2"]["downstream_unverified"],
                         idx["t-b2"]["downstream_candidates"])


# ------------------------------------------------------------------ ID 基準の処理・失敗時に既存を壊さない

class Args:
    def __init__(self, **kw):
        self.all = kw.get("all", False)
        self.pref = kw.get("pref")
        self.id = kw.get("id")
        self.only = kw.get("only")


class TestIdBasedProcessing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dams = [
            {"id": "toyama-otani", "name": "大谷ダム", "lat": 36.5, "lon": 137.0},
            {"id": "ishikawa-otani", "name": "大谷ダム", "lat": 36.6, "lon": 136.5},   # 同名・別のダム
            {"id": "toyama-x", "name": "X ダム", "lat": 36.7, "lon": 137.1},
        ]

    def test_select_targets_by_id_is_exact_and_unknown_id_is_an_error(self):
        got = bb.select_targets(self.dams, Args(id="ishikawa-otani"))
        self.assertEqual([d["id"] for d in got], ["ishikawa-otani"])
        with self.assertRaises(bb.UnknownTarget):
            bb.select_targets(self.dams, Args(id="toyama-otani,toyama-typo"))
        # 名前指定は同名を両方拾う（だから id が要る）
        self.assertEqual(len(bb.select_targets(self.dams, Args(only="大谷"))), 2)

    def write_spec(self, rows):
        p = self.tmp / "spec.csv"
        head = "dam_id,dam_name,binran_no,binran_name,basin_raw,total_km2,direct_km2,indirect_km2,all_direct,note\n"
        p.write_text(head + "".join(rows), encoding="utf-8")
        return p

    def test_spec_is_keyed_by_dam_id_and_same_name_does_not_collide(self):
        p = self.write_spec(["toyama-otani,大谷ダム,1,大谷,,10,10,0,1,\n", "ishikawa-otani,大谷ダム,2,大谷,,99,99,0,1,\n"])
        old = bb.SPEC_CSV
        bb.SPEC_CSV = p
        self.addCleanup(setattr, bb, "SPEC_CSV", old)
        spec = bb.load_spec({d["id"] for d in self.dams})
        self.assertEqual(spec["toyama-otani"]["direct_km2"], "10")
        self.assertEqual(spec["ishikawa-otani"]["direct_km2"], "99")

    def test_spec_rejects_missing_duplicate_and_unknown_ids(self):
        old = bb.SPEC_CSV
        self.addCleanup(setattr, bb, "SPEC_CSV", old)
        ids = {d["id"] for d in self.dams}
        for rows in (["  ,大谷ダム,1,,,10,10,0,1,\n"],
                     ["toyama-otani,A,1,,,1,1,0,1,\n", "toyama-otani,B,2,,,2,2,0,1,\n"],
                     ["nowhere-1,A,1,,,1,1,0,1,\n"]):
            bb.SPEC_CSV = self.write_spec(rows)
            with self.assertRaises(bb.SpecError):
                bb.load_spec(ids)

    def test_shipped_spec_csv_matches_dams_json(self):
        bb.SPEC_CSV = ROOT / "dam_basin_spec.csv"
        data = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))
        spec = bb.load_spec({d["id"] for d in data["dams"]})
        names = {d["id"]: d["name"] for d in data["dams"]}
        self.assertGreaterEqual(len(spec), 23)
        for i, row in spec.items():
            self.assertEqual(row["dam_name"], names[i], i)       # 名前が id と食い違っていない


class TestBuildBasinsFailureKeepsExisting(unittest.TestCase):
    """build_basins.main: 1基失敗しても、既存の成果（その基の分も含め）を消さない。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dams = [
            {"id": "t-a1", "name": "A", "lat": 36.0, "lon": 137.0},
            {"id": "t-b2", "name": "B", "lat": 36.1, "lon": 137.0},
        ]
        (self.tmp / "dams.json").write_text(json.dumps({"dams": self.dams}), encoding="utf-8")
        (self.tmp / "spec.csv").write_text(
            "dam_id,dam_name,binran_no,binran_name,basin_raw,total_km2,direct_km2,indirect_km2,all_direct,note\n",
            encoding="utf-8")
        self.out = self.tmp / "basins"
        self.out.mkdir()
        self.prev_feats = [{"type": "Feature", "properties": {"id": d["id"], "name": d["name"], "computed_area_km2": 1.0},
                            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
                           for d in self.dams]
        self.prev_meta = [{"id": d["id"], "name": d["name"], "computed_area_km2": 1.0, "zoom": 14, "tiles": 9,
                           "resolution_m": 9.5} for d in self.dams]
        (self.out / "basins.geojson").write_text(json.dumps({"type": "FeatureCollection", "properties": {},
                                                             "features": self.prev_feats}), encoding="utf-8")
        (self.out / "basins_meta.json").write_text(json.dumps(self.prev_meta), encoding="utf-8")
        self.saved = (bb.DAMS_JSON, bb.SPEC_CSV, bb.delineate)
        bb.DAMS_JSON, bb.SPEC_CSV = self.tmp / "dams.json", self.tmp / "spec.csv"
        self.addCleanup(self.restore)

    def restore(self):
        bb.DAMS_JSON, bb.SPEC_CSV, bb.delineate = self.saved

    def run_main(self, delineate):
        bb.delineate = delineate
        old = sys.argv
        sys.argv = ["build_basins.py", "--id", "t-a1,t-b2", "--out", str(self.out),
                    "--cache-root", str(self.tmp / "cache"), "--offline"]
        buf = io.StringIO()
        try:
            so = sys.stdout
            sys.stdout = buf
            return bb.main()
        finally:
            sys.stdout = so
            sys.argv = old

    @staticmethod
    def ok_result(area):
        coords = [[0, 0], [2, 0], [2, 2], [0, 0]]
        return coords, {"computed_area_km2": area, "zoom": 14, "resolution_m": 9.5, "tiles": 9, "vertices": 4,
                        "outlet_method": "snap", "seconds": 0.1}

    def read(self):
        gj = json.loads((self.out / "basins.geojson").read_text(encoding="utf-8"))
        meta = json.loads((self.out / "basins_meta.json").read_text(encoding="utf-8"))
        return gj, meta

    def test_dem_fetch_error_keeps_previous_result_and_exits_nonzero(self):
        def delineate(name, lat, lon, official, verbose=True, sizing_area=None):
            if name == "B":
                raise dem_tiles.DemFetchError("標高タイル 14/1/1 を取得できませんでした（3回試行）: timed out")
            return self.ok_result(5.0)
        rc = self.run_main(delineate)
        self.assertEqual(rc, 1)
        gj, meta = self.read()
        by = {f["properties"]["id"]: f for f in gj["features"]}
        self.assertEqual(by["t-a1"]["properties"]["computed_area_km2"], 5.0)         # 成功分は更新
        self.assertEqual(by["t-b2"]["properties"]["computed_area_km2"], 1.0)         # 失敗分は既存のまま
        self.assertEqual({m["id"]: m["computed_area_km2"] for m in meta}, {"t-a1": 5.0, "t-b2": 1.0})
        self.assertFalse([m for m in meta if "error" in m])                          # 失敗の穴を書き込まない

    def test_all_fail_writes_nothing(self):
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}

        def delineate(*a, **k):
            return None, {"error": "標高データが取得できませんでした", "error_kind": "dem_nodata"}
        rc = self.run_main(delineate)
        self.assertEqual(rc, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_unreadable_existing_output_aborts_instead_of_overwriting(self):
        (self.out / "basins_meta.json").write_text("{ broken", encoding="utf-8")
        before = (self.out / "basins.geojson").read_bytes()
        rc = self.run_main(lambda *a, **k: self.ok_result(5.0))
        self.assertEqual(rc, 3)
        self.assertEqual(before, (self.out / "basins.geojson").read_bytes())

    def test_unchanged_result_does_not_rewrite(self):
        def delineate(*a, **k):
            return self.ok_result(1.0)
        # 既存と同じ面積でも、ポリゴンが違えば「変更あり」。同一ならば書き換えない
        rc = self.run_main(delineate)
        self.assertEqual(rc, 0)
        gj1 = (self.out / "basins.geojson").read_bytes()
        rc = self.run_main(delineate)
        self.assertEqual(rc, 0)
        self.assertEqual(gj1, (self.out / "basins.geojson").read_bytes())


class TestBuildFlowgridsCli(unittest.TestCase):
    """build_flowgrids.main を一時ディレクトリで実行し、失敗しても既存の配信物・レポートを壊さないことを確かめる。"""

    def setUp(self):
        import build_flowgrids as bf
        self.bf = bf
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dams = [fake_dam("t-a1"), fake_dam("t-b2")]
        (self.tmp / "docs/data").mkdir(parents=True)
        (self.tmp / "docs/data/dams.json").write_text(json.dumps({"dams": self.dams}), encoding="utf-8")
        (self.tmp / "data/basins").mkdir(parents=True)
        feats = [{"type": "Feature", "properties": {"id": d["id"]}, "geometry": {"type": "Polygon",
                  "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}} for d in self.dams]
        (self.tmp / "data/basins/basins.geojson").write_text(json.dumps({"features": feats}), encoding="utf-8")
        (self.tmp / "data/basins/basins_meta.json").write_text(json.dumps(
            [{"id": d["id"], "computed_area_km2": 1.0, "zoom": 14, "tiles": 9} for d in self.dams]), encoding="utf-8")
        (self.tmp / "spec.csv").write_text(
            "dam_id,dam_name,binran_no,binran_name,basin_raw,total_km2,direct_km2,indirect_km2,all_direct,note"
            + chr(10), encoding="utf-8")
        self.saved = (bf.ROOT, bf.REPORT_DEFAULT, bb.SPEC_CSV)
        bf.ROOT, bf.REPORT_DEFAULT, bb.SPEC_CSV = self.tmp, self.tmp / "data/watershed/qa_report.json", self.tmp / "spec.csv"
        self.addCleanup(self.restore)
        store = wp.Store(self.tmp)
        for d in self.dams:                                       # 2基を配信済みにしておく
            gen = fake_gen(d)
            ring = ring_of(gen["rec"])
            wp.apply_result(store, d, wp.judge(gen, ring, d["id"]), gen, ring, {"id": d["id"]})
        wp.assemble(store, self.dams)
        self.run_cli("--assemble-only")
        (self.tmp / "data/watershed/qa_report.json").write_text("{}", encoding="utf-8")   # 置き換わるかを見る

    def restore(self):
        self.bf.ROOT, self.bf.REPORT_DEFAULT, bb.SPEC_CSV = self.saved

    def snapshot(self):
        return {p.relative_to(self.tmp).as_posix(): p.read_bytes()
                for p in sorted(self.tmp.rglob("*")) if p.is_file() and p.name != "qa_report.json"}

    def run_cli(self, *argv):
        so, sys.stdout = sys.stdout, io.StringIO()
        try:
            return self.bf.main(list(argv))
        finally:
            sys.stdout = so

    def test_failed_run_keeps_deliverables_and_report_covers_all_dams(self):
        before = self.snapshot()
        rc = self.run_cli("--id", "t-a1", "--offline", "--cache-root", str(self.tmp / "nocache"))
        self.assertEqual(rc, 1)                                          # 生成失敗 → 終了コード1
        self.assertEqual(before, self.snapshot())                        # 配信物・状態は1バイトも変わらない
        rep = json.loads((self.tmp / "data/watershed/qa_report.json").read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in rep["dams"]], ["t-a1", "t-b2"])   # 失敗した回でも全基分が残る
        self.assertTrue(all(r["published"] for r in rep["dams"]))

    def test_evaluate_writes_nothing_except_report(self):
        before = self.snapshot()
        out = self.tmp / "rep.json"
        rc = self.run_cli("--id", "t-a1", "--offline", "--evaluate", "--report", str(out),
                          "--cache-root", str(self.tmp / "nocache"))
        self.assertEqual(rc, 1)
        after = {k: v for k, v in self.snapshot().items() if k != "rep.json"}      # 書いてよいのは --report だけ
        self.assertEqual(before, after)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["failures"][0]["kind"], "dem_fetch")

    def test_unknown_id_and_no_target_are_usage_errors(self):
        self.assertEqual(self.run_cli("--id", "t-typo", "--offline"), 2)
        self.assertEqual(self.run_cli("--offline"), 2)


# ------------------------------------------------------------------ app.js（実コード）との一致

def _run_jscript(js: str) -> str:
    """Windows 標準の JScript（cscript）で app.js から切り出した実コードを動かす。"""
    import re
    import subprocess
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//[^\r\n]*$", "", js)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.js"
        p.write_text(js, encoding="ascii", errors="replace")
        r = subprocess.run(["cscript", "//nologo", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return r.stdout


@unittest.skipUnless(os.name == "nt" and shutil.which("cscript"), "Windows の cscript がある環境のみ")
class TestAppJsParity(unittest.TestCase):
    src = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")

    def cut(self, start, end):
        a = self.src.index(start)
        return self.src[a:self.src.index(end, a)]

    def test_python_smooth_ring_matches_appjs_smoothRing(self):
        fn = self.cut("function smoothRing(ring, iters) {", "function ringBounds(ring)")
        n = 60
        rings = {
            "circle": [[137 + 0.01 * math.cos(2 * math.pi * k / n), 36 + 0.01 * math.sin(2 * math.pi * k / n)] for k in range(n)],
            "star": [[137 + (0.01 if k % 2 else 0.004) * math.cos(2 * math.pi * k / n),
                      36 + (0.01 if k % 2 else 0.004) * math.sin(2 * math.pi * k / n)] for k in range(n)],
        }
        for v in rings.values():
            v.append(v[0])
        prog = fn + chr(10) + "var R=" + json.dumps(rings) + ";" + chr(10) + (
            'var out=[];for(var k in R){var r=smoothRing(R[k],2);var s=k+"|"+r.length;'
            'for(var i=0;i<r.length;i+=1){s+="|"+r[i][0].toFixed(9)+","+r[i][1].toFixed(9);}out.push(s);}'
            "WScript.Echo(out.join(String.fromCharCode(10)));")
        got = _run_jscript(prog).strip().splitlines()
        self.assertEqual(len(got), 2)
        for line in got:
            parts = line.split("|")
            js_pts = [tuple(map(float, x.split(","))) for x in parts[2:]]
            py = wc.smooth_ring(rings[parts[0]], 2)
            self.assertEqual(len(js_pts), len(py), parts[0])
            self.assertLess(max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(js_pts, py)), 1e-8, parts[0])

    def test_appjs_version_check_is_per_part(self):
        fn = self.cut("function sameVersion(got, want) {", "function loadGrid(id)")
        prog = (fn + chr(10)
                + "var r=[sameVersion('a','a'),sameVersion('a','b'),sameVersion(null,'b'),"
                  "sameVersion('a',null),sameVersion('a','')];"
                + "WScript.Echo(r.join(','));")
        # 期待版と一致 / 不一致（止める）/ 版なしのデータ・期待版なしの索引は比較しない
        self.assertEqual(_run_jscript(prog).strip(), "true,false,true,true,true")


if __name__ == "__main__":
    unittest.main()
