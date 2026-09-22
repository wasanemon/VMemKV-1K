# 2026-09-22: 1KB LTM Get (Hit) 改善2案の試作結果

## 提言

**両案とも、今回のLTM全4条件で改善した。まず案1を提言する。**
案1は保存容量を増やさず、既存の読込み経路の選択を変えるだけで15〜27%改善した。
案2も13〜23%改善し、読込み量を減らしたが、T2容量が28.3%増える。
これは試作の方向性確認であり、製品への採用判断や公開AWS環境での改善率の保証ではない。

対象は[候補メモ](20260922_1kb_ltm_get_hit_improvements.md)の案1・案2のみ。
MultiGetは実装・測定していない。製品の `src/`・`include/` は変更していない。

## LTMの結果

同じ専用harness内の比較。各条件2回の算術平均。括弧内は現行比。

| 分布 | スレッド | 現行Get | 案1: 境界またぎだけpread | 案2: ページ内配置 |
|---|---:|---:|---:|---:|
| Uniform | 1 | 12,399 ops/s | **14,958（+20.6%）** | **15,293（+23.3%）** |
| Zipf | 1 | 50,142 ops/s | **63,432（+26.5%）** | **56,552（+12.8%）** |
| Uniform | 16 | 147,340 ops/s | **173,675（+17.9%）** | **179,535（+21.9%）** |
| Zipf | 16 | 637,294 ops/s | **732,850（+15.0%）** | **728,309（+14.3%）** |

測定順を逆転した2回目でも、両案は全条件でそれぞれの現行版を上回った。
現行Uniform / 1スレッドは12,245〜12,552 ops/sで、他の構成も反復差は小さかったが、
2回だけなので信頼区間や統計的有意性は評価しない。

### 読込み量

`/proc/self/io` の `read_bytes` の計測区間差分をGet数で割った値。
カーネル先読みや少量のSwapに伴う読込みも含み、T2の要求バイト数そのものではない。

| 分布 | スレッド | 現行 | 案1 | 案2 |
|---|---:|---:|---:|---:|
| Uniform | 1 | 3,716 B/Get | 3,924 B/Get | 3,037 B/Get |
| Zipf | 1 | 855 B/Get | 825 B/Get | 747 B/Get |
| Uniform | 16 | 3,707 B/Get | 3,919 B/Get | 3,026 B/Get |
| Zipf | 16 | 792 B/Get | 859 B/Get | 692 B/Get |

案1はUniformで約6%、Zipf / 16スレッドで約8%多く読む。
読込み量削減による改善ではなく、読込みのまとめ方・待ち方の変更が有利に働いたと考える。
この全体比較ではデバイスI/Oの合流や待ち時間内訳を採取していない。
その後の[単件GetのI/Oトレース](20260922_1kb_get_pread_io_trace.md)では、
両ページが非常駐かつ物理配置が連続する境界またぎ128回すべてで、
現行の直列4KB×2件・待機2回が、案1では8KB×1件・待機1回になることを確認した。
最初のbioから8KBで、block層での後段の合流ではなかった。
全体の15〜27%改善幅の内訳まで確定したものではない。
`pread()`のI/O待ちはmajor faultとして数えられないので、fault件数の減少をそのまま高速化率とは扱わない。
案2は全条件で読込み量が約13〜18%減った。

### 容量

| 配置 | T2のレコード・余白使用量 | ページをまたぐ1KBレコード |
|---|---:|---:|
| 密詰め（現行・案1） | 7,030,530,024 B（6.55 GiB） | 1,703,532 / 6,607,641件（25.78%） |
| ページ内配置（案2） | 9,021,631,608 B（8.40 GiB） | **0件** |

案2の追加余白は1,991,101,584 B、**約1.85 GiB / 28.3%増**。
ヘッダー24B + キー16B + 値1024Bのレコードを1ページに3件入れ、残り904Bを余白にした。
8B値はT1インラインのまま。容量増により同じRAMで保持できるレコード数は減るが、
今回の条件では境界またぎの解消による利益が上回った。

## 常駐時の対照

同じ8,259,552キーをHigh=16GiB / Max=32GiBで測った。
T2の全データと両Getが使うmmapのページテーブルを事前に温め、2秒ウォームアップ後に6秒測定。
Uniform / 1スレッド、順序を逆転して各2回。公開In-Memoryベンチマークの再現ではない。

| 構成 | 平均ops/s | 現行比 |
|---|---:|---:|
| 現行 | 335,940 | — |
| 案1 | 330,142 | **−1.7%** |
| 案2 | 342,694 | **+2.0%** |

