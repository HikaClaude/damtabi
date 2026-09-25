#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集水域を本番に組み込んだときの公開用成果物を、リポジトリの外で作って確かめる（公開しない）。

本番準備の一覧（data/watershed/release_ready.json）を、リポジトリの**写しの中でだけ** release.json として使い、
通常の公開用ビルド（build_flowgrids.py --assemble-only → build_site.py）をそのまま実行する。
プレビュー専用の処理（模擬承認の全候補・画面の帯）は使わない。

  python scripts/build_release_candidate.py                 # 作って確かめる（配信しない）
  python scripts/build_release_candidate.py --serve         # 作って 127.0.0.1 で配信（Ctrl+C で終了）

守ること
- リポジトリの docs/・data/watershed/release.json には書かない（写しの中だけ）
- 写しは git が追跡しているファイルと、まだ追加していない新しいファイル（.gitignore 対象は除く）
- 確かめること: 公開物が docs/watershed/{index.json, basins.geojson, flow/<id>.json} だけで、
  一覧の全基が approved になり、保留（holds.json）のダムが混ざらず、索引・輪・格子の版と ID が揃うこと。
  もう一度ビルドしても変わらないこと
- 配信は 127.0.0.1 のみ。写しを置くフォルダには目印を置き、目印のあるフォルダだけを作り直す
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ws_common as wc  # noqa: E402
import ws_pipeline as wp  # noqa: E402

DEFAULT_OUT = Path(tempfile.gettempdir()) / "damtabi-release-candidate"
MARKER = ".damtabi-release-candidate"


class CandidateError(Exception):
    pass


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def copy_repo(out: Path) -> int:
    """追跡中＋未追加（.gitignore 対象を除く）のファイルを写す。"""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT,
                           capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    n = 0
    for rel in filter(None, files):
        src = ROOT / rel
        if not src.is_file():
            continue                      # 削除済みでまだ index に残っているもの
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    return n


