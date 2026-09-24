#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域「雨の行き先」の生成・判定・配信物の組み立て（1基単位）。

build_flowgrids.py（CLI）から使う。標高タイルの取得は build_basins 側にあり、
ここは「標高配列を受け取って配信物を作る」部分だけを持つ。だから合成データで試験できる。

設計（旧実装の問題との対応）
----------------------------
1. **1基単位の差分**: 対象のダムだけを作り直し、他のダムの配信物・状態には触れない。
   旧実装は対象だけで索引とポリゴンを作り直したため、`--id X` で実行すると他のダムが
   索引から消え、版のずれで画面が「再読み込みしてください」になった。
2. **QA不合格は候補にしない**: watershed_qa.evaluate が block を返した新しい結果は
   公開候補に入れない（`data/watershed/staged/held/` へ退避）。既に候補の版があればそれを残す。
3. **版は内容から**: 格子ファイルの版 = 格子レコード全体のハッシュ、ポリゴンの版 = 全
   ポリゴンのハッシュ、索引の版 = 索引全体のハッシュ。索引が各部の期待版を持つ。
4. **ID で処理**: 便覧・川の防災情報・保留・状態ファイルはすべて dam_id で引く。
5. **失敗しても壊さない**: 生成に失敗したダムは何も書かない。書き出しは一時ファイル経由。
6. **公開の境界**: docs/ は GitHub Pages がそのまま配信する。docs/watershed/ に置くのは
   **公開可能なダム**（個別承認・QA pass・輪郭方式 exact・承認時の格子版と輪の版が一致）の
   索引・輪・格子だけ。QA 合格の公開候補は data/watershed/staged/ に置き、公開しない。
   docs/watershed/ に想定外のファイルがあれば、消さずに staged/quarantine/ へ移す。

置き場所
  data/watershed/dams/<dam_id>.json       状態（下記）
  data/watershed/staged/flow/<id>.json    公開候補（QA 合格）の格子
  data/watershed/staged/index.json        公開候補全基の索引（release_status 付き・公開しない）
  data/watershed/staged/basins.geojson    公開候補全基の輪（公開しない）
  data/watershed/staged/held/<id>.json    QA で保留した新しい結果（追跡しない）
  data/watershed/staged/quarantine/       docs/watershed/ から移した想定外のファイル（追跡しない）
  data/watershed/release.json             公開承認（人が1基ずつ書く）
  docs/watershed/{index.json, basins.geojson, flow/<id>.json}   公開可能なダムの分だけ

状態ファイル data/watershed/dams/<dam_id>.json
  published: QA に合格した公開候補の版（索引の項目・輪・輪郭方式・格子の版・その時のQA）。
             無ければ null。キー名は互換のため。**公開してよいかは release_status が決める**
  latest   : 直近に生成した結果とQA判定（hold でも残す）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_basins as bb  # noqa: E402
import dem_tiles  # noqa: E402
import watershed_qa as wq  # noqa: E402
import ws_common as wc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = "ws-pipeline/2"
MAX_SIDE = 620              # 粗格子の最大一辺。これを超えない最小の間引き係数を選ぶ
FINE_RECOMPUTE_LIMIT_PCT = 1.0   # build_basins の面積と、ここで作り直した面積の許容差 %

# 人が「原因不明のため保留」と決めたダム（id → 理由）。QA の合否とは別の理由で止める。
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
    # 2026-09-24: 白岩川の HOLD を人の判断で解除した。保留の理由は「水面起点で面積が便覧比 +16.4%（±15% 超）で、
    # 原因が特定できない」ことで、別の河川につながる等の誤りの証拠は無かった。観光向けの概略として面積差は許容し、
    # 既存の河道起点案（便覧比 −6.5%）を表示する（build_basins.RIVER_OUTLET_ONLY）。画面に
    # 「地形から推定した概略で、実際の集水域と異なる場合があります」と注記する（data/watershed/notes.json）。
    # 面積を合わせるための再計算・調整はしていない。公開承認ではない。

}

