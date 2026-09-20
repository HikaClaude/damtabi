#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域パイプラインの共通部品（numpy のみ。ネットワーク・ファイルI/Oなし）。

build_flowgrids.py / watershed_qa.py / テストが同じ実装を使うための置き場。
ここにある関数の多くは「ブラウザ（docs/app.js の ws モジュール）が実際にやること」の
Python 移植である。移植元の対応関係は各関数の docstring に書いた。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math

import numpy as np

# 8近傍（時計回り、0 = 北）。docs/app.js の DI/DJ と同じ順序
D8 = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


# ---------------------------------------------------------------- 詰め込み / 展開

def pack4(a: np.ndarray) -> str:
    """0..15 の配列を 4bit/セルに詰めて base64 にする（D8 流向）。"""
    flat = a.astype(np.uint8).ravel()
    if len(flat) % 2:
        flat = np.append(flat, np.uint8(15)).astype(np.uint8)
    packed = ((flat[0::2] << 4) | flat[1::2]).astype(np.uint8)
    return base64.b64encode(packed.tobytes()).decode()


def pack1(mask: np.ndarray) -> str:
    """真偽配列を 1bit/セルに詰めて base64 にする（集水域マスク）。"""
    return base64.b64encode(np.packbits(mask.astype(np.uint8).ravel()).tobytes()).decode()


def unpack4(s: str, h: int, w: int) -> np.ndarray:
    """pack4 の逆。app.js の Grid.dirAt と同じ読み方（上位4bitが偶数番）。"""
    b = np.frombuffer(base64.b64decode(s), dtype=np.uint8)
    out = np.empty(len(b) * 2, dtype=np.uint8)
    out[0::2] = b >> 4
    out[1::2] = b & 15
    return out[:h * w].reshape(h, w)


def unpack1(s: str, h: int, w: int) -> np.ndarray:
    """pack1 の逆。app.js の Grid.maskAt と同じ読み方（MSB first）。"""
    b = np.frombuffer(base64.b64decode(s), dtype=np.uint8)
    return np.unpackbits(b)[:h * w].reshape(h, w).astype(bool)


# ---------------------------------------------------------------- 座標

def cell_lonlat(rec: dict, ii: np.ndarray, jj: np.ndarray):
    """粗格子のセル中心の経緯度。app.js の Grid.prototype.cellLngLat と同じ式。"""
    world = 256.0 * (2 ** rec["zoom"])
    px = rec["x0"] * 256 + (jj + 0.5) * rec["coarse"]
    py = rec["y0"] * 256 + (ii + 0.5) * rec["coarse"]
    lon = px / world * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py / world))))
    return lon, lat


def point_in_poly(lon: float, lat: float, ring) -> bool:
    """1点の内外判定（偶奇規則）。app.js の inRing と同じ。"""
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


def points_in_ring(lon: np.ndarray, lat: np.ndarray, ring) -> np.ndarray:
    """多数の点の内外判定（偶奇規則。inRing のベクトル版）。lon/lat は同形の配列。"""
    inside = np.zeros(lon.shape, dtype=bool)
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if y1 == y2:
            continue
        cond = (y1 > lat) != (y2 > lat)
        if not cond.any():
            continue
        xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
        inside ^= cond & (lon < xin)
    return inside


# ---------------------------------------------------------------- 描画される輪

