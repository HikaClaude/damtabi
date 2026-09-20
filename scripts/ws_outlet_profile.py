#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出口周辺の面積プロファイルを、標高キャッシュだけから測る（読み取り専用・オフライン）。

ダムの座標から半径 r 以内で「集水量が最大のセル」を出口候補とし、その上流面積を測る。
半径を変えたときの面積の動きが、出口の位置に対する集水域の感度（合流の有無など）を示す。
あわせて、半径 150m の候補から下流へ 3km までの面積の増え方も記録する。

- 何も配信しない・build_basins/build_flowgrids の出力を変えない。標高タイルは読むだけ（--offline 固定、
  地理院への通信をしない）。出力は指定した JSON だけ。
- 測定の半径（PROFILE_RADII_M）は格子点であって、判定の閾値ではない。
- 結果は dam_id をキーにする。途中で止まっても、終わった基までは書き出してある（--resume で続きから）。

使い方:
  python scripts/ws_outlet_profile.py --basins-dir <build_basins の出力> --out profiles.json \
      --dem-fallback <読み取り専用の標高キャッシュ> --cache-root <obs_master.json のある cache> --workers 4
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_basins as bb  # noqa: E402
import dem_tiles  # noqa: E402
import ws_evidence as we  # noqa: E402

_CTX: dict = {}


def _init(basins_dir: str, cache_root: str, fallbacks: list[str]) -> None:
    """ワーカーの初期化。offline 固定（通信しない）。"""
    bb.configure_source(Path(cache_root) / "dem", offline=True, fallback_dirs=[Path(p) for p in fallbacks])
    _CTX["dams"] = {d["id"]: d for d in json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))["dams"]}
    _CTX["meta"] = {m["id"]: m for m in json.loads((Path(basins_dir) / "basins_meta.json").read_text(encoding="utf-8"))}


def profile_one(dam: dict, meta: dict, dem: np.ndarray, fd: np.ndarray, acc: np.ndarray,
                mpp: float, oi: int, oj: int) -> dict:
    """1基分の面積プロファイル。dem は使わず、流向・集水量の配列だけで測る（合成データで試験できる）。"""
    radii = []
    cells = {}
    for r_m in we.PROFILE_RADII_M:
        r = max(3, int(r_m / mpp))
        i0, j0 = max(0, oi - r), max(0, oj - r)
        sub = acc[i0:oi + r + 1, j0:oj + r + 1]
        di, dj = np.unravel_index(int(np.argmax(sub)), sub.shape)
        ci, cj = i0 + int(di), j0 + int(dj)
        cells[r_m] = (ci, cj)
        radii.append({"r_m": r_m, "dist_m": round(math.hypot(ci - oi, cj - oj) * mpp),
                      "area_km2": round(int(acc[ci, cj]) * mpp * mpp / 1e6, 2)})
    # 半径 150m の候補から下流へ D8 をたどり、上流面積の増え方を 250m ごとに記録する
    i, j = cells[150]
    down, travelled, nxt = [], 0.0, 0.0
    for _ in range(100000):
        if travelled >= nxt:
            down.append([round(travelled), round(int(acc[i, j]) * mpp * mpp / 1e6, 2)])
            nxt += 250
        k = int(fd[i, j])
        if k < 0:
            break
        di, dj = bb.D8[k]
        travelled += mpp * (1.4142 if di and dj else 1)
        i, j = i + di, j + dj
        if not (0 <= i < fd.shape[0] and 0 <= j < fd.shape[1]) or travelled > 3000:
            break
    return {"mpp_m": round(mpp, 2), "radii": radii, "downstream_km2": down}


def _work(did: str) -> tuple[str, dict]:
    d, m = _CTX["dams"][did], _CTX["meta"][did]
    z, pad = m["zoom"], int((m["tiles"] ** 0.5 - 1) / 2)
    dem, x0, y0 = bb.build_grid(d["lat"], d["lon"], z, pad)
    mpp = bb.meters_per_px(d["lat"], z)
    px, py = bb.tile_xy(d["lat"], d["lon"], z)
    oi, oj = int((py - y0) * 256), int((px - x0) * 256)
    fd = bb.flow_dir(bb.fill_sinks(dem))
    acc = bb.flow_accum(fd)
    return did, profile_one(d, m, dem, fd, acc, mpp, oi, oj)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="出口周辺の面積プロファイル（読み取り専用・オフライン）")
    ap.add_argument("--basins-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache-root", type=Path, default=ROOT / "cache")
    ap.add_argument("--dem-fallback", type=Path, action="append", default=[])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--ids", help="dam_id のカンマ区切り（省略すると basins_meta の全基）")
    ap.add_argument("--resume", action="store_true", help="既存の --out にある基は測り直さない")
    args = ap.parse_args(argv)

    metas = json.loads((args.basins_dir / "basins_meta.json").read_text(encoding="utf-8"))
    ids = [i.strip() for i in args.ids.split(",")] if args.ids else [m["id"] for m in metas]
    known = {m["id"] for m in metas}
    bad = [i for i in ids if i not in known]
    if bad:
        print("basins_meta に無い dam_id: " + ", ".join(bad))
        return 2

    doc = {"radii_m": list(we.PROFILE_RADII_M), "profiles": {}}
    if args.resume and args.out.exists():
        old = json.loads(args.out.read_text(encoding="utf-8"))
        if old.get("radii_m") == doc["radii_m"]:
            doc["profiles"] = old.get("profiles", {})
    todo = [i for i in ids if i not in doc["profiles"]]
    area = {m["id"]: m["computed_area_km2"] for m in metas}
    todo.sort(key=lambda i: -area[i])                # 大きい流域から（負荷の偏りを避ける）
    print(f"対象 {len(ids)} 基 / 未測定 {len(todo)} 基 / workers {args.workers}（offline・通信なし）")

    def save():
        dem_tiles.atomic_write_bytes(
            args.out, (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8"))

    fails = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init,
                             initargs=(str(args.basins_dir), str(args.cache_root),
                                       [str(p) for p in args.dem_fallback])) as ex:
        futs = {ex.submit(_work, i): i for i in todo}
        for n, fu in enumerate(as_completed(futs), 1):
            i = futs[fu]
            try:
                did, prof = fu.result()
                doc["profiles"][did] = prof
                save()                                # 1基終わるごとに書く（途中で止まっても失われない）
                print(f"[{n}/{len(todo)}] {did} 完了 ({(time.time()-t0)/60:.1f}分)", flush=True)
            except dem_tiles.DemFetchError as e:
                fails.append((i, f"dem_unavailable: {e}"))
                print(f"[{n}/{len(todo)}] {i} 失敗（標高キャッシュに無い）: {e}", flush=True)
            except Exception as e:                    # 想定外
                fails.append((i, f"{type(e).__name__}: {e}"))
                print(f"[{n}/{len(todo)}] {i} 失敗: {type(e).__name__}: {e}", flush=True)
    save()
    print(f"完了 {len(doc['profiles'])}/{len(ids)} 基 / 失敗 {len(fails)}")
    for i, m in fails:
        print(f"  - {i}: {m}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
