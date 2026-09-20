#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地理院の標高タイル（DEM10B テキスト形式）の取得とキャッシュ。numpy のみ。

旧実装の問題
------------
`except Exception: f.write_text("")`  ── **どんな失敗でも空ファイルを書き、次回から取りに行かない。**
一時的な通信障害（タイムアウト・5xx・接続切れ・429）と、地理院にそのタイルが
本当に無い（海域などで 404）とを区別できず、通信障害の穴が「海」として永久に固まる。
集水域が黙って欠けたまま、QA にも出ない。

この実装の規則
--------------
| 結果                        | 扱い                                                        |
|-----------------------------|-------------------------------------------------------------|
| 200 かつ 256×256 の数値      | `dem_z_x_y.txt` にアトミックに保存。kind = "ok"              |
| **404**                     | 「本当に無い」と確認した印 `dem_z_x_y.missing` を保存。kind="missing" |
| それ以外（5xx/429/タイムアウト/切断/中身が壊れている/200で空） | 再試行し、だめなら **DemFetchError**。**何も保存しない** |
| 既存の空の `dem_z_x_y.txt`   | 由来不明。kind = "legacy_empty"（QA が集水域に接していれば止める） |
| offline で未取得             | **DemUnavailable**（DemFetchError の一種）。空扱いにしない    |

`--revalidate-empty` で legacy_empty を取り直せる（404 なら .missing に、200 なら本物に置換）。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://maps.gsi.go.jp/"}

TILE = "dem"
URL = "https://cyberjapandata.gsi.go.jp/xyz/{tile}/{z}/{x}/{y}.txt"


def _nan_tile() -> np.ndarray:
    """データの無いタイル。**float64** で返す（旧実装 np.full((256,256), nan) と同じ）。

    実データのタイルは float32。欠損タイルを1枚でも含む格子は hstack で float64 に昇格し、
    以後の解析が float64 で行われる。旧実装の出力とバイト単位で同一に保つため、
    この昇格をそのまま再現している（float32 に揃えると、欠損タイルを含む6基で
    集水域の外の d8 が1〜6セル変わる。実測）。
    """
    return np.full((256, 256), np.nan)


class DemFetchError(Exception):
    """標高タイルを確定できなかった（通信障害など）。呼び出し側はそのダムの生成を中止する。
    既存の成果物は壊さない。"""


class DemUnavailable(DemFetchError):
    """offline で、キャッシュに無いタイルが必要になった。"""


class _Missing(Exception):
    """地理院が 404 を返した。"""


def default_opener(url: str, timeout: float = 30.0):
    """(status, body) を返す。404 は _Missing、それ以外の失敗は例外のまま上へ。"""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            clen = r.headers.get("Content-Length")
            if clen is not None and clen.isdigit() and int(clen) != len(body):
                raise ConnectionError(f"途中で切れた応答 ({len(body)}/{clen} bytes)")
            return r.status, body
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _Missing() from e
        raise


