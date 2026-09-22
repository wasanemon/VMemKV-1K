# 1KB単件Getの試作比較

現在の常駐ヒント＋初期スナップショットの監査は、[コード・結果をまとめた監査入口](audit/page-residency-snapshot/README.md)から参照できる。以下は各試作を行った時点の説明。

`get_hit_1kb.cpp` は2案の方向性確認用。MultiGetは扱わない。
製品コードには変更を加えず、現在の `VMemKVStore` と読込みヘルパーを利用する。

- `dense / baseline`: 現行の単件Get。
- `dense / cross-pread`: T2のページ境界をまたぐレコードだけ、既存の常駐確認・`pread`経路へ回す。
- `padded / baseline`: レコードがページをまたがないように配置し、現行Getで読む。

両配置とも同一の既存checkpointを変換し、T1のshard構成、キー、hash、値を保持する。
変換はどちらもキー順で生存レコードを詰め直すため、古い不要レコード除去の効果は共通。
ページ内配置のオンライン割当て・更新・defragment・クラッシュ回復の製品実装は含まない。

既存のGCC 13 / Releaseビルドと取得済みGoogle Benchmarkを再利用する。
保存先は `build/ltm/{data,results}/get-hit-prototypes-20260922/`。
各ケースの初期化後に、その専用T2ファイルのキャッシュを落とし、常駐ページ数0を確認する。
12秒のウォームアップ後、10秒間、全バイトを読む単件Getを測定する。
キー・値生成、Zipf(alpha=1)、計測の乱数seedは `bench_kv.cpp` に合わせている。
ウォームアップだけは別seedにして、同じ短い要求列を再生するキャッシュの偏りを避ける。
時間制御は専用harnessなので、旧Google Benchmarkの絶対速度とは直接比較しない。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py build
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py prepare
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py matrix
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_1kb.py resident
```

`matrix` はUniform/Zipf × 1/16スレッド × 3構成を、順序を逆転して2回測る。
各ケースは独立したuser scopeでHigh=1GiB / Max=2GiBを適用・確認する。
`resident` は同じキー数・値でHigh=16GiB / Max=32GiBとし、T2全ページを読み込んでから
Uniform / 1スレッドの常駐時コストを比較する。新しい1KB In-Memory公開結果の再現ではない。
常駐時は両Getが利用するmmapのページテーブルも事前に温める。
user bus接続のため、必要に応じてサンドボックス外で実行する。

結果は `result.json`、制限とメモリ内訳は `before.*` / `after.*`、
完全なコマンドは `command.json`、対象コミット・バイナリ等のSHA256は `provenance.json` に保存する。
終了コード、キーと値の一致、checkpoint/split/defragmentの混入を確認する。
反復は方向性判断のための2回であり、信頼区間や書込み混在の保証を目的としない。

## 案1のI/O観測

`inspect_get_hit_io.cpp` は同じGetを呼び、非常駐の単件Getとpread/bio/request/待機の対応を調べる。
境界をまたぐ64キー・収まる16キーを両版で2回ずつ読む。通常LTM全体の性能比較ではない。
`run_get_hit_io.py` でビルドと実行、`capture_get_hit_io.sh` で利用者のsudo認証による
10秒間の限定トレース、`analyze_get_hit_io.py` でファイル配置・Get時刻・イベントを照合する。
手順・条件・結果は[観測報告](../../docs/benchmark/20260922_1kb_get_pread_io_trace.md)を参照。

## 案1のthroughput退行確認

`regression_throughput.cpp` は既存 `bench_kv.cpp` のGet/Insert/Update/Delete/Scan/YCSB-E本体を再利用する。
単件Getを含む混在の確認として、GetとUpdateを交互に呼ぶケースも追加した。
`run_regression_throughput.py` は製品のsrc/includeを実験ディレクトリへコピーし、
案1側だけGetの小レコード判定へページ境界の条件を追加してビルドする。
両版とも対象型を `Bloom-T1InlineValue` だけに絞る。製品ツリーの変更はない。

1KBは既存の8,259,552キーデータを常駐/LTMで使用する。
8Bは20,000,000キー、64KBは131,040キーを準備して読込み系の対照にする。
常駐はHigh=16GiB / Max=32GiB、LTMはHigh=1GiB / Max=2GiB。
初期化時のキャッシュ準備を測定区間から除き、書込みケースは毎回専用コピーへ戻す。
比較対象は `items_per_second` のみ。Get等はGoogle Benchmarkのmin_time=2秒、
Insert/Deleteは3秒、YCSB-Eは元の30秒とcheckpointスケジュールを使う。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py build
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py prepare
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py matrix
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py summarize
```

`build/ltm/results/throughput-regression-20260922/` にソースコピー・差分・バイナリ・
実行コマンド・適用メモリ制限・生JSON・throughput集計を保存する。
初回は順序を逆転した2回。低下候補の再確認には `--only`、`--repetitions`、`--seconds`、
`--cpus` を使い、CPU配置を変えた結果は初回と分けて集計する。

初回58条件と、完了した追加5条件の結果は[throughput比較報告](../../docs/benchmark/20260922_get_throughput_regression.md)を参照。追加測定はユーザー指示で途中終了した。

## 1KB値とメタデータの分離

