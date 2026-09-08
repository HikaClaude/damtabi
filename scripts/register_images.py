#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docs/img/ に置いた画像を、マニフェストへ登録する（単発）。

Codex 側で作った画像を後から差し込むための道具。
ファイル名がダムID（例 ishikawa-tedorigawa.webp）になっていれば、
置くだけでこのスクリプトが拾って illustrations.json / dam-icons.json に足す。

やること
  * docs/img/dams/<ダムID>.webp     → illustrations.json（カード。詳細パネル・個別ページ・OGP）
  * docs/img/dam-icons/<ダムID>.webp → dam-icons.json（地図ピン）
  * dams.json に無いIDのファイルは登録しない（名前の間違いに気づけるよう警告する）

使い方
  python scripts/register_images.py            そのまま登録
  python scripts/register_images.py --dry-run  何が登録されるか見るだけ
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DAMS_JSON = DOCS / "data" / "dams.json"

# 置き場所 → 登録先マニフェスト
KINDS = [
    ("dams", "illustrations.json", "カード（3:2）"),
    ("dam-icons", "dam-icons.json", "地図ピン（正方形）"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="置いた画像をマニフェストへ登録する")
    ap.add_argument("--dry-run", action="store_true", help="登録せず、何が起きるかだけ表示")
    args = ap.parse_args()

    if not DAMS_JSON.exists():
        print(f"[register_images] {DAMS_JSON} がありません。"
              "先に fetch_dams.py を実行してください。", file=sys.stderr)
        return 1

    dams = json.loads(DAMS_JSON.read_text(encoding="utf-8"))["dams"]
    known = {d["id"]: d["name"] for d in dams}

    changed = False
    for folder, manifest_name, label in KINDS:
        img_dir = DOCS / "img" / folder
        manifest = DOCS / "data" / manifest_name
        current = {}
        if manifest.exists():
            try:
                current = json.loads(manifest.read_text(encoding="utf-8")) or {}
            except json.JSONDecodeError as ex:
                print(f"[register_images] {manifest_name} を読めません: {ex}", file=sys.stderr)
                return 1

        if not img_dir.exists():
            print(f"[register_images] {img_dir} がありません（スキップ）")
            continue

        added, unknown = [], []
        for f in sorted(img_dir.glob("*.webp")):
            dam_id = f.stem
            if dam_id not in known:
                unknown.append(f.name)
                continue
            path = f"./img/{folder}/{f.name}"
            if current.get(dam_id) != path:
                current[dam_id] = path
                added.append(dam_id)

        print(f"\n[{label}] {img_dir.relative_to(ROOT)}")
        print(f"  登録済み {len(current)} 件 / 今回追加・更新 {len(added)} 件")
        for i in added:
            print(f"    + {i}（{known[i]}）")
        for u in unknown:
            # ダム名の綴り違いはここで気づける。勝手に推測して登録はしない
            print(f"    ? {u} は dams.json にIDが無いため登録しません", file=sys.stderr)

        if added and not args.dry_run:
            manifest.write_text(
                json.dumps(dict(sorted(current.items())), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
            changed = True

    if args.dry_run:
        print("\n（--dry-run のため書き込みませんでした）")
    elif changed:
        print("\n登録しました。次に実行してください:")
        print("  python scripts/build_site.py     静的ページに反映")
        print("  python scripts/make_images.py    OGP画像を作り直す")
    else:
        print("\n新しく登録するものはありませんでした。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
