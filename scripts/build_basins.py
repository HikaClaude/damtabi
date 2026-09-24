#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地理院の標高タイルから、各ダムの集水域（流域）を計算する **単発** スクリプト。

日次バッチではない。ダムを増減したときだけ実行する。

方式
----
国土数値情報の流域界（W07 流域メッシュ）は、富山県では収録が部分的で、
単位も「単位流域」のためダムの集水域には使えない（調査済み）。
そこで国土地理院の標高タイル（登録不要・地図タイルと同じ配信）から
地形解析で集水域を求める。

  1. ダム周辺の標高タイルを取得して格子を作る
  2. 窪地を埋める（Priority-Flood）
  3. D8 で流向を決める
  4. 上流セル数（集水量）を数える
  5. ダム地点を最寄りの河道へスナップし、そこへ流れ込むセルを全部拾う
  6. 外周をたどってポリゴン化し、30m 相当で簡略化する

出力は「地形から計算した概略」であって、実測の流域界ではない。
導水路があるダムでは、地形上の集水域と実際の集水範囲が一致しない。
その旨は metadata に記録し、表示側で必ず明示すること。

依存: numpy のみ（標準ライブラリ + numpy）
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dem_tiles  # noqa: E402  標高タイルの取得（通信障害と本当の欠損を分ける）
from dem_tiles import DemFetchError  # noqa: E402
import ws_common as wc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAMS_JSON = ROOT / "docs" / "data" / "dams.json"
SPEC_CSV = ROOT / "dam_basin_spec.csv"          # ダム便覧由来の流域面積（直接/間接）。dam_id で引く
OUT_DIR = ROOT / "data" / "basins"
CACHE = ROOT / "cache" / "dem"

TILE = "dem"                 # 10m メッシュ標高（z14）。低ズームは間引き済みの同系列
SIMPLIFY_M = 30.0            # ポリゴンの簡略化許容誤差
# 輪の作り方。"legacy" = 輪郭セルを重心まわりの角度順に並べる（監査済みの既存成果の方式）。
# "exact" = 輪郭を画素の辺に沿ってたどる。legacy は集水域と 3〜7% ずれる（空間QAで実測）。
# 既存22基の輪を黙って変えないため、既定は legacy。切り替えは --outline exact。
OUTLINE = "legacy"
MAX_TILES = 361              # 1基あたりの標高タイル数の上限（窪地埋めが純Pythonのため）
# 河道へのスナップ探索半径。狭いと河道に届かず、広いと隣の大きな川へ飛ぶ。
# 小さい方から試し、公式流域面積と桁が合った時点で採用する。
SNAP_CANDIDATES_M = (150.0, 250.0, 400.0, 700.0)

# 8近傍（時計回り）。index が流向コードになる
D8 = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


# ---------------------------------------------------------------- タイル

def tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def xy_latlon(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def meters_per_px(lat: float, z: int) -> float:
    return 2 * math.pi * 6378137 * math.cos(math.radians(lat)) / (256 * 2 ** z)


SOURCE: dem_tiles.TileSource | None = None


def configure_source(cache_dir: Path | None = None, **kw) -> dem_tiles.TileSource:
    """標高タイルの取得元を設定する。offline / fallback_dirs / revalidate_empty など。"""
    global SOURCE
    SOURCE = dem_tiles.TileSource(cache_dir or CACHE, **kw)
    return SOURCE


def source() -> dem_tiles.TileSource:
    return SOURCE if SOURCE is not None else configure_source()


def get_tile(z: int, x: int, y: int) -> np.ndarray:
    """1枚の標高タイル。通信障害は DemFetchError（空タイルとして固めない）。"""
    return source().get(z, x, y)[0]


def build_grid(lat: float, lon: float, z: int, pad: int, report: dict | None = None):
    """ダム周辺の標高格子。report を渡すとタイルごとの種別（ok/missing/legacy_empty）が入る。"""
    return source().build_grid(tile_xy, lat, lon, z, pad, report)


def atomic_write_text(path: Path, text: str) -> None:
    """一時ファイルへ書いてから置き換える。途中で落ちても既存の成果物を壊さない。"""
    dem_tiles.atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------- 地形解析

def fill_sinks(dem: np.ndarray) -> np.ndarray:
    """Priority-Flood。窪地を埋めて必ず外へ流れるようにする。"""
    import heapq
    h, w = dem.shape
    out = np.full(dem.shape, np.inf, dtype=np.float32)
    closed = np.zeros(dem.shape, dtype=bool)
    pq: list = []
    valid = np.isfinite(dem)

    for i in range(h):
        for j in (0, w - 1):
            if valid[i, j]:
                out[i, j] = dem[i, j]; closed[i, j] = True
                heapq.heappush(pq, (float(dem[i, j]), i, j))
    for j in range(w):
        for i in (0, h - 1):
            if valid[i, j] and not closed[i, j]:
                out[i, j] = dem[i, j]; closed[i, j] = True
                heapq.heappush(pq, (float(dem[i, j]), i, j))

    while pq:
        e, i, j = heapq.heappop(pq)
        for di, dj in D8:
            ni, nj = i + di, j + dj
            if 0 <= ni < h and 0 <= nj < w and not closed[ni, nj] and valid[ni, nj]:
                v = max(float(dem[ni, nj]), e + 1e-3)
                out[ni, nj] = v; closed[ni, nj] = True
                heapq.heappush(pq, (v, ni, nj))
    return out


def flow_dir(dem: np.ndarray) -> np.ndarray:
    """D8 流向。numpy でまとめて計算する。"""
    h, w = dem.shape
    best = np.full((h, w), -1, dtype=np.int8)
    drop = np.zeros((h, w), dtype=np.float32)
    for k, (di, dj) in enumerate(D8):
        sh = np.full((h, w), np.nan, dtype=np.float32)
        si = slice(max(0, -di), h - max(0, di))
        sj = slice(max(0, -dj), w - max(0, dj))
        ti = slice(max(0, di), h - max(0, -di))
        tj = slice(max(0, dj), w - max(0, -dj))
        sh[si, sj] = dem[ti, tj]
        d = (dem - sh) / (1.4142 if di and dj else 1.0)
        m = np.isfinite(d) & (d > drop)
        drop[m] = d[m]; best[m] = k
    best[~np.isfinite(dem)] = -1
    return best


def flow_accum(fd: np.ndarray) -> np.ndarray:
    """上流セル数。入次数0から順に流す（トポロジカル順）。"""
    h, w = fd.shape
    indeg = np.zeros((h, w), dtype=np.int32)
    for k, (di, dj) in enumerate(D8):
        m = fd == k
        if not m.any():
            continue
        src = np.argwhere(m)
        ti = src[:, 0] + di; tj = src[:, 1] + dj
        ok = (ti >= 0) & (ti < h) & (tj >= 0) & (tj < w)
        np.add.at(indeg, (ti[ok], tj[ok]), 1)

    acc = np.ones((h, w), dtype=np.int32)
    q = deque(map(tuple, np.argwhere(indeg == 0)))
    while q:
        i, j = q.popleft()
        k = fd[i, j]
        if k < 0:
            continue
        ni, nj = i + D8[k][0], j + D8[k][1]
        if not (0 <= ni < h and 0 <= nj < w):
            continue
        acc[ni, nj] += acc[i, j]
        indeg[ni, nj] -= 1
        if indeg[ni, nj] == 0:
            q.append((ni, nj))
    return acc


def upstream_of(fd: np.ndarray, oi: int, oj: int) -> np.ndarray:
    """出口セルへ流れ込むセルを全部拾う。"""
    return upstream_of_set(fd, [(oi, oj)])


def upstream_of_set(fd: np.ndarray, seeds) -> np.ndarray:
    """複数のセルへ流れ込むセルを全部拾う（貯水池の水面をまとめて起点にする用）。

    起点が1点だと、貯水池の水面のように平坦な場所では窪地埋めが作る
    わずかな傾斜の向きで水面が分割され、上流の一部しか拾えないことがある。
    （臼中ダムで水面の41%が集水域外になった。境川ダムでも同種の取りこぼし）
    """
    h, w = fd.shape
    ws = np.zeros((h, w), dtype=bool)
    q = deque()
    if isinstance(seeds, np.ndarray):
        ws |= seeds
        q.extend(map(tuple, np.argwhere(seeds)))
    else:
        for i, j in seeds:
            if not ws[i, j]:
                ws[i, j] = True
                q.append((i, j))
    while q:
        i, j = q.popleft()
        for k, (di, dj) in enumerate(D8):
            ni, nj = i - di, j - dj
            if 0 <= ni < h and 0 <= nj < w and not ws[ni, nj] and fd[ni, nj] == k:
                ws[ni, nj] = True
                q.append((ni, nj))
    return ws


# 貯水池の水面をどう見分けるか
LAKE_FLAT_M = 0.6         # 3x3 の標高差がこれ未満なら「平ら」
LAKE_SEARCH_M = 2000.0    # 堤体からこの距離までに水面があるはず
LAKE_NEAR_M = 700.0       # 水面は堤体のこれくらい近くまで来ているはず
LAKE_MIN_CELLS = 30
LAKE_MAX_KM2 = 60.0       # これを超える「平ら」は湖ではない（平野・海など）
LAKE_BAND_M = 1.5         # 水位からこの範囲を水面とみなす（標高タイルのばらつき分）


# 貯水池の水面を起点にせず、河道起点（従来のスナップ）で集水域を作るダム（dam_id → 理由）。
# スナップの方法・半径・判定は他のダムと同じ。面積を合わせるための調整はしない。
RIVER_OUTLET_ONLY: dict[str, str] = {
    "toyama-shiraiwagawa":
        "2026-09-24 人の判断: 観光向けの概略として、水面起点（便覧比 +16.4%）ではなく既存の河道起点案"
        "（便覧比 −6.5%）を表示する。水面起点では、河道起点の出口（堤体より下流・標高110.7m）を通らない"
        "約5.5km²を含んでいた（作業保存 wsq/audit_shirai_shiraiwagawa.json）。",
}


def pick_outlet(dem, fd, acc, oi: int, oj: int, mpp: float, official: float | None,
                use_reservoir: bool = True):
    """集水域の起点を決める。build_basins と build_flowgrids で同じものを使う。

    返り値: (起点セルの集合 seeds, 代表セル (i,j), 方式名, 使った探索半径)

    貯水池が見つかればその水面全体を起点にする。見つからなければ従来どおり
    堤体の近くで集水量が最大のセルへ寄せる。半径は小さい方から試し、
    公式の**直接流域**と桁が合った時点で採用する（合計を渡すと導水分まで
    地形から探しに行って出口が飛ぶ。有峰ダムで実測）。
    """
    lake, level = find_reservoir(dem, oi, oj, mpp) if use_reservoir else (None, None)
    if lake is not None:
        li, lj = np.argwhere(lake)[int(np.argmin(dem[lake]))]
        return lake, (int(li), int(lj)), "reservoir", 0.0

    best = None
    for snap_m in SNAP_CANDIDATES_M:
        snap = max(4, int(snap_m / mpp))
        i0, j0 = max(0, oi - snap), max(0, oj - snap)
        sub = acc[i0:oi + snap + 1, j0:oj + snap + 1]
        di, dj = np.unravel_index(int(np.argmax(sub)), sub.shape)
        ci, cj = i0 + di, j0 + dj
        carea = float(upstream_of(fd, ci, cj).sum()) * mpp * mpp / 1e6
        cand = (snap_m, ci, cj, carea)
        if best is None:
            best = cand
        if official:
            r = carea / official
            if 0.5 <= r <= 1.6:
                best = cand
                break
            if r > 1.6:
                break
            best = cand
        else:
            if carea > 0.3:
                best = cand
                break
            best = cand
    snap_used, si, sj, _ = best
    return None, (int(si), int(sj)), "snap", snap_used


def find_reservoir(dem: np.ndarray, oi: int, oj: int, mpp: float):
    """堤体のそばにある「平らな連結域」＝貯水池の水面を探す。

    集水域は本来「貯水池に流れ込む範囲」である。堤体の近くで集水量が最大の
    セルを1点だけ選ぶ方式だと、その点が水面に乗ったときに水面が分割され、
    上流を取りこぼす（臼中で水面の41%が集水域外、境川で −38.7%）。
    水面全体を起点にすればこれを避けられる。

    標高のヒストグラムの最頻値で水面を探すと、山腹の標高に引きずられて
    失敗する（境川で実測）。ここでは「周囲との標高差がない」ことで見分ける。

    見つからなければ (None, None) を返し、従来のスナップ方式に落とす。
    """
    h, w = dem.shape
    r = max(40, int(LAKE_SEARCH_M / mpp))
    i0, i1 = max(0, oi - r), min(h, oi + r + 1)
    j0, j1 = max(0, oj - r), min(w, oj + r + 1)
    sub = dem[i0:i1, j0:j1]
    if sub.size < LAKE_MIN_CELLS or not np.isfinite(sub).any():
        return None, None

    # 3x3 の最大−最小。水面はここが 0 に近い。
    big = np.where(np.isfinite(sub), sub, -1e9)
    small = np.where(np.isfinite(sub), sub, 1e9)
    mx = big.copy()
    mn = small.copy()
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            mx = np.maximum(mx, np.roll(np.roll(big, di, 0), dj, 1))
            mn = np.minimum(mn, np.roll(np.roll(small, di, 0), dj, 1))
    flat = np.isfinite(sub) & ((mx - mn) < LAKE_FLAT_M)
    flat[0, :] = flat[-1, :] = flat[:, 0] = flat[:, -1] = False   # roll の折り返し除け

    ci, cj = oi - i0, oj - j0
    near = max(4, int(LAKE_NEAR_M / mpp))
    # 堤体の近くにある平坦セルを起点にする（近い順に探す）
    cand = np.argwhere(flat)
    if not len(cand):
        return None, None
    dist2 = (cand[:, 0] - ci) ** 2 + (cand[:, 1] - cj) ** 2
    k = int(np.argmin(dist2))
    if dist2[k] > near * near:
        return None, None
    start = (int(cand[k][0]), int(cand[k][1]))

    hh, ww = flat.shape

    def grow(ok: np.ndarray) -> np.ndarray:
        seen = np.zeros_like(ok)
        if not ok[start]:
            return seen
        seen[start] = True
        q = deque([start])
        while q:
            i, j = q.popleft()
            for di, dj in D8:
                ni, nj = i + di, j + dj
                if 0 <= ni < hh and 0 <= nj < ww and ok[ni, nj] and not seen[ni, nj]:
                    seen[ni, nj] = True
                    q.append((ni, nj))
        return seen

    # 1) まず「平ら」だけで連結成分を取り、そこから水位を決める
    core = grow(flat)
    if int(core.sum()) < LAKE_MIN_CELLS:
        return None, None
    level = float(np.nanmedian(sub[core]))

    # 2) その水位の帯で取り直す。標高タイルの水面には多少のばらつきがあり、
    #    「平ら」だけだと水面の一部しか取れない（臼中で 0.014km² しか取れなかった）。
    band = np.isfinite(sub) & (np.abs(sub - level) < LAKE_BAND_M)
    band[0, :] = band[-1, :] = band[:, 0] = band[:, -1] = False
    seen = grow(band)
    n = int(seen.sum())
    if n < LAKE_MIN_CELLS or n * mpp * mpp / 1e6 > LAKE_MAX_KM2:
        return None, None

    out = np.zeros(dem.shape, dtype=bool)
    out[i0:i1, j0:j1] = seen
    return out, level


# ---------------------------------------------------------------- 輪郭

def outline(mask: np.ndarray) -> np.ndarray:
    """マスクの縁のセルを、重心まわりの角度順に並べて外周とする。

    厳密な輪郭追跡ではないが、簡略化して概略を見せる用途には十分。
    凹んだ形は多少丸まる（概略表示である旨を画面に明記する前提）。
    """
    h, w = mask.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = mask
    nb = (pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:])
    edge = mask & ~nb
    pts = np.argwhere(edge).astype(float)
    if len(pts) < 3:
        pts = np.argwhere(mask).astype(float)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 0] - c[0], pts[:, 1] - c[1])
    return pts[np.argsort(ang)]


