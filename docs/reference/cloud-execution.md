# クラウド実行境界と追試手順

この版の検証は **コード生成・標準ライブラリのmockテスト・オフラインCLI** までです。
ファイル送信、学習、推論、Azure配置、Hosted Agentの起動・コンテナーbuildは実施していません。
元デモの過去の実行結果を本リポジトリの実績と扱いません。設定例の接続先・モデルは置換が必要です。価格は実行時に確認します。

## 1. 依存と認証

通常のprepare/import/stageとmockテストはPython 3.11以降の標準ライブラリだけで動作します。
計画作成時にSDK、Azure CLI、azdをインストールしたり認証情報を探索したりしません。

| 用途 | SDK境界 | 状態 |
|---|---|---|
| 制御側: start/status | rootの任意extra `cloud`: `openai>=3,<4`, `azure-ai-projects>=2.5,<3`, `azure-identity>=1.25,<2` | lazy import、API形状を公式資料と照合、サービス未検証 |
| Hosted Agent | `deploy/hosted-agent/requirements.txt`。OpenAI **2.54.0**、Projects **2.3.0**、agent-framework-core **1.15.0**、foundry **1.11.0**、hosting **1.0.0b260821** 等を分離固定 | 元のskeletonの組合せ。今回の新コードで互換性・cloud動作は未検証 |
| ARM | Python `urllib` + lazy `DefaultAzureCredential`。API version **2024-10-01** | payload/mock検証、サービス未検証 |

Hosted側の依存をrootと一環境へ混ぜません。追試用の環境で
`python -m pip install ".[cloud]"`、Hosted側は専用コンテナー/環境に導入します。
SDK解決後の実バージョン、OS、リージョン、モデルversion、料金取得日をrunに記録してください。
依存の固定はそのサービスのPreview機能が安定版であるという意味ではありません。

APIキーの読取・設定・保存は実装していません。すべて`DefaultAzureCredential`を利用し、
HostedではマネージドIDを推奨します。必要なRBAC、project endpoint、モデル利用可否、
利用するモデルとリージョンを実行前に確認してください。

## 2. オフライン操作

以下は**新リポジトリroot**で実行します。`runs`以下の出力先は毎回新しい名前にします。
既存ファイル/出力ディレクトリの上書きはしません。

```powershell
# 同梱データはSCRIPTED説明用例。teacher観測・性能証拠ではない
python scripts\prepare_data.py --input data\samples\traces.jsonl --output runs\data --seed 42

# fine-tuned model IDを人間が選択し、設定を別versionで確定してからpayloadを生成
python scripts\deployment.py prepare `
  --config configs\examples\cloud-deployment.json --output runs\deployment-plan.json

# 実際のexport済み会話を正規化して取り込む。これは送信コマンドではない
python scripts\collect.py --input runs\export.jsonl --format conversation `
  --output-dir runs\collected

# Hosted Agentに許可する一回限りの入力集合を固定
python scripts\collect.py --prepare --input runs\prompts.jsonl `
  --config configs\examples\cloud-collection.json --output-dir runs\collection-plan