# 公開承認（人が1基ずつ記録する）。QA の pass や評価区分 G1〜G4 は承認ではない。
# 承認は「その時の格子・輪・輪の作り方」に結び付ける。作り直せば版が変わり、承認は失効する。
# 公開できる輪郭方式は exact だけ（legacy は輪郭セルを角度順に並べた近似で、細格子の集水域と
# 3〜7% ずれる）。legacy の候補は承認を書いても公開しない（release_status = not_exact）。
# 画面（docs/app.js の releasedDams）も、release_schema が一致し、release_status が approved・
# qa_status が pass・outline_method が exact のダムだけを出す（公開索引の二重確認）。
RELEASE_SCHEMA = "ws-release/1"
RELEASABLE_OUTLINE = "exact"
RELEASE_FILE = Path("data") / "watershed" / "release.json"
RELEASE_KEYS = ("flow_version", "ring_version", "outline_method", "approved_by", "approved_on")
# 個別の保留（人が記録する）。誤りが具体的に疑われる基を、理由を付けて公開から外す。
# 保留は承認より優先する（承認が書かれていても release_status は held）。QA の判定は変えない。
HOLDS_SCHEMA = "ws-holds/1"
HOLDS_FILE = Path("data") / "watershed" / "holds.json"
HOLD_KEYS = ("reason", "since", "by")
# 画面に出す注記（人が記録する）。データから自動では決められない事実だけを書く（例: 位置資料の精度の制約）。
# 面積の照合材料の有無・差は app.js が索引の値から自動で書くので、ここには書かない。
NOTES_SCHEMA = "ws-notes/1"
NOTES_FILE = Path("data") / "watershed" / "notes.json"
NOTE_KEYS = ("text", "basis")

HEADER_SOURCE = ("国土地理院 地理院タイル（標高タイル DEM10B・テキスト形式）を"
                 "DAM TABI が加工して作成")
HEADER_NOTE = "地形から計算した概略の集水域。公式に確定した集水区域や実測の流域界ではない。"


class PipelineError(Exception):
    """配信物の整合が取れない（書き出しを中止する）。"""


# ---------------------------------------------------------------- 粗格子

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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1基の生成