def simplify(points: np.ndarray, tol: float) -> np.ndarray:
    """Douglas-Peucker。"""
    def rec(pts):
        if len(pts) < 3:
            return list(pts)
        a, b = pts[0], pts[-1]
        ab = b - a
        L = math.hypot(*ab)
        v = pts - a
        d = np.abs(v[:, 0]) + np.abs(v[:, 1]) if L == 0 \
            else np.abs(ab[0] * v[:, 1] - ab[1] * v[:, 0]) / L
        i = int(np.argmax(d))
        if d[i] > tol:
            return rec(pts[:i + 1])[:-1] + rec(pts[i:])
        return [pts[0], pts[-1]]
    return np.array(rec(np.asarray(points, float)))


# ---------------------------------------------------------------- 1 基分

def pick_zoom(area_km2: float | None) -> tuple[int, int]:
    """流域面積からズームと必要な範囲を決める。大きい流域は粗い解像度で足りる。"""
    a = area_km2 or 30.0
    z = 14 if a < 30 else (13 if a < 150 else 12)
    side_km = 2.6 * math.sqrt(a) + 3.0          # 余裕をみた一辺
    tile_km = 2 * math.pi * 6378137 * math.cos(math.radians(36.6)) / (2 ** z) / 1000
    pad = max(1, math.ceil(side_km / tile_km / 2))
    return z, pad


