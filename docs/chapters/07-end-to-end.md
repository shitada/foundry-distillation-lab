# 07 業務全体を3者で比較する

## 現在地
次の行動の評価を終え、実際の業務の流れを評価する段階です。

## 目的
teacher / base / fine_tunedが、同じ一つの顧客入力から安全に業務を完了、または適切な確認質問で停止できるかを調べます。

## 前提
一回の顧客入力に対して複数のモデル・ツール呼び出しを許します。顧客シミュレーターはありません。確認質問に架空の返事を自動生成して会話を継続させません。

## 実施内容
```powershell
python scripts\evaluate.py --help
python scripts\deployment.py --help
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-e2e.json --output runs\e2e-scores.json
```

上の実行は**合成の保存済み記録を採点するオフライン例**で、実測成績ではありません。サンプルの`evidence_kind`は`synthetic_illustration_not_measurement`であり、E2Eには未記録・証拠不足が意図的に含まれます。全行が成功しないこと自体は例の不具合ではありません。入力bundleとレビューの契約は`docs\reference\evaluation-contract.md`を参照します。出力JSONは既存ファイルを上書きせず、同じ出力先で再実行するとexit 2で拒否します。

保存済み/模擬結果による採点と、実推論のrunnerを区別します。**現版の`evaluate.py --send`はローカルrunnerからの直接モデル推論であり、Hosted Agentを呼び出す評価経路ではありません。** Hosted Agent経由のE2E実証は別の追試課題として残ります。live推論には`--approval`と新しい`--run-dir`を使いますが、承認が単一対象なので、`models`を1者だけにした入力・対象に対して承認し、1者ずつ実行します。保存された記録をあとで同条件か検査してオフライン集計します。3者を1対象の承認で実行しません。

`configs\examples\evaluation-live.json`はtools/casesが空で予備費が`null`の**意図的に未完成なテンプレート**です。そのまま実行可能な計画でも承認でもありません。対象、ケース、ツール、件数、予算と回復予備費を確定し、入力hashに結び付けて承認するまで送信しません。直接モデル経路のChat CompletionsとHosted Agentの契約を混同せず、実環境の認証・SDK互換性・提供モデルは別途確認します。

live入力はcases/messages/toolsとendpoint/deployment/configを内包する自己完結したbundleです。外部データファイルを実行中に参照して差し替える方式ではありません。ファイル全体のSHA256を承認に結び付け、最初に読んだbytesのdigestも照合します。各モデル要求の送信前に予算を予約してjournalへ記録し、SDKの暗黙の再試行を無効にします。timeout等で結果が不明なら、承認が残っていても無条件に再送してはいけません。

保存済みHosted Agent観測には、別の**オフラインimport**があります。

```powershell
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-hosted-cases.json --import-hosted data\samples\evaluation-hosted-capture.json --model-label teacher --output runs\hosted-import-demo.json
python scripts\evaluate.py --mode e2e --input runs\hosted-import-demo.json --output runs\hosted-import-demo-scores.json
```

この同梱captureも合成例で、実際のHosted Agent実行ではありません。入力・agent/model/endpoint・toolsのidentityと保存済み観測を照合し、欠けたイベントや最終状態を捏造しません。例はteacherの1記録のみで、他の予定枠は未記録です。captureのreviewは元情報として残しますが、新しいrecordのhashへ自動で再承認しません。モデルHTTPだけの旧`foundry-responses-capture`は、実際のツール実行や副作用の証拠を欠くため、このE2E importの代替にはできません。

実際のcapture実装とRetailSessionを合成HTTP fixtureで接続し、出力した`hosted-retail-evidence`をimport・採点するオフライン結合テストがあります。これはHostedサーバーやSDK・認証の実環境確認ではありません。`final_state_scope: observed_tool_returns_not_store_snapshot`が示す通り、最終状態は観測したツール戻り値の範囲であり、ストア全体のsnapshotや実システムの永続化を証明しません。

live配置には第4章の別承認が必要です。モデル以外のcontract、ツール、状態、最大step、時刻条件は一致させます。Hosted Agentの配置設計でも会話ごとに状態を分離し、payloadに学習データ・oracle・採点コードを入れません。

モデル配置の計画だけなら、次のコマンドでローカルJSONを作れます。

```powershell
python scripts\deployment.py prepare --config configs\examples\cloud-deployment.json --output runs\deployment-plan.json
```

出力には対象resource ID、runの所有者識別子、SKU/model、見積り、API version等が入ります。これはARM要求の**計画作成**で、資源作成やリージョンの利用可否確認ではありません。例のID、モデル、金額は差替え用です。`deploy`・`status`・`cleanup`はネットワーク操作であり、各段階の明示的承認が必要です。新しい所有者識別子を生成しただけで、既存資源を自分の所有物として扱ってはいけません。Hosted Agentのstageは小売実行に必要なファイルだけをallowlistで収録しますが、Hosted Agent配置の成功をこのモデル配置計画で検証したとは言えません。

ケースを少なくとも正常、境界、不足情報、拒否、回復へ分けます。各ケースで呼出し順序・引数・結果、最終状態、日本語説明、要求終了時点の時間、全usageを保存します。モデルの不正要求、ツールで阻止した不正要求、実際の不正な書込みを別々に数えます。

自動採点後に人間が説明と状態をレビューします。確認が必要なケースは確認質問が成功になり得ますが、解決できるのに質問して逃げたケースとは区別します。teacherと同じ道筋かどうかではなく、必須条件を守って正しい結果になったかを判断します。

採点行の状態は`technical_failure`（技術的失敗）、`quality_failure`（品質不合格）、`review_pending`（人間確認待ち）、`confirmed_success`（確認済み成功）で区別します。E2Eの確認済み成功には、そのケースの**証拠hashに結び付いた明示的なreviewer記録**が必要です。自動チェックを通っただけで成功へ昇格しません。

## 出力
ケース×variant×反復の記録、暫定自動採点、人間レビュー、エラー分類、時間・usageの表です。`overall`、`per_model`、`per_case`の分母には予定された未記録枠も含めます。基盤障害、未試行、step上限到達を通常の業務失敗に埋め込まず、母数とともに表示します。usageの未知は`null`、時間は観測数`n`・中央値・nearest-rank方式のp95を記録し、全予定件数が測れたか確認します。

`usage_total`は欠測が一つでもあれば`null`です。`usage_known_subtotal`と`usage_reported_n`は観測済み部分を説明する値で、総費用の根拠としてそのまま使いません。tokenの単位は`input_tokens`、`output_tokens`、`cached_input_tokens`で、cacheは入力の内数です。第8章の`report.py --prepare-evaluation`で保存済み採点reportを費用入力へ接続できます。行と集計を照合し、全予定枠で完全に観測された項目だけを要求当たり平均へ変換します。未知のusage・レビュー、runtime差を埋めたり、価格を取得したりはしません。

## 解釈
例えば100件予定で80件のみ実行し、そのうち60件成功なら「成功率75%」だけでは不十分です。20件未試行の理由と、人間確認済み件数を併記します。費用集計では失敗・回復の呼出しも支出に含めます。

## 完了条件
全予定件数の状態が追跡でき、3者の条件が一致し、主要な境界と人間レビューを満たしています。欠測や条件混在があれば比較不能として保留します。

## 限界
live通し評価は未実施です。保存済み模擬結果の採点成功はモデル品質の検証ではありません。反復不足のp95や、小さなカテゴリの成功率は不安定です。最終holdoutを改善へ使わず、不確実性を記録します。