def generate(dam: dict, meta: dict, spec_row: dict | None, dem: np.ndarray,
             x0: int, y0: int, tiles: list[dict] | None = None) -> dict:
    """標高配列から、配信用の流向格子レコードと索引項目・QA用の事実を作る。

    返り値: {"rec": 格子レコード（version なし）, "index": 索引項目（下流・file・v を除く）,
             "facts": QA 用の事実, "routing": 粗格子の D8 と出口}
    輪と雨を同じ解析結果から作る（細格子の集水域を粗格子へ間引いて雨の範囲にする）。
    """
    z = meta["zoom"]
    mppf = bb.meters_per_px(dam["lat"], z)
    px, py = bb.tile_xy(dam["lat"], dam["lon"], z)
    fi0, fj0 = int((py - y0) * 256), int((px - x0) * 256)

    sp0 = spec_row or {}
    official = _num(sp0.get("direct_km2") or sp0.get("total_km2")) or None

    ffd = bb.flow_dir(bb.fill_sinks(dem))
    facc = bb.flow_accum(ffd)
    # build_basins とまったく同じ起点の選び方を使う（別の実装を持たない）
    lake, (li, lj), method, _r = bb.pick_outlet(dem, ffd, facc, fi0, fj0, mppf, official,
                                                  use_reservoir=dam["id"] not in bb.RIVER_OUTLET_ONLY)
    wf = (bb.upstream_of_set(ffd, lake) if lake is not None
          else bb.upstream_of(ffd, int(li), int(lj)))
    del ffd, facc

    # 標高欠損（通信障害ではなく、データ自体の欠け）に関する事実
    touches_edge = bool(wf[0, :].any() or wf[-1, :].any() or wf[:, 0].any() or wf[:, -1].any())
    nodata_adj = int((wc.dilate(wf, 1) & ~wf & ~np.isfinite(dem)).sum())
    dem_facts = {
        "touches_edge": touches_edge,
        "nodata_adjacent_cells": nodata_adj,
        "tiles_touching_catchment": dem_tiles.tiles_touching(wf, tiles or [], dem),
        "tile_counts": _counts(tiles),
        "grid_nan_pct": round(float((~np.isfinite(dem)).mean()) * 100, 2),
    }

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

    # 到着点は「点」ではなく「貯水池の水面」にする（粗格子では水面が平らで、1点だと
    # 集水域の何割かが「出口へ届かない」ことになる。境川で 36.2%）。
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
    oc = np.argwhere(outs)
    ci, cj = oc[int(np.argmax([acc[a, b] for a, b in oc]))]

    fine = meta["computed_area_km2"]
    fine_here = float(wf.sum()) * mppf * mppf / 1e6
    drift = (area / fine - 1) * 100 if fine else None
    codes = np.where(fd < 0, 15, fd).astype(np.uint8)
    rec = {
        "id": dam["id"], "name": dam["name"],
        "zoom": z, "x0": x0, "y0": y0, "coarse": c,
        "w": w, "h": h, "cell_m": round(mpp, 1),
        "outlet": [int(ci), int(cj)],
        # 到着点の集合（貯水池の水面）。ここへ入れば「ダムに着いた」とみなす。
        "outlets": [[int(a), int(b)] for a, b in oc],
        "area_km2": round(area, 2),
        "fine_area_km2": fine,
        "d8": wc.pack4(codes),
        "mask": wc.pack1(ws),
    }

    div = meta.get("diversion") or {}
    direct = _num(sp0.get("direct_km2"))
    ref = meta.get("kawabou_area_km2")
    err = round((fine / direct - 1) * 100, 1) if direct else None
    index = {
        "id": dam["id"], "name": dam["name"], "lat": dam["lat"], "lon": dam["lon"],
        "area_km2": round(area, 2), "fine_area_km2": fine,
        # official_area_km2 は「直接流域」。合計は official_total_km2 に分ける。
        "official_area_km2": direct,
        "official_total_km2": _num(sp0.get("total_km2")),
        "official_indirect_km2": _num(sp0.get("indirect_km2")),
        "official_source": "ダム便覧" if direct is not None else None,
        "reference_area_km2": ref,
        "reference_source": "川の防災情報" if ref is not None else None,
        "area_error_pct": err,
        "grid_drift_pct": round(drift, 1) if drift is not None else None,
        "lake_km2": meta.get("lake_km2"),
        "lake_inside_pct": meta.get("lake_inside_pct"),
        "outlet_method": meta.get("outlet_method"),
        "cell_m": round(mpp, 1),
        "diversion": div,
    }
    facts = {
        "area_error_pct": err, "grid_drift_pct": drift,
        "official_area_km2": direct,
        "diversion_status": div.get("status"),
        "outlet_method": meta.get("outlet_method"),
        "dem": dem_facts,
        "fine_recompute_pct": (round((fine_here / fine - 1) * 100, 2) if fine else None),
        "method_here": method,
    }
    return {"rec": rec, "index": index, "facts": facts, "routing": (fd, int(ci), int(cj))}


def _counts(tiles):
    c = {"ok": 0, "missing": 0, "legacy_empty": 0}
    for t in tiles or []:
        c[t["kind"]] += 1
    return c


def judge(gen: dict, ring, dam_id: str, thresholds: dict | None = None) -> dict:
    """生成結果を、**書き出す文字列と同じ形**（版を付けた格子レコード）で検査して判定する。"""
    rec = dict(gen["rec"])
    rec["version"] = wc.content_version(rec)
    spatial = wq.spatial_metrics(rec, ring)
    facts = dict(gen["facts"])
    res = wq.evaluate(spatial, facts, thresholds, manual_hold=EXCLUDED.get(dam_id))
    # 生成時の作り直しと build_basins の面積が食い違うなら、輪と雨の元が別物
    fr = facts.get("fine_recompute_pct")
    if fr is not None and abs(fr) > FINE_RECOMPUTE_LIMIT_PCT:
        res["reasons"].append(wq._reason(
            "fine_recompute_mismatch", wq.BLOCK,
            f"build_basins の集水域面積と、ここで作り直した面積が {fr:+.2f}% 食い違う",
            fr, FINE_RECOMPUTE_LIMIT_PCT))
        res["status"] = "hold"
        res["block_codes"].append("fine_recompute_mismatch")
    return {"rec": rec, "spatial": spatial, "gate": res}


