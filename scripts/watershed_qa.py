#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域成果の品質判定（QA）と配信ゲート。numpy のみ。

考え方
------
- 検査するのは **利用者に届く成果物そのもの**（配信用の流向格子 JSON と、
  ブラウザが実際に描く輪）。生成途中の配列ではなく、書き出した文字列を
  読み戻して調べる。詰め込み（pack1/pack4）の取り違えも同時に検出できる。
- 数値の閾値をまとめて THRESHOLDS に置く。**block に当たったダムは配信しない。**
  warn は配信できるが、理由を記録して画面と報告に残す。
- 「雨を輪の中へ切り落として見かけだけ一致させる」ことはしない。ここは検査だけを
  行い、食い違いは食い違いとして数値と理由で出す。

判定の単位はダム1基（dam_id）。名前は使わない。
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws_common as wc  # noqa: E402

# ---------------------------------------------------------------- 閾値
#
# 旧実装にあった3つ（面積誤差・輪と雨の面積ずれ・雨が届かない割合）は「警告のみ」だった。
# ここでは block（配信しない）に変える。値は旧実装と同じ。
# 空間QAの4つは新設。根拠は docs（README 集水域章）に書き、既存22基の分布を見て置いた。
THRESHOLDS = {
    # --- 旧来（面積ベース）
    "area_error_pct": 15.0,        # 便覧の直接流域との誤差 |%|
    "grid_drift_pct": 15.0,        # 細格子ポリゴンと粗格子マスクの面積差 |%|
    "unreached_pct": 10.0,         # 雨の範囲のうち出口へ届かない割合 %
    # --- 空間QA（描画される輪 × 雨マスク）
    "tol_cells": 2,                # 「境界のずれ」とみなす幅（粗格子のセル数）
    "mask_far_outside_pct": 2.0,   # 雨マスクのうち、輪の外へ tol_cells 超はみ出す割合 %
    "ring_far_only_pct": 5.0,      # 輪の内側のうち、雨マスクから tol_cells 超離れた割合 %
    "path_outside_pct": 2.0,       # 雨の経路が輪（+tol）の外へ出る割合 %（マスク内セル基準）
    "ring_self_intersections": 0,  # 描画される輪の自己交差の数（0 を超えたら block）
    "mask_min_largest_pct": 95.0,  # 雨マスクの最大連結成分が全体に占める割合の下限 %
}

BLOCK = "block"
WARN = "warn"


def _reason(code, severity, msg, value=None, limit=None):
    r = {"code": code, "severity": severity, "message": msg}
    if value is not None:
        r["value"] = value
    if limit is not None:
        r["limit"] = limit
    return r


# ---------------------------------------------------------------- 空間QA

def _goal_mask(rec: dict, h: int, w: int) -> np.ndarray:
    g = np.zeros((h, w), dtype=bool)
    for a, b in (rec.get("outlets") or [rec["outlet"]]):
        if 0 <= a < h and 0 <= b < w:
            g[a, b] = True
    return g


def path_metrics(d8: np.ndarray, goal: np.ndarray, mask: np.ndarray, allowed: np.ndarray):
    """雨マスクの各セルから D8 をたどったとき、(1)出口へ届くか (2)途中で allowed の外へ出るか。

    出口から上流へ向かって1回だけ走査する（セルごとにたどり直さない）。
    D8 コード 15 は「流向なし」。app.js の trace() と同じ規約。
    """
    h, w = d8.shape
    reach = np.zeros((h, w), dtype=bool)
    leaves = np.zeros((h, w), dtype=bool)
    q = deque()
    for i, j in np.argwhere(goal):
        reach[i, j] = True
        leaves[i, j] = not allowed[i, j]
        q.append((int(i), int(j)))
    while q:
        i, j = q.popleft()
        for k, (di, dj) in enumerate(wc.D8):
            ni, nj = i - di, j - dj
            if 0 <= ni < h and 0 <= nj < w and not reach[ni, nj] and d8[ni, nj] == k:
                reach[ni, nj] = True
                leaves[ni, nj] = leaves[i, j] or (not allowed[ni, nj])
                q.append((ni, nj))
    n = int(mask.sum())
    unreached = int((mask & ~reach).sum())
    outside = int((mask & reach & leaves).sum())
    return {
        "unreached_pct": round(unreached / max(1, n) * 100, 2),
        "path_outside_pct": round(outside / max(1, n) * 100, 2),
    }


