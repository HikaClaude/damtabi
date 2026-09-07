#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ダム旅 — 貯水率を更新して公開するところまでを 1 本にまとめた手動更新ツール。

既存の処理を順番に呼ぶだけで、取得のしかたは何も変えていません。

  1. python scripts/fetch_dams.py    貯水率を 1 回だけ取得
  2. 取得結果の点検               件数・状態・観測時刻がおかしくないか
  3. python scripts/build_site.py    ダムページ・sitemap などを作り直す
  4. 生成物の点検                 ファイルが揃っているか、JSON が壊れていないか
  5. 確認してから公開             GitHub へ push
  6. damtabi.com に出たか確認     反映されるまで待って照合

**自動実行はしません。** 人が実行したときに 1 回だけ取得します
（川の防災情報の利用条件により、定期的な収集はしない方針のため）。

途中で少しでもおかしければ公開へ進まず、日本語で理由を出して止まります。
その場合 docs/data/dams.json は実行前の状態に戻します。

使い方（ふつうは 更新.cmd をダブルクリックするだけ）:
  python scripts/update_and_publish.py            対話つき
  python scripts/update_and_publish.py --yes      確認を省いて公開まで進む
  python scripts/update_and_publish.py --no-push  公開せず、手元の更新だけ
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAMS_JSON = ROOT / "docs" / "data" / "dams.json"
SITE_JSON = ROOT / "site.json"

# このツールが commit してよいファイル。これ以外は絶対に触らない。
# （集水域の試作など、作りかけの変更を巻き込んで公開しないため）
PUBLISH_PATHS = [
    "docs/data/dams.json",
    "docs/dam",
    "docs/index.html",
    "docs/sitemap.xml",
    "docs/robots.txt",
    "docs/manifest.json",
]

LINE = "─" * 62


# ---------------------------------------------------------------- 表示

def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, title: str) -> None:
    say()
    say(f"[{n}/{total}] {title}")


_cleanup = []


def on_stop(fn) -> None:
    """中止時に必ず走らせたい後始末（バックアップの復元・削除など）を登録する。"""
    _cleanup.append(fn)


def stop(reason: str, detail: str = "") -> None:
    """公開に進まず終了する。理由は必ず日本語で書くこと。"""
    for fn in _cleanup:
        try:
            fn()
        except Exception:
            pass
    say()
    say(LINE)
    say("  中止しました")
    say(LINE)
    say()
    say(f"  {reason}")
    if detail:
        say()
        for line in detail.splitlines():
            say(f"    {line}")
    say()
    say("  公開はしていません。サイトは今まで通りのままです。")
    say()
    raise SystemExit(1)


# ---------------------------------------------------------------- 外部コマンド

def run(cmd: list[str], title: str) -> subprocess.CompletedProcess:
    """子プロセスを動かす。出力はそのまま見せる（何が起きたか隠さない）。"""
    try:
        return subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8",
                              errors="replace", capture_output=True)
    except OSError as e:
        stop(f"{title} を実行できませんでした。", str(e))


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, text=True,
                          encoding="utf-8", errors="replace", capture_output=True)


def git_ok(*args: str) -> str:
    r = git(*args)
    if r.returncode != 0:
        stop("Git の操作に失敗しました。",
             f"git {' '.join(args)}\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def dirty_files() -> set[str]:
    """まだ commit していない変更のあるファイル（パスは / 区切り）。"""
    out = git("status", "--porcelain", "-uall").stdout
    files = set()
    for line in out.splitlines():
        if len(line) > 3:
            files.add(line[3:].strip().strip('"'))
    return files


def hashes_of(paths: list[str]) -> dict[str, str]:
    """公開対象ファイルの中身のハッシュ。実行の前後で比べて変化を見る。"""
    r = git("ls-files", "-s", "--", *paths)
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0].split()[1]
    # 作業ツリーの実際の中身で上書きする（index ではなくファイルを見る）
    for name in list(out):
        f = ROOT / name
        out[name] = git("hash-object", str(f)).stdout.strip() if f.exists() else "なし"
    return out


# ---------------------------------------------------------------- 点検