# ---------------------------------------------------------------- 状態と配信物

class Store:
    """状態・公開候補（data/watershed/）と公開物（docs/watershed/）の読み書き。1ダムずつアトミックに書く。"""

    PUBLIC_FILES = ("index.json", "basins.geojson")   # docs/watershed/ 直下に置いてよいもの（＋flow/<id>.json）

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.out = self.root / "docs" / "watershed"            # 公開（GitHub Pages が配信する）
        self.out_flow = self.out / "flow"
        self.stage = self.root / "data" / "watershed" / "staged"   # 公開候補（公開しない）
        self.flow = self.stage / "flow"
        self.local = self.stage / "held"
        self.quarantine = self.stage / "quarantine"
        self.state = self.root / "data" / "watershed" / "dams"

    # ---- 状態
    def state_path(self, dam_id: str) -> Path:
        return self.state / f"{dam_id}.json"

    def load_state(self, dam_id: str) -> dict | None:
        p = self.state_path(dam_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def all_states(self) -> dict:
        if not self.state.exists():
            return {}
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.state.glob("*.json"))}

    @staticmethod
    def _dump(obj, indent=None) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=indent,
                          separators=(",", ":") if indent is None else None) + "\n"

    def write_state(self, dam_id: str, st: dict) -> None:
        dem_tiles.atomic_write_bytes(self.state_path(dam_id), self._dump(st, indent=1).encode("utf-8"))

    # ---- 格子（published=True は公開候補の格子、False は QA 保留の退避。どちらも公開しない）
    def flow_path(self, dam_id: str, published: bool) -> Path:
        return (self.flow if published else self.local) / f"{dam_id}.json"

    def write_flow(self, dam_id: str, rec: dict, published: bool) -> None:
        text = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        dem_tiles.atomic_write_bytes(self.flow_path(dam_id, published), text.encode("utf-8"))

    def read_flow(self, dam_id: str, published: bool = True) -> dict | None:
        p = self.flow_path(dam_id, published)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    # ---- 公開承認
    def load_release(self) -> dict:
        """承認記録（dam_id → 承認）。ファイルが無ければ「承認なし」。形が違えば PipelineError。"""
        p = self.root / RELEASE_FILE
        if not p.exists():
            return {}
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != RELEASE_SCHEMA:
            raise PipelineError(f"{RELEASE_FILE}: schema は {RELEASE_SCHEMA!r}: {doc.get('schema')!r}")
        approvals = doc.get("approvals")
        if not isinstance(approvals, dict):
            raise PipelineError(f"{RELEASE_FILE}: approvals は dam_id をキーにした object")
        for did, a in approvals.items():
            missing = [k for k in RELEASE_KEYS if not (isinstance(a, dict) and a.get(k))]
            if missing:
                raise PipelineError(f"{RELEASE_FILE}: {did} に {', '.join(missing)} がありません")
        return approvals


    def load_holds(self) -> dict:
        """個別の保留（dam_id → {reason, since, by}）。ファイルが無ければ保留なし。形が違えば PipelineError。"""
        p = self.root / HOLDS_FILE
        if not p.exists():
            return {}
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != HOLDS_SCHEMA:
            raise PipelineError(f"{HOLDS_FILE}: schema は {HOLDS_SCHEMA!r}: {doc.get('schema')!r}")
        holds = doc.get("holds")
        if not isinstance(holds, dict):
            raise PipelineError(f"{HOLDS_FILE}: holds は dam_id をキーにした object")
        for did, h in holds.items():
            missing = [k for k in HOLD_KEYS if not (isinstance(h, dict) and h.get(k))]
            if missing:
                raise PipelineError(f"{HOLDS_FILE}: {did} に {', '.join(missing)} がありません")
        return holds


    def load_notes(self) -> dict:
        """画面に出す注記（dam_id → {text, basis}）。ファイルが無ければ注記なし。形が違えば PipelineError。"""
        p = self.root / NOTES_FILE
        if not p.exists():
            return {}
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != NOTES_SCHEMA:
            raise PipelineError(f"{NOTES_FILE}: schema は {NOTES_SCHEMA!r}: {doc.get('schema')!r}")
        notes = doc.get("notes")
        if not isinstance(notes, dict):
            raise PipelineError(f"{NOTES_FILE}: notes は dam_id をキーにした object")
        for did, n in notes.items():
            missing = [k for k in NOTE_KEYS if not (isinstance(n, dict) and n.get(k))]
            if missing:
                raise PipelineError(f"{NOTES_FILE}: {did} に {', '.join(missing)} がありません")
        return notes