def delineate(name: str, lat: float, lon: float, official: float | None,
              verbose=True, sizing_area: float | None = None, use_reservoir: bool = True):
    """1基の集水域。失敗は (None, {"error", "error_kind"}) か DemFetchError（通信障害）。"""
    z, pad = pick_zoom(sizing_area if sizing_area else official)
    outlet_ll = None      # 1回目で決めた出口を緯度経度で固定し、拡大後も同じ点を使う
    for attempt in range(4):
        n_tiles = (2 * pad + 1) ** 2
        t0 = time.time()
        rep: dict = {}
        dem, x0, y0 = build_grid(lat, lon, z, pad, report=rep)
        if not np.isfinite(dem).any():
            return None, {"error": "標高データが取得できませんでした（全タイルに実データが無い）",
                          "error_kind": "dem_nodata", "dem_tiles": rep.get("counts")}

        filled = fill_sinks(dem)
        fd = flow_dir(filled)
        acc = flow_accum(fd)

        # ダムの座標は堤体を指すので、河道の中心から数百m離れていることがある。
        # 半径内で最も集水量の大きいセル（＝本流）へ寄せる。
        # ただし半径を広げすぎると隣の大きな川に飛ぶ（角川ダムで実測）。
        # そこで小さい半径から試し、公式流域面積と桁が合った時点で採用する。
        # 公式面積は「どの河道に乗せるか」の判定にのみ使い、結果の値には手を加えない。
        mpp = meters_per_px(lat, z)
        px, py = tile_xy(lat, lon, z)
        oj = int((px - x0) * 256); oi = int((py - y0) * 256)

        # --- まず貯水池を探す。見つかれば「水面に流れ込む範囲」を集水域とする。
        lake, lake_lv = find_reservoir(dem, oi, oj, mpp) if use_reservoir else (None, None)
        if lake is not None:
            ws = upstream_of_set(fd, lake)
            area = float(ws.sum()) * mpp * mpp / 1e6
            # 代表点（表示・記録用）は水面のうち最も低いセル
            li, lj = np.argwhere(lake)[int(np.argmin(dem[lake]))]
            si, sj = int(li), int(lj)
            snap_used = 0.0
            method = "reservoir"
            lake_cells = int(lake.sum())
        elif outlet_ll is not None:
            # 既に出口が決まっているので、同じ地点を新しい格子で指すだけ。
            # ここで選び直すと流向場の変化でスナップ先が隣の川へ移ることがある。
            ox, oy = tile_xy(outlet_ll[0], outlet_ll[1], z)
            si = int((oy - y0) * 256); sj = int((ox - x0) * 256)
            ws = upstream_of(fd, si, sj)
            area = float(ws.sum()) * mpp * mpp / 1e6
            snap_used = 0.0
            method = "snap-fixed"
            lake_cells = 0
        else:
            best = None
            for snap_m in SNAP_CANDIDATES_M:
                snap = max(4, int(snap_m / mpp))
                i0, j0 = max(0, oi - snap), max(0, oj - snap)
                sub = acc[i0:oi + snap + 1, j0:oj + snap + 1]
                di, dj = np.unravel_index(int(np.argmax(sub)), sub.shape)
                ci, cj = i0 + di, j0 + dj
                cws = upstream_of(fd, ci, cj)
                carea = float(cws.sum()) * mpp * mpp / 1e6
                cand = (snap_m, ci, cj, cws, carea)
                if best is None:
                    best = cand
                if official:
                    r = carea / official
                    if 0.5 <= r <= 1.6:
                        best = cand
                        break
                    if r > 1.6:
                        break
                    best = cand
                else:
                    if carea > 0.3:
                        best = cand
                        break
                    best = cand
            snap_used, si, sj, ws, area = best
            outlet_ll = xy_latlon(x0 + sj / 256.0, y0 + si / 256.0, z)
            method = "snap"
            lake_cells = 0

        # 縁に達していたら切れている可能性が高いので広げて再計算。
        # ただし公式面積と既に整合しているなら広げない
        # （広げると流向場が変わり、スナップ先が隣の川に移ることがある。角川ダムで実測）。
        touches = bool(ws[0, :].any() or ws[-1, :].any() or ws[:, 0].any() or ws[:, -1].any())
        # 拡大には上限を置く。窪地埋めは純Pythonなので、際限なく広げると
        # 現実的な時間で終わらない（1辺が2倍になると4倍の時間がかかる）。
        nxt = pad + max(1, pad // 2)
        if touches and attempt < 3 and (2 * nxt + 1) ** 2 <= MAX_TILES:
            if verbose:
                print(f"    …範囲の端に達したので拡大して再計算 (pad {pad} → {nxt})")
            pad = nxt
            continue
        if touches and verbose:
            print(f"    ※端に達したまま（タイル上限 {MAX_TILES}）。切れている可能性あり")

        pts = simplify(wc.outline_exact(ws) if OUTLINE == "exact" else outline(ws), SIMPLIFY_M / mpp)
        coords = []
        for i, j in pts:
            la, lo = xy_latlon(x0 + j / 256.0, y0 + i / 256.0, z)
            coords.append([round(lo, 5), round(la, 5)])
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])

        meta = {
            "computed_area_km2": round(area, 2),
            "zoom": z, "resolution_m": round(mpp, 1),
            "tiles": n_tiles, "vertices": len(coords),
            "outlet_method": method,
            "lake_cells": lake_cells,
            "lake_km2": round(lake_cells * mpp * mpp / 1e6, 3),
            "lake_level_m": round(lake_lv, 1) if lake is not None else None,
            # 貯水池が集水域に収まっているか。収まらなければ取りこぼしている。
            "lake_inside_pct": (round(float((lake & ws).sum()) / max(1, lake_cells) * 100, 1)
                                if lake is not None else None),
            "outlet_distance_m": round(math.hypot(si - oi, sj - oj) * mpp),
            "snap_distance_m": round(math.hypot(si - oi, sj - oj) * mpp),
            "snap_radius_m": snap_used,
            "touches_edge": bool(touches),
            "outline": OUTLINE,
            "dem_tiles": rep.get("counts"),
            "seconds": round(time.time() - t0, 1),
        }
        if official:
            meta["official_area_km2"] = official
            meta["area_error_pct"] = round((area / official - 1) * 100, 1)
        return coords, meta
    return None, {"error": "範囲を広げても収まりませんでした", "error_kind": "no_fit"}


