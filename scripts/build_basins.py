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
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DAMS_JSON = ROOT / "docs" / "data" / "dams.json"
SPEC_CSV = ROOT / "dam_basin_spec.csv"          # ダム便覧由来の流域面積（直接/間接）
OUT_DIR = ROOT / "data" / "basins"
CACHE = ROOT / "cache" / "dem"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://maps.gsi.go.jp/"}

TILE = "dem"                 # 10m メッシュ標高（z14）。低ズームは間引き済みの同系列
SIMPLIFY_M = 30.0            # ポリゴンの簡略化許容誤差
REQUEST_INTERVAL = 0.05      # 相手サーバへの礼儀
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


def get_tile(z: int, x: int, y: int) -> np.ndarray:
    f = CACHE / f"{TILE}_{z}_{x}_{y}.txt"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://cyberjapandata.gsi.go.jp/xyz/{TILE}/{z}/{x}/{y}.txt"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                f.write_bytes(r.read())
        except Exception:
            f.write_text("")          # 海域などは空。次回も取りに行かない
        time.sleep(REQUEST_INTERVAL)
    txt = f.read_text()
    if not txt.strip():
        return np.full((256, 256), np.nan)
    return np.array([[np.nan if v == "e" else float(v) for v in line.split(",")]
                     for line in txt.strip().splitlines()], dtype=np.float32)


def build_grid(lat: float, lon: float, z: int, pad: int):
    cx, cy = tile_xy(lat, lon, z)
    tx, ty = int(cx), int(cy)
    xs = range(tx - pad, tx + pad + 1)
    ys = range(ty - pad, ty + pad + 1)
    rows = [np.hstack([get_tile(z, X, Y) for X in xs]) for Y in ys]
    return np.vstack(rows), tx - pad, ty - pad


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
    h, w = fd.shape
    ws = np.zeros((h, w), dtype=bool)
    ws[oi, oj] = True
    q = deque([(oi, oj)])
    while q:
        i, j = q.popleft()
        for k, (di, dj) in enumerate(D8):
            ni, nj = i - di, j - dj
            if 0 <= ni < h and 0 <= nj < w and not ws[ni, nj] and fd[ni, nj] == k:
                ws[ni, nj] = True
                q.append((ni, nj))
    return ws


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
              verbose=True, sizing_area: float | None = None):
    z, pad = pick_zoom(sizing_area if sizing_area else official)
    outlet_ll = None      # 1回目で決めた出口を緯度経度で固定し、拡大後も同じ点を使う
    for attempt in range(4):
        n_tiles = (2 * pad + 1) ** 2
        t0 = time.time()
        dem, x0, y0 = build_grid(lat, lon, z, pad)
        if not np.isfinite(dem).any():
            return None, {"error": "標高データが取得できませんでした"}

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

        if outlet_ll is not None:
            # 既に出口が決まっているので、同じ地点を新しい格子で指すだけ。
            # ここで選び直すと流向場の変化でスナップ先が隣の川へ移ることがある。
            ox, oy = tile_xy(outlet_ll[0], outlet_ll[1], z)
            si = int((oy - y0) * 256); sj = int((ox - x0) * 256)
            ws = upstream_of(fd, si, sj)
            area = float(ws.sum()) * mpp * mpp / 1e6
            snap_used = 0.0
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

        # 縁に達していたら切れている可能性が高いので広げて再計算。
        # ただし公式面積と既に整合しているなら広げない
        # （広げると流向場が変わり、スナップ先が隣の川に移ることがある。角川ダムで実測）。
        touches = bool(ws[0, :].any() or ws[-1, :].any() or ws[:, 0].any() or ws[:, -1].any())
        if touches and attempt < 3:
            if verbose:
                print(f"    …範囲の端に達したので拡大して再計算 (pad {pad} → {pad + max(1, pad // 2)})")
            pad += max(1, pad // 2)
            continue

        pts = simplify(outline(ws), SIMPLIFY_M / mpp)
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
            "snap_distance_m": round(math.hypot(si - oi, sj - oj) * mpp),
            "snap_radius_m": snap_used,
            "snap_radius_m": snap_used,
            "touches_edge": bool(touches),
            "seconds": round(time.time() - t0, 1),
        }
        if official:
            meta["official_area_km2"] = official
            meta["area_error_pct"] = round((area / official - 1) * 100, 1)
        return coords, meta
    return None, {"error": "範囲を広げても収まりませんでした"}


