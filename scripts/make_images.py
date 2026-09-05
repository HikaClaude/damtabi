#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PWA アイコンと OGP 画像を生成する（**単発**スクリプト）。

日次バッチではありません。ダムを増減したときだけ実行すれば十分です。
生成物は日々変わる数値を含まないので、リポジトリにコミットしたまま使えます。

  docs/img/icon.svg               （このスクリプトが書き出す。他は SVG からラスタ化）
  docs/img/icon-192.png
  docs/img/icon-512.png
  docs/img/icon-maskable-512.png
  docs/img/icon-180.png           （apple-touch-icon）
  docs/img/og/site.png            （1200x630・トップ用）
  docs/img/og/{slug}.png          （1200x630・ダムごと）

ラスタ化にはローカルの Chrome / Edge のヘッドレスを使います（Python の画像ライブラリ不要）。
Chrome が無い環境ではスキップし、SVG だけ書き出して終わります。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
IMG = DOCS / "img"
OG = IMG / "og"
DATA = DOCS / "data" / "dams.json"
ILLUST = DOCS / "data" / "illustrations.json"
SITE_JSON = ROOT / "site.json"


def load_site() -> dict:
    cfg = {"site_name": "ダム旅", "name_en": "DAM TABI",
           "producer": "DAM TABI LAB", "tagline": "", "region": "富山県"}
    if SITE_JSON.exists():
        cfg.update({k: v for k, v in json.loads(SITE_JSON.read_text(encoding="utf-8")).items()
                    if not k.startswith("_")})
    return cfg


SITE = load_site()

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

FONT_STACK = ('"Hiragino Kaku Gothic ProN","Yu Gothic UI","Yu Gothic",'
              '"Noto Sans JP","Meiryo",system-ui,sans-serif')

# ダムの堤体と貯水池を思わせるマーク。
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="ダム旅">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1c6ba8"/><stop offset="1" stop-color="#14507d"/>
    </linearGradient>
    <linearGradient id="water" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#7fd3f0"/><stop offset="1" stop-color="#2f9fd4"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#sky)"/>
  <!-- 貯水池 -->
  <path d="M96 214 C150 190 190 232 256 226 C322 220 362 186 416 208 L416 300 L96 300 Z" fill="url(#water)"/>
  <!-- 堤体（アーチダムの断面） -->
  <path d="M96 300 C160 300 176 340 256 340 C336 340 352 300 416 300 L416 336
           C352 336 336 400 256 400 C176 400 160 336 96 336 Z" fill="#f4f7fa"/>
  <!-- 放流 -->
  <rect x="238" y="336" width="36" height="76" rx="18" fill="#bfe6f7"/>
</svg>
"""


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ヘッドレス Chrome はウィンドウ幅を 512 未満にできない（Windows）。
# そこで CSS 上は常に 512px 以上で描き、device-scale-factor で目的の画素数に落とす。
MIN_W = 512


def rasterize(chrome: str, html: str, out: Path, css_w: int, css_h: int, scale: float = 1.0) -> bool:
    """css_w×css_h で描画し、scale 倍した画素数の PNG を書き出す。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.html"
        src.write_text(html, encoding="utf-8")
        profile = Path(tmp) / "profile"
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size={max(css_w, MIN_W)},{css_h}",
            f"--force-device-scale-factor={scale}",
            "--default-background-color=00000000",
            "--virtual-time-budget=4000",
            f"--screenshot={out}",
            src.as_uri(),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except (subprocess.TimeoutExpired, OSError) as ex:
            print(f"  ! {out.name}: {ex}", file=sys.stderr)
            return False
    return out.exists()


def icon_html(size: int, maskable: bool) -> str:
    """SVG をぴったり size×size で描く。maskable は安全域(80%)に縮めて中央配置。"""
    inner = 0.78 if maskable else 1.0
    pad = (1 - inner) / 2 * size
    bg = "#14507d" if maskable else "transparent"
    return f"""<!DOCTYPE html><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;width:{size}px;height:{size}px;overflow:hidden;background:{bg}}}
  svg{{position:absolute;left:{pad}px;top:{pad}px;width:{size * inner}px;height:{size * inner}px}}
</style>
{ICON_SVG}"""


def og_from_illust_html(title: str, subtitle: str, img_uri: str) -> str:
    """ダムのイラストから OGP 画像を作る。

    イラストは 3:2 で作る前提。OGP は 1200x630（およそ 1.9:1）なので、
    object-fit: cover で中央を切り出す。文字は下部のグラデーションの上に置くので
    どんな絵でも読める。**ダムごとに OGP 画像を手作りする必要はない。**
    """
    return f"""<!DOCTYPE html><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;width:1200px;height:630px;overflow:hidden;background:#0f4470}}
  .bg{{position:absolute;inset:0}}
  .bg img{{width:100%;height:100%;object-fit:cover;display:block}}
  .veil{{position:absolute;left:0;right:0;bottom:0;height:62%;
    background:linear-gradient(180deg,rgba(6,30,52,0) 0%,rgba(6,30,52,.55) 45%,rgba(6,30,52,.90) 100%)}}
  .inner{{position:absolute;left:0;right:0;bottom:0;padding:0 78px 62px;
    font-family:{FONT_STACK};color:#fff}}
  h1{{font-size:{78 if len(title) <= 9 else 62}px;font-weight:800;line-height:1.2;margin:0;
     text-shadow:0 2px 20px rgba(0,0,0,.45)}}
  p{{font-size:29px;margin:16px 0 0;opacity:.94;font-weight:600;
     text-shadow:0 1px 12px rgba(0,0,0,.5)}}
  .tag{{position:absolute;top:52px;left:78px;font-family:{FONT_STACK};color:#fff;
    font-size:23px;font-weight:700;letter-spacing:.14em;opacity:.9;
    text-shadow:0 2px 12px rgba(0,0,0,.5)}}
</style>
<div class="bg"><img src="{img_uri}" alt=""></div>
<div class="veil"></div>
<div class="tag">{SITE['site_name']}</div>
<div class="inner"><h1>{title}</h1><p>{subtitle}</p></div>
"""