def load_json(path: Path, label: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stop(f"{label} が見つかりません。", str(path))
    except json.JSONDecodeError as e:
        stop(f"{label} が壊れています（JSON として読めません）。", f"{path}\n{e}")


def check_data(new: dict, old: dict | None) -> dict:
    """取得結果がまともかどうか。おかしければここで止める。"""
    dams = new.get("dams")
    if not isinstance(dams, list) or not dams:
        stop("ダムのデータが空でした。取得に失敗しています。")

    need = ("id", "name", "rate_irrigation", "rate_effective", "data_status")
    for d in dams:
        missing = [k for k in need if k not in d]
        if missing:
            stop("ダムのデータに必要な項目がありません。",
                 f"{d.get('name', '(名前不明)')} に {', '.join(missing)} がありません")

    counts: dict[str, int] = {}
    for d in dams:
        counts[d["data_status"]] = counts.get(d["data_status"], 0) + 1

    total = len(dams)
    ok = counts.get("ok", 0)
    failed = counts.get("fetch_failed", 0)

    if failed and failed * 3 >= total:
        stop(f"{total}基のうち {failed}基で取得に失敗しました。",
             "通信の調子が悪いか、提供元が一時的に応答していない可能性があります。\n"
             "しばらく時間をおいて、もう一度実行してください。")

    if old:
        old_dams = old.get("dams") or []
        if old_dams and len(old_dams) != total:
            stop(f"ダムの数が前回と違います（前回 {len(old_dams)}基 → 今回 {total}基）。",
                 "ダムの一覧は変わらないはずなので、取得がおかしくなっています。")

        old_ok = sum(1 for d in old_dams if d.get("data_status") == "ok")
        if old_ok >= 4 and ok * 2 < old_ok:
            stop(f"値を取得できたダムが急に減りました（前回 {old_ok}基 → 今回 {ok}基）。",
                 "提供元が一時的に不調な可能性があります。\n"
                 "しばらく時間をおいて、もう一度実行してください。")

        a, b = old.get("base_obs_time"), new.get("base_obs_time")
        if a and b and b < a:
            stop(f"観測時刻が前回より古くなっています（前回 {a} → 今回 {b}）。",
                 "取得がおかしくなっています。時間をおいて、もう一度実行してください。")

    return counts


def check_built() -> None:
    """サイトの生成物が揃っているか。"""
    for name in ("sitemap.xml", "robots.txt", "manifest.json", "index.html"):
        f = ROOT / "docs" / name
        if not f.exists() or f.stat().st_size == 0:
            stop(f"サイトの生成に失敗しました（docs/{name} がありません）。")

    load_json(ROOT / "docs" / "manifest.json", "docs/manifest.json")

    pages = sorted((ROOT / "docs" / "dam").rglob("index.html"))
    dams = load_json(DAMS_JSON, "docs/data/dams.json").get("dams", [])
    # ダムごとのページ + 一覧ページ
    if len(pages) < len(dams) + 1:
        stop(f"ダムのページが足りません（{len(dams)}基ぶん必要ですが {len(pages) - 1}枚しかありません）。")


# ---------------------------------------------------------------- 公開の確認

def wait_until_live(expect: str, base_url: str, minutes: int = 6) -> bool:
    """damtabi.com に新しい観測時刻が出るまで待つ。"""
    url = base_url.rstrip("/") + "/data/dams.json"
    deadline = time.time() + minutes * 60
    tries = 0
    while time.time() < deadline:
        tries += 1
        try:
            req = urllib.request.Request(
                f"{url}?t={int(time.time())}",
                headers={"User-Agent": "damtabi-updater", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as r:
                live = json.loads(r.read().decode("utf-8"))
            if live.get("base_obs_time") == expect:
                return True
            say(f"    …まだ反映前です（公開中の値: {live.get('base_obs_time')}）")
        except Exception as e:
            say(f"    …確認中（{type(e).__name__}）")
        time.sleep(20)
    return False


# ---------------------------------------------------------------- 本体

def main() -> int:
    ap = argparse.ArgumentParser(description="ダム旅の貯水率を更新して公開する")
    ap.add_argument("--yes", action="store_true", help="確認を省いて公開まで進む")
    ap.add_argument("--no-push", action="store_true", help="公開せず手元の更新だけ")
    args = ap.parse_args()

    total_steps = 5 if args.no_push else 6

    say(LINE)
    say("  ダム旅 — 貯水率の更新")
    say(LINE)
    say()
    say("  提供元に負担をかけないよう、取得は 1 回だけ行います。")

    if not (ROOT / ".git").exists():
        stop("リポジトリの場所が正しくありません。", str(ROOT))

    # 実行前の状態を控えておく
    old = load_json(DAMS_JSON, "docs/data/dams.json") if DAMS_JSON.exists() else None
    backup = None
    if DAMS_JSON.exists():
        backup = DAMS_JSON.with_suffix(".json.bak")
        shutil.copyfile(DAMS_JSON, backup)

    before_dirty = dirty_files()
    before_hash = hashes_of(PUBLISH_PATHS)

    def restore() -> None:
        """取得前の dams.json に戻し、控えを消す。"""
        if backup and backup.exists():
            shutil.copyfile(backup, DAMS_JSON)
            backup.unlink()

    def drop_backup() -> None:
        if backup and backup.exists():
            backup.unlink()

    on_stop(restore)

    # ---- 1. 取得
    step(1, total_steps, "いまの貯水率を取りに行きます")
    r = run([sys.executable, "scripts/fetch_dams.py"], "貯水率の取得")
    if r.returncode != 0:
        restore()
        stop("貯水率を取得できませんでした。",
             "インターネットにつながっているか確認して、もう一度実行してください。\n\n"
             + (r.stderr or r.stdout or "").strip()[-800:])
    say("    取得できました。")

    # ---- 2. 取得結果の点検
    step(2, total_steps, "取れた内容を点検します")
    new = load_json(DAMS_JSON, "docs/data/dams.json")
    counts = check_data(new, old)
    n_total = len(new["dams"])
    n_ok = counts.get("ok", 0)
    n_nodata = n_total - n_ok
    say(f"    {n_total}基ぶんのデータが揃っています。")

    # ---- 3. サイトを作り直す
    step(3, total_steps, "サイトのページを作り直します")
    r = run([sys.executable, "scripts/build_site.py"], "サイトの生成")
    if r.returncode != 0:
        restore()
        stop("サイトのページを作り直せませんでした。",
             (r.stderr or r.stdout or "").strip()[-800:])
    say("    作り直しました。")

    # ---- 4. 生成物の点検
    step(4, total_steps, "できあがったファイルを点検します")
    check_built()
    say("    問題ありません。")

    # 今回の実行で中身が変わったファイルだけを公開対象にする
    after_hash = hashes_of(PUBLISH_PATHS)
    changed = sorted(n for n, h in after_hash.items() if before_hash.get(n) != h)

    # 更新前から別の変更が残っていたファイルは、巻き込まないように止める
    overlap = [n for n in changed if n in before_dirty]
    if overlap:
        restore()
        stop("公開しようとしたファイルに、今回の更新とは別の変更が残っています。",
             "先にそちらを片づけてから実行してください。\n\n" + "\n".join(overlap))

    say()
    say(LINE)
    say("  今回の結果")
    say(LINE)
    say(f"    観測時刻     {new.get('base_obs_time', '不明')}")
    say(f"    収録         {n_total}基")
    say(f"    値が取れた   {n_ok}基")
    say(f"    値がない     {n_nodata}基（欠測・未提供・データ提供なし）")
    say(f"    前回から     {'変化なし' if not changed else str(len(changed)) + ' ファイルを更新'}")
    say(LINE)

    if not changed:
        say()
        say("  提供元の値が前回から変わっていないため、公開する変更はありません。")
        say("  サイトは今まで通りのままです。")
        drop_backup()
        return 0

    if args.no_push:
        say()
        say("  手元の更新まで終わりました（公開はしていません）。")
        drop_backup()
        return 0

    # ---- 5. 公開してよいか確認
    step(5, total_steps, "公開してよいか確認します")
    if not args.yes:
        say()
        try:
            ans = input("    damtabi.com に反映しますか？  [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            restore()
            say()
            say("  公開せずに終わりました。サイトは今まで通りのままです。")
            drop_backup()
            return 0

    add = git("add", "--", *changed)
    if add.returncode != 0:
        stop("公開の準備に失敗しました。", (add.stderr or "").strip())

    msg = f"data: 貯水率を更新（{new.get('base_obs_time', '')} 観測）"
    c = git("commit", "-m", msg)
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        stop("記録（commit）に失敗しました。", (c.stderr or c.stdout).strip())

    p = git("push", "origin", "HEAD")
    if p.returncode != 0:
        stop("GitHub へ送れませんでした。",
             "手元には記録済みなので、通信が戻ってからもう一度実行すれば送られます。\n\n"
             + (p.stderr or p.stdout).strip()[-600:])
    say("    GitHub へ送りました。")

    # ---- 6. 公開の反映を確認
    step(6, total_steps, "damtabi.com に出るまで待ちます（数分かかります）")
    base_url = "https://damtabi.com"
    try:
        base_url = json.loads(SITE_JSON.read_text(encoding="utf-8")).get("base_url", base_url)
    except Exception:
        pass

    live = wait_until_live(new.get("base_obs_time", ""), base_url)
    drop_backup()

    say()
    say(LINE)
    if live:
        say("  公開まで完了しました")
        say(LINE)
        say()
        say(f"    {base_url} に {new.get('base_obs_time')} 観測の値が出ています。")
        say(f"    {n_total}基のうち {n_ok}基に値があり、{n_nodata}基は値なしです。")
    else:
        say("  公開の反映がまだ確認できていません")
        say(LINE)
        say()
        say("    GitHub へは送れています。公開まで数分かかることがあります。")
        say(f"    しばらくしてから {base_url} を開いて、観測時刻が")
        say(f"    {new.get('base_obs_time')} になっていれば成功です。")
    say()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  中断しました。公開はしていません。")
        raise SystemExit(1)
