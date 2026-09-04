#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""富山県のダム貯水率を「川の防災情報」から取得し、静的 JSON に書き出すバッチ。

設計方針（最重要）
------------------
* 値が取れなかったものは **絶対に埋めない**。推定値も前回値の流用もしない。
* 取れなかった理由（欠測 / 未提供 / 閉局 / 取得失敗 / 提供元なし）を
  フィールド単位で JSON に残す。SPA 側はそれを「—（理由）」として表示する。
* 出力は毎回まっさらに作り直す。前回の JSON は一切参照しない。

データ源
--------
国土交通省「川の防災情報」(www.river.go.jp) が SPA 用に配信している静的 JSON。
  観測所マスタ : /kawabou/file/files/master/obs/dam/{obsFcd}.json
  時系列データ : /kawabou/file/files/tmlist/dam/{YYYYMMDD}/{HHmm}/{obsFcd}.json
  最新観測時刻 : /kawabou/file/system/tmCrntTime.json

obsFcd は事務所コード・項目種別・観測所コードの連結（サイトの app.js と同一規則）:
  ("0000"+ofcCd).slice(-5) + ("00"+itmkndCd).slice(-3) + ("0000"+obsCd).slice(-5)
ダムの itmkndCd は 7。

依存ライブラリなし（標準ライブラリのみ）。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 定数

ROOT = Path(__file__).resolve().parent.parent
DAMS_CSV = ROOT / "toyama_dams.csv"
NODATA_CSV = ROOT / "toyama_dams_nodata.csv"
SLUGS_CSV = ROOT / "dam_slugs.csv"
SITE_JSON = ROOT / "site.json"
OUT_JSON = ROOT / "docs" / "data" / "dams.json"
# 観測所マスタ（住所・読み・洪水期制限水位）はほぼ変わらないので、ローカルに持っておく。
# 相手サーバへのリクエストを 1 回あたり約半分に減らすため。
MASTER_CACHE = ROOT / "cache" / "obs_master.json"
MASTER_CACHE_DAYS = 30

BASE_FILES = "https://www.river.go.jp/kawabou/file/files"
URL_CRNT_TIME = "https://www.river.go.jp/kawabou/file/system/tmCrntTime.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.river.go.jp/kawabou/pcfull/tm",
    "Accept": "application/json,text/plain,*/*",
}

ITMKND_DAM = 7

# 品質コード（kawabou の app.js より）
CCD_CLOSED = 140   # 閉局
CCD_MISSING = 190  # 欠測
CCD_NOT_PROVIDED_MIN = 130  # 130 以上は「その観測所では提供されない項目」

REQUEST_INTERVAL_SEC = 0.3   # 相手サーバへの礼儀
TIMEOUT_SEC = 30
RETRY_PER_REQUEST = 3
# 最新スロットが未生成のことがあるので、10 分ずつ遡って探す
TIME_SLOT_FALLBACKS = 6

# 「貯水率で色分けする」ための閾値。設計理由は README.md を参照。
#
# 利水貯水率は洪水期制限水位運用のため 100% に張り付く基が多い。
# 等分割ではなく「満水からの目減り」を見る非線形な刻みにしてある。
THRESHOLDS: dict[str, Any] = {
    "default_basis": "irrigation",
    "bases": {
        "irrigation": {
            "label": "利水貯水率",
            "field": "rate_irrigation",
            "bins": [
                {"min": 95, "max": None, "label": "95%以上（ほぼ満水）", "color": "#1d4ed8"},
                {"min": 80, "max": 95, "label": "80–95%", "color": "#0891b2"},
                {"min": 60, "max": 80, "label": "60–80%", "color": "#15803d"},
                {"min": 40, "max": 60, "label": "40–60%", "color": "#ea9a1a"},
                {"min": None, "max": 40, "label": "40%未満（少ない）", "color": "#dc2626"},
            ],
        },
        "effective": {
            "label": "有効貯水率",
            "field": "rate_effective",
            "bins": [
                {"min": 60, "max": None, "label": "60%以上", "color": "#1d4ed8"},
                {"min": 40, "max": 60, "label": "40–60%", "color": "#0891b2"},
                {"min": 25, "max": 40, "label": "25–40%", "color": "#15803d"},
                {"min": 10, "max": 25, "label": "10–25%", "color": "#ea9a1a"},
                {"min": None, "max": 10, "label": "10%未満", "color": "#dc2626"},
            ],
        },
    },
    "no_data_color": "#94a3b8",
}

