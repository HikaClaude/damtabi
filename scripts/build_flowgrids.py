#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域の「雨の行き先」試作用に、粗解像度の流向グリッドを作る（単発・ローカル専用）。

build_basins.py と同じ標高タイルキャッシュ（cache/dem）から、ダムごとに
D8 流向の格子を作り、ブラウザ側で
  ・任意の地点から雨滴の経路をトレースする
  ・集水域内に雨を降らせて流れを描く
ために使う。

出力は docs/watershed/ に置く。ここは **.gitignore 対象**。
国土地理院の標高データから作った派生物であり、公開・再配布の可否は
照会（inquiry/gsi-elevation-derivative.md）の回答待ち。

アプリ側はこのデータが「無ければ機能ごと出さない」設計にしてある。
したがってコミットしなければ公開サイトには一切現れない。
照会の回答が出るまで .gitignore の行を外さないこと。

格子は表示演出用に粗くしてある（最大約600セル四方）。面積や流路の細部は
basins.geojson（30m相当）より粗い。概略であることを画面に明記する前提。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_basins as bb  # noqa: E402  タイル取得・地形解析を再利用する

OUT = ROOT / "docs" / "watershed"
MAX_SIDE = 620              # 粗格子の最大一辺。これを超えない最小の間引き係数を選ぶ

# ---------------------------------------------------------------- 公開の可否
#
# 公開する索引に載せないダム。データはローカルに残す（評価と原因調査のため）が、
# docs/watershed/index.json と basins.geojson からは外す。
# アプリは索引に無いダムを一切参照しないので、雨の到達先・下流の案内にも出てこない。
#
# 「注記を出しているから公開してよい」とはしない。原因が分からないものは載せない。
EXCLUDED: dict[str, str] = {
    # 2026-09-18: 境川は原因（出口が貯水池の水面に乗って上流を取りこぼす）が判明し、
    # 貯水池シード方式で −38.7% → −0.3% に収まったため除外を解除した。
    #
    # 2026-09-19: 臼中も除外を解除した。いったん「堤体で切ると水面が2つに割れる」ことを
    # 理由に保留したが、割れた小さい方は 117セル・標高 336.6〜337.5m で、水面（336.0m）や
    # 堤体（337.3m）とほぼ同じ高さだった。下流の川（393m先で 286.8m）ではない。
    # **「2つに割れた」ことは下流の水面を巻き込んだ証拠にならない**（軸平行 161m 四方で
    # 切るため、曲がった細い貯水池では腕が切り落とされる）。この検査は手がかりであって
    # 判定ではない、と check_reservoirs.py 側の文言も直した。
    "toyama-shiraiwagawa":
        "貯水池シードにすると 22.43km²（便覧直接比 −6.5%）から 27.95km²（+16.5%）へ、"
        "誤差が 23pt 悪化する。水面を起点にした他の20基では誤差が縮むか変わらないので、"
        "この1基だけ逆に動いている。**原因は特定できていない。** "
        "「堤体で切ると水面が3つに割れる」「堤体(120.1m)が水位(120.3m)より低い」ことは確認したが、"
        "前者は曲がった貯水池でも起きる（臼中で確認）、後者は正常な20基でも起きる"
        "（徳山 −130.0m など。dams.json の座標が堤体下流側を指すため）。"
        "どちらも下流混入の証拠にならないので、根拠不足のまま公開しない。",
}

# 配信する範囲（既定）。ここに無いダムは「未生成」で、入口ボタンも出ない。
# 集水域ポリゴン（build_basins.py）は dams.json の全基について計算できるが、
# 1基ずつ水面の取り方と輪・雨の一致を確認するまで配信対象にはしない。
# 対象の指定方法は build_basins.py と同じ（--pref / --id / --only / --all）。
DEFAULT_PREF = "toyama"

# 公開に耐える品質の目安。超えたら生成時に警告を出す（自動で除外はしない）。
MAX_GRID_DRIFT_PCT = 15.0   # 粗格子の集水域が細格子ポリゴンからずれてよい上限
MAX_AREA_ERROR_PCT = 15.0   # 直接流域と比べた面積誤差の上限
MAX_UNREACHED_PCT = 10.0    # 雨の範囲のうち、粗格子で出口へ届かないセルの上限