# 配置用build contextだけをoffline作成。Docker build/registry push/配置は行わない
python deploy\hosted-agent\stage.py --output runs\hosted-build
```

`training.json`の`model`はモデルと必要なversionを明示し、`training_type`は
`Standard` / `GlobalStandard` / `Developer`から選びます。Global/Developerのデータ所在地を
確認してください。hyperparametersの自動値を使う場合はキー自体を省略します。
`"batch_size": "auto"`を明示送信する旧実験固有の回避策や再送機構は移植していません。

同梱の20件は元の架空storeを用いたSCRIPTEDの日本語配送問い合わせ例であり、
teacherの生成結果でも性能評価結果でもありません。`prepare_data.py`は
`normalized.jsonl`、`train.jsonl`、`validation.jsonl`、`development.jsonl`、`final.jsonl`、
development由来だけの`next-actions.jsonl`、`audit.json`、`manifest.json`を出力します。
order/conversationの連結groupを60/15/15/残余に分け、10独立group以上が必要です。
manifestの`quality_review_approved: false`を確認してください。1カテゴリだけの例なので業務範囲を
代表せず、学習・評価へ進むための業務品質/個人情報レビューは未完了です。

学習の`start`は、送信前にtrain/validationのUTF-8 JSONLを読み、UTF-8 BOM付きの新ファイルに正規化します。
元入力・正規化後の内部識別値、bytes・行数・設定を`upload-plan.json`へ自動保存します。
10件未満のtrain、空行、完全一致するtrain/validation行、想定外のトップレベルフィールドを拒否します。
これは**構造検査**であり、業務品質・意味的重複・最終評価漏洩・個人情報レビューの代替ではありません。
評価正解やケースmetadataはSFT payloadへ含めません。

## 3. 実行前の確認と保存する記録

対象・入力・呼び出し上限と、学習・推論・配置の保持費を確認します。
見積りは使用量や請求とは分け、不明な値を0と扱いません。
コマンドは入力・設定のsnapshotとサービスの受付記録（receipt）を自動保存します。
内部識別値と送信記録は、取り違えや重複送信を防ぐために自動管理します。

**タイムアウト・切断・応答保存失敗では、受け付けられたか不明です。**
SDKの自動retryを無効にし、変更を伴う要求を自動で送り直しません。
記録の削除や別runによる再送ではなく、元の要求をサービス側で確認してください。
読み取り専用の状態確認は繰り返せます。ローカルの待機終了は、クラウドの学習停止や資源削除ではありません。

## 4. 学習の実行入口（今回は未実施）

第5章の分割済みデータと、接続先・モデル・学習条件を記入した設定を使います。
具体例は[第6章の実践手順](../how-to/06-training-and-evaluation.md#3-学習を開始し状態を確認する)を参照してください。

```powershell
python scripts\train.py start --train runs\data\train.jsonl `
  --validation runs\data\validation.jsonl --config runs\training-config.json `
  --run-dir runs\training
python scripts\train.py status --run-dir runs\training --wait `
  --timeout-seconds 3600 --poll-seconds 10
```

`start`は入力を検査・保存し、学習用と検証用をアップロードします。
両ファイルがprocessedになるのを待ってから学習を1回依頼します。
`status`はGETだけで、`--wait`なしなら1回、ありなら指定間隔・時間内で状態を確認します。
待機の時間切れでも`start`を送り直しません。事前に決めた検証データと基準でcheckpointを選び、
理由を記録します。最終評価は候補選びに使わず、配置はFoundryの画面で行います。

run直下に`config.json`、`train.jsonl`、`validation.jsonl`、`upload-plan.json`、
両データの`*-upload-receipt.json`、`submit-plan.json`、`job-receipt.json`を保存します。
送信記録は`attempts`、読み取りの観測は`observations`に保存します。
`status`は失敗・時間切れ・学習ジョブ未確認の場合に0以外の終了コードを返します。
job receiptの保存だけが失敗した場合は、成功した送信記録からIDを確認できます。学習依頼を再送する機能ではありません。

既知IDがあればreceiptを起点とするGETで照合します。IDのない結果不明upload/jobは、
portal/公式SDKのGET inventory照合でIDと所有を調べます。このCLIはIDを捏造したり、
曖昧な一致を採用したり、POSTで照合したりしません。判断できなければ停止のままです。

## 5. trace取得ルート（Preview境界を明示）

`collect.py`はFoundryの未確認なinvocations Preview URLを推測して直接呼びません。
`--invoke`は公式の`azd ai agent invoke`を起動するnetwork modeです。
「問い合わせを送ったのでtrace収集成功」とも表示しません。次の2つの**実データ**入力を扱います。

1. `--format conversation`: 既存の観測からexportしたJSONL。
   1行に`conversation_id`, `category`, `messages`（Chat Completions形式のuser/assistant/tool順序）、
   任意の正本`tools`, `response_id`, `usage`を含めます。評価正解は含めません。
   Frozen system prompt/tool contractと完全なtool call/result順序を検査します。
   業務品質・プライバシーレビュー前で、出典未指定なら`exported_trace_unreviewed`と表示します。
   入力の`source_kind`は保持するため、SCRIPTED説明用例をcloud観測へ格上げしません。
   collection manifestにも`source_kinds`を残します。
2. `--format responses-capture`: このHosted skeletonがmodel HTTP境界で保存したJSONL。
   requestの`instructions`/完全な`input`/`tools`と、SSEの
   `response.completed`等に含まれるresponse JSONを取得します。認証headerは保存しません。
   内部reasoningは学習メッセージへ変換しません。既知usageは各model call分を保存します。
   `previous_response_id`依存、欠落call、failed/incomplete、未完了tool sequenceは拒否します。

