# BPFで失効させるページ別常駐ヒント：最小試作結果（2026-09-23）

**仕組みは動作したが、今回の最小試作では案1に対する明確な総合的優位は示せなかった。** 常駐Zipfは改善した一方、常駐Uniformは低下し、LTMの差は小さい。約13.1MiBの管理RAMとBPF運用を加える見返りとしては弱く、現時点で案1より優先することは勧めない。ユーザーによる正式な採否判断とは区別する。

## 比較結果

1KB Get Hit、各版2回の算術平均、単位ops/s。比較基準は案1適用済みのバイナリで、案1適用前との比較ではない。

| 条件 | スレッド | 案1 | 常駐ヒント | 案1比 |
|---|---:|---:|---:|---:|
| 常駐 Uniform | 1 | 327,695 | 324,215 | −1.06% |
| 常駐 Zipf | 1 | 412,136 | 421,120 | +2.18% |
| LTM Uniform | 1 | 18,391 | 18,542 | +0.82% |
| LTM Zipf | 1 | 42,892 | 42,839 | −0.12% |

常駐Uniformは2回とも低下、常駐Zipfは2回とも改善した。LTM Uniformは1回目の案1が18,204、2回目が18,577 ops/sで、ヒント版18,537／18,547に対する増減の向きが逆転した。LTMに明確な改善があるとは扱わない。原因別の時間内訳は測っていない。

測定前に「常駐2条件とも+1%超、LTM2条件とも−1%以上なら16スレッドを追加」と定めた。今回は常駐Uniformが条件を満たさず、計16実行で終了した。この目安は統計的有意差の基準ではない。閾値調整、ビット圧縮、他ワークロード、追加プロファイルへは広げていない。

## 実装と反映状況

`codex/tsx-probe-1kb`から未コミット作業を保持して`codex/page-residency-hints`を作成。7ブランチの参照先はすべて`e75746c2d03e0d53edaf566c157aad95172b887c`。各試作は専用コミットになっていない。製品src/includeは案1の条件式1箇所のみで、今回の変更は実験用コピーとprototype部品だけ。未コミット・未push。

- Linuxのpage cache追加・削除tracepointから、dev/inodeとページindexで固定T2だけを選び、mmap可能なBPF array mapを直接更新する。イベント転送キューや常駐デーモンは作らない。
- 1ページあたり64bitに世代と状態（未知0／確認済み1／再登録禁止2）を格納する。削除で世代更新＋再登録禁止、追加で世代更新＋未知とする。
- Getでは案1が常駐確認へ回すページ跨ぎ小レコードだけを対象とする。2ページとも確認済みなら既存base_mmap_scanから読む。その他は既存mincoreを実行し、各ページが常駐かつ同じ世代の未知状態の場合だけCASで確認済みを登録する。
- 削除通知後にmincoreが常駐を返しても、再追加前は登録できない。追加通知だけでは読込み完了を意味しないため、その時点で確認済みにしない。
- 境界・キー・レコードの検証と既存preadを再利用する。8Bインライン、ページ内小レコード、mutable tail、Scanの分岐は変更しない。大レコードと共用する常駐確認ヘルパーには対象判定が加わるが、大レコードにはヒントを適用しない。
- 固定checkpoint1個・1 Store・通常の4KiBファイルページを前提とする。BPF未設定なら案1の経路を使えるが、今回はBPFのロード失敗を検証失敗として測定を止める。

## 確認と制約

既存clang 15・libbpf 0.5.0を利用し、追加パッケージ導入なしでビルドした。ユーザーがsudoで確認・測定用スクリプトを実行した。カーネル設定・TSX設定は変更していない。

- 実機のtracepointフィールド配置を確認。8KiBの専用ファイルで、2ページとも状態が初期0→追加4→確認済み5→削除10→再追加12となることを確認した。終了時に一時ファイルを削除し、BPFを解除した。
- 案1は1,024キー×2回、ヒント版は実BPF接続で1,024キー×3回の全バイト照合とMiss確認に成功。ヒント版のBPFなしフォールバックも確認した。
- CASの小テストで、世代変更後の古い確認結果と、再登録禁止状態からの登録を拒否することを確認した。実際の全ての競合タイミングを網羅した検証ではない。
- 比較中のヒント登録は終了後の共有map状態でも確認した。状態数の集計は計測後で、正確な実行中常駐率やmincore省略率としては扱わない。

