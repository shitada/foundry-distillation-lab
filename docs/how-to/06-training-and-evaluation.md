# 第6章の実践手順：学習前後を同じ問題で比べる

[第6章](../chapters/06-training.md)の実験を、7段階で進めます。評価①は手元から配置済みモデルを呼び、次のツール呼び出しや回答を採点します。

| 段階 | 行うこと | 得られるもの |
|---|---|---|
| 1 | 接続先・設定・認証を準備する | 学習前後で共用する設定 |
| 2 | 学習前の生徒モデルを評価する | 比較の基準となる応答とルール検査の結果 |
| 3 | 学習を開始し、状態を確認する | 学習記録と学習済みモデル |
| 4 | 学習で作成されたモデルを配置する | 学習済みモデルの識別子と配置名 |
| 5 | 学習後の生徒モデルを評価する | 同じ問題への学習後の応答 |
| 6 | 自動採点して、前後を比較する | 自動判定・理由と、読むための比較表 |
| 7 | 記録を保管し、後片付けする | 保存した結果と残存資源の確認 |

画面はFoundry (new)を基準にしています（公式資料参照日：2026年9月24日）。

## 1. 接続先・設定・認証を準備する

**何を・なぜ：** 同じ種類・版の生徒モデルを比較できるよう、接続先と生成条件を先に決めます。

### モデルとFoundryの準備

