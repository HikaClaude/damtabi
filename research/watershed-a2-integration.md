# A2（dam_id による共有リンク）と集水域の統合メモ

作成 2026-09-18 / 対象ブランチ `feat/watershed-integration`（未コミット）
**A2 側（`feature/registry-ids` / `C:\Users\ryouh\damtabi-registry`）は読み取りのみ。今回は一切変更していない。**

---

## 0. 前提の確認（実コードで確認した事実）

| 項目 | 実測 |
|---|---|
| 統合先 `a539295` に A2 は入っているか | **入っていない。** `selectFromHash()` は `#dam=<ダム名>` を名前一致で解くだけで、`#dam_id=` の解析は存在しない |
| A2 の所在 | `feature/registry-ids` の**未コミット作業**（`docs/app.js` に `hashParams()` / `singleParam()` / `showDamNotFound()` / `showDamCandidates()` / `#dam_id=` 優先の `selectFromHash()`） |
| A2 は資産の版付けを持つか | **持つ。** `docs/sw.js` の `var VERSION = "v4e8df0a49c";` を `scripts/build_site.py` の `asset_version()` が内容から自動生成する |
| `asset_version()` の入力 | `ASSET_VERSIONED_FILES = ("app.js", "style.css", "page.css")` ＋ `sw.js` 自身（版付け部分を正規化したもの） |

**「行が重ならないので rebase だけでよい」とは言えない。** 下の 2 と 3 は、行の衝突が無くても壊れる。

---

## 1. 資産の版付けから watershed が漏れる（要対応・循環に注意）

`asset_version()` の入力に `docs/watershed/` が入っていないため、**集水域データだけを作り直しても SW の版が上がらない。**
上がらなければ `shell-<VERSION>` に残った旧世代の格子・ポリゴンが使われ続ける。

### 循環を作らないこと

素朴に「`docs/watershed/` 配下の全ファイルをハッシュに入れる」とすると循環する。

```
index.json は version を含む
  → version は index.json を含むハッシュから作られる
  → index.json が変わる → version が変わる → …
```

`asset_version()` が `sw.js` に対して `_canonicalize_sw_source()` で版付け部分を打ち消しているのと同じ問題である。

### 推奨する方法（循環しない・追加計算もほぼ不要）

**集水域側が既に持っている生成版をそのまま使う。**
`scripts/build_flowgrids.py` は、索引・ポリゴン・格子の**中身**（D8・マスク・面積）から
`sha256` の先頭12桁を作り、`index.json` の `version` に書いている。
これは索引ファイル自身の中身には依存しないので、循環しない。

```python
# build_site.py の asset_version() に足す想定（A2 着地後に A2 側で行う）
def _watershed_version() -> str:
    p = DOCS / "watershed" / "index.json"
    if not p.exists():
        return ""                      # データが無い環境では版に影響させない
    return json.loads(p.read_text(encoding="utf-8")).get("version", "")

# h.update(...) の並びに1行足すだけ
h.update(_watershed_version().encode("utf-8"))
```

- `index.json` 全体ではなく **`version` の値だけ**を入れる。`generated`（日付）や整形の差で
  無関係に版が変わるのを避けるため。
- データが無い環境（現在の `main`）では空文字になり、**既存の版文字列を変えない**。
- 集水域を作り直すと `version` が変わり、SW の版も必ず変わる。

### これは「保険」であって唯一の防御ではない

本ブランチ側では、SW の版に頼らない対策を既に入れてある（`docs/app.js` の `ws` モジュール）。

1. 索引が指す格子・ポリゴンの URL は `?v=<生成版>` 付き。索引が新しければ必ず新しい方を取りに行く。
2. 格子・ポリゴンにも `version` を書き込み、**索引の版と違えば描画せず**「再読み込みしてください」と出す。
3. `docs/sw.js` で `/watershed/index.json` を**ネットワーク優先**にした。
   Cache API の `match` は request の `cache: "no-cache"` を見ないので、
   キャッシュ優先のままだと索引だけが無期限に古くなる（実測して修正済み）。

