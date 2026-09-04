ダムのイラストはこのフォルダに置いてください。

  例)  docs/img/dams/unazuki.webp

置いたあと docs/data/illustrations.json に、
ダムID（dams.json の id）またはダム名をキーにしてパスを書きます。

  {
    "0215560700006": "./img/dams/unazuki.webp",
    "有峰ダム":       "./img/dams/arimine.webp"
  }

バッチ(fetch_dams.py)を再実行しても illustrations.json は上書きされません。
推奨アスペクト比 16:10、幅 800px 程度。