def coarsen(dem: np.ndarray, c: int) -> np.ndarray:
    """c×c ブロック平均（NaN は無視。全 NaN のブロックは NaN のまま）。"""
    h, w = dem.shape
    H, W = h // c, w // c
    d = dem[:H * c, :W * c].reshape(H, c, W, c)
    with np.errstate(invalid="ignore"):
        s = np.nansum(d, axis=(1, 3))
        n = np.isfinite(d).sum(axis=(1, 3))
        out = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    return out.astype(np.float32)


def pack4(a: np.ndarray) -> str:
    """0..15 の配列を 4bit/セルに詰めて base64 にする。"""
    flat = a.astype(np.uint8).ravel()
    if len(flat) % 2:
        flat = np.append(flat, np.uint8(15)).astype(np.uint8)  # append は int64 に昇格するので戻す
    packed = ((flat[0::2] << 4) | flat[1::2]).astype(np.uint8)
    return base64.b64encode(packed.tobytes()).decode()


def pack1(mask: np.ndarray) -> str:
    return base64.b64encode(np.packbits(mask.astype(np.uint8).ravel()).tobytes()).decode()


def point_in_poly(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xin:
                inside = not inside
    return inside


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="集水域の流向格子と配信用の索引を作る（単発）。"
                    "対象の指定は build_basins.py と同じ。既定は富山県。")
    ap.add_argument("--pref", default=DEFAULT_PREF,
                    help="県で絞る。dam_id の接頭辞（カンマ区切り。既定: %s）" % DEFAULT_PREF)
    ap.add_argument("--id", help="dam_id で絞る。カンマ区切り")
    ap.add_argument("--only", help="ダム名の部分一致で絞る")
    ap.add_argument("--all", action="store_true", help="dams.json の全基を対象にする")
    ap.add_argument("--yes", action="store_true", help="確認を省く")
    args = ap.parse_args()
    if args.id or args.only:
        args.pref = None          # 個別指定を優先する（県の既定を足さない）

    data = json.loads((ROOT / "docs" / "data" / "dams.json").read_text(encoding="utf-8"))
    spec = bb.load_spec()
    metas = {m["id"]: m for m in json.loads(
        (ROOT / "data" / "basins" / "basins_meta.json").read_text(encoding="utf-8"))}
    gj = json.loads((ROOT / "data" / "basins" / "basins.geojson").read_text(encoding="utf-8"))
    rings = {f["properties"]["id"]: f["geometry"]["coordinates"][0] for f in gj["features"]}
    areas = {f["properties"]["id"]: f["properties"]["computed_area_km2"] for f in gj["features"]}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "flow").mkdir(exist_ok=True)

    # 下流チェーン: 自分の堤体座標を含む「他の」集水域を面積昇順に並べる。
    # 地形上の包含関係から導いたもの（実河川の管理系統ではない）。
    downstream: dict[str, list[str]] = {}
    for d in data["dams"]:
        hits = [(areas[i], i) for i, ring in rings.items()
                if i != d["id"] and point_in_poly(d["lon"], d["lat"], ring)]
        downstream[d["id"]] = [i for _, i in sorted(hits)]

    # 対象を決める。build_basins と同じ関数を使い、仕組みを二重に持たない。
    targets = bb.select_targets(data["dams"], args)
    if not targets:
        print("指定に一致するダムがありません。全件へは切り替えません。")
        print(f"  指定: pref={args.pref!r} id={args.id!r} only={args.only!r}")
        return 2
    ready = [d for d in targets
             if metas.get(d["id"]) and "computed_area_km2" in metas[d["id"]]]
    print(f"対象 {len(targets)} 基（集水域メタあり {len(ready)} 基）/ "
          f"dams.json の全 {len(data['dams'])} 基中")
    for d in ready[:12]:
        print(f"  - {d['id']}  {d['name']}")
    if len(ready) > 12:
        print(f"  … ほか {len(ready) - 12} 基")
    if len(ready) > 30 and not args.yes:
        print(f"\n{len(ready)} 基は標高タイルを大量に取得します。--yes を付けて実行してください。")
        return 2
    target_ids = {d["id"] for d in ready}

    index = []
    records: dict[str, dict] = {}    # id -> 流向格子（版を決めてから書く）
    routing: dict[str, tuple] = {}   # id -> (粗格子のD8, 出口i, 出口j, x0, y0, z, c)
    skipped: list[str] = []
    t_all = time.time()
    for n, d in enumerate(data["dams"], 1):
        m = metas.get(d["id"])
        if not m or "computed_area_km2" not in m:
            continue
        if d["id"] not in target_ids:
            skipped.append(d["id"])
            continue
        z = m["zoom"]
        pad = int((m["tiles"] ** 0.5 - 1) / 2)

        t0 = time.time()
        dem, x0, y0 = bb.build_grid(d["lat"], d["lon"], z, pad)
        mppf = bb.meters_per_px(d["lat"], z)
        px, py = bb.tile_xy(d["lat"], d["lon"], z)
        fi0 = int((py - y0) * 256)
        fj0 = int((px - x0) * 256)

        # --- 輪と雨を同じ解析結果から作る ------------------------------------
        # 以前は粗格子で独立に集水域を求めていたため、描く輪（細格子）と
        # 雨が降る範囲（粗格子）が別物になっていた（臼中 +59.9% / 有峰 −9.2%）。
        # ここでは build_basins と同じ細格子の集水域を求め、それを粗格子へ
        # 間引いて雨の範囲にする。粗格子の D8 は経路を描くためだけに使う。
        sp0 = spec.get(d["name"]) or {}
        try:
            official = float(sp0.get("direct_km2") or sp0.get("total_km2") or 0) or None
        except ValueError:
            official = None
        ffd = bb.flow_dir(bb.fill_sinks(dem))
        facc = bb.flow_accum(ffd)
        # build_basins とまったく同じ起点の選び方を使う（別の実装を持たない）
        lake, (li, lj), method, _r = bb.pick_outlet(dem, ffd, facc, fi0, fj0, mppf, official)
        wf = (bb.upstream_of_set(ffd, lake) if lake is not None
              else bb.upstream_of(ffd, int(li), int(lj)))
        del ffd, facc

        c = 1
        while max(dem.shape) // c > MAX_SIDE:
            c += 1
        cd = coarsen(dem, c) if c > 1 else dem
        fd = bb.flow_dir(bb.fill_sinks(cd))
        acc = bb.flow_accum(fd)
        mpp = mppf * c
        h, w = fd.shape

        # 細格子の集水域を粗格子へ。ブロックの過半が入っていれば「内側」。
        if c > 1:
            blk = wf[:h * c, :w * c].reshape(h, c, w, c)
            ws = blk.sum(axis=(1, 3)) * 2 >= c * c
        else:
            ws = wf.copy()
        area = float(ws.sum()) * mpp * mpp / 1e6

        # 到着点は「点」ではなく「貯水池の水面」にする。
        # 粗格子では水面が平らなので、1点だけを終点にすると水面のどこで
        # 止まるかが窪地埋めの微小な傾きで決まり、集水域の何割かが
        # 「出口へ届かない」ことになる（境川で 36.2% が届かなかった）。
        # 実際にも雨は堤体の1点ではなく貯水池へ集まる。
        if lake is not None and c > 1:
            lb = lake[:h * c, :w * c].reshape(h, c, w, c)
            outs = lb.any(axis=(1, 3)) & ws
        elif lake is not None:
            outs = lake & ws
        else:
            outs = np.zeros_like(ws)
        if not outs.any():
            # 水面が無い（スナップ方式）ダムは、集水域のうち集水量が最大のセル
            masked = np.where(ws, acc, -1)
            oi_, oj_ = np.unravel_index(int(np.argmax(masked)), masked.shape)
            outs = np.zeros_like(ws)
            outs[oi_, oj_] = True
        # 雨の範囲のうち、粗格子の流れで到着点へ届かないセルの割合。
        # 届かないセルには雨を降らせない（アプリ側が経路なしの滴を捨てる）。
        reach = bb.upstream_of_set(fd, outs)
        unreached = float((ws & ~reach).sum()) / max(1, int(ws.sum())) * 100
        oc = np.argwhere(outs)
        # 代表点（互換のため残す）は到着点のうち集水量が最大のセル
        ci, cj = oc[int(np.argmax([acc[a, b] for a, b in oc]))]

        fine = m["computed_area_km2"]
        drift = (area / fine - 1) * 100 if fine else None

        codes = np.where(fd < 0, 15, fd).astype(np.uint8)
        rec = {
            "id": d["id"], "name": d["name"],
            "zoom": z, "x0": x0, "y0": y0, "coarse": c,
            "w": w, "h": h, "cell_m": round(mpp, 1),
            "outlet": [int(ci), int(cj)],
            # 到着点の集合（貯水池の水面）。ここへ入れば「ダムに着いた」とみなす。
            "outlets": [[int(a), int(b)] for a, b in oc],
            "area_km2": round(area, 2),
            "fine_area_km2": fine,
            "d8": pack4(codes),
            "mask": pack1(ws),
        }
        # 版を確定してから書くので、ここでは持っておくだけ
        records[d["id"]] = rec
        routing[d["id"]] = (fd, int(ci), int(cj), x0, y0, z, c)

        div = m.get("diversion") or {}
        sp = spec.get(d["name"]) or {}

        # 比較の基準は「直接流域」。合計（直接＋間接）と比べると、導水のあるダムで
        # 地形計算が外れているように見える。ダム便覧が内訳を出しているものはそれを使う。
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        direct = num(sp.get("direct_km2"))
        total = num(sp.get("total_km2"))
        indirect = num(sp.get("indirect_km2"))
        err = round((fine / direct - 1) * 100, 1) if direct else None
        # 川の防災情報の流域面積。ダム便覧に記載が無いダムでも比較材料になる。
        # 「公式値」とは別枠で持つ（出典も定義も違うため混ぜない）。
        ref = m.get("kawabou_area_km2")

        index.append({
            "id": d["id"], "name": d["name"], "lat": d["lat"], "lon": d["lon"],
            "area_km2": round(area, 2), "fine_area_km2": fine,
            # official_area_km2 は「直接流域」。合計は official_total_km2 に分ける。
            "official_area_km2": direct,
            "official_total_km2": total,
            "official_indirect_km2": indirect,
            "official_source": "ダム便覧" if direct is not None else None,
            "reference_area_km2": ref,
            "reference_source": "川の防災情報" if ref is not None else None,
            "area_error_pct": err,
            "grid_drift_pct": round(drift, 1) if drift is not None else None,
            "unreached_pct": round(unreached, 1),
            "lake_km2": m.get("lake_km2"),
            "lake_inside_pct": m.get("lake_inside_pct"),
            "outlet_method": m.get("outlet_method"),
            "cell_m": round(mpp, 1),
            "diversion": div,
            "downstream_candidates": downstream.get(d["id"], []),
            "file": f"flow/{d['id']}.json",
        })
        kb = len(rec["d8"]) / 1024
        warn = ""
        if drift is not None and abs(drift) > MAX_GRID_DRIFT_PCT:
            warn += f"  ※輪と雨のずれ {drift:+.1f}%"
        if err is not None and abs(err) > MAX_AREA_ERROR_PCT:
            warn += f"  ※直接流域比 {err:+.1f}%"
        if unreached > MAX_UNREACHED_PCT:
            warn += f"  ※雨が届かない範囲 {unreached:.1f}%"
        print(f"[{len(records):>2}] {d['name']:<10} z{z} c{c} {w}x{h} ({mpp:.0f}m) "
              f"面積 {area:.1f} km2（精算比 {drift:+.1f}%） {kb:.0f}KB "
              f"{time.time()-t0:.0f}秒{warn}")

    # ---- 下流の案内は「包含」だけで確定させない ----
    #
    # ポリゴンに含まれることは候補の抽出でしかない。ここでは実際に、そのダムの
    # 地点から相手の流向格子をたどって相手の出口に届くかを確かめ、届いたものだけを
    # 「地形をたどると入る」と言える範囲として残す。順序は確かめていないので付けない。
    for r in index:
        checked, unverified = [], []
        for cid in r.get("downstream_candidates", []):
            rt = routing.get(cid)
            if not rt:
                unverified.append(cid)
                continue
            fdc, oi_, oj_, x0_, y0_, z_, c_ = rt
            cx, cy = bb.tile_xy(r["lat"], r["lon"], z_)
            i = int((cy - y0_) * 256 / c_)
            j = int((cx - x0_) * 256 / c_)
            ok = False
            if 0 <= i < fdc.shape[0] and 0 <= j < fdc.shape[1]:
                for _ in range(400000):
                    if i == oi_ and j == oj_:
                        ok = True
                        break
                    k = int(fdc[i, j])
                    if k < 0:
                        break
                    i += bb.D8[k][0]
                    j += bb.D8[k][1]
                    if not (0 <= i < fdc.shape[0] and 0 <= j < fdc.shape[1]):
                        break
            (checked if ok else unverified).append(cid)
        r["downstream_reaches"] = checked
        r["downstream_unverified"] = unverified

    # ---- 公開する索引と、ローカル評価用の索引を分ける ----
    #
    # 公開索引に載せないダムは、ポリゴンも下流の案内からも外す。
    # アプリは索引に無いダムのファイルを読まないので、雨の到達先にも現れない。
    pub_ids = {r["id"] for r in index if r["id"] not in EXCLUDED}

    def strip(rec: dict) -> dict:
        r = dict(rec)
        for key in ("downstream_candidates", "downstream_reaches", "downstream_unverified"):
            r[key] = [i for i in r.get(key, []) if i in pub_ids]
        return r

    published = [strip(r) for r in index if r["id"] in pub_ids]
    local_all = [dict(r, excluded_reason=EXCLUDED.get(r["id"])) for r in index]

    # ---- 生成版（内容ハッシュ）----
    # 索引・ポリゴン・格子が同じ生成で作られたことを、利用者側で確かめられるようにする。
    # 同じ SW キャッシュ名や同じ dam_id では「同じ版」を保証できない。
    digest = hashlib.sha256()
    for r in sorted(index, key=lambda x: x["id"]):
        digest.update(r["id"].encode())
        digest.update(records[r["id"]]["d8"].encode())
        digest.update(records[r["id"]]["mask"].encode())
        digest.update(f"{r['fine_area_km2']}:{r['area_km2']}".encode())
    gen = digest.hexdigest()[:12]

    header = {
        "version": gen,
        "generated": time.strftime("%Y-%m-%d"),
        "source": "国土地理院 地理院タイル（標高タイル DEM10B・テキスト形式）を"
                  "DAM TABI が加工して作成",
        "note": "地形から計算した概略の集水域。公式に確定した集水区域や実測の流域界ではない。",
        "excluded": {k: v for k, v in EXCLUDED.items()
                     if k in {r['id'] for r in index}},
    }

    # 格子は版を書き込んでから保存する。索引の file も版付きURLにする。
    local_dir = OUT / "flow" / "_local"
    for did, rec in records.items():
        rec["version"] = gen
        dest = (local_dir if did in EXCLUDED else (OUT / "flow"))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{did}.json").write_text(
            json.dumps(rec, separators=(",", ":")), encoding="utf-8")
    for lst in (published, local_all):
        for r in lst:
            r["file"] = f"flow/{r['id']}.json?v={gen}"

    (OUT / "index.json").write_text(
        json.dumps(dict(header, dams=published), ensure_ascii=False, indent=1),
        encoding="utf-8")
    # ローカル評価用（.gitignore 対象）。除外したダムもここには残す。
    (OUT / "index.local.json").write_text(
        json.dumps(dict(header, dams=local_all), ensure_ascii=False, indent=1),
        encoding="utf-8")

    # 公開するポリゴンは索引に載せたダムだけにする（除外分を配信しない）
    full = json.loads((ROOT / "data" / "basins" / "basins.geojson").read_text(encoding="utf-8"))
    full["features"] = [f for f in full["features"] if f["properties"]["id"] in pub_ids]
    full["version"] = gen
    (OUT / "basins.geojson").write_text(
        json.dumps(full, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    pub_bytes = sum(f.stat().st_size for f in (OUT / "flow").glob("*.json"))
    pub_bytes += (OUT / "basins.geojson").stat().st_size + (OUT / "index.json").stat().st_size
    print(f"\n完了 {(time.time()-t_all)/60:.1f} 分")
    print(f"公開対象 {len(published)} 基 / "
          f"{len(list((OUT/'flow').glob('*.json'))) + 2} ファイル / "
          f"{pub_bytes/1048576:.2f} MB")
    if EXCLUDED:
        print("公開索引から除外（ローカルには残す）:")
        for k, v in EXCLUDED.items():
            print(f"  - {k}: {v}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
