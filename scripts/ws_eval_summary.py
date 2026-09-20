#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_flowgrids --evaluate の判定レポートを、人間/Astra が見る単位に分類して集計する（読むだけ）。

使い方:
  python scripts/ws_eval_summary.py --report eval80.json --basins-dir <build_basins の出力> --out summary.json --md summary.md

分類（1基につき1つ）
  PASS          QA に合格し、警告も無い
  WARN          QA に合格したが警告がある（配信はできるが理由が残る）
  HOLD          QA の品質ゲートで止まった（面積誤差・空間QA・人が保留 など）
  FAIL          QA は動いたが、データ・処理の整合が取れない（DEM 欠損・端・再計算の食い違い）
  UNEVALUABLE   判定できなかった（集水域が計算できない・標高が取れない・例外）

WARN はさらに「検証材料」で分ける。便覧の直接流域が無い基は面積誤差のゲートが効かないので、
それだけで「検証済み」とは言えない。川の防災情報の流域面積（参考値）との乖離を材料にする。
  official     便覧の直接流域があり、ゲートを通っている
  ref_ok       便覧なし。参考値と ±REF_OK_PCT% 以内
  ref_divergent 便覧なし。参考値と ±REF_OK_PCT% を超えて乖離（参考値は導水込み・集計単位の違いを含み得る）
  no_material  便覧も参考値も無く、面積を検証する材料が無い

これは分類であって、配信の可否を決めるものではない。判定基準（watershed_qa）は変えない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws_evidence as we  # noqa: E402

REF_OK_PCT = we.REF_OK_PCT  # 参考値との一致の目安 ±15%（面積誤差のゲートと同じ既存の値。ここで新設しない）

# 判定は動いたが、データ・処理の整合が取れていない block
INTEGRITY_CODES = {"fine_recompute_mismatch", "dem_edge", "dem_nodata_adjacent",
                   "dem_unverified_empty_tile", "dem_missing_tile"}

PREF_NAMES = {"toyama": "富山", "ishikawa": "石川", "gifu": "岐阜", "fukui": "福井", "nagano": "長野"}


def classify(row: dict, failure: dict | None, meta: dict | None, official) -> dict:
    out = {"id": row["id"], "name": row.get("name"), "pref": row["id"].split("-")[0]}
    sp = row.get("spatial") or {}
    facts = row.get("facts") or {}
    kw_err = (meta or {}).get("kawabou_error_pct")
    kw = (meta or {}).get("kawabou_area_km2")

    if failure:
        out.update(cls="UNEVALUABLE", codes=[failure["kind"]], detail=failure["message"])
        return out

    codes_b, codes_w = row.get("block_codes") or [], row.get("warn_codes") or []
    if codes_b:
        integrity = [c for c in codes_b if c in INTEGRITY_CODES]
        out.update(cls="FAIL" if integrity else "HOLD", codes=codes_b)
    elif codes_w:
        out.update(cls="WARN", codes=codes_w)
    else:
        out.update(cls="PASS", codes=[])

    # 面積を検証する材料
    if facts.get("official_area_km2") is not None or official is not None:
        mat = "official"
    elif kw is None:
        mat = "no_material"
    elif kw_err is not None and abs(kw_err) <= REF_OK_PCT:
        mat = "ref_ok"
    else:
        mat = "ref_divergent"
    out["material"] = mat
    out["metrics"] = {
        "iou": sp.get("iou"), "mask_far_outside_pct": sp.get("mask_far_outside_pct"),
        "ring_far_only_pct": sp.get("ring_far_only_pct"), "path_outside_pct": sp.get("path_outside_pct"),
        "unreached_pct": sp.get("unreached_pct"), "area_error_pct": facts.get("area_error_pct"),
        "grid_drift_pct": facts.get("grid_drift_pct"), "fine_recompute_pct": facts.get("fine_recompute_pct"),
        "computed_km2": (meta or {}).get("computed_area_km2"), "reference_km2": kw,
        "reference_error_pct": kw_err, "outlet_method": facts.get("outlet_method"),
        "diversion": facts.get("diversion_status"),
    }
    out["dem"] = row.get("dem")
    return out


def needs_human(c: dict) -> bool:
    """人間/Astra が個別に見るべきか（判定基準ではなく、確認負荷の見積り用）。"""
    if c["cls"] in ("HOLD", "FAIL", "UNEVALUABLE"):
        return True
    if c["cls"] == "WARN" and c["material"] in ("ref_divergent", "no_material"):
        return True
    return False


