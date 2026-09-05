#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""イラスト（カード／アイコン）を Web 配信用に最適化する **単発** スクリプト。

生成AIが書き出した PNG は 1 枚 3MB 前後あり、そのまま置くとサイトが数倍に膨らむ。
観光アプリはスマホで見られるので、ここは削っておく価値が大きい。

やること
  * 指定した幅にリサイズ（アスペクト比は維持）
  * WebP に変換（写真的なイラストでは PNG の 1/10 前後になる）
  * 出力先に保存

依存ライブラリは増やさない。make_images.py と同じくローカルの Chrome を使う。
（canvas に描いて toDataURL で WebP 化し、base64 を回収する）

使い方
  python scripts/optimize_images.py --src <入力ディレクトリ> --out docs/img/dams \\
      --width 1600 --quality 82 --strip-double-ext
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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


def find_chrome() -> str | None:
    import shutil
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


PAGE = """<!DOCTYPE html><meta charset="utf-8"><body><pre id="out">WAIT</pre>
<script>
(function () {
  var img = new Image();
  img.onload = function () {
    var scale = Math.min(1, %(width)d / img.naturalWidth);
    var w = Math.round(img.naturalWidth * scale);
    var h = Math.round(img.naturalHeight * scale);
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    var x = c.getContext("2d");
    x.imageSmoothingEnabled = true;
    x.imageSmoothingQuality = "high";
    x.drawImage(img, 0, 0, w, h);
    try {
      document.getElementById("out").textContent =
        w + "x" + h + "|" + c.toDataURL("%(mime)s", %(quality)s);
    } catch (e) {
      document.getElementById("out").textContent = "ERR " + e.message;
    }
  };
  img.onerror = function () { document.getElementById("out").textContent = "ERR load"; };
  img.src = "%(src)s";
})();
</script>"""


def convert(chrome: str, src: Path, out: Path, width: int, quality: int, mime: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "c.html"
        page.write_text(
            PAGE % {"width": width, "quality": quality / 100, "mime": mime,
                    "src": src.resolve().as_uri()},
            encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={Path(tmp) / 'p'}",
            "--allow-file-access-from-files",   # canvas を汚染扱いにしないため
            "--virtual-time-budget=20000",
            "--dump-dom", page.as_uri(),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as ex:
            return False, f"chrome: {ex}"

    dom = r.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    if not m:
        return False, "出力を取得できませんでした"
    payload = m.group(1).strip()
    if payload.startswith("ERR") or payload == "WAIT":
        return False, payload

    dims, _, data_url = payload.partition("|")
    b64 = data_url.split(",", 1)[1] if "," in data_url else ""
    if not b64:
        return False, "dataURL が空です"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(b64))
    return True, dims


def main() -> int:
    ap = argparse.ArgumentParser(description="イラストを Web 配信用に最適化（単発）")
    ap.add_argument("--src", required=True, help="入力ディレクトリまたはファイル")
    ap.add_argument("--out", default=str(ROOT / "docs" / "img" / "dams"), help="出力ディレクトリ")
    ap.add_argument("--width", type=int, default=1600, help="最大幅px（これ以下なら拡大しない）")
    ap.add_argument("--quality", type=int, default=82, help="画質 1-100")
    ap.add_argument("--format", choices=["webp", "jpeg"], default="webp")
    ap.add_argument("--strip-double-ext", action="store_true",
                    help="'name.png.png' のような二重拡張子を 1 つに直す")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("[optimize] Chrome / Edge が見つかりません。", file=sys.stderr)
        return 1

    src = Path(args.src)
    files = sorted(p for p in ([src] if src.is_file() else src.iterdir())
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
    if not files:
        print(f"[optimize] {src} に画像がありません。", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    mime = "image/webp" if args.format == "webp" else "image/jpeg"
    ext = ".webp" if args.format == "webp" else ".jpg"

    total_before = total_after = 0
    ng = 0
    for f in files:
        stem = f.name
        if args.strip_double_ext:
            # "toyama-goi.png.png" → "toyama-goi"
            while True:
                s2 = re.sub(r"\.(png|jpg|jpeg|webp)$", "", stem, flags=re.I)
                if s2 == stem:
                    break
                stem = s2
        else:
            stem = f.stem
        out = out_dir / (stem + ext)
        ok, info = convert(chrome, f, out, args.width, args.quality, mime)
        before = f.stat().st_size
        after = out.stat().st_size if ok and out.exists() else 0
        total_before += before
        if ok:
            total_after += after
            print(f"[optimize] ok  {f.name}  →  {out.name}  "
                  f"{info}  {before/1024/1024:.2f}MB → {after/1024:.0f}KB "
                  f"({after/before*100:.1f}%)")
        else:
            ng += 1
            print(f"[optimize] NG  {f.name}: {info}", file=sys.stderr)

    print(f"[optimize] 合計 {total_before/1024/1024:.1f}MB → {total_after/1024/1024:.2f}MB"
          f"（{ng} 件失敗）")
    return 1 if ng else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
