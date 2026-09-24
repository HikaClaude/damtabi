#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域「雨の行き先」のローカルプレビュー（公開しない・承認しない）。

リポジトリの外（既定は一時フォルダ）に docs/ の写しを作り、QA に合格した公開候補を
**プレビュー専用の模擬承認**で表示できるようにして、127.0.0.1 だけで配信する。

  python scripts/preview_watershed.py                # 作って配信（ブラウザが開く）。Ctrl+C で終了
  python scripts/preview_watershed.py --no-serve     # 作るだけ
  python scripts/preview_watershed.py --port 8791    # ポートを変える

守ること
- リポジトリの docs/・data/watershed/release.json には一切書かない。模擬承認は写しの中だけ
  （approved_by に「プレビュー（公開承認ではない）」と書く）
- 配信は 127.0.0.1 のみ（同じ LAN の他の端末からは見えない）
- 個別に保留したダム（data/watershed/holds.json）は既定で表示しない（公開時と同じ）。
  起動時に保留の理由を表示する。確かめるために見たいときは --include-held
- 画面の注記（data/watershed/notes.json）は写しにも持ち込む
- 写しを置いたフォルダには目印ファイルを置き、目印のあるフォルダだけを作り直す（消す）
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_site as bs  # noqa: E402
import ws_pipeline as wp  # noqa: E402

DEFAULT_OUT = Path(tempfile.gettempdir()) / "damtabi-watershed-preview"
MARKER = ".damtabi-watershed-preview"
BANNER = ('<div id="preview-banner" style="position:fixed;left:50%;transform:translateX(-50%);bottom:6px;z-index:50;'
          'background:#7a3e00;color:#fff;font:600 12px/1.6 system-ui,sans-serif;padding:3px 12px;border-radius:999px;'
          'opacity:.9;pointer-events:none">プレビュー（公開していません・承認前）</div>')


class PreviewError(Exception):
    pass


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def build(out: Path, include_held: bool = False) -> dict:
    out = out.resolve()
    if _inside(out, ROOT):
        raise PreviewError(f"プレビューはリポジトリの外に作ります（指定: {out}）")
    if out.exists():
        if not (out / MARKER).exists():
            raise PreviewError(f"{out} はプレビュー用のフォルダではありません（目印 {MARKER} が無い）。別のフォルダを指定してください")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER).write_text("DAM TABI 集水域プレビュー（消してよい）\n", encoding="utf-8")

    # 画面一式（公開用の docs/watershed は持ち込まない。写しの中で作り直す）
    shutil.copytree(ROOT / "docs", out / "docs", ignore=shutil.ignore_patterns("watershed"))
    shutil.copytree(ROOT / "data/watershed/dams", out / "data/watershed/dams")
    shutil.copytree(ROOT / "data/watershed/staged/flow", out / "data/watershed/staged/flow")
    holds_src = ROOT / wp.HOLDS_FILE
    store = wp.Store(out)
    holds = json.loads(holds_src.read_text(encoding="utf-8"))["holds"] if holds_src.exists() else {}
    for extra in (wp.NOTES_FILE,):                      # 画面の注記は写しにも入れる
        if (ROOT / extra).exists():
            (out / extra).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / extra, out / extra)
    if not include_held and holds_src.exists():
        (out / wp.HOLDS_FILE).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(holds_src, out / wp.HOLDS_FILE)

    # プレビュー専用の模擬承認（写しの中だけ）。QA 合格・exact の候補すべて
    approvals = {}
    for did, st in store.all_states().items():
        pub = st.get("published")
        if pub and pub.get("outline_method") == wp.RELEASABLE_OUTLINE:
            approvals[did] = {"flow_version": pub["flow_version"], "ring_version": wp.ring_version(pub["ring"]),
                              "outline_method": pub["outline_method"],
                              "approved_by": "プレビュー（公開承認ではない）", "approved_on": "preview"}
    rel = out / wp.RELEASE_FILE
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_text(json.dumps({"schema": wp.RELEASE_SCHEMA, "approvals": approvals}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    dams = json.loads((out / "docs/data/dams.json").read_text(encoding="utf-8"))["dams"]
    plan = wp.assemble(store, dams)

    # 資産の版（Service Worker のキャッシュ世代）を写しの内容で付け直す
    saved = bs.DOCS
    try:
        bs.DOCS = out / "docs"
        ver = bs.asset_version()
        if not (bs.write_sw_version(ver) and bs.write_index_assets(ver)):
            raise PreviewError("資産の版付けに失敗しました")
    finally:
        bs.DOCS = saved
    idx = out / "docs/index.html"
    idx.write_text(idx.read_text(encoding="utf-8").replace("</body>", BANNER + "\n</body>"), encoding="utf-8")
    return {"out": out, "public_ids": plan["public_ids"], "holds": holds, "include_held": include_held,
            "asset_version": ver}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):          # 1行ずつの通信記録は出さない
        pass


def serve(docs: Path, port: int, open_browser: bool) -> None:
    handler = functools.partial(_Handler, directory=str(docs))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"\n配信中: {url}   （このパソコンからだけ見えます）")
        print("終了するには、この画面で Ctrl+C を押してください。\n")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n終了しました。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="集水域のローカルプレビュー（公開しない・承認しない）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"写しを置くフォルダ（既定 {DEFAULT_OUT}）")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-serve", action="store_true", help="作るだけで配信しない")
    ap.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    ap.add_argument("--include-held", action="store_true", help="個別に保留したダムも表示する（確かめるため。既定は外す）")
    args = ap.parse_args(argv)
    try:
        r = build(args.out, args.include_held)
    except (PreviewError, wp.PipelineError) as e:
        print(f"プレビューを作れませんでした: {e}")
        return 2
    print(f"プレビューを作りました: {r['out']}")
    print(f"表示する集水域: {len(r['public_ids'])} 基（プレビュー専用の模擬承認。公開承認ではありません）")
    if r["holds"]:
        print("個別に保留しているダム" + ("（確認のため表示しています）" if r["include_held"] else "（表示していません）") + ":")
        for did, h in r["holds"].items():
            print(f"  - {did}: {h['reason']}")
    if not args.no_serve:
        serve(r["out"] / "docs", args.port, not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