def og_html(title: str, subtitle: str, tag: str) -> str:
    return f"""<!DOCTYPE html><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;width:1200px;height:630px;overflow:hidden}}
  body{{
    font-family:{FONT_STACK};
    background:linear-gradient(135deg,#0f4470 0%,#1c6ba8 55%,#2f9fd4 100%);
    color:#fff;position:relative;
  }}
  .wave{{position:absolute;left:0;right:0;bottom:0;height:210px;
    background:linear-gradient(180deg,rgba(127,211,240,.00) 0%,rgba(127,211,240,.30) 100%)}}
  .dam{{position:absolute;left:0;right:0;bottom:0;height:120px;
    background:#f4f7fa;clip-path:ellipse(78% 100% at 50% 100%);opacity:.16}}
  .inner{{position:absolute;inset:0;padding:74px 78px;display:flex;flex-direction:column;justify-content:center}}
  .tag{{font-size:26px;font-weight:700;letter-spacing:.16em;opacity:.85;margin-bottom:22px}}
  h1{{font-size:{78 if len(title) <= 9 else 62}px;font-weight:800;line-height:1.22;margin:0;
     letter-spacing:.01em;text-shadow:0 2px 18px rgba(0,0,0,.20)}}
  p{{font-size:31px;margin:24px 0 0;opacity:.92;line-height:1.5;font-weight:600}}
  .rule{{width:104px;height:7px;background:#7fd3f0;border-radius:4px;margin:30px 0 0}}
  .src{{position:absolute;left:78px;bottom:44px;font-size:19px;opacity:.72;font-weight:600}}
</style>
<div class="wave"></div><div class="dam"></div>
<div class="inner">
  <div class="tag">{tag}</div>
  <h1>{title}</h1>
  <p>{subtitle}</p>
  <div class="rule"></div>
</div>
<div class="src">{SITE['producer']} ／ 出典：国土交通省 川の防災情報・地理院タイル</div>
"""


def resolve_illust(src: str | None) -> Path | None:
    """illustrations.json のパス（"./img/dams/x.webp" 等）を実ファイルに解決する。"""
    if not src or src.startswith(("http://", "https://", "data:")):
        return None
    rel = src.lstrip("./")
    f = DOCS / rel
    return f if f.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description="PWA アイコンと OGP 画像を生成（単発）")
    ap.add_argument("--icons-only", action="store_true")
    args = ap.parse_args()

    IMG.mkdir(parents=True, exist_ok=True)
    (IMG / "icon.svg").write_text(ICON_SVG, encoding="utf-8", newline="\n")
    print("[make_images] docs/img/icon.svg")

    chrome = find_chrome()
    if not chrome:
        print("[make_images] Chrome/Edge が見つからないため PNG 生成をスキップしました。\n"
              "               icon.svg だけ書き出しています。PNG が必要な環境で再実行してください。",
              file=sys.stderr)
        return 0
    print(f"[make_images] renderer = {chrome}")

    # 常に 512x512 の CSS で描き、scale で 192/180 に落とす（正方形を保つため）
    for name, size, maskable in (
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-180.png", 180, False),
        ("icon-maskable-512.png", 512, True),
    ):
        ok = rasterize(chrome, icon_html(MIN_W, maskable), IMG / name,
                       MIN_W, MIN_W, size / MIN_W)
        print(f"[make_images] {'ok ' if ok else 'NG '} docs/img/{name} ({size}x{size})")

    if args.icons_only:
        return 0

    if not DATA.exists():
        print("[make_images] dams.json が無いので OGP 画像は作れません。", file=sys.stderr)
        return 1
    data = json.loads(DATA.read_text(encoding="utf-8"))

    # ダムID → イラストのパス。ここに登録されていれば OGP をイラストから自動生成する。
    illust: dict[str, str] = {}
    if ILLUST.exists():
        try:
            illust = json.loads(ILLUST.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as ex:
            print(f"[make_images] illustrations.json を読めません: {ex}", file=sys.stderr)

    OG.mkdir(parents=True, exist_ok=True)
    ok = rasterize(chrome, og_html(SITE["site_name"],
                                   "旅の寄り道に、ダムはいかが？",
                                   SITE["name_en"]), OG / "site.png", 1200, 630, 1.0)
    print(f"[make_images] {'ok ' if ok else 'NG '} docs/img/og/site.png")

    for dam in data["dams"]:
        slug = dam.get("slug")
        if not slug:
            continue
        off = dam.get("official") or {}
        sub = f"{off.get('water_system','')}水系 {off.get('river','')}"
        if dam.get("address"):
            sub += "　" + dam["address"]

        # イラストが登録されていればそれを使う。無ければ汎用カード。
        src = illust.get(dam["id"]) or illust.get(dam["name"])
        path = resolve_illust(src)
        if path:
            html = og_from_illust_html(dam["name"], sub, path.as_uri())
            kind = "illust"
        else:
            html = og_html(dam["name"], sub, SITE["site_name"])
            kind = "generic"

        ok = rasterize(chrome, html, OG / f"{dam['id']}.png", 1200, 630, 1.0)
        print(f"[make_images] {'ok ' if ok else 'NG '} docs/img/og/{dam['id']}.png ({kind})")

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
