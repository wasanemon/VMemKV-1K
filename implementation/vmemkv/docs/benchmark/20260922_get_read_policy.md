# 案1への追加A/B：Getの先読み制御と常駐確認の省略

**最終判断：Bはユーザー判断で棄却。今回の目的はLTMの改善であり、常駐時の改善を採用理由にしない。LTMの上積みは小さく、Uniform・16スレッドでは−3.63%だった。追加実装・測定は進めない。Aは見送り、案1は有望・上司の確認待ちを維持する。**

## 初回の結果

**AのLTM改善は+0.26%にとどまった。BはLTMで+0.29〜+0.81%、常駐Uniformで+3.71%。今回の範囲ではBが案1の常駐コストを軽減する候補となった。**

全条件1スレッド・論理CPU 0固定、各版2回の平均。基準は案1適用済みであり、元実装に対する改善率ではない。

| 追加案 | 条件 | 案1（ops/s） | 追加後（ops/s） | 増減 |
|---|---|---:|---:|---:|
| A | LTM Uniform | 18,587 | 18,636 | +0.26% |
| B | LTM Uniform | 18,611 | 18,665 | +0.29% |
| B | LTM Zipf | 42,976 | 43,322 | +0.81% |
| B | 常駐 Uniform | 328,829 | 341,028 | +3.71% |

初回16実行は正常終了し、同じメモリ制限とCPU配置を適用した。その後、ユーザー依頼でBの3条件を追加した（次節）。閾値調整・組合せ評価は行っていない。

- A：今回の短時間比較では採用を後押しする大きなthroughput差は得られなかった。不要な先読みの有無や量は未測定。
- B：常駐Uniformでは2回とも案1を上回った。LTMの上積みは小さい。今回は元実装を測っていないため、案1適用前の性能を完全に回復したとは断定しない。
- ほかの操作・スレッド数や長時間の負荷変化へは一般化しない。試作の統計は傾向を推定するもので、現在の各ページの常駐状態を保証するものではない。

## Bの追加3条件（ユーザー依頼による追加12実行）

**常駐Zipfは+3.04%だったが、LTM Uniform・16スレッドは−3.63%で2回とも低下した。Bを全条件向けの改善として採用する根拠は得られていない。**

既存の案1/BバイナリのSHA256を照合してそのまま使用した。実装・閾値・ビルドは変更していない。各版2回の平均：

| 条件 | スレッド | 案1（ops/s） | B（ops/s） | 増減 |
|---|---:|---:|---:|---:|
| 常駐 Zipf Get Hit | 1 | 418,833 | 431,572 | +3.04% |
| LTM Uniform Get Hit | 16 | 248,108 | 239,094 | -3.63% |
| LTM Zipf Get/Update 50%ずつ | 1 | 14,589 | 14,763 | +1.19% |

個々のthroughput（ops/s）：

| 条件 | 案1の2回 | Bの2回 |
|---|---|---|
| resident Zipf Get-Hit / 1スレッド | 422,844, 414,821 | 431,256, 431,888 |
| ltm Uniform Get-Hit / 16スレッド | 247,960, 248,257 | 239,294, 238,895 |
| ltm Zipf GetUpdate50 / 1スレッド | 14,645, 14,533 | 14,921, 14,605 |

- 前節と同じデータ・メモリ制限・Google Benchmark min_time=2秒。順序は各条件で案1→B→B→案1。
- 1スレッドはCPU 0、16スレッドは同一NUMAノードの16物理コア（論理CPU 0,2,…,30）へ固定。各比較内では同じ配置。
- 混在はGetとUpdateを交互に呼び、合計操作数をthroughputとして数える。各回、同じcheckpointから作った専用コピーへ戻し、コピー作成・fsync・キャッシュ準備は計測から除外した。
- 混在の+1.19%は小さく、今回の2回だけで明確な改善とは判断しない。常駐Zipfは2回ともBが上回り、LTM Uniform・16スレッドは2回とも下回った。低下の原因は切り分けていない。
- この追加範囲で終了。Bの閾値調整・原因別計測・他条件への拡大はしていない。

再現コマンド：`source build/ltm/env.sh` の後に `python3 implementation/vmemkv/benchmark/prototypes/run_get_read_policy_extra.py`。
結果・完全なコマンド・CPU/メモリ制限・バイナリ照合記録は `build/ltm/results/get-read-policy-20260922/B-extra/`。
混在用の一時DBは `build/ltm/data/get-read-policy-extra-20260922/working*` に作成し、終了後に削除。元データと測定ログは保持する。

## 実装と反映状況

`codex/get-read-policy`上で、既存の未コミット作業を保持し、製品の
`src/vmemkv/read_path.hpp`のGet条件を案1の
`is_small && size_hint <= kPageSize - offset % kPageSize`へ変更した。
mainのコミットは`e75746c2d03e0d53edaf566c157aad95172b887c`のままであり、今回もコミット・pushは行っていない。
作業ツリーの案1を実験ディレクトリへコピーし、基準・A・Bを同じ条件でビルドした。
A/Bの変更は実験用コピーと試作部品だけにあり、製品src/includeには案1以外を適用していない。
値・メタデータ分離の試作は使っていない。