def run(out: Path, *args: str) -> str:
    r = subprocess.run([sys.executable, *args], cwd=out, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        raise CandidateError(f"{' '.join(args)} が失敗しました（{r.returncode}）\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


def public_files(out: Path) -> list[str]:
    d = out / "docs" / "watershed"
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())


def snapshot(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in (out / "docs").rglob("*") if p.is_file()}


def verify(out: Path) -> dict:
    store = wp.Store(out)
    ready = wp.Store(ROOT).load_release(wp.RELEASE_READY_FILE)
    holds = store.load_holds()
    idx = json.loads((out / "docs/watershed/index.json").read_text(encoding="utf-8"))
    ids = [d["id"] for d in idx["dams"]]
    problems = []
    if sorted(ids) != sorted(ready):
        problems.append(f"公開索引の基数 {len(ids)} が一覧 {len(ready)} と一致しない: "
                        f"欠け {sorted(set(ready) - set(ids))} / 余分 {sorted(set(ids) - set(ready))}")
    mixed = sorted(set(ids) & set(holds))
    if mixed:
        problems.append(f"保留のダムが公開索引に入っている: {mixed}")
    expected_files = sorted(["basins.geojson", "index.json"] + [f"flow/{i}.json" for i in ids])
    if public_files(out) != expected_files:
        problems.append("docs/watershed/ のファイルが索引と一致しない")
    gj = json.loads((out / "docs/watershed/basins.geojson").read_text(encoding="utf-8"))
    if [f["properties"]["id"] for f in gj["features"]] != ids or gj.get("version") != idx["basins_version"]:
        problems.append("輪（basins.geojson）の ID・版が索引と一致しない")
    for d in idx["dams"]:
        rec = json.loads((out / "docs/watershed/flow" / f"{d['id']}.json").read_text(encoding="utf-8"))
        if rec.get("id") != d["id"] or rec.get("version") != d["v"] or wc.content_version(rec) != d["v"]:
            problems.append(f"{d['id']}: 格子の ID・版が索引と一致しない")
        if (d["release_status"], d["qa_status"], d["outline_method"]) != ("approved", "pass", "exact"):
            problems.append(f"{d['id']}: 公開の条件がそろっていない {d['release_status']}/{d['qa_status']}/{d['outline_method']}")
    dams = json.loads((out / "docs/data/dams.json").read_text(encoding="utf-8"))["dams"]
    unknown = sorted(set(ids) - {d["id"] for d in dams})
    if unknown:
        problems.append(f"dams.json に無い ID: {unknown}")
    if problems:
        raise CandidateError("公開用成果物の確認に失敗しました:\n  - " + "\n  - ".join(problems))
    return {"public": len(ids), "holds_excluded": sorted(holds), "files": len(public_files(out)),
            "cautions": sorted(d["id"] for d in idx["dams"] if d.get("caution")), "index_version": idx["version"]}


def build(out: Path) -> dict:
    out = out.resolve()
    if _inside(out, ROOT):
        raise CandidateError(f"写しはリポジトリの外に作ります（指定: {out}）")
    if out.exists():
        if not (out / MARKER).exists():
            raise CandidateError(f"{out} は本番相当の写しのフォルダではありません（目印 {MARKER} が無い）")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER).write_text("DAM TABI 集水域の本番相当の写し（消してよい）\n", encoding="utf-8")
    n = copy_repo(out)
    ready = ROOT / wp.RELEASE_READY_FILE
    if not ready.exists():
        raise CandidateError(f"{wp.RELEASE_READY_FILE} がありません")
    # 写しの中でだけ、本番準備の一覧を release.json として使う
    shutil.copyfile(ready, out / wp.RELEASE_FILE)
    log = run(out, "scripts/build_flowgrids.py", "--assemble-only")
    log += run(out, "scripts/build_site.py")
    first = snapshot(out)
    # もう一度、通常の手順で作り直しても変わらない
    run(out, "scripts/build_flowgrids.py", "--assemble-only")
    run(out, "scripts/build_site.py")
    second = snapshot(out)
    changed = sorted(k for k in set(first) | set(second)
                     if first.get(k) != second.get(k) and k != "docs/sitemap.xml")   # sitemap は日付だけ変わりうる
    if changed:
        raise CandidateError(f"2回目のビルドで変わったファイルがあります: {changed[:10]}")
    r = verify(out)
    r.update({"out": out, "copied_files": n, "log": log})
    return r


def serve(docs: Path, port: int) -> None:
    import functools
    import http.server

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(docs))) as httpd:
        print(f"\n配信中: http://127.0.0.1:{port}/   （このパソコンからだけ見えます。公開していません）")
        print("終了するには、この画面で Ctrl+C を押してください。\n", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n終了しました。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="集水域の本番相当の公開用成果物を、リポジトリの外で作って確かめる（公開しない）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"写しを置くフォルダ（既定 {DEFAULT_OUT}）")
    ap.add_argument("--serve", action="store_true", help="作ったあと 127.0.0.1 で配信する")
    ap.add_argument("--port", type=int, default=8792)
    args = ap.parse_args(argv)
    try:
        r = build(args.out)
    except (CandidateError, wp.PipelineError, subprocess.CalledProcessError) as e:
        print(f"作れませんでした: {e}")
        return 2
    print(f"本番相当の写し: {r['out']}（{r['copied_files']} ファイルを写して、通常の公開用ビルドを実行）")
    print(f"公開用の集水域: {r['public']} 基 / docs/watershed/ のファイル {r['files']} 件 / 索引版 {r['index_version']}")
    print(f"保留で外したダム: {', '.join(r['holds_excluded']) or 'なし'}")
    print(f"注記の付くダム（notes.json）: {', '.join(r['cautions']) or 'なし'}")
    print("リポジトリの docs/ と release.json は変更していません。")
    if args.serve:
        serve(r["out"] / "docs", args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
