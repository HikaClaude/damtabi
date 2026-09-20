#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域「雨の行き先」の配信データを、**1基ずつ安全に**作る（単発・ローカル専用）。

build_basins.py が作った集水域ポリゴン（data/basins/）と標高タイルから、ダムごとに
D8 流向の格子を作り、品質判定（scripts/watershed_qa.py）に通し、合格したものだけを
docs/watershed/ に配信する。ブラウザ側は

  ・任意の地点から雨滴の経路をトレースする
  ・集水域内に雨を降らせて流れを描く

ために、この格子を「押したときだけ」読む。標高タイルはブラウザへ送らない。

使い方
------
  python scripts/build_flowgrids.py --id toyama-usunaka          # 1基だけ作り直す
  python scripts/build_flowgrids.py --pref toyama --offline --evaluate --report out.json
                                                                 # 何も書かず判定だけ見る
  python scripts/build_flowgrids.py --assemble-only              # 状態から索引を組み直すだけ

対象を指定しないと何もしない（全件へ落とさない）。公開はこのスクリプトの仕事ではない
（commit・push しない）。

失敗しても既存の配信物を壊さない
--------------------------------
- 標高タイルを取れない（通信障害）→ そのダムは何も書かない。既存はそのまま。exit 1。
- QA 不合格（hold）→ 新しい結果は配信しない。配信中の版があれば残す。exit 0（結果であって失敗ではない）。
- 書き出しは一時ファイル経由。索引は最後に書く。

国土地理院の標高データから作った派生物であり、配信の可否は inquiry/ の回答に基づく。
格子は表示演出用に粗くしてある（最大約600セル四方）。面積や流路の細部は
basins.geojson（30m相当）より粗い。概略であることを画面に明記する前提。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_basins as bb  # noqa: E402  タイル取得・地形解析を再利用する
import dem_tiles  # noqa: E402
import watershed_qa as wq  # noqa: E402
import ws_common as wc  # noqa: E402
import ws_pipeline as wp  # noqa: E402

# 互換の再エクスポート（README や旧手順が参照する名前）
EXCLUDED = wp.EXCLUDED
coarsen = wp.coarsen
pack4, pack1, point_in_poly = wc.pack4, wc.pack1, wc.point_in_poly
MAX_SIDE = wp.MAX_SIDE
MAX_GRID_DRIFT_PCT = wq.THRESHOLDS["grid_drift_pct"]
MAX_AREA_ERROR_PCT = wq.THRESHOLDS["area_error_pct"]
MAX_UNREACHED_PCT = wq.THRESHOLDS["unreached_pct"]

# 配信する範囲（既定）。ここに無いダムは「未生成」で、入口ボタンも出ない。
DEFAULT_PREF = "toyama"

REPORT_DEFAULT = ROOT / "data" / "watershed" / "qa_report.json"


def report_row(dam_id, name, latest, published):
    sp = (latest or {}).get("spatial") or {}
    return {
        "id": dam_id, "name": name,
        "status": (latest or {}).get("status"),
        "published": published,
        "block_codes": (latest or {}).get("block_codes") or [],
        "warn_codes": [r["code"] for r in ((latest or {}).get("reasons") or []) if r["severity"] == wq.WARN],
        "reasons": (latest or {}).get("reasons") or [],
        "flow_version": (latest or {}).get("flow_version"),
        "spatial": sp,
        "dem": (latest or {}).get("dem"),
        "facts": (latest or {}).get("facts"),
    }


def print_table(rows):
    nan = float("nan")
    print(f"{'dam_id':<27}{'判定':<6}{'IoU':>6}{'輪外%':>7}{'輪のみ%':>8}{'経路外%':>8}{'未到達%':>8}  理由")
    for r in rows:
        s = r["spatial"] or {}
        print(f"{r['id']:<27}{(r['status'] or '-'):<6}"
              f"{s.get('iou', nan):>6.3f}{s.get('mask_far_outside_pct', nan):>7.1f}"
              f"{s.get('ring_far_only_pct', nan):>8.1f}{s.get('path_outside_pct', nan):>8.1f}"
              f"{s.get('unreached_pct', nan):>8.1f}  {','.join(r['block_codes'])}")