`packed_values_1kb.cpp` / `run_packed_values_1kb.py` は、既存の密詰めcheckpointから
値を1024B単位、ヘッダーとキーを40B単位の別ファイルに保存する読取り専用試作。
全キー16B・全外部値1024Bを変換時に確認し、GetではT1でキーを確定して値だけを読む。
元のT1をそのまま使うため、密詰めoffset / 1064をスロット番号として解釈する。
更新・可変長データ・通常checkpoint/recoveryへの対応は含めない。
Uniform、1/16スレッド、各版2回の最小比較と容量は
[試作報告](../../docs/benchmark/20260922_packed_values_1kb.md)を参照。

## 案1への追加A/B：読込み方針

`run_get_read_policy.py`は、作業ツリーに適用済みの案1をコピーし、
基準・A（独立fd＋POSIX_FADV_RANDOM）・B（ページ跨ぎ小レコードの適応的mincore省略）をビルドする。
試作部品は`get_read_policy.hpp`。製品src/includeには案1の条件式だけを残す。
既存データとベンチマーク本体を再利用し、AはLTM Uniform、BはLTM Uniform/Zipfと常駐Uniform、
すべて1スレッド・各版2回に限定する。
結果・制約・再現コマンドは[比較報告](../../docs/benchmark/20260922_get_read_policy.md)を参照。

`run_get_read_policy_extra.py`はBの追加比較用。上記の測定済みバイナリを再利用し、
常駐Zipf Get・1スレッド、LTM Uniform Get・16スレッド、LTM Zipf Get/Update 50%ずつ・1スレッドを
各版2回だけ比較する。混在は毎回専用checkpointコピーへ戻し、結果を`B-extra/`に保存する。

## 小レコードの固定buffered pread

`run_fixed_pread_1kb.py`は案1適用済みの作業ツリーを基準とし、不変baseの小レコードGetを
ページ跨ぎに関係なく、事前mincoreなしの既存preadへ回す実験用コピーを作る。
対象は`size_hint <= 4096`。8BのT1インラインと製品ツリーの案1は維持し、A/Bの制御は含めない。
LTM Uniform/Zipf・1/16スレッド・各版2回の比較では案1比+1.37〜+9.06%。
値照合・比較の手順、反映状況、制約は[比較報告](../../docs/benchmark/20260922_fixed_pread_1kb.md)を参照。

`run_fixed_pread_1kb_extra.py`は同じ測定済みバイナリで、常駐1KB Get HitのUniform/Zipf ×
1/16スレッドと、LTM Zipf Get/Update 50%ずつ・1スレッドを各版2回だけ追加比較する。
結果は`fixed-pread-1kb-20260922/extra-20260923/`へ保存し、混在には毎回専用DBコピーを使う。

## 案1の常駐Get低下の原因切り分け

`run_resident_mincore_diagnosis.py`は常駐1KB Get Hitの4条件だけで、元実装・案1・
同一案1バイナリのmincore置換版を各2回比較する。`resident_mincore_stub.c`は全ページを
事前に温める診断専用で、mincoreだけを常駐結果に置き換える。製品やLTMには使わない。
条件と結果は[原因調査報告](../../docs/benchmark/20260923_proposal1_resident_cause.md)を参照。

## TSX/RTMの利用可否確認

`probe_rtm_1kb.cpp`はCPUIDでRTM対応を確認し、対応時だけ温めた2ページの試し読みを試す。
このホストはRTM bit=0のため命令を実行せず、Getへの組込み・ベンチマークには進んでいない。
ブランチと再現手順は[確認報告](../../docs/benchmark/20260923_tsx_probe_1kb.md)を参照。

## BPFで失効させるページ別常駐ヒント

`page_residency.bpf.c`が対象T2の追加・削除イベントで共有mapの世代と状態を更新し、
`page_residency_hints.hpp`がページ跨ぎ小レコードのGetで確認済み情報を参照する。
既存libbpfを使う`page_residency_loader.py`と`run_page_residency.py`で、map・ローダーも
同じ測定用cgroupに含め、Getは一般ユーザーで実行する。ロードにはsudoが必要。
1スレッドの4条件では常駐Uniform−1.06%、常駐Zipf+2.18%、LTMの差は小さく、拡大せず終了。
制約・反映状況・再現手順は[試作結果](../../docs/benchmark/20260923_page_residency_hints.md)を参照。

### 常駐ヒントのビットマップ化

`page_residency_bitmap.hpp` / `page_residency_bitmap.bpf.c`は64ページ分の常駐・登録禁止・世代を24Bで管理する。
`build_page_residency_bitmap.py`で実験コピーをビルドし、`measure_page_residency_bitmap.sh`で案1・従来版・圧縮版を比較する。
管理本体は13.1MiBから0.614MiBへ減ったが、1スレッドの4条件では従来版からのthroughput改善を確認できなかった。
結果・制約・再現手順は[圧縮試作の記録](../../docs/benchmark/20260923_page_residency_bitmap.md)を参照。

## 両ページcoldの場合だけDirect I/O

`probe_direct_1kb.py`で、このホストの既存不変T2に対する512B境界のDirect I/Oと
ページキャッシュ非投入を確認する。`run_cold_direct_1kb.py`は案1のコピーを基準に、
既存mincoreで両ページcoldだった小レコードだけ`cold_direct_1kb.hpp`のDirect I/Oへ回す。
片方常駐はbuffered pread、両方常駐はmmap。通常ソースは案1だけを残す。
LTM・1スレッドでは案1比でUniform −4.10%、Zipf −4.75%。計8実行で終了し16スレッドは未測定。
検証片は`cold_direct_verify.inc`。条件・結果・再現手順は
[比較報告](../../docs/benchmark/20260923_cold_direct_1kb.md)を参照。