# ---------------------------------------------------------------- main

class SpecError(ValueError):
    pass


def load_spec(dam_ids=None) -> dict:
    """ダム便覧由来の流域面積（直接/間接）。**dam_id をキーにする**（名前では引かない）。

    旧実装は dam_name で引いていた。全国に広げると同名のダムが現れる（例: 別の県の
    「大谷ダム」）ため、名前一致では別のダムの流域面積を黙って当ててしまう。
    dam_id が空・重複・dams.json に無い行は SpecError にして止める。

    直接・間接流域の「入口」（列）:
      direct_km2 / indirect_km2 / total_km2  流域面積（km²）。分かっている値だけを書く
      source                                 出典（例: ダム便覧）
      basin_status                           confirmed（確認済み）/ unconfirmed（未確認）
      note                                   備考
    - unconfirmed の行は数値を持てない（推測値を入れさせない）。行が無いダムも「未確認」と同じ扱い。
    - confirmed の行は、直接または合計の数値と、出典（source またはダム便覧番号）が必須。
    - basin_status 列が無い旧形式は、数値の有無から推定する（互換）。
    """
    import csv
    if not SPEC_CSV.exists():
        return {}
    out = {}
    with SPEC_CSV.open(encoding="utf-8-sig", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), 2):
            where = f"{SPEC_CSV.name} {n}行目"
            did = (r.get("dam_id") or "").strip()
            if not did:
                raise SpecError(f"{where}（{r.get('dam_name')}）に dam_id がありません")
            if did in out:
                raise SpecError(f"{where}: dam_id {did} が重複しています")
            if dam_ids is not None and did not in dam_ids:
                raise SpecError(f"{where}: dam_id {did} は dams.json にありません")
            _check_basin_columns(r, where)
            out[did] = r
    return out


BASIN_STATUSES = ("confirmed", "unconfirmed")
_BASIN_NUMBERS = ("total_km2", "direct_km2", "indirect_km2")