def ring_version(ring) -> str:
    """輪（経緯度の列）の内容ハッシュ。承認を輪の形に結び付けるために使う。"""
    return wc.content_version({"ring": ring}, exclude=())


def release_status(pub: dict, approval: dict | None, held: dict | None = None) -> str:
    """公開候補1基の公開可否。approved だけが公開可能。

    held      : 人が個別に保留している（data/watershed/holds.json）。承認より優先する
    not_exact : 輪郭方式が exact でない（legacy・不明）。承認があっても公開しない
    unapproved: 承認の記録が無い
    stale     : 承認後に作り直した（格子版・輪の版・輪郭方式のどれかが承認時と違う）
    approved  : 承認が今の格子・輪・輪郭方式（exact）と一致
    qa は pass が前提（公開候補は QA 合格の版だけ）。
    """
    if held:
        return "held"
    if pub.get("outline_method") != RELEASABLE_OUTLINE:
        return "not_exact"
    if not approval:
        return "unapproved"
    same = (approval["flow_version"] == pub["flow_version"]
            and approval["ring_version"] == ring_version(pub["ring"])
            and approval["outline_method"] == RELEASABLE_OUTLINE)
    return "approved" if same else "stale"


RELEASE_STATUSES = ("approved", "stale", "unapproved", "not_exact", "held")