Hosted取得は、別途検証したHosted Agentへ**計画内のpromptを1回だけ送信**する手順です。
`prompts.jsonl`は各行を`{"conversation_id":"...","category":"返品","prompt":"..."}`にします。
`cloud-collection.json`では、1会話あたりの`max_model_calls: 12`と`max_tool_calls: 24`を設定します。
上限に達したら停止します。費用の見積りは設定の実行条件とは分けて確認し、実際の使用量で振り返ります。
containerには`COLLECTION_PLAN`, `COLLECTION_RUN_DIR`でplanと永続書込先を渡します。
planはimageへ焼き込まず、実行環境へmountします。`main.py`は設定を読み、入力と呼び出し回数を制限します。

### 具体的な将来のinvoke入口（今回未実行）

配置済みagentの**version付きendpoint**を`cloud-invocation.json`のコピーへ明示し、
collection-planに存在する`conversation_id`を選びます。endpointは設定したproject配下でなければ
拒否し、versionの一致も確認します。例の`REPLACE_...`を実入力IDへ置換してください。

```powershell
# offline: agent endpoint/version・prompt・元collection-plan hashを一つのinputに固定
python scripts\collect.py --prepare-invocation `
  --input runs\collection-plan\collection-plan.json `
  --config configs\examples\cloud-invocation.json --output-dir runs\one-invocation

# network: 対象・入力・費用を確認して実行
python scripts\collect.py --invoke --input runs\one-invocation\invocation-plan.json `
  --output-dir runs\invocation
```

外側の接続先は`https://<account>.services.ai.azure.com/api/projects/<project>/agents/<agent>/versions/<version>`。
planに入力と接続先を固定し、内部のモデル呼び出しにも回数上限を設けます。
使用量・費用は外側と内側の同じ処理を二重計上しません。

wrapperが実際に組み立てるCLIは、現在のMicrosoft Learnで確認した次の形式です
（実験記録を残すため、通常は上のwrapperを使用）:

```text
azd ai agent invoke "<計画内のprompt>" --agent-endpoint "<version付きendpoint>" --version "<version>"
  --protocol responses --new-session --output raw --timeout 300 --no-prompt
```

元デモにあった`--new-conversation`ではなく、現行公式資料の`--new-session`を使います。
wrapperは入力・対象を照合し、送信前の記録を保存して、
`shell=False`の1回だけのsubprocessとして実行します。wrapperによる再試行はありません。
タイムアウト・非zero終了・raw形式不一致は結果不明として再送を拒否します。
raw応答headers/stderrは保存せず、解析したbodyとusageを`invocation-receipt.json`へ保存します。
このreceiptは`trace_capture_verified: false`であり、model traceの回収成功を意味しません。

**残る具体的な障壁:** azd Foundry extensionの実インストールversionと上記flags/生HTTP出力形式、
CLI内部のHTTP retry有無、Entra認証、ResponsesHostServerとserviceの接続、planのmount、
単一replicaの永続volumeとcaptureの回収方法を、今回の新templateでまだ検証していません。
SDKモデルwriteは`max_retries=0`にしていますが、azd内部のretry設定をSDK設定で制御できるとは
主張しません。CLIがwriteを自動retryしないことを先に確認するまでこの外側invokeを実行しないでください。
Hostedサービス配置やvolume構成を自動化する完成テンプレートは提供していません。
ここが実際のクラウド収集を開始できない残課題であり、成功stubで補いません。

外側の一つのuser textをplanのhashと照合し、各会話で新しい`RetailSession`を作ります。
会話継続・履歴・tool result・runtime設定overrideを受け付けません。
**永続volume・単一replica**を追試条件としてください。共有されないローカルfilesystemで複数replicaを
動かすと共通の送信記録・回数上限を保証できません。今回のテンプレートはdistributed lockを実装していません。
各内部model POST前に送信を記録し、response captureを`*.capture.jsonl`へcreate-only保存します。
失敗時にも同じ問い合わせを自動再送しません。

### Hosted評価②へ渡す観測証跡

各会話は`sha256(conversation_id)`の先頭24桁をprefixに、次を永続先へ出力します。

| artifact | 内容 |
|---|---|
| `<prefix>-<call_index>.capture.jsonl` | 実際のmodel requestとterminal response、対象/collection-plan hash |
| `<prefix>-00001.event.json`等 | 観測のたびにcreate-only保存したattempt/model/tool start・finish |
| `<prefix>.evidence.json` | `kind: hosted-retail-evidence`, `schema_version: 1`。会話ID/category/input hash、source、runtime identity、status、elapsed seconds、events、captureファイルhash、実際の最終回答、nullable usage |