def write_report(path: Path, rows, mode: str, extra=None, stamp: bool = True):
    """判定レポート。stamp=False なら時刻を入れない（内容が同じなら同じバイト列＝無関係な差分を出さない）。"""
    doc = {
        **({"generated": time.strftime("%Y-%m-%dT%H:%M:%S+09:00")} if stamp else {}),
        "pipeline": wp.PIPELINE, "mode": mode,
        "thresholds": wq.THRESHOLDS,
        "counts": {"pass": sum(1 for r in rows if r["status"] == "pass"),
                   "hold": sum(1 for r in rows if r["status"] == "hold"),
                   "total": len(rows)},
        "dams": rows,
    }
    if extra:
        doc.update(extra)
    text = (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    if path.exists() and path.read_bytes() == text:
        return
    dem_tiles.atomic_write_bytes(path, text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="集水域の流向格子を1基ずつ作り、QAに通して、合格したものだけ配信物にする（単発）。"
                    "対象の指定は build_basins.py と同じ。既定は何もしない。")
    ap.add_argument("--pref", help="県で絞る。dam_id の接頭辞（カンマ区切り）")
    ap.add_argument("--id", help="dam_id で絞る。カンマ区切り")
    ap.add_argument("--only", help="ダム名の部分一致で絞る（同名に注意）")
    ap.add_argument("--all", action="store_true", help="dams.json の全基を対象にする")
    ap.add_argument("--yes", action="store_true", help="確認を省く")
    ap.add_argument("--evaluate", action="store_true",
                    help="判定だけ行い、docs・状態には何も書かない（--report だけ書く）")
    ap.add_argument("--report", type=Path, help="判定結果の JSON の書き出し先")
    ap.add_argument("--assemble-only", action="store_true",
                    help="標高を使わず、状態から索引・ポリゴンを組み直す")
    ap.add_argument("--prune-orphans", action="store_true",
                    help="状態の無い配信ファイル（旧実装の成果）を削除する。既定は止めて報告")
    ap.add_argument("--basins-dir", type=Path, default=ROOT / "data" / "basins",
                    help="build_basins.py の出力先（既定 data/basins）。別方式の輪を評価するときに使う")
    ap.add_argument("--unpublish", help="dam_id を配信から外す（格子ファイルも削除。状態には残る）")
    bb.add_source_args(ap)
    args = ap.parse_args(argv)

    data = json.loads((ROOT / "docs" / "data" / "dams.json").read_text(encoding="utf-8"))
    dams = data["dams"]
    by_id = {d["id"]: d for d in dams}
    store = wp.Store(ROOT)

    if args.unpublish:
        if args.unpublish not in by_id:
            print(f"dams.json に無い dam_id: {args.unpublish}")
            return 2
        st = store.load_state(args.unpublish)
        if not st or not st.get("published"):
            print(f"{args.unpublish} は配信していません。")
            return 0
        st["published"] = None
        store.write_state(args.unpublish, st)
        fp = store.flow_path(args.unpublish, True)
        if fp.exists():
            fp.unlink()
        args.assemble_only = True
        print(f"{args.unpublish} を配信から外しました（状態は残してあります）。")

    if args.assemble_only:
        try:
            plan = wp.assemble(store, dams, write=True, prune_orphans=args.prune_orphans)
        except wp.PipelineError as e:
            print(f"組み立てを中止しました（何も書いていません）: {e}")
            return 3
        print(f"配信 {len(plan['published_ids'])} 基 / 索引版 {plan['index_version']} / "
              f"ポリゴン版 {plan['basins_version']} / 書き換え: {plan['written'] or 'なし'}")
        return 0

    if not (args.pref or args.id or args.only or args.all):
        print("対象が指定されていません。--pref / --id / --only / --all のいずれかを付けてください。")
        return 2
    if args.id or args.only:
        args.pref = None          # 個別指定を優先する（県の既定を足さない）
    try:
        targets = bb.select_targets(dams, args)
    except bb.UnknownTarget as e:
        print(f"対象を決められません: {e}")
        return 2
    if not targets:
        print("指定に一致するダムがありません。全件へは切り替えません。")
        print(f"  指定: pref={args.pref!r} id={args.id!r} only={args.only!r}")
        return 2

    spec = bb.load_spec(set(by_id))
    if args.basins_dir != ROOT / "data" / "basins" and not args.evaluate:
        print("--basins-dir を既定以外にできるのは --evaluate のときだけです（配信物に別の輪を混ぜない）。")
        return 2
    metas = {m["id"]: m for m in json.loads(
        (args.basins_dir / "basins_meta.json").read_text(encoding="utf-8"))}
    gj = json.loads((args.basins_dir / "basins.geojson").read_text(encoding="utf-8"))
    features = {f["properties"]["id"]: f for f in gj["features"]}

    print(f"対象 {len(targets)} 基 / dams.json の全 {len(dams)} 基中"
          + ("  [offline]" if args.offline else "") + ("  [evaluate: 何も書かない]" if args.evaluate else ""))
    for d in targets[:12]:
        print(f"  - {d['id']}  {d['name']}")
    if len(targets) > 12:
        print(f"  … ほか {len(targets) - 12} 基")
    if len(targets) > 30 and not args.yes:
        print(f"\n{len(targets)} 基は標高タイルを大量に取得します。--yes を付けて実行してください。")
        return 2

    bb.setup_source(args)
    rows, failures = [], []
    t_all = time.time()
    for n, d in enumerate(targets, 1):
        did = d["id"]
        m = metas.get(did)
        f = features.get(did)
        if not m or "computed_area_km2" not in m or not f:
            failures.append((did, "no_basin", "data/basins に集水域の計算結果がありません（先に build_basins.py）"))
            print(f"[{n}/{len(targets)}] {did}: 失敗（集水域の計算結果なし）→ 既存は変更しません")
            continue
        ring = f["geometry"]["coordinates"][0]
        t0 = time.time()
        try:
            z = m["zoom"]
            pad = int((m["tiles"] ** 0.5 - 1) / 2)
            trep: dict = {}
            dem, x0, y0 = bb.build_grid(d["lat"], d["lon"], z, pad, report=trep)
            gen = wp.generate(d, m, spec.get(did), dem, x0, y0, trep["tiles"])
            del dem
            judged = wp.judge(gen, ring, did)
        except dem_tiles.DemFetchError as e:
            failures.append((did, "dem_fetch", str(e)))
            print(f"[{n}/{len(targets)}] {did}: 失敗（標高タイル: 通信障害など）{e} → 既存は変更しません")
            continue
        except Exception as e:                                   # 想定外。書かずに続ける
            failures.append((did, "exception", f"{type(e).__name__}: {e}"))
            print(f"[{n}/{len(targets)}] {did}: 失敗（{type(e).__name__}: {e}）→ 既存は変更しません")
            continue

        gate = judged["gate"]
        latest = {"status": gate["status"], "block_codes": gate["block_codes"], "reasons": gate["reasons"],
                  "flow_version": judged["rec"]["version"], "spatial": judged["spatial"],
                  "dem": gen["facts"]["dem"],
                  "facts": {k: v for k, v in gen["facts"].items() if k != "dem"}}
        outcome = "(evaluate)"
        published = False
        if not args.evaluate:
            outcome = wp.apply_result(store, d, judged, gen, ring, f["properties"])["outcome"]
            st = store.load_state(did)
            published = bool(st and st.get("published"))
        rows.append(report_row(did, d["name"], latest, published))
        sp = judged["spatial"]
        print(f"[{n}/{len(targets)}] {did:<27} {gate['status']:<4} IoU {sp['iou']:.3f} "
              f"面積 {gen['index']['area_km2']:.1f}km² {time.time()-t0:.0f}秒  {outcome}"
              + (("  ※" + ",".join(gate["block_codes"])) if gate["block_codes"] else ""))

    print()
    print_table(rows)
    rc = 0
    if not args.evaluate:
        try:
            plan = wp.assemble(store, dams, write=True, prune_orphans=args.prune_orphans)
            print(f"\n配信 {len(plan['published_ids'])} 基 / 索引版 {plan['index_version']} / "
                  f"ポリゴン版 {plan['basins_version']} / 書き換え: {plan['written'] or 'なし'}")
        except wp.PipelineError as e:
            print(f"\n組み立てを中止しました（索引・ポリゴンは書いていません）: {e}")
            rc = 3
    fails = [{"id": i, "kind": k, "message": m} for i, k, m in failures]
    if args.evaluate:
        # 今回評価した基だけの報告（状態・既定のレポートには触れない）
        if args.report:
            write_report(args.report, rows, "evaluate", {"failures": fails})
            print(f"判定レポート: {args.report}")
    else:
        # 既定のレポートは「全ダムの直近の判定」。失敗した回でも、他のダムの記録を消さない
        order = {d["id"]: i for i, d in enumerate(dams)}
        rows_all = [report_row(i, s.get("name"), s.get("latest"), bool(s.get("published")))
                    for i, s in sorted(store.all_states().items(), key=lambda kv: order.get(kv[0], 1 << 30))]
        path = args.report or REPORT_DEFAULT
        write_report(path, rows_all, "state", stamp=False)
        print(f"判定レポート（全{len(rows_all)}基の直近の判定）: {path}")
    print(f"完了 {(time.time()-t_all)/60:.1f} 分 / 合格 {sum(1 for r in rows if r['status']=='pass')} / "
          f"保留 {sum(1 for r in rows if r['status']=='hold')} / 失敗 {len(failures)}")
    if failures:
        print("失敗（既存の配信物は変更していません）:")
        for i, k, m in failures:
            print(f"  - {i} [{k}] {m}")
        return 1 if rc == 0 else rc
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