ページを固定しているわけではなく、ヒント参照直後の回収・PTE解除でfaultする余地は残る。不変baseのmmap読取りに戻るので、古いヒントは経路選択に影響する。複数Store、checkpoint切替、監視障害の自動復旧、更新・recovery等の汎用対応は未実装。BPFリンクはローダーが保持し、各実行終了時に解除する。

## 条件とメモリ計上

- Xeon Gold 5418N、Linux 5.15.0-186-generic、GCC 13.4.0 / `-O3 -DNDEBUG`。既存の案1バイナリと、そのソースコピーにヒントを追加して作成したバイナリを比較。
- `Bloom-T1InlineValue`、既存8,259,552キー、16Bキー、80%が1024B値・20%が8B値。Uniform／Zipf（α=1）、値の全バイトを読む同期単件Get。
- CPU 0、Google Benchmark min_time=2秒、各版2回で2回目は順序を逆転。常駐はT2とmmapを事前に温め、LTMはT2のキャッシュを落としてから較正・計測する。ヒントは初期未知で、通常Getの常駐確認を通じて蓄積する。事前に全ページを確認済みにはしない。
- 常駐High=16GiB/Max=32GiB、LTM High=1GiB/Max=2GiB。両版とも同じPythonローダーが一時cgroupに入り、Get子プロセスは一般ユーザーで実行する。ヒント版はそのcgroup内でBPF mapを作成する。
- T2実使用量7,030,530,024B、1,716,438ページだけを管理。状態本体13,731,504B（約13.1MiB）。map作成前後のmemory.current増分は13,811,712〜13,856,768Bで、管理費等も含む約13.2MiBが同じcgroupに計上された。ローダー自体のメモリも両版の予算内に含める。

この比較は以前のローダーなしの比較と絶対速度を合算しない。保存容量は増えないが、管理RAMと共有状態参照・登録のコスト、追加・回収イベントごとのCPU費用、sudoとカーネル依存が加わる。対象外ファイルでもtracepointプログラムの入口とフィルターは実行される。これらを含めた今回のthroughputでは、常駐Uniformの改善まで両立できなかった。

## 再現・保存先

主要部品は`benchmark/prototypes/`の`page_residency.bpf.c`、`page_residency_loader.py`、`page_residency_hints.hpp`、`build_page_residency.py`、`run_page_residency.py`。

```bash
mkdir -p build/ltm/results/page-residency-hints-20260923
clang -target bpf -mcpu=v3 -O2 -g -I/usr/include/x86_64-linux-gnu \
  -c implementation/vmemkv/benchmark/prototypes/page_residency.bpf.c \
  -o build/ltm/results/page-residency-hints-20260923/page_residency.bpf.o
python3 implementation/vmemkv/benchmark/prototypes/build_page_residency.py
sudo bash implementation/vmemkv/benchmark/prototypes/probe_page_residency.sh
sudo bash implementation/vmemkv/benchmark/prototypes/measure_page_residency.sh
```

既存の準備済みDB・案1の保存済みソース／ビルドを利用する。新しい測定では出力先を分け、保存済みの実行ディレクトリを上書きしない。

生データは`build/ltm/results/page-residency-hints-20260923/`（Git管理外）。`source/`、`candidate.patch`、ビルドコマンド、`provenance.json`、`runner-provenance.json`に実装と対象を保存。`probe-result.json`・`event-formats.json`にBPFの小さな確認を記録。`measurements/`には各実行コマンド・一般ユーザーUID・適用制限・map作成前後メモリ・値照合・終了コード・生throughput・集計・拡大しない判断を保存した。16測定と2検証の全18scopeが正常終了した。
