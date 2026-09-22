# 両ページcold時だけDirect I/O：最小比較（2026-09-23）

## 反映状況と変更

現在ブランチは`codex/cold-direct-1kb`。`codex/fixed-pread-1kb`から既存の未コミット作業を保持して作成した。mainを含む5ブランチのコミットは`e75746c2d03e0d53edaf566c157aad95172b887c`のままで、コミット・pushは行っていない。

**比較基準は案1適用済みの通常ソースのコピー。** 通常の`implementation/vmemkv/src/`・`include/`には案1のみを残し、今回の変更は`build/ltm/results/cold-direct-1kb-20260923/source/cold-direct/`という別ファイルに適用した。前回の固定buffered pread、A/Bの制御、値分離は混ぜていない。mainのコミットには案1も未反映。

案1のページ跨ぎ小レコードについて、既存の1回の`mincore()`で得たビット列を使い分ける。

| 2ページの常駐状態 | 読取り |
|---|---|
| 両方常駐 | 既存mmap |
| 片方だけ常駐 | 既存buffered pread |
| 両方非常駐 | 512B単位のDirect I/O |

`try_read_resident_base_record()`に両ページcoldを返す引数を加え、`read_large_get_cold()`で`size_hint <= 4096`かつ両ページcoldのときだけ追加経路へ進む。ページ内の小レコードは案1どおりmmap。8BのT1インライン・mutable tail・Scanの分岐は変更しない。大レコードの読取り方式も維持するが、共用mincoreヘルパーのループは変更される。

追加部品`cold_direct_1kb.hpp`は初期化時に同じinodeを`O_RDONLY | O_DIRECT`で別openし、終了時にcloseする。既存buffered fdは残す。読取り範囲は`size_hint`を覆うよう512B境界へ切下げ／切上げし、4096B整列のスレッド別8KiBバッファへ読む。callbackはバッファ内のレコードを直接参照する。丸めた末尾が不変baseを超える場合、fdが使えない場合、短い読取り・検証失敗時は既存buffered preadへ戻る。

## 実機の成立条件

ホストはLinux 5.15.0-186-generic、対象ファイルはext4、論理／物理セクターはこのホストのsdaで512B。ファイルシステムはLVM上にある。デバイスのセクターサイズだけで可否を判断せず、既存の不変T2ファイルで次を確認した。

| 実レコードoffset | Direct I/Oのoffset | 長さ | 読取り前 | Direct後 | buffered対照後 |
|---:|---:|---:|---|---|---|
| 3,192 | 3,072 | 1,536B | cold/cold | cold/cold | 常駐/常駐 |
| 40,432 | 39,936 | 2,048B | cold/cold | cold/cold | 常駐/常駐 |

両読取りとも全バイトがbuffered読取りと一致した。offsetを1BずらしたDirect I/Oは`EINVAL`となった。正常読取り後にページキャッシュへ投入されず、buffered対照では投入されたため、今回の2ケースはbufferedフォールバックの挙動ではないと判断した。カーネル経路やブロックI/Oのトレースは追加していない。