def build_evidence_report(cls: list, dams: list, metas: dict, spec: dict, obs: dict | None,
                          profiles: dict | None) -> dict:
    """全ダムの証拠値と評価区分（G1〜G4）。dam_id 基準。公開可否には接続しない。"""
    by_id = {d["id"]: d for d in dams}
    out = []
    for c in cls:
        i = c["id"]
        meta = metas.get(i)
        if meta is None:                       # 集水域が計算できていない基は、証拠を作れない
            ev = None
            group, why = "G4", ["qa_not_passed"]
        else:
            ev = we.build_evidence(by_id[i], meta, spec.get(i), obs, (profiles or {}).get(i))
            group, why = we.evaluation_group(c["cls"], ev)
        out.append({"id": i, "name": c["name"], "pref": c["pref"], "qa_class": c["cls"], "qa_codes": c["codes"],
                    "evaluation_group": group, "group_name": we.GROUPS[group], "group_reasons": why,
                    "evidence": ev})
    counts = Counter(o["evaluation_group"] for o in out)
    by_pref = defaultdict(Counter)
    for o in out:
        by_pref[o["pref"]][o["evaluation_group"]] += 1
    return {
        "evidence_version": we.EVIDENCE_VERSION,
        "note": "評価区分（G1〜G4）は人手監査へ送る量を見積もるための分類で、公開可否・掲載可否には接続しない。"
                "区分の規則が使う数値は既存の ±15%（参考面積との一致の目安）だけ。",
        "ref_ok_pct": we.REF_OK_PCT,
        "groups": {k: {"name": we.GROUPS[k], "count": counts.get(k, 0)} for k in we.GROUPS},
        "by_pref": {p: {k: by_pref[p].get(k, 0) for k in we.GROUPS} for p in by_pref},
        "profiles_collected": bool(profiles),
        "dams": out,
    }