def smooth_ring(ring, iters: int = 2):
    """**ブラウザが実際に地図へ描く輪**。docs/app.js の smoothRing(ring, iters) の移植。

    ポリゴンそのものではなく、この関数を通した線と面が利用者に見える。
    したがって「描画後の輪」と雨マスクの空間的な一致は、生の ring ではなく
    これを通した輪で検査しなければならない。
    """
    r = [tuple(p) for p in ring[:-1]]
    for _ in range(3):
        keep = []
        n = len(r)
        for i in range(n):
            a = r[(i + n - 1) % n]
            b = r[i]
            c = r[(i + 1) % n]
            v1 = (a[0] - b[0], a[1] - b[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            n1 = math.sqrt(v1[0] * v1[0] + v1[1] * v1[1])
            n2 = math.sqrt(v2[0] * v2[0] + v2[1] * v2[1])
            cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) if (n1 and n2) else -1
            if cos < 0.87:
                keep.append(b)
        if len(keep) == len(r) or len(keep) < 8:
            break
        r = keep
    for _ in range(iters):
        out = []
        n = len(r)
        for i in range(n):
            b = r[i]
            q = r[(i + 1) % n]
            out.append((b[0] * 0.75 + q[0] * 0.25, b[1] * 0.75 + q[1] * 0.25))
            out.append((b[0] * 0.25 + q[0] * 0.75, b[1] * 0.25 + q[1] * 0.75))
        r = out
    r.append(r[0])
    return r


# ---------------------------------------------------------------- 内容ハッシュ（版）

def canonical(obj) -> bytes:
    """キー順・空白に依存しない正規化 JSON。内容ハッシュの入力に使う。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_version(obj, exclude=("version",)) -> str:
    """内容ハッシュ（sha256 の先頭12桁）。exclude のキー（トップレベル）は入力から外す。

    旧実装は d8・mask・面積だけをハッシュしており、ポリゴンや出口・格子の位置決め
    （x0/y0/zoom/coarse）が変わっても版が変わらなかった。ここでは対象の全フィールドを
    入力にする。
    """
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in exclude}
    return hashlib.sha256(canonical(obj)).hexdigest()[:12]


# ---------------------------------------------------------------- 形状の検査

def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def self_intersections(ring) -> int:
    """閉じた輪の自己交差（隣り合わない辺どうしの真の交差）の数。

    角度順に並べた輪郭点は、凹んだ集水域で自分自身と交差することがある。
    交差があると、偶奇規則で塗ったときに穴が空いたり裏返ったりする。
    """
    pts = np.asarray(ring, dtype=float)
    n = len(pts) - 1
    if n < 4:
        return 0
    ax, ay = pts[:-1, 0], pts[:-1, 1]
    bx, by = pts[1:, 0], pts[1:, 1]
    count = 0
    for i in range(n):
        j = np.arange(i + 2, n)
        if i == 0:
            j = j[j != n - 1]          # 最初と最後の辺は隣り合う
        if not len(j):
            continue
        d1 = _orient(ax[i], ay[i], bx[i], by[i], ax[j], ay[j])
        d2 = _orient(ax[i], ay[i], bx[i], by[i], bx[j], by[j])
        d3 = _orient(ax[j], ay[j], bx[j], by[j], ax[i], ay[i])
        d4 = _orient(ax[j], ay[j], bx[j], by[j], bx[i], by[i])
        count += int(np.sum((d1 * d2 < 0) & (d3 * d4 < 0)))
    return count


# ---------------------------------------------------------------- 格子の形態

def dilate(mask: np.ndarray, k: int) -> np.ndarray:
    """8近傍で k セル膨らませる（枠の外は False のまま）。"""
    out = mask.copy()
    h, w = mask.shape
    for _ in range(max(0, k)):
        p = np.zeros((h + 2, w + 2), dtype=bool)
        p[1:-1, 1:-1] = out
        nxt = np.zeros_like(out)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nxt |= p[1 + di:1 + di + h, 1 + dj:1 + dj + w]
        out = nxt
    return out


def components(mask: np.ndarray) -> list[int]:
    """8連結の連結成分の大きさ（降順）。"""
    from collections import deque
    h, w = mask.shape
    seen = np.zeros_like(mask)
    sizes = []
    for i, j in np.argwhere(mask):
        if seen[i, j]:
            continue
        q = deque([(i, j)])
        seen[i, j] = True
        n = 0
        while q:
            a, b = q.popleft()
            n += 1
            for di, dj in D8:
                ni, nj = a + di, b + dj
                if 0 <= ni < h and 0 <= nj < w and mask[ni, nj] and not seen[ni, nj]:
                    seen[ni, nj] = True
                    q.append((ni, nj))
        sizes.append(n)
    return sorted(sizes, reverse=True)


# ---------------------------------------------------------------- 輪郭

def outline_exact(mask: np.ndarray) -> np.ndarray:
    """マスクの外周を、画素の辺に沿って**実際にたどる**（crack following）。

    build_basins.outline() は輪郭セルを重心まわりの角度順に並べるだけで、凹んだ集水域や
    細長い集水域では輪郭を取りこぼす（細格子の集水域に対する IoU 0.90〜0.955、集水域の
    最大 7% が輪の外に出る。空間QAで検出）。こちらは輪郭そのものをたどるので、
    輪は集水域マスクと画素単位で一致する。

    返り値: (行, 列) の角の座標（セル中心は +0.5）。最大の外周ループだけ（穴・飛び地は捨てる）。
    2ループが1点で接する所（斜めに接するセル）は、内側を左に見て**左へ曲がる**辺を優先する
    ことで、連結を分けずに1周にする（8連結の集水域を割らない）。
    """
    h, w = mask.shape
    out: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a, b):
        out.setdefault(a, []).append(b)

    for i, j in np.argwhere(mask):
        i, j = int(i), int(j)
        if i == 0 or not mask[i - 1, j]:
            add((i, j), (i, j + 1))                 # 上辺: 右へ
        if j == w - 1 or not mask[i, j + 1]:
            add((i, j + 1), (i + 1, j + 1))         # 右辺: 下へ
        if i == h - 1 or not mask[i + 1, j]:
            add((i + 1, j + 1), (i + 1, j))         # 下辺: 左へ
        if j == 0 or not mask[i, j - 1]:
            add((i + 1, j), (i, j))                 # 左辺: 上へ

    def turn_rank(prev, cur, nxt):
        # 進行方向に対して 左(0) → 直進(1) → 右(2) の順に優先
        d1 = (cur[0] - prev[0], cur[1] - prev[1])
        d2 = (nxt[0] - cur[0], nxt[1] - cur[1])
        cross = d1[1] * d2[0] - d1[0] * d2[1]       # 画像座標（行が下向き）での外積
        return 1 if cross == 0 else (0 if cross < 0 else 2)

    loops = []
    while out:
        start = next(iter(out))
        cur, prev, loop = start, None, [start]
        while True:
            nxts = out.get(cur)
            if not nxts:
                break
            if len(nxts) > 1 and prev is not None:
                nxts.sort(key=lambda n: turn_rank(prev, cur, n), reverse=True)   # pop() が最優先を取る
            nx = nxts.pop()
            if not nxts:
                del out[cur]
            prev, cur = cur, nx
            if cur == start:
                break
            loop.append(cur)
        loops.append(loop)

    def area2(loop):
        a = np.array(loop, dtype=float)
        return abs(float(np.dot(a[:, 0], np.roll(a[:, 1], -1)) - np.dot(a[:, 1], np.roll(a[:, 0], -1))))

    return np.array(max(loops, key=area2), dtype=float)
