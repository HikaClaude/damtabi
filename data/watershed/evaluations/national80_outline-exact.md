# 集水域QA 全80基の分類（outline=exact / evaluate-only）

| 分類 | 基数 |
|---|---|
| PASS | 6 |
| WARN | 73 |
| HOLD | 1 |
| FAIL | 0 |
| UNEVALUABLE | 0 |

## 県別

| 県 | PASS | WARN | HOLD | FAIL | UNEVALUABLE | 計 |
|---|---|---|---|---|---|---|
| 富山 | 6 | 16 | 1 | 0 | 0 | 23 |
| 石川 | 0 | 12 | 0 | 0 | 0 | 12 |
| 岐阜 | 0 | 11 | 0 | 0 | 0 | 11 |
| 福井 | 0 | 13 | 0 | 0 | 0 | 13 |
| 長野 | 0 | 21 | 0 | 0 | 0 | 21 |

## WARN の検証材料

- ref_ok: 50
- official: 14
- ref_divergent: 6
- no_material: 3

## 理由コード（分類別）

- HOLD / area_error: 1
- HOLD / manual_hold: 1
- WARN / diversion_unknown: 59
- WARN / diversion_yes: 3
- WARN / no_official_direct_area: 59
- WARN / no_reservoir_surface: 53

## 個別確認が必要と見積もる基: 10 / 80（12.5%）

- toyama-shiraiwagawa 白岩川ダム [HOLD] manual_hold,area_error | 参考値乖離 16.5%
- ishikawa-hakkagawa 八ヶ川ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface | 参考値乖離 -15.3%
- ishikawa-oya 小屋ダム [WARN] no_official_direct_area,diversion_unknown | 参考値乖離 -25.5%
- fukui-kuzuryu 九頭竜ダム [WARN] no_official_direct_area,diversion_unknown | 参考値乖離 -38.6%
- fukui-takinami 滝波ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface
- fukui-kaitani 開谷ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface
- fukui-yoshinosegawa 吉野瀬川ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface
- nagano-uchimura 内村ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface | 参考値乖離 -82.5%
- nagano-toyooka 豊丘ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface | 参考値乖離 -20.9%
- nagano-onikuma 小仁熊ダム [WARN] no_official_direct_area,diversion_unknown,no_reservoir_surface | 参考値乖離 -54.5%

## 解釈上の注意（数字だけで「合格」と読まないこと）

- **PASS が少ない(6)のは、QA が厳しいからではなく警告が付くから。** 品質ゲート（block）に掛かったのは白岩川の1基だけ（79/80 が block なし）。
  WARN 73基の理由は「便覧の直接流域が無い(59)」「導水の有無が未確認(59)」「貯水池の水面を検出できずスナップ方式(53)」「導水あり(3)」で、いずれも QA の失敗ではなく**検証できていないこと**の表明。
- **exact の輪では、空間QA（輪×雨マスク）はほぼ満点になる**（輪も雨マスクも同じ細格子の集水域から作るため、80基とも輪外はみ出し 0.0%・経路流出 ≤0.01%）。
  空間QAは「配信物の取り違え・詰め込み・位置ずれ」を防ぐ**回帰ガード**であって、「集水域そのものが正しいか」は保証しない。
  集水域が正しいかを見る手段は面積の照合だけで、便覧の直接流域があるのは21基のみ。残り59基は川の防災情報の流域面積（参考値）が唯一の材料。
- **参考値の信頼度（富山で較正）**: 便覧と参考値を両方持つ18基のうち15基(83%)が±15%以内。乖離3基は導水（室牧 +51%）と集計単位の違い（臼中 +257%・朝日小川 +141%。算出値は便覧と一致していた）。
  つまり「参考値と一致」は有力な材料だが、「乖離」は算出の誤りとは限らない（逆に導水込みの値で乖離が隠れることもない）。便覧なし56基では、参考値との差の中央値は 1.3%、±5%以内 48基、±15%以内 50基。
- **導水の有無が未確認なのは59基**。導水があると地形上の集水域と実際の集水範囲が一致しないが、QA は参考値との乖離以外で検出できない。
- 評価は evaluate-only・オフライン（地理院への追加通信 0）。何も配信しておらず、docs/watershed・状態は変更していない。白岩川は保留のまま。
