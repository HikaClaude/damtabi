#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""貯水池シードの検出結果を点検し、確認用の画像を書き出す（単発・ローカル専用）。

見たいのは「公式面積に近いか」ではなく、**水面の取り方が妥当か**である。
次の3つを数値で出す。

  1. 水面が堤体をまたいで下流へ伸びていないか の**手がかり**
     （堤体の位置で切ったとき、水面が2つ以上に割れないか）
     ※割れること自体は混入の証拠ではない。曲がった貯水池でも割れる。
  2. 標高帯が近いだけの別の平地・隣接水域を巻き込んでいないか
     （水面の標高幅、広がり、堤体からの距離）
  3. 集水域が水面を丸ごと含んでいるか（含まなければ取りこぼし）

画像は `docs/watershed/_check/<id>.png`（.gitignore 対象・公開しない）。
  水色 = 検出した水面 / 赤の点 = 集水域 / 黄 = 堤体 / 背景 = 標高の陰影
"""

from __future__ import annotations

import io
import json
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_basins as bb  # noqa: E402

OUT = ROOT / "docs" / "watershed" / "_check"
MAX_PX = 900

# 堤体で切ったとき、これ以上のかたまりが2つ以上残れば、水面が堤体をまたいで
# 下流へつながっている疑いが強い
LAKE_PIECE_MIN_CELLS = 30   # これ以上のかたまりを「独立した水面」と数える


def hillshade(dem: np.ndarray) -> np.ndarray:
    d = np.where(np.isfinite(dem), dem, np.nan)
    gy, gx = np.gradient(np.nan_to_num(d, nan=float(np.nanmin(d)) if np.isfinite(d).any() else 0))
    s = np.clip(0.5 - (gx + gy) * 0.05, 0, 1)
    v = (150 + 90 * s).astype(np.uint8)
    out = np.dstack([v, v, v])
    out[~np.isfinite(dem)] = (235, 240, 245)
    return out


def straddles_dam(lake: np.ndarray, oi: int, oj: int, mpp: float):
    """堤体の位置で水面を切ったとき、水面が2つ以上に割れるかを調べる。

    貯水池は堤体で**せき止められている**ので、堤体は水面の端にある。
    端を切り落としても水面はひとつのまま残る。
    もし水面が堤体をまたいで下流の川までつながっていれば、堤体のところで
    切ると「上流の貯水池」と「下流の川」の2つに割れる。

    「標高帯が近いだけの下流の水面を巻き込んでいないか」を、面積の一致に
    頼らずに確かめるための検査。

    返り値: (割れた数, 最大成分の割合%, 2番目の成分のセル数)
    """
    cut = lake.copy()
    r = max(2, int(80.0 / mpp))
    h, w = lake.shape
    cut[max(0, oi - r):oi + r + 1, max(0, oj - r):oj + r + 1] = False

    seen = np.zeros_like(cut)
    sizes = []
    for s in np.argwhere(cut):
        s = (int(s[0]), int(s[1]))
        if seen[s]:
            continue
        q = deque([s])
        seen[s] = True
        n = 0
        while q:
            i, j = q.popleft()
            n += 1
            for di, dj in bb.D8:
                ni, nj = i + di, j + dj
                if 0 <= ni < h and 0 <= nj < w and cut[ni, nj] and not seen[ni, nj]:
                    seen[ni, nj] = True
                    q.append((ni, nj))
        sizes.append(n)
    sizes.sort(reverse=True)
    if not sizes:
        return 0, 0.0, 0
    big = [n for n in sizes if n >= LAKE_PIECE_MIN_CELLS]
    total = sum(sizes)
    return len(big), sizes[0] / total * 100, (sizes[1] if len(sizes) > 1 else 0)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="貯水池シードの検出結果を点検する（dam_id で指定）")
    ap.add_argument("ids", nargs="*", help="dam_id（省略すると、水面を検出できた全基）")
    bb.add_source_args(ap)
    args = ap.parse_args()
    bb.setup_source(args)

    # dam_id で引く（名前では引かない。全国に広げると同名のダムが現れる）
    dams = {d["id"]: d for d in json.loads(
        (ROOT / "docs" / "data" / "dams.json").read_text(encoding="utf-8"))["dams"]}
    metas = {m["id"]: m for m in json.loads(
        (ROOT / "data" / "basins" / "basins_meta.json").read_text(encoding="utf-8"))}

    targets = args.ids or [i for i, m in metas.items() if m.get("outlet_method") == "reservoir"]
    unknown = [i for i in targets if i not in dams]
    if unknown:
        print("dams.json に無い dam_id: " + ", ".join(unknown))
        return 2
    OUT.mkdir(parents=True, exist_ok=True)

    print("%-12s %8s %8s %7s %8s %8s %7s %s" % (
        "ダム", "水面km2", "標高幅m", "堤体m", "分断数", "広がりm", "湖内%", "判定"))
    rows = []
    for did in targets:
        d, m = dams[did], metas.get(did)
        name = d["name"]
        if not m:
            print(f"{did}: 集水域のメタなし")
            continue
        z = m["zoom"]
        pad = int((m["tiles"] ** 0.5 - 1) / 2)
        dem, x0, y0 = bb.build_grid(d["lat"], d["lon"], z, pad)
        mpp = bb.meters_per_px(d["lat"], z)
        px, py = bb.tile_xy(d["lat"], d["lon"], z)
        oj = int((px - x0) * 256)
        oi = int((py - y0) * 256)

        lake, lv = bb.find_reservoir(dem, oi, oj, mpp)
        fd = bb.flow_dir(bb.fill_sinks(dem))
        if lake is None:
            print("%-12s %8s %8s %7s %8s %8s %7s %s" % (
                name[:6], "—", "—", "—", "—", "—", "—", "水面なし（スナップ方式）"))
            continue

        ws = bb.upstream_of_set(fd, lake)
        el = dem[lake]
        ii, jj = np.nonzero(lake)
        span = float(el.max() - el.min())
        near = min(math.hypot(int(a) - oi, int(b) - oj) * mpp for a, b in zip(ii, jj))
        pieces, top_pct, second = straddles_dam(lake, oi, oj, mpp)
        size = max((ii.max() - ii.min() + 1), (jj.max() - jj.min() + 1)) * mpp
        inside = float((lake & ws).sum()) / max(1, int(lake.sum())) * 100
        verdict = "OK"
        if pieces >= 2:
            # 【注意】割れること自体は混入の証拠ではない。軸平行の正方形で切るので、
            # 曲がった細い貯水池では正常でも腕が切り落とされる（臼中で確認）。
            # 分かれた成分の標高が水面と大きく違うときだけ、下流の川の疑いがある。
            verdict = f"要確認: 堤体で{pieces}つに割れる（成分の標高を見ること）"
        elif inside < 99.9:
            verdict = "★水面を取りこぼし"
        rows.append((name, pieces, second, verdict))
        print("%-12s %8.3f %8.2f %7.0f %8d %8.0f %7.1f %s" % (
            name[:6], lake.sum() * mpp * mpp / 1e6, span, near, pieces, size, inside, verdict))

        # ---- 画像 ----
        sel = np.argwhere(ws | lake)
        i0, j0 = sel.min(axis=0)
        i1, j1 = sel.max(axis=0)
        pad_px = 12
        i0 = max(0, i0 - pad_px); j0 = max(0, j0 - pad_px)
        i1 = min(dem.shape[0], i1 + pad_px); j1 = min(dem.shape[1], j1 + pad_px)
        sub = dem[i0:i1, j0:j1]
        step = max(1, int(max(sub.shape) / MAX_PX) + (1 if max(sub.shape) > MAX_PX else 0))
        sub = sub[::step, ::step]
        L = lake[i0:i1, j0:j1][::step, ::step]
        W = ws[i0:i1, j0:j1][::step, ::step]
        img = hillshade(sub)
        img[W] = (0.45 * img[W] + np.array([255, 120, 120]) * 0.55).astype(np.uint8)
        img[L] = (40, 110, 230)
        di, dj = (oi - i0) // step, (oj - j0) // step
        if 0 <= di < img.shape[0] and 0 <= dj < img.shape[1]:
            a0, b0 = max(0, di - 4), max(0, dj - 4)
            img[a0:di + 5, b0:dj + 5] = (255, 210, 0)
        from PIL import Image
        Image.fromarray(img).save(OUT / f"{m['id']}.png")

    print()
    print(f"画像: {OUT}")
    ng = [r for r in rows if r[3] != "OK"]
    if ng:
        print("要確認:", ", ".join(f"{r[0]}（{r[3]}）" for r in ng))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(main())