def apply_result(store: Store, dam: dict, judged: dict, gen: dict, ring, feature_props: dict,
                 outline_method: str | None = None) -> dict:
    """1基分の結果を書き出す。返り値は報告用の要約。**他のダムには触れない。**

    pass : 公開候補の staged/flow/<id>.json を置き換え、published を更新する（公開はしない）
    hold : 新しい結果は staged/held/<id>.json へ。published はそのまま（あれば残す）
    """
    did = dam["id"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    st = store.load_state(did) or {"id": did, "name": dam["name"], "published": None}
    rec = judged["rec"]
    gate = judged["gate"]
    latest = {
        "status": gate["status"], "block_codes": gate["block_codes"], "reasons": gate["reasons"],
        "flow_version": rec["version"], "spatial": judged["spatial"],
        "dem": gen["facts"]["dem"], "generated": now, "pipeline": PIPELINE,
        "facts": {k: v for k, v in gen["facts"].items() if k != "dem"},
    }
    st["name"] = dam["name"]
    st["latest"] = latest
    if gate["status"] == "pass":
        store.write_flow(did, rec, published=True)
        lp = store.flow_path(did, published=False)
        if lp.exists():
            lp.unlink()                      # 合格したので、保留時の退避は要らない
        st["published"] = {
            "flow_version": rec["version"],
            "index": gen["index"],
            "ring": ring,
            # build_basins の --outline（legacy / exact）。不明なら null のまま（承認できない）
            "outline_method": outline_method,
            "feature_properties": feature_props,
            "qa": {"warn_codes": [r["code"] for r in gate["reasons"] if r["severity"] == wq.WARN],
                   "spatial": judged["spatial"]},
            "generated": now, "pipeline": PIPELINE,
        }
        outcome = "published"
    else:
        store.write_flow(did, rec, published=False)
        prev = st.get("published")
        outcome = ("held (previous published version kept: %s)" % prev["flow_version"]) if prev \
            else "held (not published)"
    store.write_state(did, st)
    return {"id": did, "status": gate["status"], "outcome": outcome,
            "block_codes": gate["block_codes"], "flow_version": rec["version"]}


# ---------------------------------------------------------------- 組み立て

def _routing_reaches(fd, goal_cells, i, j) -> bool:
    goals = set(map(tuple, goal_cells))
    h, w = fd.shape
    for _ in range(400000):
        if (i, j) in goals:
            return True
        k = int(fd[i, j])
        if k == 15 or k < 0 or k > 7:
            return False
        i += wc.D8[k][0]
        j += wc.D8[k][1]
        if not (0 <= i < h and 0 <= j < w):
            return False
    return False


def _index_records(ids, pub, flows, approvals, holds=None, notes=None):
    """ids のダムの索引レコードとポリゴンを作る。下流の案内は ids の中だけで探す
    （公開索引が、公開していないダムを下流として名指ししないように）。"""
    areas = {i: pub[i]["index"]["fine_area_km2"] for i in ids}
    rings = {i: pub[i]["ring"] for i in ids}
    routing = {}
    for i in ids:
        r = flows[i]
        h, w = r["h"], r["w"]
        routing[i] = (wc.unpack4(r["d8"], h, w), r["outlets"], r["x0"], r["y0"], r["zoom"], r["coarse"])
    dams_out = []
    for i in ids:
        rec = dict(pub[i]["index"])
        # 下流の案内: 「包含」は候補。実際に流向格子をたどって出口に届いたものだけ確定
        cands = [k for _, k in sorted((areas[k], k) for k in ids
                                      if k != i and wc.point_in_poly(rec["lon"], rec["lat"], rings[k]))]
        ok, un = [], []
        for cid in cands:
            fd, goals, x0, y0, z, c = routing[cid]
            cx, cy = bb.tile_xy(rec["lat"], rec["lon"], z)
            ii, jj = int((cy - y0) * 256 / c), int((cx - x0) * 256 / c)
            reach = (0 <= ii < fd.shape[0] and 0 <= jj < fd.shape[1]
                     and _routing_reaches(fd, goals, ii, jj))
            (ok if reach else un).append(cid)
        rec["downstream_candidates"] = cands
        rec["downstream_reaches"] = ok
        rec["downstream_unverified"] = un
        # 雨が届かない割合。配信した d8 から測り直した値（状態の qa に保存してある）
        rec["unreached_pct"] = round(pub[i]["qa"]["spatial"]["unreached_pct"], 1)
        rec["v"] = pub[i]["flow_version"]
        rec["file"] = f"flow/{i}.json?v={pub[i]['flow_version']}"
        # 公開候補は QA に合格した版だけなので qa_status は常に pass。
        # 公開してよいかは release_status が決める（QA 合格・評価区分とは別）。
        rec["qa_status"] = "pass"
        rec["outline_method"] = pub[i].get("outline_method")
        rec["release_status"] = release_status(pub[i], approvals.get(i), (holds or {}).get(i))
        if (holds or {}).get(i):
            rec["hold_reason"] = holds[i]["reason"]          # 公開索引には載らない（held は公開しない）
        if (notes or {}).get(i):
            rec["caution"] = notes[i]["text"]                # 画面の注記（根拠は notes.json の basis）
            if notes[i].get("label"):
                rec["caution_label"] = notes[i]["label"]      # 注記の見出し（無ければ画面側の既定）
        dams_out.append(rec)
    # ポリゴン: ids の輪だけ
    features = [{"type": "Feature", "properties": pub[i]["feature_properties"],
                 "geometry": {"type": "Polygon", "coordinates": [pub[i]["ring"]]}} for i in ids]
    return dams_out, features


def _index_doc(dams_out, features, excluded):
    """索引の本体。ポリゴンの版は features 全体の内容ハッシュ、索引の版は索引全体の内容ハッシュ。"""
    basins_version = wc.content_version({"features": features}, exclude=())
    methods = sorted({str(d["outline_method"]) for d in dams_out})
    index = {
        "version": None, "generated": time.strftime("%Y-%m-%d"),
        "source": HEADER_SOURCE, "note": HEADER_NOTE,
        "pipeline": PIPELINE, "basins_version": basins_version,
        "release_schema": RELEASE_SCHEMA,
        # 全基が同じ作り方なら その名前、混在なら mixed（各ダムの outline_method を見る）
        "outline_method": (dams_out[0]["outline_method"] if len(methods) == 1 else "mixed") if dams_out else None,
        "excluded": excluded, "dams": dams_out,
    }
    index["version"] = wc.content_version(index, exclude=("version", "generated"))
    return index, basins_version


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.exists() and path.read_bytes() == data:
        return False
    dem_tiles.atomic_write_bytes(path, data)
    return True


def _write_index_pair(dirpath: Path, index: dict, features: list, basins_version: str) -> list[str]:
    """索引とポリゴンを書く。版が同じなら書き換えない（generated の日付だけの差分を出さない）。"""
    def unchanged(path: Path, value: str) -> bool:
        try:
            return path.exists() and json.loads(path.read_text(encoding="utf-8")).get("version") == value
        except Exception:
            return False
    written = []
    bp = dirpath / "basins.geojson"
    if not unchanged(bp, basins_version):
        gj = {"type": "FeatureCollection", "features": features, "version": basins_version}
        dem_tiles.atomic_write_bytes(bp, (json.dumps(gj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        written.append("basins.geojson")
    ip = dirpath / "index.json"
    if not unchanged(ip, index["version"]):
        dem_tiles.atomic_write_bytes(ip, (json.dumps(index, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        written.append("index.json")
    return written


def _quarantine(store: Store, src: Path, rel: str) -> str:
    """公開してはいけないファイルを、消さずに staged/quarantine/ へ移す。移した先（root 相対）を返す。"""
    dst = store.quarantine / rel
    if dst.exists() and dst.read_bytes() != src.read_bytes():
        dst = dst.with_name(f"{dst.stem}.{time.strftime('%Y%m%d%H%M%S')}{dst.suffix}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        src.unlink()                       # 同じ内容が既に隔離済み
    else:
        src.replace(dst)
    return dst.relative_to(store.root).as_posix()


def assemble(store: Store, dams: list[dict], write: bool = True, prune_orphans: bool = False) -> dict:
    """状態から、公開候補の索引（staged/）と公開物（docs/watershed/）を作り直す。標高データは使わない。

    - 公開候補 = 状態の published が非 null のダム（QA 合格）。staged/ に索引・輪を書く。
    - 公開可能 = 公開候補のうち release_status が approved のもの。docs/watershed/ には
      その索引・輪・格子（staged/flow から複製）だけを書く。0基なら空の索引と輪を書く。
    - docs/watershed/ にそれ以外のファイルがあれば取り除く。公開候補の格子と同じ内容なら
      消すだけ（staged/flow に残っている）、それ以外は staged/quarantine/ へ移す（消さない）。
    - staged/flow/<id>.json の版が published.flow_version と一致しなければ PipelineError（書かない）。
    - 状態の無い staged/flow ファイルは孤児として止める。prune_orphans なら quarantine へ移す。
    - 内容が変わらなければ書き換えない（無関係な差分と版の変動を出さない）。
    """
    order = {d["id"]: i for i, d in enumerate(dams)}
    states = store.all_states()
    pub = {i: s["published"] for i, s in states.items() if s.get("published")}
    unknown = [i for i in states if i not in order]
    if unknown:
        raise PipelineError("dams.json に無い dam_id の状態があります: " + ", ".join(unknown))
    ids = sorted(pub, key=lambda i: order[i])
    approvals = store.load_release()
    holds = store.load_holds()
    notes = store.load_notes()
    stray = [i for i in list(approvals) + list(holds) + list(notes) if i not in order]
    if stray:
        raise PipelineError(f"{RELEASE_FILE} / {HOLDS_FILE} / {NOTES_FILE} に dams.json に無い dam_id があります: " + ", ".join(stray))

    # 公開候補の格子ファイルの整合
    flows, flow_bytes = {}, {}
    for i in ids:
        p = store.flow_path(i, published=True)
        if not p.exists():
            raise PipelineError(f"{i}: 公開候補の格子 {p.relative_to(store.root).as_posix()} がありません")
        flow_bytes[i] = p.read_bytes()
        rec = json.loads(flow_bytes[i].decode("utf-8"))
        if rec.get("version") != pub[i]["flow_version"] or wc.content_version(rec) != rec.get("version"):
            raise PipelineError(f"{i}: 公開候補の格子の版が状態と一致しません（内容が書き換わっている）")
        flows[i] = rec
    on_disk = sorted(p.stem for p in store.flow.glob("*.json")) if store.flow.exists() else []
    orphans = [i for i in on_disk if i not in pub]
    if orphans and write and not prune_orphans:
        # 候補の格子なのに状態が無い（または published でない）。黙って残すと索引と食い違う
        raise PipelineError("状態の無い（または公開候補でない）格子ファイルがあります: "
                            + ", ".join(orphans) + "  → --prune-orphans で staged/quarantine/ へ移せます")

    cand_dams, cand_features = _index_records(ids, pub, flows, approvals, holds, notes)
    cand_index, cand_bv = _index_doc(cand_dams, cand_features,
                                     {k: v for k, v in EXCLUDED.items() if k in states})
    status = {d["id"]: d["release_status"] for d in cand_dams}
    public_ids = [i for i in ids if status[i] == "approved"]
    pub_dams, pub_features = _index_records(public_ids, pub, flows, approvals, None, notes)
    # 公開索引には、公開しないダムの話（保留の理由など）を載せない
    pub_index, pub_bv = _index_doc(pub_dams, pub_features, {})

    release = {s: [i for i in ids if status[i] == s] for s in RELEASE_STATUSES}
    plan = {"published_ids": ids, "public_ids": public_ids, "orphans": orphans,
            "index_version": pub_index["version"], "basins_version": pub_bv,
            "candidate_index_version": cand_index["version"], "candidate_basins_version": cand_bv,
            "release": release, "approvals_not_candidates": [i for i in approvals if i not in pub],
            "written": [], "staged_written": [], "removed": [], "quarantined": []}
    if not write:
        plan["index"], plan["features"] = pub_index, pub_features
        plan["candidate_index"], plan["candidate_features"] = cand_index, cand_features
        return plan

    for i in orphans:
        plan["quarantined"].append(_quarantine(store, store.flow_path(i, True), f"staged_flow/{i}.json"))

    # 1) 公開候補（staged/）
    plan["staged_written"] = _write_index_pair(store.stage, cand_index, cand_features, cand_bv)

    # 2) 公開物（docs/watershed/）。格子 → 輪 → 索引の順に書き、最後に余計なものを取り除く
    for i in public_ids:
        if _write_if_changed(store.out_flow / f"{i}.json", flow_bytes[i]):
            plan["written"].append(f"flow/{i}.json")
    plan["written"] += _write_index_pair(store.out, pub_index, pub_features, pub_bv)
    allowed = set(Store.PUBLIC_FILES) | {f"flow/{i}.json" for i in public_ids}
    for p in sorted(store.out.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        rel = p.relative_to(store.out).as_posix()
        if p.is_dir():
            if not any(p.iterdir()):
                p.rmdir()                  # 空になった flow/ などは残さない
            continue
        if rel in allowed:
            continue
        stem = p.stem if rel.startswith("flow/") and rel.count("/") == 1 else None
        if stem in flow_bytes and p.read_bytes() == flow_bytes[stem]:
            p.unlink()                     # 公開候補と同じ内容。staged/flow に残っている
            plan["removed"].append(rel)
        else:
            plan["quarantined"].append(_quarantine(store, p, rel))

    # 3) ローカル評価用（公開しない）: 直近の判定も含めて全ダム
    local = dict(cand_index, dams=[])
    for i, s in sorted(states.items(), key=lambda kv: order[kv[0]]):
        lat = s.get("latest") or {}
        entry = {"id": i, "name": s.get("name"), "candidate": bool(s.get("published")),
                 "latest_status": lat.get("status"), "latest_flow_version": lat.get("flow_version"),
                 "block_codes": lat.get("block_codes"), "excluded_reason": EXCLUDED.get(i),
                 "release_status": status.get(i), "approval_recorded": i in approvals,
                 "hold_reason": (holds.get(i) or {}).get("reason")}
        local["dams"].append(entry)
    dem_tiles.atomic_write_bytes(store.stage / "index.local.json",
                                 (json.dumps(local, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return plan