def _check_basin_columns(r: dict, where: str) -> None:
    """直接・間接流域の入口の検証。値は書き換えない（basin_status の推定を補うだけ）。"""
    nums = {}
    for k in _BASIN_NUMBERS:
        v = (r.get(k) or "").strip()
        if v:
            try:
                nums[k] = float(v)
            except ValueError:
                raise SpecError(f"{where}: {k} が数値ではありません: {v!r}")
            if nums[k] < 0:
                raise SpecError(f"{where}: {k} が負です: {v}")
    status = (r.get("basin_status") or "").strip()
    if status == "":
        status = "confirmed" if ("direct_km2" in nums or "total_km2" in nums) else "unconfirmed"
        r["basin_status"] = status
    if status not in BASIN_STATUSES:
        raise SpecError(f"{where}: basin_status は {BASIN_STATUSES} のいずれか: {status!r}")
    if status == "unconfirmed" and nums:
        raise SpecError(f"{where}: 未確認(unconfirmed)の行に数値は入れられません（{', '.join(nums)}）")
    if status == "confirmed":
        if "direct_km2" not in nums and "total_km2" not in nums:
            raise SpecError(f"{where}: 確認済み(confirmed)の行には直接または合計の流域面積が必要です")
        if not (r.get("source") or "").strip() and not (r.get("binran_no") or "").strip():
            raise SpecError(f"{where}: 確認済み(confirmed)の行には出典(source)が必要です")


def diversion_flag(spec: dict | None) -> dict:
    """導水（間接流域）の有無を機械判定する。判定できないものは未確認とする。"""
    if spec and not spec.get("basin_raw") and (spec.get("basin_status") or "").strip() == "confirmed":
        # 入口から直接・間接の数値だけを受け取った行（ダム便覧の原文 basin_raw が無い）
        def _f(k):
            v = (spec.get(k) or "").strip()
            return float(v) if v else None
        dr, ind, tot = _f("direct_km2"), _f("indirect_km2"), _f("total_km2")
        if ind is not None and ind > 0:
            return {"status": "yes", "direct_km2": dr, "indirect_km2": ind,
                    "note": "他の河川からの導水があり、地形上の集水域と実際の集水範囲は一致しない"}
        if ind == 0 or (dr is not None and tot is not None and dr == tot):
            return {"status": "none", "direct_km2": dr if dr is not None else tot, "indirect_km2": 0.0,
                    "note": "全て直接流域（入力された流域面積）"}
        return {"status": "unknown", "note": "直接/間接の内訳が入力されていない"}
    if not spec or not spec.get("basin_raw"):
        return {"status": "unknown",
                "note": "ダム便覧に該当記載を確認できず、導水の有無は未確認"}
    ind = spec.get("indirect_km2", "").strip()
    dr = spec.get("direct_km2", "").strip()
    if spec.get("all_direct", "").strip() == "1":
        return {"status": "none", "direct_km2": float(dr) if dr else None,
                "indirect_km2": 0.0, "note": "全て直接流域（ダム便覧）"}
    if ind:
        return {"status": "yes", "direct_km2": float(dr) if dr else None,
                "indirect_km2": float(ind),
                "note": "他の河川からの導水があり、地形上の集水域と実際の集水範囲は一致しない"}
    return {"status": "unknown", "note": "直接/間接の内訳を読み取れなかった"}


def select_targets(dams: list, args) -> list:
    """対象を明示的に決める。既定では何もしない（全件生成しない）。

    2026-09-18 に、対象未指定のまま実行して 80 基すべてを計算してしまい、
    地理院から **2,869 タイル（1.2GB）を意図せず追加取得**した。
    標高タイルは1基あたり数百枚を取りに行くので、既定を「全件」にしてはいけない。

    --id は dam_id の完全一致。存在しない id は黙って捨てず、UnknownTarget で止める
    （打ち間違いで「何も起きない」「別のダムが処理される」を防ぐ）。
    """
    if args.all:
        return list(dams)

    picked: list = []
    seen = set()

    def add(d):
        if d["id"] not in seen:
            seen.add(d["id"])
            picked.append(d)

    if args.pref:
        want = {p.strip() for p in args.pref.split(",") if p.strip()}
        for d in dams:
            if d["id"].split("-")[0] in want:
                add(d)
    if args.id:
        want = [i.strip() for i in args.id.split(",") if i.strip()]
        known = {d["id"] for d in dams}
        bad = [i for i in want if i not in known]
        if bad:
            raise UnknownTarget("dams.json に無い dam_id: " + ", ".join(bad))
        wanted = set(want)
        for d in dams:
            if d["id"] in wanted:
                add(d)
    if args.only:
        # 名前の部分一致は便宜用。同名が複数あり得るので、必ず id を表示して確認できるようにする
        for d in dams:
            if args.only in d["name"]:
                add(d)
    return picked


class UnknownTarget(ValueError):
    pass


def load_kawabou(data: dict, cache_root: Path) -> dict:
    """川の防災情報の観測所マスタにある流域面積。**dam_id で引く**。"""
    out = {}
    mc = cache_root / "obs_master.json"
    if not mc.exists():
        return out
    obs = json.loads(mc.read_text(encoding="utf-8")).get("obs", {})
    for d in data["dams"]:
        fcd = (d.get("observation") or {}).get("obs_fcd")
        if fcd and fcd in obs and obs[fcd].get("bsnArea") is not None:
            out[d["id"]] = float(obs[fcd]["bsnArea"])
    return out


