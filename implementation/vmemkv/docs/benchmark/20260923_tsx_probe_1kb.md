# TSXによるページ試し読み：利用可否の最小確認（2026-09-23）

**最終判断：ユーザーが棄却。有効化や追加実装・測定は進めない。** 以下は棄却前の利用可否確認記録。

**このホストの現在の設定ではRTMを利用できず、Getへの組込み・性能比較には進まなかった。提案自体の性能を否定した結果ではない。**

`codex/fixed-pread-1kb`から、既存の未コミット変更を保持して`codex/tsx-probe-1kb`を作成した。6ブランチの参照先はすべて`e75746c2d03e0d53edaf566c157aad95172b887c`。今回追加した実行コードは利用可否を確認する独立プローブだけで、通常の製品src/includeと過去の実験用コピーは変更していない。製品作業ツリーは案1のみ。コミット・pushなし。

## 実測

- CPU：Intel Xeon Gold 5418N、96論理CPU。Linux 5.15.0-186-generic、GCC 13.4.0。
- `/proc/cpuinfo`：全96論理CPUで`rtm`・`hle`フラグなし。`tsxldtrk`は全CPUにあるが、RTM対応の判定には使わない。
- CPU 0に固定してCPUIDを直接実行：leaf 7 / subleaf 0 のEBX=`0xf3bfb7ef`、**RTMを示すEBX bit 11は0**。ECX=`0xfb417ffe`、EDX=`0xffdd4432`。RTM_ALWAYS_ABORTも0だが、RTM対応ビットが0のため使用しない。
- プローブはビルド・実行に成功し、`attempts=0`、`decision=use_proposal1`を返した。XBEGIN/XENDは実行していない。案1へ戻す利用可否判定に相当し、DBのGetフォールバック経路を今回実装・測定したという意味ではない。
- `/sys/devices/system/cpu/vulnerabilities/tsx_async_abort`は`Not affected`。これはRTM利用可能の証拠としては扱わない。無効化・非公開の原因がBIOS、OS、マイクロコード等のどこにあるかは未特定。

Intelの[5418N仕様](https://www.intel.com/content/www/us/en/products/sku/232392/intel-xeon-gold-5418n-processor-45m-cache-1-80-ghz/specifications.html)ではTSX対応とされるが、実際の利用可否は別である。[Intel SDM Volume 1](https://cdrdv2-public.intel.com/843827/253665-sdm-vol-1-dec-24.pdf)のRTM検出規則では、アプリケーションはRTM命令を使う前にCPUIDのRTM対応を確認する必要がある。また[IntelのMSR説明](https://www.intel.com/content/www/us/en/developer/articles/technical/software-security-guidance/technical-documentation/cpuid-enumeration-and-architectural-msrs.html)にはRTM機能を非公開にする制御も記載されるが、このホストでその設定が原因とは確認していない。

## 範囲と保留事項

元の提案は、不変baseのページ跨ぎ小レコードで`base_mmap_scan`の対象2ページをRTM内で1回だけ試し読みし、成功時だけmincoreを省き、abort時は案1のmincore→mmap／preadへ戻すもの。利用可能な別環境で試す場合も、abortを非常駐と断定せず、既存の境界・キー検証を維持する。

今回、RTM内の常駐／非常駐アクセス、Getへの組込み、自動フォールバックの実動作、throughputの利得・cold時の負担は未評価。利用不可という前提が判明した時点で終了し、BIOS・OS・セキュリティ設定の変更、sudo、性能測定は行っていない。案1の有望判断と既存案の判断は維持する。

## 再現・保存先

```bash
g++-13 -std=c++17 -O2 \
  implementation/vmemkv/benchmark/prototypes/probe_rtm_1kb.cpp \
  -o build/ltm/results/tsx-probe-1kb-20260923/probe-rtm
taskset -c 0 build/ltm/results/tsx-probe-1kb-20260923/probe-rtm
```

ソース：[probe_rtm_1kb.cpp](../../benchmark/prototypes/probe_rtm_1kb.cpp)。RTM対応時のみ、温めた2ページの試し読みを100回独立に試す小さな分岐を含むが、このホストでは未実行。その部分もGetの実装や再試行ループではない。

`build/ltm/results/tsx-probe-1kb-20260923/`（Git管理外）に`build-command.json`、`probe.json`、`environment.json`、バイナリを保存した。コマンド・CPU機能フラグ・CPUID出力・終了コード・カーネル・対象コミット／ブランチ・ソース／バイナリのSHA256を記録している。