`runtime_identity`は実際のpackage version、小売code/contract hash、runtimeコードhashを含みます。
adapter向け`runtime.system_prompt_sha256`は実際のinstruction文字列のUTF-8 hash、
`tool_schema_sha256`はcanonicalなツールschema hashです。
`implementation_hashes`、`runtime_hashes`、`sdk_versions`も実際のローカルruntimeから記録し、
取得できない項目は`null`です。remote attestationではありません。
envelopeの`events`は評価adapterのcanonicalなmodel/tool start・finish、attempt_finishだけです。
attempt_startは`runtime.attempt_start`とdurable eventファイルへ分離し、raw Responses eventを混ぜません。
外部サービスのagent deployment/versionをコンテナーから照会したわけではないため
`hosted_binding: null`を保持します。version付きendpointとの結び付けは外側の
invocation-plan/receiptと運用上の配置証拠を併用し、推定で埋めません。

tool eventsは実際の`RetailSession.call`直前/直後を記録します。
`tool_start`は`tool_event_id`、`provider_call_id`、`name`、`arguments`、
`tool_finish`は同じ`call_id`と`status: completed`/`unknown`、実際の`result`または`null`です。
call IDは**まだ消費していない実際のmodel proposal**と名前/canonical引数が一意に対応した場合だけ
その観測IDを使用し、`call_id_source: unique_unconsumed_observed_proposal`を記録します。
これはframework contextからの直接取得ではなく、実際の提案と実行wrapperの一意な対応です。
0件/複数一致は`tool_blocked`/unknownとして**toolを実行する前に停止**します。
既出call IDの再利用、未消費proposalを残した次のmodel送信も止めます。曖昧なIDや実行成功を生成しません。
Pinned frameworkでの引数変換/並列tool順序を含む実際の適合性検証は残っています。
tool exception後は追加tool/model呼出を止め、副作用不明として保持します。
model eventsには`model_call_index`、localな`call_id: model-N`、実際のrequest/output/status、
OpenAI形式に変換した`message`、nullable usageを記録します。
tool call IDは実際のResponses `function_call.call_id`をassistant messageへ保持します。

evidenceの`final_answer`は観測した完了応答からのみ取得します。
`user_input`、`agent`、`runtime`、`tools`、`answer`、`latency_seconds`、
normalized `usage`もadapter向けに明示します。agent ID/versionの不明値は`null`です。
小売packageの公開APIにprivate storeのsnapshot機能はありません。
代わりに、完了した実回答がありtool例外がない場合だけ、**観測したtool returnの記録**を
`final_state`へ記録します。`terminal`は`answer_only`または`simulation_submitted`、
`submissions`は実際の`submit_resolution`返値が`処理シミュレーション完了`かつ
`external_side_effect: false`だったものだけです。
`final_state_scope: observed_tool_returns_not_store_snapshot`を区別してください。
失敗/未完了なら`final_state: null`、`final_state_available: false`を保持し、
空の成功状態に置き換えてはいけません。評価②adapterは観測したtool結果・回答・イベントだけを採点し、
足りない状態、欠落イベント、unknown usage、未確認runtime identityをunknown/人間レビュー対象にします。
このartifactを保存できたこと自体は人間確認済み業務成功ではありません。

実際に回収したcaptureファイルを（同じrunのみ）結合して取り込みます:

```powershell
$rows = Get-ChildItem runs\hosted-capture\*.capture.jsonl |
  Sort-Object Name | ForEach-Object { Get-Content $_.FullName -Encoding utf8 }
if (Test-Path runs\capture-export.jsonl) { throw "Export already exists" }
$rows | Set-Content runs\capture-export.jsonl -Encoding utf8
python scripts\collect.py --input runs\capture-export.jsonl --format responses-capture `
  --output-dir runs\captured-dataset