def add_source_args(ap: argparse.ArgumentParser) -> None:
    """標高タイルの取得元に関する共通の引数（build_flowgrids と共有）。"""
    ap.add_argument("--cache-root", type=Path, default=ROOT / "cache",
                    help="キャッシュの置き場（dem/ と obs_master.json）。書き込み先")
    ap.add_argument("--dem-fallback", type=Path, action="append", default=[],
                    help="読み取り専用の追加の標高タイル置き場（複数可）。書き換えない")
    ap.add_argument("--offline", action="store_true",
                    help="ネットワークを使わない。キャッシュに無いタイルは DemUnavailable で止める")
    ap.add_argument("--max-fetch", type=int, default=None,
                    help="地理院へのリクエスト数の上限（超えたら止める。想定外の大量通信の防止）")
    ap.add_argument("--revalidate-empty", action="store_true",
                    help="由来不明の空タイル（旧実装が書いたもの）を地理院で取り直して 404 かどうか確定させる")


def setup_source(args) -> dem_tiles.TileSource:
    return configure_source(args.cache_root / "dem", offline=args.offline,
                            revalidate_empty=args.revalidate_empty,
                            max_network_requests=args.max_fetch,
                            fallback_dirs=list(args.dem_fallback))


def _ordered(d: dict, order: dict) -> list:
    return [d[k] for k in sorted(d, key=lambda i: order.get(i, 1 << 30))]