# 「何日前の値になったら注意を出すか」も site.json で一元管理し、
# JSON に載せて地図側に渡す（地図と静的ページで基準をずらさないため）。
def _freshness() -> dict:
    default = {"notice_days": 7, "warn_days": 14}
    try:
        fr = json.loads(SITE_JSON.read_text(encoding="utf-8")).get("freshness") or {}
        return {k: fr.get(k, v) for k, v in default.items()}
    except (OSError, json.JSONDecodeError):
        return default


THRESHOLDS["freshness"] = _freshness()

JST = dt.timezone(dt.timedelta(hours=9))


# ---------------------------------------------------------------- 取得

class FetchError(Exception):
    """このバッチが投げる唯一の取得系例外。呼び出し側で理由を JSON に残す。"""


def http_get_json(url: str) -> Any:
    last: Exception | None = None
    for attempt in range(RETRY_PER_REQUEST):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
                raw = res.read()
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                break  # 404 はリトライしても無駄
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = e
        if attempt < RETRY_PER_REQUEST - 1:
            time.sleep(1.0 * (attempt + 1))
    raise FetchError(f"{type(last).__name__}: {last}")


def obs_fcd(ofc_cd: int, obs_cd: int, itmknd_cd: int = ITMKND_DAM) -> str:
    return f"{ofc_cd:05d}{itmknd_cd:03d}{obs_cd:05d}"


def latest_obs_time() -> dt.datetime:
    """サイトが「今の観測時刻」として配信している値を基準にする。"""
    data = http_get_json(URL_CRNT_TIME)
    return dt.datetime.strptime(data["crntObsTime"], "%Y/%m/%d %H:%M").replace(tzinfo=JST)


def slot_path(when: dt.datetime) -> str:
    return f"{when:%Y%m%d}/{when:%H%M}"


# ---------------------------------------------------------------- 値の解釈

def read_value(values: dict, key: str) -> dict:
    """観測値 1 項目を {value, status, reason} に正規化する。

    値が信用できないときは value を必ず None にする。埋めない。
    """
    raw = values.get(key)
    ccd = values.get(key + "Ccd")

    if ccd == CCD_MISSING:
        return {"value": None, "status": "missing", "reason": "欠測（観測所からデータが届いていない）"}
    if ccd == CCD_CLOSED:
        return {"value": None, "status": "closed", "reason": "閉局（観測を行っていない）"}
    if ccd is None or ccd >= CCD_NOT_PROVIDED_MIN:
        return {"value": None, "status": "not_provided", "reason": "未提供（この観測所では公表されていない項目）"}
    if raw is None:
        return {"value": None, "status": "missing", "reason": "欠測（値が空）"}
    return {"value": raw, "status": "ok", "reason": None}


def flood_season(master: dict) -> dict | None:
    """洪水期制限水位の運用期間（年は運用開始年なので月日だけ使う）。"""
    lvl = master.get("lmtStg1")
    start, end = master.get("lmtStgStrt1"), master.get("lmtStgEnd1")
    if lvl is None or not start or not end:
        return None
    try:
        s = dt.datetime.strptime(start, "%Y/%m/%d %H:%M")
        e = dt.datetime.strptime(end, "%Y/%m/%d %H:%M")
    except ValueError:
        return None
    return {
        "limit_level_m": lvl,
        "start_md": f"{s.month:02d}-{s.day:02d}",
        "end_md": f"{e.month:02d}-{e.day:02d}",
    }


def in_flood_season(fs: dict | None, when: dt.datetime) -> bool | None:
    if not fs:
        return None
    md = f"{when.month:02d}-{when.day:02d}"
    s, e = fs["start_md"], fs["end_md"]
    return s <= md <= e if s <= e else (md >= s or md <= e)


# ---------------------------------------------------------------- 1 基分