### A：独立fd＋POSIX_FADV_RANDOM

Store初期化後、測定前に`/proc/self/fd/<read_fd>`を`O_RDONLY | O_CLOEXEC`で別途openする。
同じinodeであることと`posix_fadvise(..., POSIX_FADV_RANDOM)`の成功を確認して、Get用read_fdを置換する。
fdの破棄には既存T2Memoryのデストラクタを利用し、毎回のGetには処理を追加しない。
Scan用mmapが参照する元のopen file descriptionは変更しない。
製品の`adopt_best_effort()`改修を広げず、読込み方針だけを試すための初期化時差し替えである。

`dup()`はopen file descriptionを共有し、Linuxの`POSIX_FADV_RANDOM`は先読み方針を変える。
今回別openにした理由は[dup(2)](https://man7.org/linux/man-pages/man2/dup.2.html)と
[posix_fadvise(2)](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)に基づく。
先読み量やデバイスI/Oは今回測定しないため、速度差だけで余分なI/Oの有無を断定しない。

### B：ページ跨ぎ小レコードだけ常駐傾向で選択

既存`read_large_get_cold()`に入るうち、サイズヒントが4KB以下でページをまたぐ要求だけが対象。
スレッド別に小さな状態（対象Storeの識別、モード、3個のカウンター）を保持する。

- 初期状態・混在時は案1どおり毎回mincore。32回の観測で31回以上が常駐なら直接mmap、
  31回以上が非常駐なら直接preadへ切り替える。
- 省略中も64回に1回は確認する。傾向と逆の結果が出た時点で毎回確認へ戻り、学習し直す。
- 直接mmapは既存`read_base_record_via()`、直接preadは既存のバッファ読込み部分を使用。
  base境界・キー照合・短い読込み等の検証は残す。大きなレコード・Scan・mutable tailは変更しない。
- 試作は各プロセスで単一Storeを使う。複数Storeの寿命・アドレス再利用を含む汎用的な統計管理、
  ページ別管理、閾値の最適化は実装しない。

混在するZipfでは切替が役立たない、または誤推定で遅くなる可能性がある。
特に直接mmapの推定が外れるとページ読込み待ちが戻る。性能への影響だけを対象とした試作である。

## 最小比較の条件

- 同一ホスト：Xeon Gold 5418N / Linux 5.15.0-186-generic / GCC 13.4、`-O3 -DNDEBUG`。
  既存libvmemkv.a、Google Benchmark v1.8.4、既存bench_kv.cppのGet Hit本体を使用。
- `Bloom-T1InlineValue`、8,259,552キー、キー16B、キー数の80%が1024B値・20%が8B値。
  値の全バイトを走査する同期単件Get。全条件1スレッド、同じ論理CPUへ固定する。
- A：LTM Uniform。B：LTM Uniform、常駐Uniform、LTM Zipf（α=1）。A/Bはそれぞれ案1と独立比較。
- LTMはMemoryHigh=1GiB / MemoryMax=2GiB、常駐はHigh=16GiB / Max=32GiB。
  プロセス内で制限を確認し、適用CPU affinityとともに保存する。
- LTMは初期化時にT2キャッシュを落とす。常駐はT2全体と各mmapのPTEを温める。
  初期化は測定から除外し、Getの反復数はGoogle Benchmarkの較正を使う（min_time=2秒）。
- 各比較は基準→候補→候補→基準の各版2回、計16実行。throughput（items_per_second）だけを評価。
  案1を適用する前の元実装との再比較、A+B、他操作、16スレッドへの拡大は行わない。

## 動作確認

3版それぞれで同じ1,024キー（先頭512件＋固定seedのサンプル512件）を2回読み、
元の値生成関数と全バイト・長さを照合した。Missも確認して成功。
Bの状態遷移は、常駐・非常駐・混在・傾向逆転・64回ごとの再確認を小さい確認関数で検証した。
全テストや更新・回復の再検証は実施していない。

## 再現・記録

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_get_read_policy.py build
python3 implementation/vmemkv/benchmark/prototypes/run_get_read_policy.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_get_read_policy.py matrix
```

元データ：`build/ltm/data/get-hit-prototypes-20260922/dense`（再作成なし）。
結果：`build/ltm/results/get-read-policy-20260922/`（Git管理外）。
`source/`に3版のソースコピー、`proposal1-working-tree.patch`と`adaptive.patch`に差分、
`provenance.json`に対象コミット・ブランチ・コンパイラ・SHA256を保存する。
`A/`と`B/`の各実行に完全なコマンド・環境・メモリ制限・CPU配置・生JSON・throughput・終了コードを保存。
`plan.json`が測定範囲、`summary.json`が各2回の平均と個々の値。

試作部品：[get_read_policy.hpp](../../benchmark/prototypes/get_read_policy.hpp) / [ランナー](../../benchmark/prototypes/run_get_read_policy.py)。
