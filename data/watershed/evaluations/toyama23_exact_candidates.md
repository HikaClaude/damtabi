# 富山23基 exact 公開候補（2026-09-24、13e5c94 から）

DEM は手元キャッシュのみ（`--offline --max-fetch 0`、新規取得 0）。QA は現行ゲート（平滑化した輪 `smoothRing(..., 2)`・±15% を含む閾値は不変）。
「生の輪（参考）」は平滑化しない輪で同じ閾値を当てた参考値で、判定には使わない。**公開承認は0件**（`docs/watershed/` は空、`release.json` の approvals は空）。

- 23基とも exact の輪は手元キャッシュだけで生成できた（外部取得が必要な基はなかった）。
- 全基で**格子（D8・マスク）・出口・面積は legacy と同一**。変わったのは描く輪だけ。
- 候補 21基: exact・unapproved。legacy の候補21基は `data/watershed/staged/legacy/` に保存（格子・状態の published・data/basins の行）。
- 白岩川（HOLD）と久婦須川（QA hold）は evaluate-only。候補にせず、状態・data/basins の入力も legacy のまま。
- 舟川・大谷は便覧の直接流域が未確認（`dam_basin_spec.csv` の unconfirmed のまま）。

| dam_id | 生成 | 候補 | exact QA（平滑化・判定） | 生の輪（参考） | legacy からの変化 | 出口の根拠 | 保留理由 |
|---|---|---|---|---|---|---|---|
| toyama-unazuki | 成功 | exact・unapproved | pass IoU 0.9976 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9964 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 608.76 km²（不変）・頂点 1216→1000・IoU 0.936→0.9976・legacy 判定 pass(候補) | snap-fixed・z12・legacy と同一 はい | — |
| toyama-muromaki | 成功 | exact・unapproved | pass IoU 0.9952 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9958 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 84.17 km²（不変）・頂点 508→301・IoU 0.9689→0.9952・legacy 判定 pass(候補) | snap-fixed・z13・legacy と同一 はい | — |
| toyama-kumanogawa | 成功 | exact・unapproved | pass IoU 0.9936 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.994 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 39.75 km²（不変）・頂点 294→188・IoU 0.9747→0.9936・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-kubusugawa | 成功 | なし | pass IoU 0.9929 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9936 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 59.01 km²（不変）・頂点 509→288・IoU 0.9225→0.9929・legacy 判定 hold(rain_outside_ring,path_leaves_ring) | reservoir・水面 0.14 km²（集水域内 100.0%）・z13・legacy と同一 はい | QA hold 維持（legacy: rain_outside_ring 3.12%・path_leaves_ring 3.54%）。exact は evaluate-only で pass だが解除しない・候補にしない |
| toyama-wadagawa | 成功 | exact・unapproved | pass IoU 0.9929 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9939 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 32.15 km²（不変）・頂点 257→175・IoU 0.9834→0.9929・legacy 判定 pass(候補) | reservoir・水面 0.247 km²（集水域内 100.0%）・z13・legacy と同一 はい | — |
| toyama-togagawa | 成功 | exact・unapproved | pass IoU 0.9921 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9929 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 38.54 km²（不変）・頂点 427→235・IoU 0.9631→0.9921・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-sakaigawa | 成功 | exact・unapproved | pass IoU 0.9945 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9955 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 37.6 km²（不変）・頂点 165→136・IoU 0.988→0.9945・legacy 判定 pass(候補) | reservoir・水面 0.987 km²（集水域内 100.0%）・z13・legacy と同一 はい | — |
| toyama-tori | 成功 | exact・unapproved | pass IoU 0.9938 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9944 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 45.65 km²（不変）・頂点 225→160・IoU 0.9855→0.9938・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-goi | 成功 | exact・unapproved | pass IoU 0.9876 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9899 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 13.24 km²（不変）・頂点 184→101・IoU 0.9815→0.9876・legacy 判定 pass(候補) | snap・z14・legacy と同一 はい | — |
| toyama-konadegawa | 成功 | exact・unapproved | pass IoU 0.9932 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.994 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 32.08 km²（不変）・頂点 221→179・IoU 0.9843→0.9932・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-johana | 成功 | exact・unapproved | pass IoU 0.9842 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9883 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 10.31 km²（不変）・頂点 164→81・IoU 0.9569→0.9842・legacy 判定 pass(候補) | reservoir・水面 0.108 km²（集水域内 100.0%）・z14・legacy と同一 はい | — |
| toyama-usunaka | 成功 | exact・unapproved | pass IoU 0.9893 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9913 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 13.12 km²（不変）・頂点 93→82・IoU 0.985→0.9893・legacy 判定 pass(候補) | reservoir・水面 0.126 km²（集水域内 100.0%）・z14・legacy と同一 はい | — |
| toyama-asahiogawa | 成功 | exact・unapproved | pass IoU 0.993 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9947 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 29.18 km²（不変）・頂点 135→107・IoU 0.991→0.993・legacy 判定 pass(候補) | snap・z14・legacy と同一 はい | — |
| toyama-kadokawa | 成功 | exact・unapproved | pass IoU 0.9881 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9906 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 16.03 km²（不変）・頂点 147→95・IoU 0.9849→0.9881・legacy 判定 pass(候補) | snap・z14・legacy と同一 はい | — |
| toyama-kamiichigawa | 成功 | exact・unapproved | pass IoU 0.993 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.995 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 45.91 km²（不変）・頂点 296→185・IoU 0.9761→0.993・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-kamiichigawa-daini | 成功 | exact・unapproved | pass IoU 0.9932 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9941 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 39.57 km²（不変）・頂点 240→163・IoU 0.976→0.9932・legacy 判定 pass(候補) | snap・z13・legacy と同一 はい | — |
| toyama-shiraiwagawa | 成功 | なし | hold IoU 0.9906 / 輪外 0.0% / 経路外 0.0% manual_hold,area_error | hold IoU 0.9935 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 27.95 km²（不変）・頂点 355→149・IoU 0.9591→0.9906・legacy 判定 hold(manual_hold,area_error,path_leaves_ring) | reservoir・水面 0.049 km²（集水域内 100.0%）・z14・legacy と同一 はい | HOLD 維持（manual_hold・面積誤差 +16.4%）。evaluate-only・候補にしない |
| toyama-funagawa | 成功 | exact・unapproved | pass IoU 0.9778 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9828 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 3.51 km²（不変）・頂点 51→46・IoU 0.974→0.9778・legacy 判定 pass(候補) | snap・z14・legacy と同一 はい | 便覧の直接流域 未確認（unconfirmed のまま）・warn no_official_direct_area |
| toyama-fusegawa | 成功 | exact・unapproved | pass IoU 0.9858 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9902 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 13.21 km²（不変）・頂点 103→77・IoU 0.9835→0.9858・legacy 判定 pass(候補) | reservoir・水面 0.072 km²（集水域内 100.0%）・z14・legacy と同一 はい | — |
| toyama-otani | 成功 | exact・unapproved | pass IoU 0.9638 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.976 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 1.48 km²（不変）・頂点 52→37・IoU 0.9521→0.9638・legacy 判定 pass(候補) | snap・z14・legacy と同一 はい | 便覧の直接流域 未確認（unconfirmed のまま）・warn no_official_direct_area |
| toyama-kurobe | 成功 | exact・unapproved | pass IoU 0.998 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9977 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 181.09 km²（不変）・頂点 390→407・IoU 0.982→0.998・legacy 判定 pass(候補) | reservoir・水面 1.14 km²（集水域内 100.0%）・z12・legacy と同一 はい | — |
| toyama-arimine | 成功 | exact・unapproved | pass IoU 0.9971 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9955 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 50.44 km²（不変）・頂点 209→220・IoU 0.9727→0.9971・legacy 判定 pass(候補) | reservoir・水面 1.545 km²（集水域内 100.0%）・z12・legacy と同一 はい | — |
| toyama-dashidaira | 成功 | exact・unapproved | pass IoU 0.9982 / 輪外 0.0% / 経路外 0.0% | pass IoU 0.9966 / 輪外 0.0% / 経路外 0.0% | 格子同一 はい・面積 459.82 km²（不変）・頂点 975→799・IoU 0.9524→0.9982・legacy 判定 pass(候補) | snap-fixed・z12・legacy と同一 はい | — |