def build_dam(row: dict, base_time: dt.datetime, slugs: dict, master_cache: dict) -> dict:
    ofc = int(row["ofc_cd"])
    obs = int(row["obs_cd"])
    fcd = obs_fcd(ofc, obs)

    sl = slugs.get(row["dam_name"], {})
    rec: dict[str, Any] = {
        # id は「都道府県 + ローマ字読み」で作る自前の安定キー。
        # 取得元の観測所コードに依存しないので、データ源を変えても URL と
        # イラストの割り当てが壊れない。
        "id": f"{sl.get('pref', 'unknown')}-{sl.get('slug') or fcd}",
        "pref": sl.get("pref", ""),
        "slug": sl.get("slug", ""),
        "name": row["dam_name"],
        "kana": None,
        "address": None,
        "water_system": row["water_system"],
        "river": row["river"],
        "manager": row["manager"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "note": (row.get("note") or "").strip() or None,
        "source": {"provider": "川の防災情報", "ofc_cd": ofc, "obs_cd": obs, "obs_fcd": fcd},
        "obs_fcd": fcd,
        "manager_office": None,
        "flood_season": None,
        "in_flood_season": None,
        "obs_time": None,
        "rate_irrigation": None,
        "rate_effective": None,
        "storage_level_m": None,
        "storage_capacity_1000m3": None,
        "inflow_m3s": None,
        "outflow_m3s": None,
        "data_status": "unknown",
        "illustration": None,
    }

    # --- マスタ（任意情報。取れなくても本体は続行する）
    master = master_cache.get(fcd)
    if master is None:
        try:
            master = http_get_json(f"{BASE_FILES}/master/obs/dam/{fcd}.json")["obsInfo"]
            master_cache[fcd] = master
        except (FetchError, KeyError, TypeError) as e:
            rec["master_error"] = str(e)
            master = {}
        time.sleep(REQUEST_INTERVAL_SEC)
    if master:
        rec["manager_office"] = master.get("jrsNm")
        rec["kana"] = master.get("obsKana")
        rec["address"] = master.get("obsAdr")
        rec["flood_season"] = flood_season(master)

    # --- 実測値。最新スロットが無ければ 10 分ずつ遡る
    tm = None
    tried: list[str] = []
    last_err = ""
    for i in range(TIME_SLOT_FALLBACKS):
        when = base_time - dt.timedelta(minutes=10 * i)
        path = slot_path(when)
        tried.append(path)
        try:
            tm = http_get_json(f"{BASE_FILES}/tmlist/dam/{path}/{fcd}.json")
            break
        except FetchError as e:
            last_err = str(e)
        time.sleep(REQUEST_INTERVAL_SEC)

    if tm is None:
        rec["data_status"] = "fetch_failed"
        err = {"value": None, "status": "fetch_failed",
               "reason": f"取得失敗（{last_err}）"}
        rec["rate_irrigation"] = dict(err)
        rec["rate_effective"] = dict(err)
        rec["fetch_error"] = {"tried_slots": tried, "error": last_err}
        return rec

    v = tm.get("obsValue") or {}
    rec["obs_time"] = v.get("obsTime")
    rec["rate_irrigation"] = read_value(v, "storPcntIrr")
    rec["rate_effective"] = read_value(v, "storPcntEff")
    rec["storage_level_m"] = read_value(v, "storLvl")
    rec["storage_capacity_1000m3"] = read_value(v, "storCap")
    rec["inflow_m3s"] = read_value(v, "allSink")
    rec["outflow_m3s"] = read_value(v, "allDisch")

    statuses = {rec["rate_irrigation"]["status"], rec["rate_effective"]["status"]}
    if statuses == {"ok"}:
        rec["data_status"] = "ok"
    elif "ok" in statuses:
        rec["data_status"] = "partial"
    else:
        rec["data_status"] = rec["rate_irrigation"]["status"]

    if rec["obs_time"]:
        try:
            obs_dt = dt.datetime.strptime(rec["obs_time"], "%Y/%m/%d %H:%M").replace(tzinfo=JST)
            rec["in_flood_season"] = in_flood_season(rec["flood_season"], obs_dt)
        except ValueError:
            pass

    time.sleep(REQUEST_INTERVAL_SEC)
    return rec


def build_nodata_dam(row: dict, slugs: dict) -> dict:
    reason = row["reason"].strip()
    blank = {"value": None, "status": "no_source", "reason": reason}
    sl = slugs.get(row["dam_name"], {})
    return {
        "id": f"{sl.get('pref', 'unknown')}-{sl.get('slug') or row['dam_name']}",
        "pref": sl.get("pref", ""),
        "slug": sl.get("slug", ""),
        "name": row["dam_name"],
        "kana": None,
        "address": (row.get("address") or "").strip() or None,
        "water_system": row["water_system"],
        "river": row["river"],
        "manager": row["manager"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "note": (row.get("note") or "").strip() or None,
        "source": {"provider": None, "checked": row.get("source_checked", "")},
        "manager_office": None,
        "flood_season": None,
        "in_flood_season": None,
        "obs_time": None,
        "rate_irrigation": dict(blank),
        "rate_effective": dict(blank),
        "storage_level_m": dict(blank),
        "storage_capacity_1000m3": dict(blank),
        "inflow_m3s": dict(blank),
        "outflow_m3s": dict(blank),
        "data_status": "no_source",
        "illustration": None,
    }


# ---------------------------------------------------------------- main

def load_master_cache(force_refresh: bool) -> dict:
    """観測所マスタのローカルキャッシュ。取得回数を減らすのが目的。

    住所・読み・洪水期制限水位はほぼ変わらない静的情報なので、
    毎日取りに行く必要がない。30 日で自動的に取り直す。
    """
    if force_refresh or not MASTER_CACHE.exists():
        return {}
    try:
        blob = json.loads(MASTER_CACHE.read_text(encoding="utf-8"))
        saved = dt.datetime.fromisoformat(blob["saved_at"])
        if (dt.datetime.now(JST) - saved).days >= MASTER_CACHE_DAYS:
            print("[fetch_dams] マスタキャッシュが古いので取り直します")
            return {}
        return blob.get("obs", {})
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return {}


def save_master_cache(cache: dict) -> None:
    if not cache:
        return
    MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MASTER_CACHE.write_text(
        json.dumps({"saved_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
                    "obs": cache}, ensure_ascii=False, indent=1),
        encoding="utf-8", newline="\n")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser(description="富山県ダム貯水率の日次取得バッチ")
    ap.add_argument("--out", type=Path, default=OUT_JSON, help="出力先 JSON")
    ap.add_argument("--refresh-master", action="store_true",
                    help="観測所マスタのキャッシュを無視して取り直す")
    ap.add_argument("--skip-nodata", action="store_true",
                    help="データ提供なしのダム（黒部・有峰・出し平）を出力に含めない")
    args = ap.parse_args()

    started = dt.datetime.now(JST)
    print(f"[fetch_dams] 開始 {started:%Y-%m-%d %H:%M:%S%z}")

    try:
        base_time = latest_obs_time()
    except FetchError as e:
        print(f"[fetch_dams] 致命的: 最新観測時刻を取得できません: {e}", file=sys.stderr)
        return 1
    print(f"[fetch_dams] 基準観測時刻 {base_time:%Y/%m/%d %H:%M}")

    slugs = {r["dam_name"]: r for r in read_csv(SLUGS_CSV)}
    master_cache = load_master_cache(args.refresh_master)

    dams: list[dict] = []
    for row in read_csv(DAMS_CSV):
        rec = build_dam(row, base_time, slugs, master_cache)
        irr = rec["rate_irrigation"]
        eff = rec["rate_effective"]
        print(
            f"  {rec['name']:<9} "
            f"利水={_fmt(irr):<10} 有効={_fmt(eff):<10} "
            f"[{rec['data_status']}] {rec['obs_time'] or '-'}"
        )
        dams.append(rec)

    if not args.skip_nodata and NODATA_CSV.exists():
        for row in read_csv(NODATA_CSV):
            rec = build_nodata_dam(row, slugs)
            print(f"  {rec['name']:<9} 利水=—          有効=—          [no_source]")
            dams.append(rec)

    save_master_cache(master_cache)

    counts: dict[str, int] = {}
    for d in dams:
        counts[d["data_status"]] = counts.get(d["data_status"], 0) + 1

    payload = {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "base_obs_time": f"{base_time:%Y/%m/%d %H:%M}",
        "source": {
            "name": "国土交通省 川の防災情報",
            "url": "https://www.river.go.jp/kawabou/pcfull/tm",
            "endpoint": f"{BASE_FILES}/tmlist/dam/{{YYYYMMDD}}/{{HHmm}}/{{obsFcd}}.json",
            "note": "貯水率は observatory 提供値をそのまま転記。欠測・未提供は補完していない。",
        },
        "thresholds": THRESHOLDS,
        "status_labels": {
            "ok": "取得済み",
            "partial": "一部のみ取得",
            "missing": "欠測",
            "closed": "閉局",
            "not_provided": "未提供",
            "fetch_failed": "取得失敗",
            "no_source": "データ提供なし",
        },
        "summary": {"total": len(dams), "by_status": counts},
        "dams": dams,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[fetch_dams] 出力 {args.out}  ({args.out.stat().st_size:,} bytes)")
    print(f"[fetch_dams] 内訳 {counts}")
    return 0


def _fmt(item: dict) -> str:
    return f"{item['value']}%" if item["status"] == "ok" else f"—({item['status']})"


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