def main() -> int:
    global OUTLINE
    ap = argparse.ArgumentParser(
        description="ダムの集水域を標高タイルから計算（単発）。"
                    "対象の指定は必須（--pref / --id / --only / --all のいずれか）。")
    ap.add_argument("--pref", help="県で絞る。dam_id の接頭辞（例: toyama,ishikawa）")
    ap.add_argument("--id", help="dam_id で絞る。カンマ区切り（例: toyama-usunaka）")
    ap.add_argument("--only", help="ダム名の部分一致で絞る（同名に注意。id を確認すること）")
    ap.add_argument("--all", action="store_true",
                    help="dams.json の全基を対象にする（標高タイルを大量に取得するので明示が必要）")
    ap.add_argument("--yes", action="store_true", help="確認を省く")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--outline", choices=("legacy", "exact"), default=OUTLINE,
                    help="輪の作り方。legacy=角度順（既定・既存成果と同じ）/ exact=輪郭をたどる")
    add_source_args(ap)
    args = ap.parse_args()
    OUTLINE = args.outline

    data = json.loads(DAMS_JSON.read_text(encoding="utf-8"))
    spec = load_spec({d["id"] for d in data["dams"]})
    # 川の防災情報の観測所マスタにも流域面積がある。出典が違うので別枠で記録し、
    # 便覧に記載が無いダムでは格子の大きさを決めるのに使う。
    kawabou = load_kawabou(data, args.cache_root)
    try:
        dams = select_targets(data["dams"], args)
    except UnknownTarget as e:
        print(f"対象を決められません: {e}")
        return 2

    # 対象が決まらないときは何もしない。**全件へ落とさない。**
    if not dams:
        if not (args.pref or args.id or args.only or args.all):
            print("対象が指定されていません。--pref / --id / --only / --all のいずれかを付けてください。")
            print("  例: python scripts/build_basins.py --pref toyama")
            print("      python scripts/build_basins.py --id toyama-usunaka")
        else:
            print("指定に一致するダムがありません。全件へは切り替えません。")
            print(f"  指定: pref={args.pref!r} id={args.id!r} only={args.only!r}")
        return 2

    setup_source(args)
    print(f"対象 {len(dams)} 基 / dams.json の全 {len(data['dams'])} 基中"
          + ("  [offline]" if args.offline else ""))
    for d in dams[:12]:
        print(f"  - {d['id']}  {d['name']}")
    if len(dams) > 12:
        print(f"  … ほか {len(dams) - 12} 基")
    if len(dams) > 30 and not args.yes:
        # 標高タイルを大量に取りに行くので、数が多いときは一呼吸置く
        print(f"\n{len(dams)} 基は標高タイルを大量に取得します（1基あたり数十〜数百枚）。")
        print("続けるには --yes を付けて実行してください。")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    prev_geo = args.out / "basins.geojson"
    prev_meta = args.out / "basins_meta.json"
    # 既存の結果（対象外はそのまま残す。対象でも「失敗したら」既存を残す）
    prev_features: dict = {}
    prev_metas: dict = {}
    if prev_geo.exists() and prev_meta.exists():
        try:
            gj0 = json.loads(prev_geo.read_text(encoding="utf-8"))
            prev_features = {f["properties"]["id"]: f for f in gj0["features"]}
            prev_metas = {m["id"]: m for m in json.loads(prev_meta.read_text(encoding="utf-8"))
                          if m.get("id")}
        except Exception as e:
            # 読めない既存は「無かったこと」にせず止める。黙って上書きすると成果を失う
            print(f"既存の出力を読めませんでした（{e}）。壊れている可能性があるので中止します。")
            return 3

    new_features: dict = {}
    new_metas: dict = {}
    failures: list = []
    t_all = time.time()

    for n, d in enumerate(dams, 1):
        sp = spec.get(d["id"])
        # 河道選びの目安には「直接流域」を使う。合計（直接＋間接）を渡すと、
        # 導水で水が来る分まで地形から探そうとして出口が遠くへ飛ぶ。
        # 有峰ダムで実測: 合計219.9を目安にすると候補が全て外れ、最後に試した
        # 半径700m（実距離677m）が採用されていた。直接49.9なら半径150m・
        # 実距離174mで 50.52km²（直接比 1.01）に収まる。
        official = None       # 誤差の比較に使う値（＝直接流域）
        total = None          # 記録用（直接＋間接）
        for key, dst in (("direct_km2", "d"), ("total_km2", "t")):
            try:
                v = float(sp[key]) if sp and sp.get(key) else None
            except ValueError:
                v = None
            if dst == "d":
                official = v
            else:
                total = v
        if official is None:
            official = total
        kw = kawabou.get(d["id"])
        # 格子の大きさ（＝解像度）は便覧の値で決める。川の防災情報の値を優先すると、
        # 出典間で食い違うダムで解像度が粗くなり、貯水池の水面を見失う。
        # 臼中で実測: 川防 48.2 を採ると z13（15.4m/セル）になり水面を検出できず、
        # 便覧 13.5 なら z14（7.7m/セル）で検出できる。狭すぎた場合は端に達した
        # 時点で自動的に広げるので、小さめに始めて構わない。
        sizing = total or official or kw
        print(f"[{n}/{len(dams)}] {d['id']} {d['name']}（便覧 直接 {official if official else '—'} / "
              f"合計 {total if total else '—'} / 川防 {kw if kw else '—'} km2）")

        try:
            # 通常のダムは従来どおり呼ぶ。河道起点のダムだけ水面の検出を使わない
            extra = {"use_reservoir": False} if d["id"] in RIVER_OUTLET_ONLY else {}
            coords, meta = delineate(d["name"], d["lat"], d["lon"], official, sizing_area=sizing, **extra)
            if coords and d["id"] in RIVER_OUTLET_ONLY:
                meta["river_outlet_only"] = True       # 水面起点を使わず河道起点（RIVER_OUTLET_ONLY）
        except DemFetchError as e:
            coords, meta = None, {"error": str(e), "error_kind": "dem_fetch"}
        if not coords:
            failures.append((d["id"], meta.get("error_kind"), meta.get("error")))
            kept = "既存の結果を残します" if d["id"] in prev_features else "既存の結果はありません"
            print(f"    失敗（{meta.get('error_kind')}）: {meta.get('error')}  → {kept}")
            continue

        if total is not None:
            meta["official_total_km2"] = total
        if kw:
            meta["kawabou_area_km2"] = kw
            meta["kawabou_error_pct"] = (round((meta["computed_area_km2"] / kw - 1) * 100, 1)
                                         if meta.get("computed_area_km2") else None)
        meta["id"] = d["id"]
        meta["name"] = d["name"]
        meta["diversion"] = diversion_flag(sp)
        new_metas[d["id"]] = meta
        new_features[d["id"]] = {
            "type": "Feature",
            "properties": {
                "id": d["id"], "name": d["name"],
                "computed_area_km2": meta["computed_area_km2"],
                "official_area_km2": meta.get("official_area_km2"),
                "area_error_pct": meta.get("area_error_pct"),
                "resolution_m": meta["resolution_m"],
                "diversion": meta["diversion"]["status"],
            },
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        }
        e = meta.get("area_error_pct")
        print(f"    算出 {meta['computed_area_km2']:.2f} km2"
              + (f" / 誤差 {e:+.1f}%" if e is not None else "")
              + f" / {meta['vertices']}頂点 / {meta['resolution_m']}m / {meta['seconds']}秒")

    # ---- 書き出し: 既存 + 今回成功分。失敗した基は既存のまま
    feats = dict(prev_features)
    feats.update(new_features)
    metas = dict(prev_metas)
    metas.update(new_metas)
    order = {d["id"]: i for i, d in enumerate(data["dams"])}
    features = _ordered(feats, order)
    all_metas = _ordered(metas, order)

    # 計算時間（seconds）は毎回変わるので、内容の比較からは外す
    def _stable(ms):
        return [{k: v for k, v in m.items() if k != "seconds"} for m in ms]

    changed = (features != _ordered(prev_features, order)
               or _stable(all_metas) != _stable(_ordered(prev_metas, order)))
    if not new_features:
        print("\n成功した基がないので、出力は書き換えません。")
    elif not changed:
        print("\n結果は既存と同一でした。出力は書き換えません。")
    else:
        props = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "method": "国土地理院 標高タイル（DEM10B系）からの地形解析（D8）",
            "accuracy_note": "地形から計算した概略の集水域。実測の流域界ではない。",
            "simplify_m": SIMPLIFY_M,
            "source": "国土地理院 地理院タイル（標高タイル）を加工して作成",
        }
        gj = {"type": "FeatureCollection", "properties": props, "features": features}
        # 2ファイルを一時ファイルに書き終えてから置き換える（片方だけ新しい状態を作らない）
        geo_text = json.dumps(gj, ensure_ascii=False, separators=(",", ":")) + "\n"
        meta_text = json.dumps(all_metas, ensure_ascii=False, indent=1) + "\n"
        atomic_write_text(prev_geo, geo_text)
        atomic_write_text(prev_meta, meta_text)
        size = prev_geo.stat().st_size
        print(f"\n出力 {prev_geo}  {size/1024:.1f} KB / 今回 {len(new_features)} 基・"
              f"据え置き {len(features) - len(new_features)} 基 = 計 {len(features)} 基")
    print(f"総時間 {(time.time()-t_all)/60:.1f} 分")
    if failures:
        print(f"\n失敗 {len(failures)} 基（既存の結果は残しています）:")
        for i, k, m in failures:
            print(f"  - {i} [{k}] {m}")
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
