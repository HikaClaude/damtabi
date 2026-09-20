#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域の「証拠層」: 判定の材料になる観測値を dam_id ごとに記録する（純関数・読むだけ）。

目的: 全国化のとき、正常な群を自動でふるい分け、異常な案件だけを人手監査へ送る。
ここでは**値を記録し、評価区分（G1〜G4）を再現する**ところまで。

してはいけないこと（この層の約束）
  - 公開・配信の可否には接続しない。名前・実装とも「評価区分」であって、掲載区分ではない。
  - 新しい閾値を作らない。区分の規則が使う数値は既存の 参考面積との一致の目安 ±15%
    （ws_eval_summary の REF_OK_PCT。watershed_qa の area_error ±15% と同じ値）だけ。
  - 0.2% 水面比などの値は**記録するだけ**で、合否・区分の条件にしない。
  - 外部資料の調査・推測値の入力はしない。既存データ（dams.json・観測所マスタ・
    集水域の計算結果・dam_basin_spec.csv・出口周辺の走査）から取れるものだけ。

記録する証拠（dam_id 単位）
  reference         参考面積（川の防災情報）と、算出面積との誤差
  coordinate        dams.json の座標が観測所マスタの座標と同一か・ずれ量（座標の真偽の判定はしない）
  outlet_profile    出口周辺（半径ごと）の上流面積と、採用出口から下流の面積の増え方
  method_agreement  貯水池法の面積と、河道法の候補面積の差（半径ごと）
  lake              水面の面積と、集水域に対する比（記録のみ）
  basin_info        直接・間接流域の有無・値・出典・確認状態（dam_basin_spec.csv）
