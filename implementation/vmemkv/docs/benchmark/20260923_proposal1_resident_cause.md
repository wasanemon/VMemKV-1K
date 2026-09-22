# 案1の常駐1KB Get Hit低下：原因切り分け（2026-09-23）

## 対象と方法

追加比較でも低下した常駐1KB Get HitのUniform／Zipf × 1／16スレッドだけを対象とする。LTMや他操作へは広げない。

次の3構成を各2回、計24実行で比較する。

| 構成 | Get経路 |
|---|---|
| original | 案1適用前。小レコードは直接mmap |
| proposal1 | ページ内なら直接mmap、ページ跨ぎはmincore→mmap／pread |
| mincore-stub（診断専用） | 案1と同一バイナリ。LD_PRELOADでmincoreだけを「全ページ常駐を返す関数」に置換 |

診断版は`resident_mincore_stub.c`が常駐ビットを埋めて成功を返す。案1の追加条件式、ページ範囲の計算、thread_local vectorのresize、常駐ビットの検査、レコード検証、参照するmmapは残る。これにより、追加分岐やmmap選択をまとめて消すのではなく、mincoreのシステムコールを除去した差を観測する。

既存harnessが計測前にT2全ページをbuffered preadで読み込み、各mmapのページテーブルも温める。常駐を前提に結果を固定する診断であり、**実際の常駐確認を省く製品改善案ではない。LTMには適用しない。** 既存のBや固定pread、Direct I/Oのコードは使用しない。

## 条件・対象ソース

- Xeon Gold 5418N、Linux 5.15.0-186-generic、GCC 13.4.0、`-O3 -DNDEBUG`。
- `Bloom-T1InlineValue`、既存8,259,552キー、16Bキー、キー数の80%が1024B値・20%が8B値。Zipf α=1。値の全バイトを読む同期Get Hit。
- High=16GiB／Max=32GiB。各実行で適用値を確認。全4条件をCPU 0,2,…,30の集合に制限。1スレッドもこの集合内で実行する。
- Google Benchmark min_time=5秒。初期化・全ページの事前読込みを計測から除外。各条件の2回目は3構成の順を逆転。
- CPU集合と計測時間は、元の退行追加比較と揃えた。固定preadの直近比較（1スレッドCPU 0限定・min_time=2秒）とは異なるため合算しない。
- 案1は固定pread比較時の`proposal1`バイナリをSHA256照合して再利用。originalはその保存済みソースコピーの条件式を`if (is_small)`に戻し、同じビルドコマンドで作成した。mincore-stubはproposal1と同一バイナリへのリンク＋共有ライブラリである。
- 元コミットは`e75746c2d03e0d53edaf566c157aad95172b887c`。案1の未コミット差分を含む。現在の`codex/fixed-pread-1kb`上で診断用ファイルだけを追加し、製品src/includeは案1のみのまま。コミット・pushなし。

## 結果

**今回の常駐4条件では、案1で追加されたmincore呼出しのコストが性能低下の主因と判断できる。** 案1の低下を再現し、同一バイナリでmincoreだけを置換すると元実装とほぼ同程度まで回復した。4条件とも、2回の反復それぞれで案1は元実装より低下し、診断版は案1を上回った。

各版2回の平均、単位ops/s。比率は丸め前の平均で算出し、今回測り直したoriginalを共通の基準とする。

| 条件 | original | 案1 | mincore置換 | 案1／original | 置換／original |
|---|---:|---:|---:|---:|---:|
| Uniform・1スレッド | 333,567 | 325,100 | 337,735 | −2.54% | +1.25% |
| Zipf・1スレッド | 440,989 | 424,738 | 442,719 | −3.69% | +0.39% |
| Uniform・16スレッド | 5,588,446 | 5,334,001 | 5,577,366 | −4.55% | −0.20% |
| Zipf・16スレッド | 7,314,412 | 6,914,862 | 7,284,062 | −5.46% | −0.41% |

説明は次のとおり。元実装の小レコードGetは直接mmapを参照する。案1ではページを跨ぐ場合、mmapを参照する前にmincoreを呼ぶ。今回の常駐条件ではこの確認によるI/O待ち回避の利益がなく、呼出しの負担が増える。診断版では追加条件式やvector処理、検証、mmap参照先を残したまま回復しているため、境界判定そのものだけを主因とする説明は支持されない。

これは単なる関数別サンプリングではなく、該当呼出しを取り除いた対照実験による切り分けである。一方、mincoreのカーネル内部のページ状態確認・ロック・システムコール出入りの時間配分までは測定していない。16スレッドで低下が大きい理由を特定のロック競合と断定しない。置換後に残るoriginalとの差（−0.41〜+1.25%）の原因や統計的有意性も追究していない。

**この診断版を製品に適用してよいという結論ではない。** 常駐結果を固定すると、LTMでは案1が避けたページフォールト待ちが戻り得る。Bの棄却判断を変更したり、適応制御の再実装・閾値調整を行ったりはしない。原因の切り分けができたため計24実行で終了し、sudo・追加perf採取・他ワークロードの測定は行わなかった。

## 再現と保存先

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_resident_mincore_diagnosis.py build
python3 implementation/vmemkv/benchmark/prototypes/run_resident_mincore_diagnosis.py matrix
```

ソースは[診断ランナー](../../benchmark/prototypes/run_resident_mincore_diagnosis.py)と[mincore置換部品](../../benchmark/prototypes/resident_mincore_stub.c)。sudoやカーネル設定変更は不要。systemdユーザースコープの起動にはサンドボックス外実行を使った。

`build/ltm/results/resident-mincore-diagnosis-20260923/`（Git管理外）に、ビルドコマンド、元ソースコピー、バイナリと部品のハッシュ、比較計画、各実行コマンド・環境・適用制限、ログ・終了コード、生throughputと集計を保存する。`LD_PRELOAD`の設定は各実行の`diagnostic-environment.json`に記録し、置換部品のロードをログで確認する。