O_DIRECTのアラインメント制約や不適合時の挙動は環境に依存するため、このホスト・対象ファイルでの成立確認である。mmap／buffered I/Oとの混用も性能上の注意点がある。[Linux open(2)](https://man7.org/linux/man-pages/man2/open.2.html)

## 比較条件

- Intel Xeon Gold 5418N、GCC 13.4.0、Release相当`-O3 -DNDEBUG`。両版で既存ライブラリを共用。
- `Bloom-T1InlineValue`、8,259,552キー、キー16B。キー数の80%が1024B値、20%が8B値。既存の同じ密詰めcheckpointを使用。
- T2論理使用量7,030,530,024B。LTM `MemoryHigh=1GiB / MemoryMax=2GiB`を各実行で適用・確認。
- 同期単件Get Hit、Uniform／Zipf（α=1）、値の全バイトを読む。CPU配置は1スレッドがCPU 0、16スレッドを追加する場合はCPU 0,2,…,30。
- 既存Google Benchmark本体、`min_time=2s`、明示的warmup=0。初期化後にT2のキャッシュを落として非常駐を確認し、較正・計測へ進む。初期化は計測外。
- 各版2回、2回目は実行順を逆転。まず1スレッドの2分布を計8実行、有望な場合だけ16スレッドへ拡張。性能指標はthroughputのみ。

## 結果

**今回の試作は案1より遅く、この条件では採用を勧めない。** 両分布とも2回とも低下したため、予定どおり16スレッドへ広げず、1スレッドの計8実行で終了した。

各版2回の算術平均、単位ops/s。増減は丸め前の値から計算。

| 分布 | スレッド | 案1 | 両ページcold限定DIO | 案1比 |
|---|---:|---:|---:|---:|
| Uniform | 1 | 18,588 | 17,825 | −4.10% |
| Zipf | 1 | 42,928 | 40,891 | −4.75% |

512B単位のDIOが成立することと、throughputが改善することは別だった。DIO固有の費用やキャッシュ再利用の損失は説明候補だが、原因別の内訳は測定していない。前回の固定buffered pread案との直接比較はしていない。

## 動作確認

両版で1,024キーを2回ずつ全バイト照合し、Missも確認して成功。追加版では同一のページ跨ぎレコードをcold/cold、常駐/cold、cold/常駐、常駐/常駐にして、返却先がDirect用バッファ、既存buffered用バッファ、mmapの期待どおりであることと、値の一致を確認した。Direct fd使用不可時のbufferedフォールバックも確認した。

初回の検証では、Scan側マッピングを触る準備で先読みが入り、片方だけ常駐という前提を作れなかった。準備の1ページ読取りを既存のMADV_RANDOMな主マッピングに切り替え、4状態の確認を通した。これは検証準備の修正で、性能比較する読み経路や設定の調整ではない。

## トレードオフ・限界

- 保存形式・容量は変えない。追加RAMはDirect I/O用8KiB/スレッドとfd等の管理情報。既存buffered用バッファも残る。
- Direct I/Oで読んだページはその読取りでは温まらず、再利用時に再度I/Oとなり得る。常駐観測から読取りまでに他スレッドがページを温める競合もある。mincoreの結果は瞬間の状態であり、以後の状態を固定しない。
- 小さな転送で得られる利点と、Direct I/O固有の処理費用・キャッシュ再利用の損失の釣合いをthroughputで比較する。要求長の1536B／2048B化から、SSD内部の物理読取り量やI/Oコマンド数の同率削減は断定しない。
- 試作は単一checkpointの読取り専用。fdをプロセス内で1つ保持し、整列バッファの寿命はスレッド単位。複数Store、checkpoint切替・recovery・更新・defragment、再入callbackの汎用的な寿命管理には広げていない。通常のキー検証は維持するが、製品化の保証は今回の確認範囲外。
- 常駐・書込み混在・他サイズ・全テスト・プロファイルへは広げない。各版2回は方向性確認で、統計的有意差や他ホストへの一般化を保証しない。

## 再現と記録

準備済みのデータ・ビルドを再利用し、リポジトリルートで実行する。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/probe_direct_1kb.py
python3 implementation/vmemkv/benchmark/prototypes/run_cold_direct_1kb.py build
python3 implementation/vmemkv/benchmark/prototypes/run_cold_direct_1kb.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_cold_direct_1kb.py matrix
# 今回は低下したため、16スレッドは実行していない。
```

`build/ltm/results/cold-direct-1kb-20260923/`（Git管理外）に、`direct-probe.json`、両版のソース、`cold-direct.patch`、mainから案1への`proposal1-working-tree.patch`、対象コミット・ブランチ・ハッシュの`provenance.json`、ビルド／検証コマンドとログ、各実行のコマンド／環境／制限／生JSON／終了コード、平均と生throughputの`summary.json`を保存する。