def spatial_metrics(rec: dict, ring, tol_cells: int | None = None) -> dict:
    """配信物（流向格子レコードと集水域ポリゴン）から、輪と雨の空間的な一致を測る。

    rec  : docs/watershed/flow/<id>.json の内容（mask/d8 は base64 のまま渡す）
    ring : basins.geojson のポリゴン外周（経緯度の [lon, lat] の列。閉じていること）
    """
    tol = THRESHOLDS["tol_cells"] if tol_cells is None else tol_cells
    h, w = int(rec["h"]), int(rec["w"])
    mask = wc.unpack1(rec["mask"], h, w)
    d8 = wc.unpack4(rec["d8"], h, w)
    goal = _goal_mask(rec, h, w)

    drawn = wc.smooth_ring(ring, 2)              # ブラウザが描く輪
    ii, jj = np.mgrid[0:h, 0:w]
    lon, lat = wc.cell_lonlat(rec, ii.astype(float), jj.astype(float))
    # 輪の外接矩形の外は必ず外側。全セルを判定すると重いので矩形で絞る
    xs = [p[0] for p in drawn]
    ys = [p[1] for p in drawn]
    box = (lon >= min(xs)) & (lon <= max(xs)) & (lat >= min(ys)) & (lat <= max(ys))
    inr = np.zeros((h, w), dtype=bool)
    inr[box] = wc.points_in_ring(lon[box], lat[box], drawn)
    raw = np.zeros((h, w), dtype=bool)
    raw[box] = wc.points_in_ring(lon[box], lat[box], ring)

    n_mask = int(mask.sum())
    n_ring = int(inr.sum())
    inter = int((mask & inr).sum())
    union = int((mask | inr).sum())
    ring_dil = wc.dilate(inr, tol)
    mask_dil = wc.dilate(mask, tol)
    far_out = int((mask & ~ring_dil).sum())
    far_only = int((inr & ~mask_dil).sum())

    pm = path_metrics(d8, goal, mask, ring_dil)
    comps = wc.components(mask)
    cell_km2 = (rec["cell_m"] ** 2) / 1e6

    return {
        "grid": [h, w],
        "cell_m": rec["cell_m"],
        "mask_cells": n_mask,
        "ring_cells": n_ring,
        "mask_area_km2": round(n_mask * cell_km2, 2),
        "ring_area_km2": round(n_ring * cell_km2, 2),
        "ring_vs_mask_area_pct": round((n_ring / max(1, n_mask) - 1) * 100, 2),
        "raw_ring_vs_drawn_ring_pct": round((int(raw.sum()) / max(1, n_ring) - 1) * 100, 2),
        "iou": round(inter / max(1, union), 4),
        "mask_outside_pct": round(int((mask & ~inr).sum()) / max(1, n_mask) * 100, 2),
        "ring_only_pct": round(int((inr & ~mask).sum()) / max(1, n_ring) * 100, 2),
        "mask_far_outside_pct": round(far_out / max(1, n_mask) * 100, 2),
        "ring_far_only_pct": round(far_only / max(1, n_ring) * 100, 2),
        "unreached_pct": pm["unreached_pct"],
        "path_outside_pct": pm["path_outside_pct"],
        "ring_self_intersections_raw": wc.self_intersections(ring),
        "ring_self_intersections_drawn": wc.self_intersections(drawn),
        "mask_components": len(comps),
        "mask_largest_pct": round(comps[0] / max(1, n_mask) * 100, 2) if comps else 0.0,
        "goal_cells": int(goal.sum()),
        "goal_in_mask_pct": round(int((goal & mask).sum()) / max(1, int(goal.sum())) * 100, 1),
        "tol_cells": tol,
    }


# ---------------------------------------------------------------- 配信ゲート