# ---------------------------------------------------------------- main

def load_spec() -> dict:
    """ダム便覧由来の流域面積（直接/間接）。導水の有無の判定に使う。"""
    import csv
    if not SPEC_CSV.exists():
        return {}
    out = {}
    with SPEC_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out[r["dam_name"]] = r
    return out


def diversion_flag(spec: dict | None) -> dict:
    """導水（間接流域）の有無を機械判定する。判定できないものは未確認とする。"""
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


def main() -> int:
    ap = argparse.ArgumentParser(description="ダムの集水域を標高タイルから計算（単発）")
    ap.add_argument("--only", help="ダム名の部分一致で対象を絞る")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    data = json.loads(DAMS_JSON.read_text(encoding="utf-8"))
    spec = load_spec()
    # 川の防災情報の観測所マスタにも流域面積がある。出典が違うので別枠で記録し、
    # 便覧に記載が無いダムでは格子の大きさを決めるのに使う。
    kawabou = {}
    mc = ROOT / "cache" / "obs_master.json"
    if mc.exists():
        obs = json.loads(mc.read_text(encoding="utf-8")).get("obs", {})
        for d in data["dams"]:
            fcd = (d.get("observation") or {}).get("obs_fcd")
            if fcd and fcd in obs and obs[fcd].get("bsnArea") is not None:
                kawabou[d["name"]] = float(obs[fcd]["bsnArea"])
    dams = data["dams"]
    if args.only:
        dams = [d for d in dams if args.only in d["name"]]

    args.out.mkdir(parents=True, exist_ok=True)
    features, metas = [], []
    t_all = time.time()

    for n, d in enumerate(dams, 1):
        sp = spec.get(d["name"])
        official = None
        if sp and sp.get("total_km2"):
            try:
                official = float(sp["total_km2"])
            except ValueError:
                official = None
        kw = kawabou.get(d["name"])
        sizing = official if official else kw          # 格子の大きさを決めるためだけに使う
        print(f"[{n}/{len(dams)}] {d['name']}（便覧 {official if official else '—'} / "
              f"川防 {kw if kw else '—'} km2）")

        coords, meta = delineate(d["name"], d["lat"], d["lon"], official, sizing_area=sizing)
        if kw:
            meta["kawabou_area_km2"] = kw
            meta["kawabou_error_pct"] = round((meta["computed_area_km2"] / kw - 1) * 100, 1)                 if meta.get("computed_area_km2") else None
        meta["id"] = d["id"]; meta["name"] = d["name"]
        meta["diversion"] = diversion_flag(sp)
        metas.append(meta)

        if coords:
            features.append({
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
            })
            e = meta.get("area_error_pct")
            print(f"    算出 {meta['computed_area_km2']:.2f} km2"
                  + (f" / 誤差 {e:+.1f}%" if e is not None else "")
                  + f" / {meta['vertices']}頂点 / {meta['resolution_m']}m / {meta['seconds']}秒")
        else:
            print(f"    失敗: {meta.get('error')}")

    gj = {"type": "FeatureCollection",
          "properties": {
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
              "method": "国土地理院 標高タイル（DEM10B系）からの地形解析（D8）",
              "accuracy_note": "地形から計算した概略の集水域。実測の流域界ではない。",
              "simplify_m": SIMPLIFY_M,
              "source": "国土地理院 地理院タイル（標高タイル）を加工して作成",
          },
          "features": features}
    (args.out / "basins.geojson").write_text(
        json.dumps(gj, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    (args.out / "basins_meta.json").write_text(
        json.dumps(metas, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    size = (args.out / "basins.geojson").stat().st_size
    print(f"\n出力 {args.out/'basins.geojson'}  {size/1024:.1f} KB / {len(features)} 基")
    print(f"総時間 {(time.time()-t_all)/60:.1f} 分")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
