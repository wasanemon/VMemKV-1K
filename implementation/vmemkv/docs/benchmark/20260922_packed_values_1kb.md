# 1KB値とメタデータを分離する配置の最小試作（2026-09-22）

## 結果

**Uniformの1KB LTM Get Hitで、現行比1スレッド+22.8%、16スレッド+25.2%。メタデータを含む論理使用量は現行と同じだった。**

各版2回の平均（ops/s）。8実行はすべて正常終了。追加測定は行わない。

| スレッド | 現行 | 値・メタデータ分離 | 増減 |
|---:|---:|---:|---:|
| 1 | 12,612 | 15,490 | +22.82% |
| 16 | 146,966 | 183,952 | +25.17% |

個々の測定値（ops/s、実行順序は各条件で現行→試作→試作→現行）：

| スレッド | 現行1回目 | 試作1回目 | 試作2回目 | 現行2回目 |
|---:|---:|---:|---:|---:|
| 1 | 12,611 | 15,673 | 15,307 | 12,614 |
| 16 | 147,410 | 183,918 | 183,986 | 146,522 |

| 保存対象 | 論理使用量（B） |
|---|---:|
| 現行T2 | 7,030,530,024 |
| 試作：値 | 6,766,224,384 |
| 試作：ヘッダー・キー | 264,305,640 |
| 試作：合計 | 7,030,530,024 |

両版とも合計約6.55GiB。T1は共通で、追加の索引をRAMへ保持していない。
ファイルシステムの割当量は現行7,030,534,144B、試作7,030,579,200B。
差は44KiB（約0.00064%）で、旧案2の約28.3%増を伴わない。試作用に元DBも残しているため、実験ホストでは変換先の約6.55GiBを別途消費する。

この範囲では、容量効率を維持して単件Getを改善する方向性は有望。固定長・読取り専用の試作結果であり、通常の更新や回復を含む製品実装の性能を保証するものではない。

## 何を変えたか

ブランチ：`codex/packed-1kb-values`。基準コミット：`e75746c2d03e0d53edaf566c157aad95172b887c`。
作業開始前からの未コミット変更は保持。製品のsrc/includeは変更していない。
既存の密詰めcheckpointを変換し、1KB値を連続して格納した。先頭から4件で4KBとなり、値はページをまたがない。
24Bのヘッダーと16Bのキーは、40B単位で別のメタデータファイルにそのまま保持した。
両ファイルの同じスロット番号が対応する。8Bインライン値はT1に残す。

```text
現行：  [header 24B | key 16B | value 1024B] × N
試作：  values.bin   = [value 1024B] × N
        metadata.bin = [header 24B | key 16B] × N
```

比較のため両版で同じStore・同じT1を開く。試作は密詰めoffsetを1064で割ってスロット番号とし、
`values.bin + slot * 1024` を `MAP_SHARED + MADV_RANDOM` のmmapから読む。
追加の常駐確認・preadは使わない。元のT2ファイルとmetadata.binをGet時に読む処理はない。
T1を開くため元のDBを保持する方法は実験上の簡略化で、通常のcheckpoint/recovery形式としては使えない。

成立条件は、変換元が不変・密詰めで、外部値の全キーが16B、全値が1024Bであること。
変換時に全外部エントリーについてサイズ・キー・連続offsetを検証する。
T1はprefixだけでなくhashも比較し、prefix自体が16Bなので、この条件ではキー全体の一致が確定する
（`src/t1_index/t1_index.hpp` の `find_sorted()` / `find_with_index()`）。
値長は専用形式として1024Bに固定し、size_hintから推測しない。
Get時に別のメタデータページを読む必要がない、という提案の成立条件を満たす試作である。

## 測定範囲と再現

- 同じXeon Gold 5418N / Linux 5.15.0-186-generic、GCC 13.4 / Release相当（`-O3 -DNDEBUG`）。
  既存libvmemkv.aとGoogle Benchmarkの最適化抑止関数を再利用。
- `Bloom-T1InlineValue`、8,259,552キー、キー16B、キー数の80%が1024B値・20%が8B値。
- UniformのGet Hit、1/16スレッドのみ。取得した値の全バイトを走査。MultiGetは使わない。
- `MemoryHigh=1GiB / MemoryMax=2GiB` を各ケース内から確認。CPU配置は両版とも明示固定なし。
- 両版とも元T2と値ファイルのキャッシュを落としてから、12秒ウォームアップ＋10秒計測。
  初期化・変換・キャッシュ準備は測定区間外。要求生成は既存試作と同じ式・seed。
- 各スレッド数で現行→試作→試作→現行の順、各版2回（全8実行）。
  評価指標はthroughputのみ。保存容量はファイルの論理使用量と割当量を記録。
- 結果はこの同一harness内で比較する。以前のGoogle Benchmark方式の案1の改善率とは直接比較しない。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_packed_values_1kb.py build
python3 implementation/vmemkv/benchmark/prototypes/run_packed_values_1kb.py prepare
python3 implementation/vmemkv/benchmark/prototypes/run_packed_values_1kb.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_packed_values_1kb.py matrix
```

元データ：`build/ltm/data/get-hit-prototypes-20260922/dense`。
変換先：`build/ltm/data/packed-values-1kb-20260922/`。
記録先：`build/ltm/results/packed-values-1kb-20260922/`（以上Git管理外）。
`provenance.json`にコミット・ブランチ・コンパイラ・ソースとバイナリのSHA256、
各ケースの`command.json`にコマンドと環境、`memory-limits.json`に適用制限、
`result.json`にthroughput、`layout.json`に容量を保存する。
既存結果を上書きしないため、再実行時は別の保存先を指定するようランナーを変更する。

## 最小限の動作確認

変換時に6,607,641件すべての外部レコードの形式・T1キーとの一致・連続offsetを確認した。
別実行で先頭・末尾・固定seedのサンプル合計4,096キーについて、現行Getと試作Getを
元の値生成関数の出力と照合し、存在しない16キーも確認して成功した。全テストは実行していない。

## 解釈とトレードオフ

- 値のページまたぎ解消、メタデータをキャッシュへ載せない効果、T2のキー照合を省く効果を
  合わせた比較である。改善があっても、どの効果が何割かの切分けはしていない。
- 棄却済みの「1064Bレコードを余白付きでページ内へ収める案」と異なり、
  メタデータを捨てずに密詰めで保存する。最終ページの端数とファイルシステムの割当単位は残る。
- 読取り専用の固定形式に限定した試作。可変長キー・値、mutable tail、更新、
  通常checkpoint/recovery、defragment、Scanへの製品対応は実装・検証していない。
  製品化には専用形式の識別、スロット割当てとメタデータの対応管理、更新時の経路選択が必要。
- Zipf、常駐条件、他の操作は今回測定しない。短時間各2回の方向性確認であり、公開AWS性能や一般のワークロードへの保証ではない。

実装：[packed_values_1kb.cpp](../../benchmark/prototypes/packed_values_1kb.cpp) / [ランナー](../../benchmark/prototypes/run_packed_values_1kb.py)。
