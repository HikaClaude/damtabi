ダムのカードイラストはこのフォルダに置きます。

■ ファイル名
  ダムID（dams.json の id）と同じ英数字にしてください。
    toyama-unazuki.png
    toyama-muromaki.png
  日本語ファイル名でも動きますが、英数字を推奨します（理由は README.md）。

■ 画像
  3:2（推奨 900x600 以上）。png / jpg / webp。
  OGP は 1200x630 に中央でトリミングされるので、
  上下が少し切れても成立する構図にしてください。

■ 登録
  docs/data/illustrations.json にダムIDとパスを書きます。
  パスは docs/ ルート基準で "./img/dams/..." と書いてください。

    {
      "toyama-unazuki": "./img/dams/toyama-unazuki.png"
    }

■ 反映
  python scripts/build_site.py    静的ページに反映
  python scripts/make_images.py   OGP画像を作り直す
  （地図画面は illustrations.json を直接読むので再ビルド不要）

illustrations.json はバッチが上書きしません。
貯水率を再取得してもイラストの割り当ては消えません。