全6回の計測区間でmajor fault・`read_bytes`はともに0。
案1の常駐確認コストを含めても、この対照では差は小さかった。
他の分布・スレッド数について、常駐時の退行がないとは断定しない。

## 試作と確認範囲

- **案1:** `VMemKVStore`のT1検索・インライン値処理・キー照合を使い、
  不変baseで `size_hint > 4096 - offset % 4096` の場合だけ既存の
  `read_large_get_cold()`へ回す。常駐ならmmap、非常駐なら範囲を限定したpread。
  tailや例外的な不一致は現行Getへ戻す。対象は `Bloom-T1InlineValue`。
- **案2:** オフラインでページ内に収めたcheckpointを作り、変更のない現行Getで読む。
  T1のshard構成・キー・hash・値を保持し、T2 offsetとcheckpoint checksumを更新した。
  対照も同じ変換を通してキー順に生存レコードを詰め直し、不要レコード除去の効果を共通にした。
- 小規模DBでtail、checkpoint後のbase、更新後の値、削除、ミスを確認。
  両配置の本体で、先頭の境界付近と全域の固定seedサンプル計4,096回について、
  現行Get・試作Getの全バイトを生成元の値と比較して通過した。
- LTM全24回で、T2常駐ページ数0から開始、指定メモリ制限の適用、T2使用量の不変、
  計測中のcheckpoint・split・defragmentが0、正常終了を確認した。
- 案1は既存の不変baseの保証を利用し、保存形式・永続化手順は変えない。
  案2のオンライン割当て、更新・defragmentとの統合、クラッシュ回復の保証は実装・検証していない。
  書込み混在や多数の並行更新に対する製品品質の検証も今回の範囲外。

## 条件と再現

- 対象コミット: `e75746c2d03e0d53edaf566c157aad95172b887c` と既存作業。
  試作は `benchmark/prototypes/` に追加した。
- 環境: 既存調査と同じXeon Gold 5418N / Linux 5.15.0-186-generic / ext4のホスト。
  GCC 13.4、`-O3 -DNDEBUG -std=gnu++23`、既存の `libvmemkv.a` とGoogle Benchmark 1.8.4を使用。
  依存導入、Swap設定変更、ボトルネックの再プロファイルは行っていない。
- データ: 8,259,552キー、キー16B。キー数ベースで約80%が1024B、約20%が8B。
  値は元ベンチマークと同じ疑似乱数列。Zipf(alpha=1) / Uniform、1 / 16スレッド。
- LTM: 独立したuser scopeごとにHigh=1GiB / Max=2GiB。
  専用T2のキャッシュを落として12秒ウォームアップし、初期化を除く10秒を測定。
  計測seedは42 + thread番号。ウォームアップは別seedを使い、短い要求列の再生による偏りを避けた。
  同じコールバックで値の全バイトを走査した。
  終了時のファイルキャッシュは約650〜667MiB、匿名領域は約323〜329MiB、Swapは0〜75.5MiB。
- これは短時間の試作比較。1スレッドの一部では計測中にもファイルキャッシュが増えており、
  厳密な定常状態は保証しない。公開AWS結果や以前の別harnessの速度との差を改善率には使わない。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py build
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py prepare
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py matrix
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py resident
python3 implementation/vmemkv/benchmark/prototypes/summarize_get_hit_1kb.py
```

試作の説明: [benchmark/prototypes/README.md](../../benchmark/prototypes/README.md)。
生データ・全ケースの完全なコマンドは `build/ltm/results/get-hit-prototypes-20260922/`（Git管理外）。
主要集計は `summary.json` / `summary.md`、容量は `storage.json`。
各ケースの `command.json`、`result.json`、`cache-reset.json`、`before.*` / `after.*` に条件と計測値を保存。
対象バイナリ・ソース等のSHA256は `provenance-ltm.json` / `provenance-resident.json`。
LTM実行時のソースとバイナリは `source-ltm/` / `get_hit_1kb_ltm` にも保存した。
常駐対照のビルドでは準備処理にmmapページテーブルの温めを追加したが、Getと計測ループは同じ。
既存ディレクトリへの再実行は完成済みケースをスキップする。新規比較はランナーの保存先を変える。

## 追記：案1のthroughput退行確認

追加比較では常駐1KB Get Hitに約1.5〜5.0%の低下があった。LTMの改善と常駐時のコストを併せて判断する必要がある。ほかの操作・値サイズを含む結果は[throughput比較報告](20260922_get_throughput_regression.md)を参照。
