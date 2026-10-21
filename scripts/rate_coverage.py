# -*- coding: utf-8 -*-
"""標準地図（利水貯水率の表示）で貯水率が出るダムの数え方。

docs/app.js の pinValueInfo / val と同じ選択規則で数える。
  1. 公式の利水貯水率（rate_irrigation が status=ok かつ数値）があれば、それを使う
  2. 無ければ参考貯水率（rate_irrigation_calculated が status=ok かつ数値）を使う
  3. どちらも無ければ地図では「—」

0% も数値なので「表示できる」側に数える。公式値と参考値は重複して数えない。
data_status は「ダム全体の取得状態」であって、貯水率が出るかどうかとは別なので使わない。
観測時刻の新しさもここでは見ない（貯水率を表示できるかとは別の話）。
"""
from __future__ import annotations

from typing import Any

# 「—」になった理由（rate_irrigation.status 由来）。表示順もこの並び。
REASON_LABELS = {
    "not_provided": "現在の取得先で利水貯水率が未提供",
    "missing": "欠測（現在の取得先から値が届いていない）",
    "closed": "閉局（現在の取得先の観測所）",
    "no_source": "取得元未設定",
    "fetch_failed": "取得失敗",
}
# 他の取得先の有無は表していない。あくまで今の取得先での状態。
REASON_OTHER = "その他（状態を判別できない）"

# 貯水率以外の現在値。ひとつでも数値があれば「データが一切ない」ではない。
OTHER_VALUE_KEYS = ("rate_effective", "storage_level_m", "storage_capacity_1000m3",
                    "inflow_m3s", "outflow_m3s")


def val(item: Any) -> float | None:
    """app.js の val()：status=ok かつ数値のときだけ値を返す（bool は数値扱いしない）。"""
    if not isinstance(item, dict) or item.get("status") != "ok":
        return None
    v = item.get("value")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return v


def classify(dam: dict, basis: str = "irrigation") -> tuple[str, str | None]:
    """('official'|'reference'|'none', 「—」の理由キー)。

    basis は地図の表示基準（app.js の pinValueInfo と同じ）。
      irrigation … 公式利水貯水率 → 無ければ参考貯水率
      effective  … 有効貯水率のみ（参考値は使わない）
    """
    field = "rate_effective" if basis == "effective" else "rate_irrigation"
    if val(dam.get(field)) is not None:
        return "official", None
    if basis != "effective" and val(dam.get("rate_irrigation_calculated")) is not None:
        return "reference", None
    st = (dam.get(field) or {}).get("status")
    return "none", st if st in REASON_LABELS else "other"


def summarize(dams: list[dict], basis: str = "irrigation") -> dict:
    official = reference = 0
    reasons: dict[str, int] = {}
    dash_with_other_values = 0
    for d in dams:
        kind, reason = classify(d, basis)
        if kind == "official":
            official += 1
        elif kind == "reference":
            reference += 1
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
            if any(val(d.get(k)) is not None for k in OTHER_VALUE_KEYS):
                dash_with_other_values += 1
    dash = sum(reasons.values())
    return {
        "total": len(dams),
        "shown": official + reference,
        "official": official,
        "reference": reference,
        "dash": dash,
        "reasons": reasons,
        "dash_with_other_values": dash_with_other_values,
    }


def reason_lines(s: dict) -> list[tuple[str, int]]:
    out = [(REASON_LABELS[k], s["reasons"][k]) for k in REASON_LABELS if s["reasons"].get(k)]
    if s["reasons"].get("other"):
        out.append((REASON_OTHER, s["reasons"]["other"]))
    return out


def result_lines(s: dict) -> list[str]:
    """公開前サマリ用の行（インデント込み）。"""
    lines = [
        f"    収録         {s['total']}基",
        f"    貯水率あり   {s['shown']}基（標準地図で数字が出る：公式値 {s['official']}基／参考値 {s['reference']}基）",
        f"    貯水率「—」  {s['dash']}基（標準地図で数字が出ない）",
    ]
    for label, n in reason_lines(s):
        lines.append(f"        ・{label}  {n}基")
    if s["dash"]:
        lines.append(f"      このうち {s['dash_with_other_values']}基は水位・流入量など他の値は出ています。")
    return lines


def done_line(s: dict) -> str:
    """公開完了メッセージ用の1行。"""
    return (f"{s['total']}基のうち、標準地図で貯水率が出るのは{s['shown']}基"
            f"（公式値 {s['official']}基／参考値 {s['reference']}基）、"
            f"「—」は{s['dash']}基です。")