def evidence_markdown(rep: dict) -> str:
    L = [f"# 集水域 証拠層と評価区分（{rep['evidence_version']}）\n", rep["note"] + "\n",
         "| 評価区分 | 名称 | 基数 |\n|---|---|---|"]
    for k, v in rep["groups"].items():
        L.append(f"| {k} | {v['name']} | {v['count']} |")
    L.append("\n## 県別\n\n| 県 | " + " | ".join(rep["groups"]) + " | 計 |\n|---|" + "---|" * (len(rep["groups"]) + 1))
    for p in ("toyama", "ishikawa", "gifu", "fukui", "nagano"):
        if p in rep["by_pref"]:
            b = rep["by_pref"][p]
            L.append(f"| {PREF_NAMES[p]} | " + " | ".join(str(b[k]) for k in rep["groups"]) + f" | {sum(b.values())} |")
    L.append("\n## G4（人手・追加証拠が必要）\n")
    for o in rep["dams"]:
        if o["evaluation_group"] == "G4":
            ev = o["evidence"] or {}
            ref = (ev.get("reference") or {})
            L.append(f"- {o['id']} {o['name']} [{o['qa_class']}] 理由: {','.join(o['group_reasons'])}"
                     + (f" | 参考面積誤差 {ref['error_pct']:+.1f}%" if ref.get("error_pct") is not None else ""))
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--basins-dir", type=Path, required=True)
    ap.add_argument("--dams", type=Path, default=Path(__file__).resolve().parent.parent / "docs/data/dams.json")
    ap.add_argument("--spec", type=Path, default=Path(__file__).resolve().parent.parent / "dam_basin_spec.csv")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--obs-master", type=Path, help="観測所マスタ(obs_master.json)。座標の由来の証拠に使う")
    ap.add_argument("--profiles", type=Path, help="ws_outlet_profile.py の出力。出口周辺の面積プロファイルの証拠に使う")
    ap.add_argument("--evidence-out", type=Path, help="証拠値と評価区分の JSON")
    ap.add_argument("--evidence-md", type=Path, help="証拠層の評価区分の要約")
    args = ap.parse_args()

    rep = json.loads(args.report.read_text(encoding="utf-8"))
    dams = json.loads(args.dams.read_text(encoding="utf-8"))["dams"]
    metas = {m["id"]: m for m in json.loads((args.basins_dir / "basins_meta.json").read_text(encoding="utf-8"))}
    rows = {r["id"]: r for r in rep["dams"]}
    fails = {f["id"]: f for f in rep.get("failures", [])}

    import csv
    official = {}
    with args.spec.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                official[r["dam_id"]] = float(r["direct_km2"] or r["total_km2"])
            except ValueError:
                pass

    cls = []
    for d in dams:
        i = d["id"]
        if i in rows:
            cls.append(classify(rows[i], None, metas.get(i), official.get(i)))
        else:
            f = fails.get(i) or {"kind": "not_evaluated", "message": "判定レポートに無い（集水域の計算結果が無い等）"}
            cls.append(classify({"id": i, "name": d["name"]}, f, metas.get(i), official.get(i)))
        cls[-1]["name"] = d["name"]

    order = ["PASS", "WARN", "HOLD", "FAIL", "UNEVALUABLE"]
    tot = Counter(c["cls"] for c in cls)
    by_pref = defaultdict(Counter)
    for c in cls:
        by_pref[c["pref"]][c["cls"]] += 1
    warn_mat = Counter(c["material"] for c in cls if c["cls"] == "WARN")
    codes = Counter()
    for c in cls:
        for k in c["codes"]:
            codes[(c["cls"], k)] += 1
    review = [c for c in cls if needs_human(c)]
    summary = {
        "total": len(cls), "counts": {k: tot.get(k, 0) for k in order},
        "by_pref": {p: {k: by_pref[p].get(k, 0) for k in order} for p in by_pref},
        "warn_by_verification_material": dict(warn_mat),
        "codes": {f"{k[0]}:{k[1]}": v for k, v in sorted(codes.items())},
        "needs_human_review": {"count": len(review), "ratio_pct": round(len(review) / len(cls) * 100, 1),
                               "ids": [c["id"] for c in review]},
        "thresholds": rep.get("thresholds"), "ref_ok_pct": REF_OK_PCT,
        "dams": cls,
    }
    if args.out:
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if args.evidence_out or args.evidence_md:
        import build_basins as bb
        bb.SPEC_CSV = args.spec
        spec_rows = bb.load_spec({d["id"] for d in dams})          # dam_id 基準・入口の検証つき
        obs = None
        if args.obs_master:
            obs = json.loads(args.obs_master.read_text(encoding="utf-8")).get("obs", {})
        profiles = None
        if args.profiles:
            profiles = json.loads(args.profiles.read_text(encoding="utf-8")).get("profiles", {})
        ev = build_evidence_report(cls, dams, metas, spec_rows, obs, profiles)
        if args.evidence_out:
            args.evidence_out.write_text(json.dumps(ev, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        if args.evidence_md:
            args.evidence_md.write_text(evidence_markdown(ev), encoding="utf-8")

    lines = []
    lines.append(f"# 集水域QA 全{len(cls)}基の分類（outline=exact / evaluate-only）\n")
    lines.append("| 分類 | 基数 |\n|---|---|")
    for k in order:
        lines.append(f"| {k} | {tot.get(k, 0)} |")
    lines.append("\n## 県別\n\n| 県 | PASS | WARN | HOLD | FAIL | UNEVALUABLE | 計 |\n|---|---|---|---|---|---|---|")
    for p in ("toyama", "ishikawa", "gifu", "fukui", "nagano"):
        if p in by_pref:
            b = by_pref[p]
            lines.append(f"| {PREF_NAMES[p]} | " + " | ".join(str(b.get(k, 0)) for k in order) + f" | {sum(b.values())} |")
    lines.append("\n## WARN の検証材料\n")
    for k, v in warn_mat.most_common():
        lines.append(f"- {k}: {v}")
    lines.append("\n## 理由コード（分類別）\n")
    for (c, k), v in sorted(codes.items()):
        lines.append(f"- {c} / {k}: {v}")
    lines.append(f"\n## 個別確認が必要と見積もる基: {len(review)} / {len(cls)}（{summary['needs_human_review']['ratio_pct']}%）\n")
    for c in review:
        m = c.get("metrics") or {}
        lines.append(f"- {c['id']} {c['name']} [{c['cls']}] {','.join(c['codes'])}"
                     + (f" | 参考値乖離 {m.get('reference_error_pct')}%" if m.get("reference_error_pct") is not None else ""))
    text = "\n".join(lines) + "\n"
    if args.md:
        args.md.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