A2 の版付けに watershed を足すのは、この3つに加えた**4つ目**である。足さなくても別版が混ざることは無いが、
足せば「古い版のまま黙って使い続ける」時間が短くなる。

---

## 2. A2 の新しい画面状態が集水域を消さない（要対応）

A2 は `showDamNotFound()` と `showDamCandidates()` を追加する。どちらも
`deselectMarker()` → `#panel-body` を書き換え → パネルを表示、という流れで、
**`closePanel()` を通らない。**

集水域は `select()` と `closePanel()` の中で `ws.stop(false)` を呼んで止めている。
そのため A2 適用後は、集水域を表示したまま不明な `#dam_id=` を開くと、
**輪と雨が残ったまま「見つかりませんでした」が出る。**

対応（統合時、本ブランチ側の1行として入れる）:

```js
  function showDamNotFound() {
    ws.stop(false);            // ← 追加
    deselectMarker();
    ...
  }
  function showDamCandidates(matches) {
    ws.stop(false);            // ← 追加
    deselectMarker();
    ...
  }
```

`deselectMarker()` の中に入れてもよい。`ws.stop()` はデータが無い環境でも安全に何もしない。

---

## 3. 行が重なる箇所（rebase で手当てが要る）

| 関数 | A2 の変更 | 集水域の変更 | 衝突 |
|---|---|---|---|
| `selectFromHash()` | 全面書き換え（`#dam_id=` 優先・曖昧判定） | 触っていない | なし |
| `writeHash()` | `#dam_id=` を出力 | 触っていない | なし |
| `select(id, updateHash)` | 変更なし | 先頭に `if (ws.activeId && ws.activeId !== id) ws.stop(false);` | **同じ行に両方が入る**。A2 は `select` を変えないので手当ては不要だが、`ws-jump` から `select()` を呼ぶ経路が A2 の `writeHash()`（`#dam_id=`）に切り替わることは確認する |
| `closePanel()` | `deselectMarker()` に置き換え | 先頭に `ws.stop(false);` | **同じ関数**。A2 の本文を採り、`ws.stop(false);` を先頭に残す |
| `renderPanel(dam)` | 変更あり（候補表示まわり） | `#ws-entry` の挿入と末尾の `ws.renderEntry(dam)` | **同じ関数**。両方入れる |
| `map.on("click", ...)` | `closePanel` のまま | ハンドラを差し替え | 集水域側を採る |
| `docs/sw.js` | `VERSION` を自動生成へ | `VERSION` 手書き＋`/watershed/index.json` をネットワーク優先 | **同じ行**。A2 の自動生成を採り、`isData` の判定行だけ集水域側を残す |

---

## 4. 統合時の試験（短く）

A2 を先に着地させ、本ブランチをその上に載せ直したあとで、次を確認する。

| # | 試験 | 期待 |
|---|---|---|
| T1 | `#dam_id=toyama-unazuki` で開く → 入口ボタン → 集水域が出る | 出る |
| T2 | 集水域を表示中に `#dam_id=toyama-arimine` へ移動 | 前の輪と雨が消え、有峰の輪が出る |
| T3 | 集水域を表示中に `#dam_id=存在しないID` へ移動 | **輪と雨が消え**、「見つかりませんでした」だけが出る（2 の対応が入っていないと残る） |
| T4 | 同名ダムの候補一覧が出る条件で同じ操作 | 同上 |
| T5 | 集水域を表示中にダムを選び直し、`writeHash()` が `#dam_id=` を書く | 書く。リンクを開き直すと同じダムが出る |
| T6 | `build_flowgrids.py` を再実行 → `build_site.py` を実行 | `sw.js` の `VERSION` が変わる（1 の対応が入っていれば） |
| T7 | 旧版の索引を持ったまま新版を配信（`?v=` を書き換えて再現） | 混ぜずに「再読み込みしてください」が出る |

---

## 5. やらないこと

- A2 側（`damtabi-registry`）のファイルを変更すること
- 本ブランチで `#dam_id=` を再実装すること
- `asset_version()` を本ブランチで書き換えること（A2 着地後に A2 側で行う）