```

HTTPX response hookはSSEをterminal responseまでbufferしてからframeworkへ渡すため、
streamingの時間特性を変えます。これは収集skeletonであり、本番latency比較の実装ではありません。
SDK内部の`_client.event_hooks`利用、request path、tool schemaのframework変換、
Hosted入力プロトコル、永続volume mount、identity/RBAC、container起動、出口認証を
**実環境で検証するまで使えると断言しません**。
Hostedサービス自身の配置・永続volume/exportを自動化した完成コマンドは提供していません。
公式資料の現行プロトコルを確認し、認証・配置を検証するまでライブ収集はブロック扱いです。

stagingは小売code/contractとJSON/安全機構のsupport modulesのみallowlistでコピーします。
datasets/evaluation/reporting、ケース、oracle、学習data、run履歴を含めません。
`staging-manifest.json`でhashを記録します。rootをDocker build contextに使わないでください。
rootのMIT `LICENSE`も内容を変えずstageし、Docker imageへコピーして著作権/許諾表示を保持します。

## 6. モデル配置と後片付け

これは**既存Cognitive Services account配下のモデルdeployment**用です。
Hosted Agent基盤/ACR/権限/project全体を作成・削除するコマンドではありません。
生成planはランダムsuffixと所有tokenを含み、指定model/version/SKU/capacityを固定します。

```powershell
python scripts\deployment.py deploy --plan runs\deployment-plan.json --run-dir runs\deployment
python scripts\deployment.py status --plan runs\deployment-plan.json `
  --run-dir runs\deployment-status
python scripts\deployment.py cleanup --ownership runs\deployment\ownership.json `
  --run-dir runs\cleanup
```

GETが404でなければPUTしません。201 receiptのresource ID・model・SKU・所有tag・
`systemData.createdAt`が一致したときだけownership manifestを作ります。
`status`は読み取り専用で、観測記録の識別子は自動生成します。
cleanupはそのmanifestと元create journalを照合し、再GETで所有tag/model/SKU/createdAtを確認します。
ETagがなければ削除しません。DELETE成功応答も`deletion_requested_not_verified`です。
新しいstatus観測で404を確認するまで削除完了・課金停止とは表示しません。
既存teacher、job、file、account全体をcleanup対象にはしません。

PUTに`If-None-Match: *`、DELETEに`If-Match`を送りますが、
**このCognitive Services APIでの条件付きheaderのサービス側保証は未検証**です。
独立したGETとwriteには競合時間差があります。現行REST schemaはcreate-or-updateであり、
厳密なatomic create-only/delete保証が必要な環境では、条件付き要求をサービスが保証することを
先に確認するまでlive操作を行わないでください。ランダム名/所有tag/ETagだけでその保証を主張しません。
既存resource変更の可能性を許容できない共有環境では、この点が追試の停止条件です。
作成応答が201でない/identity不足/結果不明ならmanifestを作らず、GETだけで調査します。
未証明resourceを無理に「所有」と見なす自動回復はありません。

## 7. 公式資料と照合範囲

2026-09-19にMicrosoft Learnツールで検索し、以下の公式資料のAPI形状を確認しました。
Azure MCP best-practices discoveryはtimeoutとなったため、クラウド接続の代替として再試行はしていません。

- [Foundry fine-tuning](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/fine-tuning):
  local `files.create(purpose="fine-tune")`、`fine_tuning.jobs.create`、
  `method.supervised.hyperparameters`、`extra_body.trainingType`、`jobs.retrieve`、UTF-8 BOM/512MB/10例。
  同ページには異なる世代のSDK/Preview例が混在します。APIキー例は採用していません。
- [Azure AI Projects client library](https://learn.microsoft.com/python/api/overview/azure/ai-projects-readme):
  `AIProjectClient` + `DefaultAzureCredential` + `get_openai_client`。
- [Deployments create or update (2024-10-01)](https://learn.microsoft.com/rest/api/aiservices/accountmanagement/deployments/create-or-update?view=rest-aiservices-accountmanagement-2024-10-01):
  resource path、model/sku/tags、200/201。条件付きheader保証をこの資料から捏造していません。
- [Hosted Agents](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry):
  サービスの現行提供状況とPreview制約は再確認が必要です。
- [Invoke a hosted agent](https://learn.microsoft.com/azure/foundry/agents/how-to/invoke-hosted-agent) /
  [Use azd ai with coding agents and scripts](https://learn.microsoft.com/azure/foundry/agents/how-to/use-cli-with-coding-agents):
  version付き`--agent-endpoint`、`--version`、`--protocol responses`、`--new-session`、
  `--output raw`、`--timeout`、`--no-prompt`を確認。azd自体は今回起動していません。

再利用した仕組みは元デモの`fixtures/train_student.py` / `deploy_student.py` /
`push_prompts.py`、`agent/src/zava-traces-demo/main.py` / `tools.py`、
`e2e-agent/src/main.py` / `runtime.py` / `requirements.txt`に由来します。
固定subscription/tenant/endpoint、過去run、deadline、再送例外、評価oracleは持ち込んでいません。