def parse_tile(txt: str) -> np.ndarray:
    """256×256 の標高テキストを配列にする。'e' は欠損（NaN）。形が違えば ValueError。"""
    lines = txt.strip().splitlines()
    if len(lines) != 256:
        raise ValueError(f"行数が 256 ではない: {len(lines)}")
    rows = []
    for ln in lines:
        vals = ln.split(",")
        if len(vals) != 256:
            raise ValueError(f"列数が 256 ではない: {len(vals)}")
        rows.append([np.nan if v == "e" else float(v) for v in vals])
    return np.array(rows, dtype=np.float32)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同じディレクトリの一時ファイルへ書いてから置き換える（書きかけを残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class TileSource:
    """キャッシュ付きの標高タイル取得。1つのダムの生成ごとに report() で内訳が取れる。"""

    def __init__(self, cache_dir: Path, offline: bool = False, opener=None,
                 retries: int = 3, backoff: float = 1.5, interval: float = 0.05,
                 sleep=time.sleep, revalidate_empty: bool = False, fallback_dirs=(),
                 max_network_requests: int | None = None):
        # 書き込むのは cache_dir だけ。fallback_dirs は読み取り専用の追加キャッシュ
        # （別の作業ツリーが持っている取得済みタイルを、書き換えずに再利用するため）
        self.cache = Path(cache_dir)
        self.fallbacks = [Path(d) for d in fallback_dirs]
        self.offline = offline
        self.opener = opener or default_opener
        self.retries = max(1, retries)
        self.backoff = backoff
        self.interval = interval
        self.sleep = sleep
        self.revalidate_empty = revalidate_empty
        self.kinds: dict[str, str] = {}      # "z/x/y" -> kind（この Source が見たもの）
        self.network_requests = 0
        # 想定外の大量通信を止めるための上限（None = 無制限）。超えたら DemFetchError にして通信を止める
        self.max_network_requests = max_network_requests

    # ---- パス（書き込み先は常に self.cache）
    def _txt(self, z, x, y) -> Path:
        return self.cache / f"{TILE}_{z}_{x}_{y}.txt"

    def _missing(self, z, x, y) -> Path:
        return self.cache / f"{TILE}_{z}_{x}_{y}.missing"

    def _find(self, z, x, y):
        """(実データの .txt, 404確認の印, 空の .txt) を、書き込み先→読み取り専用の順に探す。"""
        data = miss = empty = None
        for d in [self.cache] + self.fallbacks:
            t = d / f"{TILE}_{z}_{x}_{y}.txt"
            m = d / f"{TILE}_{z}_{x}_{y}.missing"
            if data is None and t.exists() and t.stat().st_size > 0:
                data = t
            if miss is None and m.exists():
                miss = m
            if empty is None and t.exists() and t.stat().st_size == 0:
                empty = t
        return data, miss, empty

    # ---- 取得
    def _download(self, z, x, y) -> tuple[str, np.ndarray | None]:
        """ネットワークから確定させる。('ok', 配列) か ('missing', None)。失敗は DemFetchError。"""
        url = URL.format(tile=TILE, z=z, x=x, y=y)
        last = None
        for attempt in range(self.retries):
            if self.max_network_requests is not None and self.network_requests >= self.max_network_requests:
                raise DemFetchError(f"通信回数の上限（{self.max_network_requests}）に達したため停止します")
            try:
                self.network_requests += 1
                status, body = self.opener(url)
                if status != 200:
                    raise ConnectionError(f"HTTP {status}")
                txt = body.decode("utf-8")
                if not txt.strip():
                    # 200 なのに空。本当に無いタイルは 404 で返る。空の 200 は異常なので確定させない
                    raise ConnectionError("HTTP 200 だが本文が空")
                arr = parse_tile(txt)         # 壊れていれば ValueError
                atomic_write_bytes(self._txt(z, x, y), body)
                mk = self._missing(z, x, y)
                if mk.exists():
                    mk.unlink()
                self.sleep(self.interval)
                return "ok", arr
            except _Missing:
                atomic_write_bytes(self._missing(z, x, y), b"404\n")
                # 取り直しで 404 が確定した場合、由来不明の空 .txt は消して .missing に一本化する
                t = self._txt(z, x, y)
                if t.exists() and t.stat().st_size == 0:
                    t.unlink()
                self.sleep(self.interval)
                return "missing", None
            except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as e:
                last = e
                if attempt + 1 < self.retries:
                    self.sleep(self.backoff * (2 ** attempt))
        raise DemFetchError(f"標高タイル {z}/{x}/{y} を取得できませんでした（{self.retries}回試行）: {last}")

    def get(self, z: int, x: int, y: int) -> tuple[np.ndarray, str]:
        """(配列, kind)。kind は ok / missing / legacy_empty。"""
        key = f"{z}/{x}/{y}"
        t, mk, emp = self._find(z, x, y)

        if t is not None:
            try:
                arr = parse_tile(t.read_text(encoding="utf-8"))
            except ValueError as e:
                # キャッシュの中身が壊れている。空扱いにせず、取り直せなければ止める
                if self.offline:
                    raise DemUnavailable(f"キャッシュのタイル {key} が壊れています: {e}") from e
                kind, arr = self._download(z, x, y)
                self.kinds[key] = kind
                return (arr if arr is not None else _nan_tile()), kind
            self.kinds[key] = "ok"
            return arr, "ok"

        if mk is not None:
            self.kinds[key] = "missing"
            return _nan_tile(), "missing"

        if emp is not None:                 # サイズ0 = 旧実装が書いた空。由来不明
            if not self.revalidate_empty:
                self.kinds[key] = "legacy_empty"
                return _nan_tile(), "legacy_empty"
            if self.offline:
                raise DemUnavailable(f"offline のため空タイル {key} を確認できません")
            kind, arr = self._download(z, x, y)
            self.kinds[key] = kind
            return (arr if arr is not None else _nan_tile()), kind

        if self.offline:
            raise DemUnavailable(f"offline でキャッシュに無いタイル {key} が必要です")
        kind, arr = self._download(z, x, y)
        self.kinds[key] = kind
        return (arr if arr is not None else _nan_tile()), kind

    # ---- 格子
    def build_grid(self, tile_xy_fn, lat: float, lon: float, z: int, pad: int, report: dict | None = None):
        """ダム周辺 (2*pad+1)^2 枚を並べた標高格子。(dem, x0, y0)。

        report を渡すと {"tiles": [{key, kind, i0, j0}], "counts": {...}} を書き込む。
        i0/j0 は格子内での左上セル。QA が「集水域に接するタイル」を判定するのに使う。
        """
        cx, cy = tile_xy_fn(lat, lon, z)
        tx, ty = int(cx), int(cy)
        rows, tiles = [], []
        for r, Y in enumerate(range(ty - pad, ty + pad + 1)):
            row = []
            for c, X in enumerate(range(tx - pad, tx + pad + 1)):
                arr, kind = self.get(z, X, Y)
                row.append(arr)
                tiles.append({"key": f"{z}/{X}/{Y}", "kind": kind, "i0": r * 256, "j0": c * 256})
            rows.append(np.hstack(row))
        dem = np.vstack(rows)
        if report is not None:
            counts = {"ok": 0, "missing": 0, "legacy_empty": 0}
            for t in tiles:
                counts[t["kind"]] += 1
            report["tiles"] = tiles
            report["counts"] = counts
        return dem, tx - pad, ty - pad


def tiles_touching(mask: np.ndarray, tiles: list[dict], dem: np.ndarray) -> list[dict]:
    """集水域（を1セル膨らませた範囲）と重なる「実データの無い」タイル（missing / legacy_empty）。"""
    from ws_common import dilate
    zone = dilate(mask, 1)
    out = []
    for t in tiles:
        if t["kind"] == "ok":
            continue
        i0, j0 = t["i0"], t["j0"]
        if zone[i0:i0 + 256, j0:j0 + 256].any():
            out.append({"key": t["key"], "kind": t["kind"]})
    return out