筆者の過去の実験では、教師に`gpt-5.4-pro`、生徒に`gpt-4.1-nano`の`2025-04-14`版を使いました。これは選択の参考であり、どの地域・時点でも利用できる推奨構成ではありません。[第4章のモデル選択](../chapters/04-environment.md#教師モデルと生徒モデルを選ぶ)と[対応モデル・地域](https://learn.microsoft.com/azure/foundry/openai/concepts/models#fine-tuning-models)を参照し、教師ありファインチューニング、ツール呼び出し、Chat Completionsへの対応と料金を確認します。

1. [Foundry](https://ai.azure.com/)で対象プロジェクトを選びます。新規作成では **Create new project → Advanced options** で課金先・リソースグループ・地域を確認します。
2. **Discover → Models → 対象モデル → Deploy → Custom settings** で、学習対象と同じ種類・版のモデルを配置します。配置名の例は`student-base`です。
3. **Build → Models**で配置が`Succeeded`になったことを確認します。学習費に加え、推論・配置の保持費と実験終了までの支出見込みを確認します。

### 接続先とモデルの名前を控える

次の値をFoundryで確認して控えます。評価用の`runs\evaluation-config.json`は[手元の認証と共通設定](#手元の認証と共通設定)、学習用の`runs\training-config.json`は[手順3](#3-学習を開始し状態を確認する)で作成します。どちらも、リポジトリのルートフォルダーにある`runs`フォルダーへ保存します。

| 控える値 | Foundryで確認する場所 | 後で記入するファイルと項目 |
|---|---|---|
| プロジェクトの接続先 | プロジェクトのウェルカム画面にある **Project endpoint** | `runs\training-config.json`の`project_endpoint` |
| モデルを呼び出す接続先 | **Build → Models → 学習前モデルの配置**を開き、コード例にある`base_url`を確認 | `runs\evaluation-config.json`の`base_url` |
| 学習前モデルの配置名 | **Build → Models**に表示される、先ほど自分で付けた名前。例：`student-base` | `runs\evaluation-config.json`の`targets`内の`base` |
| 学習対象のモデル識別子 | 選んだモデル・版に対応する値を[ファインチューニング対応モデルの一覧](https://learn.microsoft.com/azure/foundry/openai/concepts/models#fine-tuning-models)で確認。例：`gpt-4.1-nano-2025-04-14` | `runs\training-config.json`の`model` |

プロジェクトの接続先は、学習データのアップロードや学習の依頼に使います。形式は`https://<リソース>.services.ai.azure.com/api/projects/<プロジェクト>`です。

モデルを呼び出す接続先は、学習前後の評価に使います。配置のコード例から、`https://<リソース>.openai.azure.com/openai/v1/`のように`/openai/v1/`まで含む値を控えます。

学習後モデルの配置名は、[手順4](#4-学習済みモデルを選んで配置する)で配置するときに決め、`runs\evaluation-config.json`の`targets`内の`fine_tuned`に記入します。本手順では`student-fine-tuned`を使います。

### 手元の認証と共通設定

PowerShellでリポジトリのルートから実行します。以下は第4章で作成した仮想環境のPythonを直接指定するため、環境の有効化は不要です。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[cloud]"
az login --tenant "<テナントID>"
az account set --subscription "<サブスクリプションID>"
az account show --query "{subscription:id, tenant:tenantId, user:user.name}" --output table
Copy-Item configs\examples\evaluation.json runs\evaluation-config.json
```

教材は`DefaultAzureCredential`を使います。表示されたアカウントが対象環境と一致することを確認します。`runs\evaluation-config.json`は一度だけ複製し、次の内容の接続先と配置名を自分の環境に合わせます。

```json
{
  "base_url": "https://<リソース>.openai.azure.com/openai/v1/",
  "targets": {"base": "student-base", "fine_tuned": "student-fine-tuned"},
  "max_completion_tokens": 1024,
  "timeout_seconds": 60,
  "max_model_calls": 12,
  "max_tool_calls": 24
}
```

出力上限は1要求あたり、モデル・ツール呼び出し上限は1ケースあたりです。評価①は各問題を1回呼び出し、ツールを実行しません。後の評価②でも同じ設定形式を使います。比較の途中で生成条件を変更しません。

**結果：** 接続先・配置名・認証と、学習前後で共有する評価設定が揃います。

## 2. 学習前の生徒モデルを評価する

**何を・なぜ：** 学習の効果を判断する基準を、学習開始前に保存します。

[第5章](../chapters/05-data.md)の`runs\next-action-input.json`を使います。問題・履歴・ツール仕様・参照例を業務ルールと照合してください。`train.jsonl`、`validation.jsonl`、`development.jsonl`、`final.jsonl`の用途は混ぜません。同梱サンプルは説明用であり、教師の実測結果ではありません。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py run --mode next-action `
  --input runs\next-action-input.json --config runs\evaluation-config.json `
  --model base --run-dir runs\base-before
```

`runs\base-before`に`input.json`、送信前設定の`execution-start.json`、`evidence.json`、`scores.json`、実行記録の`execution.json`が自動保存されます。問題ファイルの`models`や`records`、出所ラベルを書き換える必要はありません。

**結果：** 学習前の入力・応答・ルール検査の結果が残ります。手順6では、この保存済みの応答を自動採点します。

## 3. 学習を開始し、状態を確認する

**何を・なぜ：** 学習用・検証用データを送り、固定した設定でファインチューニングします。

リポジトリのルートフォルダーにある`runs`フォルダーに、エディターで`training-config.json`を作り、以下の内容を記入します。[手順1の「接続先とモデルの名前を控える」](#接続先とモデルの名前を控える)で控えたプロジェクトの接続先を`project_endpoint`に、学習対象のモデル識別子を`model`に記入して保存します。

```json
{
  "project_endpoint": "https://<リソース>.services.ai.azure.com/api/projects/<プロジェクト>",
  "model": "gpt-4.1-nano-2025-04-14",
  "training_type": "Standard",
  "hyperparameters": {"n_epochs": 2},
  "seed": 42,
  "suffix": "retail-study"
}
```

`n_epochs: 2`は学習データを繰り返す回数、`seed: 42`は乱数の指定、`suffix`は識別用です。これらは最適値ではなく、最初の比較条件です。対象モデルの学習単価と学習量から費用を見積もります。

```powershell
.\.venv\Scripts\python.exe scripts\train.py start --train runs\data\train.jsonl `
  --validation runs\data\validation.jsonl --config runs\training-config.json `
  --run-dir runs\training
.\.venv\Scripts\python.exe scripts\train.py status --run-dir runs\training --wait `
  --timeout-seconds 3600 --poll-seconds 10
```

`start`は形式検査、入力・設定の保存、両ファイルのアップロード、処理完了待ち、学習依頼を順に行います。学習依頼は1回です。`status`は読み取り専用で、`--wait`なしなら現在の状態を1回取得します。

待機が時間切れになっても、クラウドの学習が止まったとは限りません。`status`で確認します。`start`の成否が不明なら送り直さず、保存済みreceiptやFoundryのジョブ一覧で元の要求を確認します。

**結果：** `runs\training`に入力・設定とサービスの受付記録が残り、学習状態と生成されたモデルを追えます。

## 4. 学習済みモデルを選んで配置する

**何を・なぜ：** 学習で作成されたモデルを配置し、学習前と同じ方法で呼び出せるようにします。

1. 手順3の`status`コマンドの出力で、`outcome`が`succeeded`になったことを確認します。`states` → `job` → `fine_tuned_model`に表示された学習済みモデルの識別子を控えます。本手順では、このモデルを評価に使います。
2. Foundryで、その識別子の学習済みモデルを開き、**Deploy**から配置します。配置名は`runs\evaluation-config.json`の`targets`内の`fine_tuned`に記入した`student-fine-tuned`を使います。
3. **Build → Models**で`Succeeded`と、学習済みモデル識別子・配置名の対応を確認します。配置方式は学習前と揃え、保持費と削除予定を確認します。

**結果：** 学習済みモデルが配置され、学習後の評価を開始できます。

## 5. 学習後の生徒モデルを評価する

**何を・なぜ：** 同じ問題・ツール・生成条件で、変化を測ります。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py run --mode next-action `
  --input runs\next-action-input.json --config runs\evaluation-config.json `
  --model fine_tuned --run-dir runs\fine-tuned-after
```

学習前と変えるのはモデル指定と保存先だけです。出力ファイルと失敗・未試行の有無を確認します。

**結果：** 学習後の記録が、学習前を上書きせず保存されます。

## 6. 自動採点して、前後を比較する

採点用モデルを準備し、**自動採点と比較表の作成をまとめて実行して、最後に結果を読みます。**

### 6.1 採点用モデルと設定を準備する

学習前後の生徒モデルとは別に、採点用モデルの配置を用意します。同じFoundryリソース内に、Chat Completionsの[構造化出力](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/structured-outputs)に対応するモデルを配置します。例は`gpt-4.1`、配置名は`grader`です。利用できるモデル・版は対象の地域で確認します。

採点用の配置がまだなければ、[手順1のモデル配置](#モデルとfoundryの準備)と同じ操作で配置し、**Build → Models**で`Succeeded`を確認します。認証は手順1のものを使います。

次のコマンドで採点用の設定例を`runs\grading-config.json`へ複製します。

```powershell
Copy-Item configs\examples\grading.json runs\grading-config.json
```

`runs\grading-config.json`をエディターで開き、`base_url`を手順1で控えた同じリソースの接続先、`deployment`を採点用の配置名にして保存します。設定は次の4項目です。

```json
{
  "base_url": "https://<リソース>.openai.azure.com/openai/v1/",
  "deployment": "grader",
  "max_completion_tokens": 1024,
  "timeout_seconds": 60
}
```

`max_completion_tokens`は採点1回の出力上限、`timeout_seconds`は通信の待機上限です。この設定と教材に組み込まれた採点基準を、学習前後で共用します。

### 6.2 自動採点して、比較表を作る

次の3行を上から順に実行します。最初の2行で保存済みの学習前後の応答を同じ採点用モデルで採点し、最後の1行で比較表を作ります。採点結果は`runs\base-graded`と`runs\fine-tuned-graded`、比較表は`runs\comparison\comparison.csv`に保存されます。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\base-before --config runs\grading-config.json --output-dir runs\base-graded
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\fine-tuned-after --config runs\grading-config.json --output-dir runs\fine-tuned-graded
.\.venv\Scripts\python.exe scripts\evaluate.py compare --before runs\base-graded --after runs\fine-tuned-graded --output-dir runs\comparison
```

### 6.3 比較表の代表例を読む

`runs\comparison\comparison.csv`をExcelなどの表計算ソフトで開きます。**この表は閲覧用です。判定を書き込んだり、上書き保存したりしません。** 各行が一つの問題です。まず`decision`列で変化を確認します。

| 表示される値 | 意味 |
|---|---|
| `improvement` | 自動判定が、学習前の不合格から学習後の合格へ変わった |
| `regression` | 自動判定が、学習前の合格から学習後の不合格へ変わった |
| `no_change` | 両側とも合格、または両側とも不合格だった |
| `incomparable` | 判定保留や応答取得の失敗などがあり、比較できない |

改善・悪化・変化なし・比較できない行から代表例を選び、次の列を読みます。

| 読む内容 | 列 |
|---|---|
| 問題・種類・会話履歴 | `case_id`、`category`、`messages` |
| 期待する行動と参照例 | `expected`、`reference` |
| 学習前後の応答 | `before_response`、`after_response` |
| 自動判定 | `before_automatic_decision`、`after_automatic_decision` |
| 判定の出所 | `before_assessment_source`、`after_assessment_source` |
| 採点用モデルが示した理由 | `before_judge_reason`、`after_judge_reason` |
| ルール検査や実行失敗の理由 | `before_reasons`、`after_reasons` |
| 応答時間（秒） | `before_latency_seconds`、`after_latency_seconds` |
| トークン使用量 | `before_usage`、`after_usage` |

自動判定は`success`（合格）、`failure`（不合格）、`needs_review`（判断材料が足りず保留）、`unknown`（採点エラーや応答欠落などで不明）です。出所の`model`は採点用モデル、`deterministic`はルール検査を示します。時間と使用量は評価対象の生徒モデルの記録で、採点用モデルの分は含みません。`null`は未取得を表します。

**結果：** 自動判定と実際の応答を照らして変化を説明でき、第7章で確かめたい点が整理できます。

## 7. 記録を保管して後片付けする

**何を・なぜ：** 結果を追試できる形で残し、不要な保持費を止めます。

1. 学習・評価・比較のrun、選んだモデル、設定、費用見積りと取得できた使用量・請求を保管します。個人情報や接続情報を含む記録は公開しません。
2. Foundryの **Build → Models** で、自分の実験で作った不要な配置を選び、削除します。共有の教師モデルや他人の資源は削除しません。
3. 配置が消えたことと、残した資源・ジョブ・課金状況を確認します。ローカルの待機終了とクラウド資源の削除は別です。

**結果：** 記録と残存資源が明確になります。評価①は固定履歴に対する次の判断の比較です。一件の業務全体の評価は[第7章](../chapters/07-end-to-end.md)へ進みます。

## 参照先

- [評価の入力・出力と自動採点](../reference/evaluation-contract.md)
- [クラウド実行と受付記録](../reference/cloud-execution.md)
- [結果不明時の対応と後片付け](../reference/safety.md)
- [Foundryのモデル配置](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/deploy-foundry-models)
