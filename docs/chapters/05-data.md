# 05 teacher履歴を学習用データへ変える

## 現在地
業務と評価計画を固定した後、データの出所・品質・分割を整えます。

## 目的
「大量に集めればよい」ではなく、正しい行動を含み、評価へ漏れない学習例を用意します。

## 前提
teacher traceは採点済みの正解ではありません。実際の収集には推論費がかかります。`collect.py`の通常経路は**エクスポート済み履歴のimportとオフライン収集計画**です。明示的な`--invoke`による将来のazd呼出し経路もありますが、今回未実行・cloud未検証であり、現環境での契約・retry確認と別承認が必要です。廃止されたStored Completionsを既定経路にはしません。

## 実施内容
まず公開可能な小さなサンプルで整形・分割の経路を確認します。

```powershell
python scripts\prepare_data.py --input data\samples\traces.jsonl --output runs\data --seed 42
python scripts\collect.py --help
python scripts\collect.py --input data\samples\traces.jsonl --format conversation --output-dir runs\trace-import
```

このサンプルは架空店舗に基づく**スクリプト作成の日本語配送問い合わせ20件**で、teacher出力ではありません。学習データの経路を確認するための素材であり、モデルの正しさ・蒸留の効果・業務の網羅性を示すものではありません。配送の1カテゴリのみであることを監査結果と一緒に記録します。

`collect.py`の例はimportの練習で、クラウドへ要求しません。新しい`runs\trace-import`に`traces.jsonl`と`collection-manifest.json`を生成し、元の`source_kind: illustrative_scripted_not_model_output`を保持します。manifestの`source_kinds`、`cloud_requests_made: 0`、未知usage/費用、業務・プライバシーレビューが必要との表示を確認します。importできたことをteacher観測へ格上げしません。

入力JSONLは1行に1会話のobjectを置き、`conversation_id`、`category`、`messages`を必須とします。`tools`、`source_kind`は任意です。`messages`の中身はロール付きの会話履歴であり、単なる問い合わせ文の一覧ではありません。実測teacher履歴へ置き換えるときも出所を保持し、スクリプト作成例と混ぜてteacher生成と表示しません。

実際の履歴を持つ場合は`--format conversation`、このHosted skeletonのHTTP境界captureなら`--format responses-capture`を選びます。後者は未完了callや外部履歴への依存を拒否し、内部reasoningを学習例へ変換しません。収集計画の作成と公式のHosted Agent呼出しの追試は別手順です。具体的な入力schema・承認・capture経路は[クラウド実行境界](../reference/cloud-execution.md)を参照してください。

将来の呼出しは、承認済みprompt集合を含むcollection planから1会話を固定します。`cloud-invocation.json`でconversation ID、version付きagent endpoint、version、timeout、見積りを明示します。

```powershell
# オフライン：既存collection planから一回の呼出し計画を作成
python scripts\collect.py --prepare-invocation --input runs\collection-plan\collection-plan.json --config configs\examples\cloud-invocation.json --output-dir runs\one-invocation
# 以下は今回は未実施。環境検証と別途承認の後だけ実行するnetwork操作
python scripts\collect.py --invoke --input runs\one-invocation\invocation-plan.json --output-dir runs\invocation --execute --approval runs\invocation-approval.json
```

外側の承認はversion付きagent endpointへの1回の呼出し、内側のHosted収集承認はproject endpointとmodelを対象とする各モデル要求であり、別の承認です。wrapperは予算予約・journal後にazdを1回起動しますが、**azd内部のHTTP retryはSDKの`max_retries=0`では制御できません**。インストール済み拡張のflags/raw形式/retryを検証するまでlive実行しません。receiptの`trace_capture_verified: false`は、呼出しが返ってもtrace取得完了の証拠ではないことを示します。配置、永続volume・承認mount、capture回収は自動化されていません。

teacher履歴には顧客入力、モデルの応答、ツール呼び出し・引数・結果、終了理由が必要です。非公開の内部思考を学習対象にしません。形式だけでなく、必須手順、実際の結果に基づく説明、架空業務日2026-08-31の整合性を確認します。危険な呼出しをツールが拒否した例は、そのまま良い模範例にしません。

分割単位は行のランダム抽出だけでは不十分です。実装は注文・会話のグループ単位で分割し、最低10グループを要求します。train 60%、validation 15%、development 15%、残りfinalを目安にグループを割り当てるため、会話件数の比率はグループサイズと丸めで異なります。同じ会話、注文、言い換え、同じテンプレートの意味的重複がsplitに跨がないか確認します。seedが固定されていても漏洩は防げません。重複チェック結果と、カテゴリ別件数・欠落を確認します。

学習とvalidation以外に、改善用評価と独立した最終holdoutの役割を分けます。自動splitだけで独立性が完成したと思わず、最終評価を使わない管理責任を定めます。

## 出力
| 出力 | 用途 |
|---|---|
| `normalized.jsonl` | 整形済み会話と出所を監査する |
| `train.jsonl` / `validation.jsonl` | 学習側の`messages`と`tools`だけのJSONL |
| `development.jsonl` | 改善用評価。結果を見て調整する対象 |
| `final.jsonl` | 最後まで見ない最終holdout。改善に使用しない |
| `next-actions.jsonl` | **developmentだけ**から抽出した次の行動のケース |
| `audit.json` / `manifest.json` | 件数、分割、入力/出力hashなどの証跡 |

`next-actions.jsonl`の各ケースには`case_id`、`conversation_id`、`category`、`messages`、`tools`、`reference`（assistantメッセージ全体）、`reference_text`、`ground_truth`（tool callの一覧）、`kind`（tool/text）が入ります。これは期待行動のケースであって、3者の推論済み結果ではありません。評価runnerの入力bundleへ渡すときは、そのCLIのスキーマを確認して結果を対応づけます。

次のオフラインadapterで評価入力bundleへ変換できます。

```powershell
python scripts\evaluate.py --mode next-action --prepare-next-actions --input runs\data\next-actions.jsonl --output runs\next-action-input.json
```

`kind`と`ground_truth`を期待行動の`expected.kind`/`expected.calls`へ対応づけ、参照・メタデータ・ツールと入力SHA256を保持します。出力は`records: []`、`evidence_kind: unreviewed_cases_no_predictions`、`quality_review_approved: false`です。**teacher履歴・予測・成績は生成しません。** この準備でliveフラグは使えず、出力は新規ファイルに限定します。

manifestの`quality_review_approved: false`は、変換・検査の成功が業務品質やプライバシーの承認を意味しないことを示します。別runへ流用するときはhashを照合し、必要なレビューの証跡を別途残します。出力ディレクトリは上書きしません。

## 解釈
20件のスクリプト作成サンプルが通るのは処理経路の確認であって、学習に十分な量・網羅性の証明ではありません。不正例や未解決例は「自動的に捨てて忘れる」のではなく、除外理由と検出数を残します。実際のteacher由来データの利用条件、業務・プライバシーレビューは別途必要です。

## 完了条件
誰が見ても学習と評価の出所が追え、明白な漏洩がなく、カテゴリ不足を説明できれば進みます。漏洩や不適切な模範例が見つかれば学習前に止めます。

## 限界
同一テンプレート由来の合成データは、多様な実顧客分布の代用になりません。合成でも利用条件・個人情報・機微情報の混入を確認します。検出器が見つけない意味的重複は人間が点検します。