def evaluate(spatial: dict, facts: dict, thresholds: dict | None = None,
             manual_hold: str | None = None) -> dict:
    """QA 結果を判定する。status は 'pass'（配信してよい）か 'hold'（配信しない）。

    facts: 生成時に分かる事実。次のキーを見る（無ければその検査は行わない）。
      area_error_pct, grid_drift_pct, official_area_km2, diversion_status,
      outlet_method, dem = {touches_edge, nodata_adjacent_cells,
                            tiles_touching_catchment: [{key, kind}, ...]}
    manual_hold: 人が原因不明のまま保留にしている理由（白岩川など）。あれば必ず hold。
    """
    t = dict(THRESHOLDS, **(thresholds or {}))
    R = []

    if manual_hold:
        R.append(_reason("manual_hold", BLOCK, "原因が特定できていないため保留: " + manual_hold))

    err = facts.get("area_error_pct")
    if err is not None and abs(err) > t["area_error_pct"]:
        R.append(_reason("area_error", BLOCK,
                         f"便覧の直接流域と {err:+.1f}%（許容 ±{t['area_error_pct']:.0f}%）",
                         err, t["area_error_pct"]))
    drift = facts.get("grid_drift_pct")
    if drift is not None and abs(drift) > t["grid_drift_pct"]:
        R.append(_reason("grid_drift", BLOCK,
                         f"細格子ポリゴンと粗格子マスクの面積差 {drift:+.1f}%",
                         drift, t["grid_drift_pct"]))

    if spatial["unreached_pct"] > t["unreached_pct"]:
        R.append(_reason("unreached", BLOCK,
                         f"雨の範囲の {spatial['unreached_pct']:.1f}% が出口へ届かない",
                         spatial["unreached_pct"], t["unreached_pct"]))
    if spatial["mask_far_outside_pct"] > t["mask_far_outside_pct"]:
        R.append(_reason("rain_outside_ring", BLOCK,
                         f"雨マスクの {spatial['mask_far_outside_pct']:.1f}% が描画される輪の外"
                         f"（境界から{t['tol_cells']}セル超）にある",
                         spatial["mask_far_outside_pct"], t["mask_far_outside_pct"]))
    if spatial["ring_far_only_pct"] > t["ring_far_only_pct"]:
        R.append(_reason("ring_without_rain", BLOCK,
                         f"描画される輪の内側の {spatial['ring_far_only_pct']:.1f}% に雨が降らない"
                         f"（境界から{t['tol_cells']}セル超）",
                         spatial["ring_far_only_pct"], t["ring_far_only_pct"]))
    if spatial["path_outside_pct"] > t["path_outside_pct"]:
        R.append(_reason("path_leaves_ring", BLOCK,
                         f"雨の経路の {spatial['path_outside_pct']:.1f}% が輪の外へ流れ出る",
                         spatial["path_outside_pct"], t["path_outside_pct"]))
    if spatial["ring_self_intersections_drawn"] > t["ring_self_intersections"]:
        R.append(_reason("ring_self_intersects", BLOCK,
                         f"描画される輪が自己交差している（{spatial['ring_self_intersections_drawn']}箇所）",
                         spatial["ring_self_intersections_drawn"], t["ring_self_intersections"]))
    if spatial["mask_largest_pct"] < t["mask_min_largest_pct"]:
        R.append(_reason("mask_fragmented", BLOCK,
                         f"雨マスクが分断されている（最大の連結成分が {spatial['mask_largest_pct']:.1f}%）",
                         spatial["mask_largest_pct"], t["mask_min_largest_pct"]))

    dem = facts.get("dem") or {}
    if dem.get("touches_edge"):
        R.append(_reason("dem_edge", BLOCK, "集水域が標高格子の端に達している（切れている可能性）"))
    if dem.get("nodata_adjacent_cells"):
        R.append(_reason("dem_nodata_adjacent", BLOCK,
                         f"集水域が標高欠損セルに接している（{dem['nodata_adjacent_cells']}セル）",
                         dem["nodata_adjacent_cells"], 0))
    for tl in dem.get("tiles_touching_catchment") or []:
        if tl["kind"] == "legacy_empty":
            R.append(_reason("dem_unverified_empty_tile", BLOCK,
                             f"集水域に接する標高タイル {tl['key']} が空。通信失敗か本当の欠損か"
                             "未確認（--revalidate-empty で確認する）"))
        elif tl["kind"] == "missing":
            R.append(_reason("dem_missing_tile", BLOCK,
                             f"集水域に接する標高タイル {tl['key']} は地理院に存在しない（欠損確認済み）"))

    # 警告（配信は止めない。理由を残す）
    if facts.get("official_area_km2") is None:
        R.append(_reason("no_official_direct_area", WARN,
                         "ダム便覧の直接流域が無く、面積の誤差を検証できていない"))
    if facts.get("diversion_status") in ("unknown", "yes"):
        R.append(_reason("diversion_" + facts["diversion_status"], WARN,
                         "導水の有無が未確認" if facts["diversion_status"] == "unknown"
                         else "導水あり（地形上の集水域と実際の集水範囲は一致しない）"))
    if facts.get("outlet_method") and facts["outlet_method"].startswith("snap"):
        R.append(_reason("no_reservoir_surface", WARN,
                         "貯水池の水面を検出できず、河道スナップで出口を決めている"))

    blocked = [r for r in R if r["severity"] == BLOCK]
    return {
        "status": "hold" if blocked else "pass",
        "reasons": R,
        "block_codes": [r["code"] for r in blocked],
    }
