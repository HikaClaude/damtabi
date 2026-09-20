# 集水域 証拠層と評価区分（ws-evidence/1）

評価区分（G1〜G4）は人手監査へ送る量を見積もるための分類で、公開可否・掲載可否には接続しない。区分の規則が使う数値は既存の ±15%（参考面積との一致の目安）だけ。

| 評価区分 | 名称 | 基数 |
|---|---|---|
| G1 | official_verified | 17 |
| G2 | official_with_diversion | 3 |
| G3 | reference_consistent | 50 |
| G4 | needs_evidence | 10 |

## 県別

| 県 | G1 | G2 | G3 | G4 | 計 |
|---|---|---|---|---|---|
| 富山 | 17 | 3 | 2 | 1 | 23 |
| 石川 | 0 | 0 | 10 | 2 | 12 |
| 岐阜 | 0 | 0 | 11 | 0 | 11 |
| 福井 | 0 | 0 | 9 | 4 | 13 |
| 長野 | 0 | 0 | 18 | 3 | 21 |

## G4（人手・追加証拠が必要）

- toyama-shiraiwagawa 白岩川ダム [HOLD] 理由: qa_not_passed | 参考面積誤差 +16.5%
- ishikawa-hakkagawa 八ヶ川ダム [WARN] 理由: reference_divergent | 参考面積誤差 -15.3%
- ishikawa-oya 小屋ダム [WARN] 理由: reference_divergent | 参考面積誤差 -25.5%
- fukui-kuzuryu 九頭竜ダム [WARN] 理由: reference_divergent | 参考面積誤差 -38.6%
- fukui-takinami 滝波ダム [WARN] 理由: no_verification_material
- fukui-kaitani 開谷ダム [WARN] 理由: no_verification_material
- fukui-yoshinosegawa 吉野瀬川ダム [WARN] 理由: no_verification_material
- nagano-uchimura 内村ダム [WARN] 理由: reference_divergent | 参考面積誤差 -82.5%
- nagano-toyooka 豊丘ダム [WARN] 理由: reference_divergent | 参考面積誤差 -20.9%
- nagano-onikuma 小仁熊ダム [WARN] 理由: reference_divergent | 参考面積誤差 -54.5%