"""

from __future__ import annotations

import math

EVIDENCE_VERSION = "ws-evidence/1"

# 参考面積との一致の目安 ±15%。**既存の値**（watershed_qa.THRESHOLDS["area_error_pct"] と同じ）。
# 評価区分の規則が使う数値はこれだけ。新しい閾値ではない。
REF_OK_PCT = 15.0

# 出口周辺の走査で面積を測る半径（m）。測定の格子点であって、判定の閾値ではない。
PROFILE_RADII_M = (50, 150, 250, 400, 700, 1000)

# 評価区分（公開可否とは無関係。人手監査へ送る量を見積もるための分け方）
GROUPS = {
    "G1": "official_verified",        # 便覧の直接流域があり、QA に合格し、導水の記載なし
    "G2": "official_with_diversion",  # 便覧の直接流域があり、QA に合格したが、導水あり
    "G3": "reference_consistent",     # 便覧なし。参考面積と ±15% 以内で、QA に合格
    "G4": "needs_evidence",           # 上記以外（QA 保留・参考面積と乖離・照合材料なし）
}


# ---------------------------------------------------------------- 個別の証拠

def _offset_m(lat1, lon1, lat2, lon2) -> float:
    dy = (lat2 - lat1) * 111_320.0
    dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def coordinate_evidence(dam: dict, obs_master: dict | None) -> dict:
    """dams.json の座標が、川の防災情報の観測所マスタの座標と同一か。

    座標が正しいかどうかは判定しない（観測所の座標は堤体の位置とは限らない）。
    「観測所座標のままなのか、別の情報で置き換えられているのか」と、そのずれ量だけを残す。
    """
    fcd = (dam.get("observation") or {}).get("obs_fcd")
    st = (obs_master or {}).get(fcd) if fcd else None
    if not st or st.get("lat") is None or st.get("lon") is None:
        return {"class": "no_station_master", "station_fcd": fcd, "offset_m": None}
    off = _offset_m(dam["lat"], dam["lon"], st["lat"], st["lon"])
    same = abs(st["lat"] - dam["lat"]) < 1e-6 and abs(st["lon"] - dam["lon"]) < 1e-6
    return {"class": "identical_to_station_master" if same else "differs_from_station_master",
            "station_fcd": fcd, "offset_m": round(off, 1),
            "station_lat": st["lat"], "station_lon": st["lon"]}


def reference_evidence(meta: dict) -> dict:
    ref = meta.get("kawabou_area_km2")
    comp = meta.get("computed_area_km2")
    err = round((comp / ref - 1) * 100, 1) if (ref and comp) else None
    return {"area_km2": ref, "source": "川の防災情報" if ref is not None else None,
            "computed_km2": comp, "error_pct": err}


def basin_info_evidence(spec_row: dict | None, meta: dict) -> dict:
    """直接・間接流域の情報（dam_basin_spec.csv）。行が無いダムは「未確認」。"""
    def f(k):
        v = ((spec_row or {}).get(k) or "").strip()
        return float(v) if v else None
    status = ((spec_row or {}).get("basin_status") or "").strip() or "unconfirmed"
    div = (meta.get("diversion") or {})
    return {"spec_row_present": spec_row is not None, "status": status,
            "direct_km2": f("direct_km2"), "indirect_km2": f("indirect_km2"), "total_km2": f("total_km2"),
            "source": ((spec_row or {}).get("source") or "").strip() or None,
            "binran_no": ((spec_row or {}).get("binran_no") or "").strip() or None,
            "diversion_status": div.get("status", "unknown")}


def has_official(basin: dict) -> bool:
    """便覧など出典つきの直接（または合計）流域面積があるか。pipeline の official と同じ定義。"""
    return basin["status"] == "confirmed" and (basin["direct_km2"] is not None or basin["total_km2"] is not None)


def lake_evidence(meta: dict) -> dict:
    """水面の面積と集水域に対する比。**記録のみ**（合否・評価区分の条件にしない）。"""
    detected = meta.get("outlet_method") == "reservoir"
    lake, comp = meta.get("lake_km2"), meta.get("computed_area_km2")
    return {"detected": detected, "lake_km2": lake if detected else None,
            "catchment_km2": comp,
            "ratio_pct": round(lake / comp * 100, 3) if (detected and lake is not None and comp) else None,
            "note": "記録のみ。合否・評価区分の条件ではない"}


def profile_evidence(profile: dict | None, meta: dict) -> dict:
    """出口周辺の面積プロファイル（ws_outlet_profile.py の出力）。未取得なら collected=False。"""
    if not profile:
        return {"collected": False}
    areas = [x["area_km2"] for x in profile["radii"]]
    rmap = {x["r_m"]: x["area_km2"] for x in profile["radii"]}

    def ratio(lo, hi):
        v = [a for r, a in rmap.items() if lo <= r <= hi]
        return round(max(v) / min(v), 3) if v and min(v) > 0 else None
    return {"collected": True, "radii_m": [x["r_m"] for x in profile["radii"]],
            "area_km2": areas, "dist_m": [x["dist_m"] for x in profile["radii"]],
            "downstream_km2": profile.get("downstream_km2"),
            "adopted_km2": meta.get("computed_area_km2"), "adopted_method": meta.get("outlet_method"),
            "snap_radius_m": meta.get("snap_radius_m"), "outlet_distance_m": meta.get("outlet_distance_m"),
            # 記録用の比（判定には使わない）
            "max_over_min_150_400": ratio(150, 400), "max_over_min_150_1000": ratio(150, 1000)}


def method_agreement_evidence(profile: dict | None, meta: dict) -> dict:
    """貯水池法の面積と、河道法（座標近傍の最大集水量セル）の候補面積の差を半径ごとに記録する。"""
    if meta.get("outlet_method") != "reservoir":
        return {"applicable": False, "reason": "出口の決め方が河道スナップで、比べる貯水池法の結果が無い"}
    if not profile:
        return {"applicable": True, "collected": False}
    comp = meta.get("computed_area_km2")
    diffs = [round((x["area_km2"] / comp - 1) * 100, 1) if comp else None for x in profile["radii"]]
    return {"applicable": True, "collected": True, "reservoir_km2": comp,
            "radii_m": [x["r_m"] for x in profile["radii"]],
            "snap_candidate_km2": [x["area_km2"] for x in profile["radii"]],
            "diff_pct_by_radius": diffs}


def build_evidence(dam: dict, meta: dict, spec_row: dict | None, obs_master: dict | None,
                   profile: dict | None) -> dict:
    return {
        "reference": reference_evidence(meta),
        "coordinate": coordinate_evidence(dam, obs_master),
        "outlet_profile": profile_evidence(profile, meta),
        "method_agreement": method_agreement_evidence(profile, meta),
        "lake": lake_evidence(meta),
        "basin_info": basin_info_evidence(spec_row, meta),
    }


# ---------------------------------------------------------------- 評価区分

def evaluation_group(qa_class: str, evidence: dict) -> tuple[str, list[str]]:
    """QA の分類（PASS/WARN/HOLD/FAIL/UNEVALUABLE）と証拠から、評価区分（G1〜G4）と理由を返す。

    規則（数値は REF_OK_PCT = ±15% だけ）:
      QA が HOLD / FAIL / UNEVALUABLE                       → G4（qa_not_passed）
      便覧の直接流域あり ＋ 導水あり                         → G2
      便覧の直接流域あり                                     → G1
      便覧なし ＋ 参考面積あり ＋ |誤差| ≤ REF_OK_PCT         → G3
      便覧なし ＋ 参考面積あり ＋ |誤差| > REF_OK_PCT         → G4（reference_divergent）
      便覧なし ＋ 参考面積なし                               → G4（no_verification_material）
    これは分類であって、公開可否・掲載可否ではない。
    """
    if qa_class not in ("PASS", "WARN"):
        return "G4", ["qa_not_passed"]
    basin = evidence["basin_info"]
    if has_official(basin):
        return ("G2" if basin["diversion_status"] == "yes" else "G1"), []
    ref = evidence["reference"]
    if ref["area_km2"] is None or ref["error_pct"] is None:
        return "G4", ["no_verification_material"]
    if abs(ref["error_pct"]) <= REF_OK_PCT:
        return "G3", []
    return "G4", ["reference_divergent"]
