# 評価①・評価②の入出力契約

本実装は日本語小売教材用の小さな評価器です。SDK、資格情報、ネットワークがない環境で保存済み証跡を採点できます。**API が成功したこと、自動チェックに通ったこと、業務が成功したことは別です。**

## 実行入口（Windows / PowerShell）

リポジトリのルートで実行します。依存は Python 標準ライブラリだけです。

```powershell
python scripts\evaluate.py --help
python scripts\evaluate.py --mode next-action --input data\samples\evaluation-next-action.json --output runs\next-action-demo.json
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-e2e.json --output runs\e2e-demo.json
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -p "test_evaluation*.py" -v
```

出力は新規作成のみです。同名ファイルがあれば停止します。成功時の終了コード `0` は「レポートを書いた」という意味で、品質合格ではありません。入力不正・上書き拒否などは `2` です。サンプルは `synthetic_illustration_not_measurement` と明示した架空の証跡で、実測・旧実験の再利用ではありません。`e2e` サンプルには証拠不足の base と未試行の fine_tuned を意図的に含めています。

### 第5章の整形データとの接続

```powershell
python scripts\prepare_data.py --input data\samples\traces.jsonl --output runs\data --seed 42
python scripts\evaluate.py --mode next-action --prepare-next-actions --input runs\data\next-actions.jsonl --output runs\next-action-input.json
```

`--prepare-next-actions` は development の JSONL を評価用 bundle に変換する**オフライン準備**です。`kind` / `ground_truth` を `expected.kind` / `expected.calls` に対応させ、元の reference・metadata・case tools を残します。元ファイルの SHA-256 と `quality_review_approved:false` を記録し、`records:[]` にします。予測・成績・teacher 履歴は生成しません。live フラグとは併用できません。

`data\samples\traces.jsonl` は20件の SCRIPTED な架空の配送問い合わせで、teacher 生成・性能実績ではありません。60/15/15/残りのグループ分割から development のみを評価①の準備に使い、final は調整に使いません。変換直後の bundle を通常採点すれば、全モデルが未試行と表示されるのが正しい挙動です。モデル結果を実際に収集してから別の新規 bundle に記録してください。有料実行には review、inference 設定、モデル別入力・承認を別途用意します。単一カテゴリのサンプルだけで業務カバレッジを満たしたとは判断できません。

## 共通 JSON bundle

| 項目 | 契約 |
|---|---|
| `models` | 一意なラベルの配列。省略時は `teacher`, `base`, `fine_tuned`。モデル数・ケース数の固定はない |
| `tools` | OpenAI function schema の配列。ケースの `tools` で明示的に上書きできる |
| `cases` | 一意で空でない `case_id` を持つケース配列 |
| `records` | 保存済み結果配列。`case_id` × `model` ごとに最大1件 |
| `evidence_kind` | 架空例・新規実測などの出所。未指定なら `unspecified` |

全 `cases × models` が分母です。欠けた結果も `technical_failure` / `missing_record` として残し、試行済みの良い結果だけを分母にしません。別々に実行したモデルを比較する場合は、同一のケース・ツール・条件の bundle に保存済み records を集めて再採点してください。重複レコードや予定外のケース・モデルは入力エラーです。条件混在を自動的に同等と判断しません。

JSON の重複キー、NaN、Infinity、オーバーフロー数値は拒否します。採点用 schema は標準の JSON Schema 実装全体ではなく、業務ツール用の閉じた部分集合です:

- `type`（単一の object/array/string/integer/number/boolean/null）、`properties`、`required`
- `items`、`minItems`、`maxItems`、`uniqueItems`
- `enum`、`const`、`minimum`、`maximum`、`minLength`、`maxLength`、`pattern`
- 説明用 `description`、`title`、`$schema`、`default`、`examples`

`$ref` / `oneOf` 等の未対応キーワードは黙って無視せず契約エラーです。object は `additionalProperties` の省略時も**厳格に未知キーを拒否**します。`additionalProperties: true` は扱いません。bool を整数として受け入れません。スキーマの追加機能が必要なら評価器・テスト・評価契約を一緒に更新します。

## 評価①: 次の行動

ケースは `messages` と `expected: {"kind":"tool"|"text", "calls":[...]}` を持ちます。正解 call は `{"name":"...", "arguments":{...}}`。実行時の `messages` は既存履歴そのものを使い、ツールは実行しません。

保存済み record は `case_id`, `model`, `status:"completed"`, `message`, `usage`, `latency_seconds` を持ちます。message は `content` と `tool_calls` を持つ OpenAI 型で、tool call は function 内の名前と JSON 文字列 arguments、または上記の正規化形を受け付けます。

- `strict_tool_match`: ツール名・引数の完全一致。引数オブジェクトのキー順は不問。並列 call の順番は不問だが重複回数は保持。
- `schema_valid`: 既知のツール名か、必須項目・型・列挙値・未知キーなどに違反していないか。
- `predicted_call_count`: 出力された call 数。壊れた message は件数も未知。
- テキストは存在だけを確認し、意味・日本語の正確さ・適切な確認質問かどうかは人間レビューへ分離。

寛容な「名前が同じなら8点で合格」の採点はありません。部分点から厳密一致を推測しません。ツール引数の壊れた JSON は品質失敗、取得した message 自体がない場合は技術的失敗です。評価①の人間確認が済んでも、**業務全体の成功は未測定**なので `confirmed_business_success` は false のままです。

## 評価②: 保存済みイベント

ケースは `user_input` と、次の明示的な `expected` を持ちます:

```json
{
  "initial_tools": ["get_order_details", "get_fulfillment_status"],
  "required_calls": [
    {"name": "get_order_details", "arguments": {"order_id": "ORD-DEMO-001"},
     "result": {"order_id": "ORD-DEMO-001", "items": []}}
  ],
  "allowed_mutations": [],
  "final_state": {"terminal": "answer_only", "submissions": []}
}
```

これは構造例であり、実際の業務結果の正解ではありません。必要な配送・ポリシー・在庫・計算結果は `required_calls` に独立した期待値として列挙します。`result` を指定した required call は結果も完全一致が必要です。省略すると名前・引数のみの検査となり、結果の業務的正しさは未検査です。teacher の全軌跡との一致は要求しません。

`allowed_mutations` は許可する `submit_resolution` の **名前・引数・result の一致ルール**です。既定は完全一致。必要な送信は `required_calls` にも指定します。計算IDなどは架空業務の決定的な値として期待値へ明示し、実行した結果をそのまま正解へ書き戻しません。`submit_resolution` 以外の状態変更ツールを追加する場合は、`MUTATION_TOOLS` と契約・テストも変更が必要です。

teacher の説明文の丸暗記を要求しないため、評価②に限り required call / allowed mutation に `match:"subset"` を明示できます。名前は厳密一致、arguments/result の指定したオブジェクト項目だけ再帰的に比較します。配列は順序と長さを維持し、未指定の自由文は人間レビューへ残します。subset での許可送信には少なくとも arguments の非空 `order_id` / `calculation_id` と、result 内の同一ID、`status:"処理シミュレーション完了"`, `external_side_effect:false` が必須です。単に空オブジェクトで全操作を許可することはできません。元のモデル出力引数は省略なしの完全なツール schema で検証します。

`final_state_match:"subset"` も明示的に選べます。terminal と送信件数・順番は維持し、各送信結果の業務フィールドを独立して期待値へ書きます。自由な `resolution_summary` やそれから作られる受付IDを期待値から外せます。**未指定フィールドは自動検査していません**。ポリシー・計算・必要な実行内容を required calls の結果で別途確認し、説明文は人間が確認してください。評価①には subset 一致はありません。

`final_state` は内部ストア全体の snapshot ではなく、成功したツール終了イベントから導いた
`{"terminal":"answer_only"|"simulation_submitted", "submissions":[送信結果...]}` です。保存済みの自己申告とイベント由来の状態が異なる場合は証拠不整合になります。実システムの完了・永続化を保証するフィールドではありません。

record は `answer`, `events`, `final_state` を持ちます。イベント仕様:

| `event` | 必須の主な内容 |
|---|---|
| `model_start` | 一意な文字列 `call_id` |
| `model_finish` | 同じ `call_id`, `status`, 実際の `message`, `usage` |
| `tool_start` | model が提案した一意な `call_id`, `name`, `arguments` |
| `tool_finish` | 同じ `call_id`, `status`, `result`（不明な失敗なら `error_type`） |
| `tool_blocked` | 実行前に防いだ call の `call_id`, `reason` |
| `attempt_finish` | 末尾に1回だけ `status` |

call の重複、start/finish の欠落、モデル提案と実行の不一致、必須の初期順序違反、期待結果の不一致、未許可送信、最終状態・最終回答とイベントの不整合を検査します。成功そうな回答だけ、`status:"completed"` だけ、空イベントだけでは合格になりません。

**防げた不正要求と実行された不正操作を別に数えます。** `blocked_invalid_calls` と `executed_unauthorized_actions` は異なる配列です。実行前の不明ツール・schema 違反は blocked。実行中の例外は「副作用がなかった」と推測せず unknown として停止します。ツール自身が構造化エラーと `external_side_effect:false` を返した場合だけ blocked と記録します。正常な模擬送信でも `external_side_effect:false` が必要です。防げた不正要求も品質上の問題であり、無条件合格にはしません。

## 判定と人間レビュー

優先順に `technical_failure` → `quality_failure` → `review_pending` → `confirmed_success` と判定します。最後の状態には自動チェック合格に加え、明示的な人間の確認が必要です。

record に次の `review` を追加して再採点します:

```json
{
  "decision": "confirmed_success",
  "reviewer": "reviewer-alias",
  "reviewed_at": "2026-09-19T00:00:00Z",
  "notes": "根拠・金額・説明・確認質問・実行範囲を人間が確認した記録",
  "evidence_sha256": "64桁の証跡hash"
}
```

hash は `foundry_distillation_lab.evaluation.evidence_sha256(record)` で計算します。`review` だけを除いた canonical JSON の SHA-256 です。証跡が変わると確認は無効になります。`decision:"quality_failure"` も記録できます。人間の確認は技術的失敗・自動検査失敗を上書きできません。レビュー欄は署名や認証基盤ではなく、誰が判断したかのローカル記録です。プログラムで成功を捏造する代替手段として使わないでください。

`confirmed_business_success` が true になるのは評価②で上記を満たした場合だけです。日本語説明、架空の確認ID、現実に返金したかのような主張、不要な確認質問、拒否・確認の妥当性などは人間が確認します。

## 使用量・時間・分母

- usage の正規形は `input_tokens`, `output_tokens`, `cached_input_tokens`。cache は input の内数。
- 未取得・不正な数値・不完全な集計は null。未知を 0 にしません。
- 評価②は各 `model_finish.usage` を集計します。record の自己申告合計だけは信用しません。
- `usage_total` は全スロットで既知の項目だけ合計可能です。`usage_known_subtotal` と `usage_reported_n` は既知の小計・標本数であり、全費用の推定ではありません。
- ローカル runner の `latency_seconds` はその runner 全体の壁時計経過時間です。Hosted import では capture に実際に記録された時間だけを保持し、未取得なら null。`origin` / `provenance.runtime` で出所と計測範囲を確認します。モデル単体時間、Hosted 基盤の起動時間、ローカル loop 時間を同じ指標とは扱わず、混在条件で速度同等性を主張しません。
- `overall`, `per_model`, `per_case` に分母、状態件数、確認済み成功率、usage、時間を出します。
- 各 row の `case_sha256` と `tool_contract_sha256` は比較するケースとツール契約の同一性確認用です。`evidence_sha256` は観測 record の hash でモデルごとに異なり、欠測では null。`review` には元の人間レビューを保持し、その hash と一致する観測だけを確認済みと扱います。`source_review` は import 前の履歴なので、単独では確認済み成功の根拠になりません。`category` はケースに指定された場合だけ出力します。
- `per_model` 等の `runtime_provenance` は row の観測元から `local_direct_model` / `hosted_capture` / `synthetic` / `unknown` / `mixed` を分類し、件数を併記します。設定ファイルから実行環境を推測しません。欠測は unknown、既知と未知が混在すれば mixed。これは capture metadata に基づく分類で、クラウドの真正性証明ではありません。費用への接続では root と row 両方の evidence kind と、runtime の混在も確認してください。
- 時間の母集団は「失敗も含む、時間を報告した record」。`n`, `median`, `p95`、`p95_method:"nearest_rank"` を明示。未観測時間は除外され、n に現れます。小標本の p95 は一般化しません。

## 注入可能なローカル runner

```python
from foundry_distillation_lab.evaluation import run_case
from foundry_distillation_lab.retail import RetailSession

record = run_case(case, "teacher", invoke_model, RetailSession,
                  max_model_calls=12, max_tool_calls=24)
```

`invoke_model(payload)` は `{"message": OpenAI形式message, "usage": 正規形usage}` を返します。payload は `attempt_id`, `model_label`, `messages`, `tools` のみ。期待値・oracle・レビューはモデルへ渡しません。注入先はテスト用 callable にでき、クラウド資格情報を必要としません。インジェクトした callable のネットワーク安全性はその実装の責任です。

`RetailSession()` は呼び出すたびに新規作成し、各ケース・各モデル間で状態を共有しません。session には `call(name, arguments) -> dict`, `tools`, `system_prompt` が必要です。tool batch 全体を検証してから順に実行します。返答完了、上限、schema 違反、不明ツール、重複 call ID、モデル/ツールの不明な結果で停止し、自動再試行しません。顧客との反復対話・ユーザーシミュレーターはありません。

## Hosted Agent 証跡を評価②に接続する（完全オフライン）

Hosted Agent 内で**実際に観測した**モデル呼び出しとツール実行は、保存してローカルへ取得した後で採点します。この adapter は Hosted endpoint を呼び出しません。ローカル runner のイベントを Hosted の実績に付け替える機能でもありません。

公開サンプルは、形式検証だけを目的とする明示的な架空証跡です:

```powershell
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-hosted-cases.json --import-hosted data\samples\evaluation-hosted-capture.json --model-label teacher --output runs\hosted-import-demo.json
python scripts\evaluate.py --mode e2e --input runs\hosted-import-demo.json --output runs\hosted-import-demo-scores.json
```

どちらの入力も `synthetic_illustration_not_measurement` です。実際の Hosted Agent を実行した証拠ではありません。import の出力は採点用 bundle、次のコマンドの出力が report です。JSON の単一 object / object 配列、または JSONL を受け付けます。上書き・重複するケース×モデルを拒否し、`--send` 等の live フラグとは併用できません。

### 期待する identity と観測記録

case bundle の `hosted_targets` に、評価ラベルごとの `agent_name`, `model`, `endpoint` を明示します。既知なら `agent_id`, `agent_version` も指定します。capture の値との完全一致を確認し、未知の version を指定した version の実績にしません。これはローカルに保存した metadata の整合確認であり、署名検証・リモートの真正性検証ではありません。クラウドで得た実 ID や endpoint を公開サンプルへ転記しないでください。

1つの capture は次の形です:

| フィールド | 実行環境が観測・記録する内容 |
|---|---|
| `kind`, `schema_version` | `hosted-retail-evidence`, `1` |
| `conversation_id` | case の `conversation_id`、省略時の `case_id` と対応 |
| `user_input`, `input_sha256` | 実際の顧客入力と UTF-8 SHA-256。case.user_input と両方照合 |
| `agent` | `name`、分かる場合だけ `id` / `version`。未知は null |
| `runtime` | `kind:"foundry_hosted_agent"`, 実際の `model` / `endpoint`。SDK version 等は既知の値だけ |
| `tools` | 実行した RetailSession の OpenAI chat function schema |
| `status` | 会話実行の実際の状態。モデル HTTP の成功と同一視しない |
| `events` | 本契約の model/tool start/finish と attempt_finish。tool の引数・結果・call_id は実際の実行観測 |
| `answer` | 観測した最終回答。未取得なら null |
| `final_state` | 実行した模擬送信の結果から runtime が観測した状態。未取得なら null |
| `usage`, `latency_seconds` | 実測がある場合だけ。欠落は null、推定の0や擬似時間を入れない |

adapter は `events` / `answer` / `final_state` を再生・補完しません。新しい envelope でも、欠けていればそのまま technical failure の証跡として残ります。case が要求しないテキスト確認のケースでも、モデルの start/finish と末尾の attempt_finish は必要です。model_finish.usage が欠ければ、自己申告の合計 usage が存在しても使用量の完全な観測とは扱いません。

現在の Hosted template の `Capture` はこの canonical `hosted-retail-evidence` を `<conversation-hash>.evidence.json` に出力します。実際の wrapper の tool_start / tool_finish を個別ファイルにも新規保存し、観測した模擬送信結果だけから `final_state_scope:"observed_tool_returns_not_store_snapshot"` の状態を記録します。完全性が不明なら null にします。`Capture.request/response/call_tool/finish` と実際の RetailSession を用いたオフライン round-trip テストで、出力ファイル→adapter→採点の接続を確認済みです。ただし現在の tool-call 対応づけは下記の SDK context 未検証のため、この実 emitter のツール使用例は `unverified_framework_tool_call_linkage` による technical failure として保持されます。テストの HTTP 応答も合成 fixture であり、SDK/server/container やクラウド実行を確認したものではありません。

モデル HTTP のみを記録する既存の **`foundry-responses-capture` は評価②の実行証跡として未対応**で、明示的なエラーにします。request の履歴に tool result があっても、実際の tool_start / finish、例外・副作用、agent identity、観測した最終状態を捏造して補えないためです。この旧形式は学習データの import には使用できますが、業務実行証拠とは別です。

### Hosted template 固有の native envelope

実行環境が `kind:"hosted-conversation-evidence", schema_version:1` を出す場合も、同じ `--import-hosted` で受け付けます。上記の canonical envelope に対する**観測済みフィールドの変換だけ**を行います:

- `runtime_identity.framework_agent_name` → agent.name。package_versions / retail_hashes / hosted_binding 等は `provenance.runtime.native_identity` にそのまま保存。operator 設定の binding を観測済み agent ID / version に格上げしません。
- model / endpoint は明示された runtime identity、または実際に記録された `source.target` の `endpoint|model` から読み、両方ある場合は矛盾を拒否します。`user_input`, `input_sha256`, `tools` は明示的な観測値が必要です。
- model の `model_call_index` をローカルの対応IDへ正規化。`model_finish.output` の実際の Responses message / function_call を message に変換し、Responses の usage を正規形へ変換します。未知 usage は null のままです。
- tool の明示的な `call_id` だけを対応IDとして使用し、`provider_call_id` は診断用として保持します。診断値・`tool_event_id` から実際の framework call ID を作りません。false の検証フラグなら対応IDは unknown にし、元の値も `reported_call_id` に保持します。不完全な対応は technical failure です。
- tool_finish の `returned` は実際の result、`exception_unknown` は不明な終了として処理。`completed_unreviewed` は自動チェックに進める完了状態であり、業務成功ではありません。
- `final_answer` と `elapsed_seconds` を観測値として保持。`final_state_available` が true でない限り、final_state は null のままです。false と非null state の矛盾は拒否します。

`source`、元 envelope の hash、`capture_files` のリンク/hash、native の known_usage を保持します。adapter は参照先の capture ファイルを読み直して不足イベントを補完しません。リンク/hash を保持したことと、その全参照先を検証したことは別です。buffered HTTP の計測時間を通常の streaming 経路の時間と比較可能だとは扱いません。

Hosted template が使用する SDK の tool middleware で同じ call_id を実行と対応づけられない場合は、collector が不完全/unknown と記録する必要があります。名前・引数だけから曖昧な call を成功扱いにしません。grader・case・oracle は Hosted payload に含めず、期待値は import 時にオフラインで結合します。

現在の wrapper は、実際に観測した未消費のモデル提案のうち名前と canonical 引数が一意に一致するものを診断用に対応づけます。これは **SDK の tool context から直接取得した call ID の検証ではありません**。イベントの `call_id_source` / `framework_context_call_id_available` / `framework_call_id_verified` と provenance の `tool_call_linkage` をそのまま保持します。Hosted import の tool イベントでは直接の framework identity が明示的に検証済みであることが必要です。false・未記録・診断用 proposal matching のままなら、値が一見一致していても authoritative な join に使わず `unverified_framework_tool_call_linkage` として technical failure にします。人間レビューでこの失敗を上書きできません。実際の SDK context からIDを取得・検証する実装とテストができるまで、ツール使用ケースの Hosted 業務成功判定・採用は保留です。

### 出所とレビューを失わない

imported record の `provenance` に runtime、agent、入力 identity、capture ファイル・各行の SHA-256、capture 内位置、元の evidence kind を保持します。report の rows にも provenance が残ります。未取得の resource ID / version は既知のふりをしません。合流前の bundle に存在する別モデルの records と reviews は変更しません。

capture に `review` がある場合、その内容は `source_review` にそのまま保持します。import により envelope の hash が変わるため、その review を自動で新しい record に付け替えません。新 record の hash に対して人間が確認し直すまで review_pending です。これにより古い確認を使って別の runtime の成果を confirmed_success にすることを防ぎます。

## 評価②から第8章の費用入力へ接続する

採点 report の集計を手で転記する代わりに、reporting の準備 adapter を使います:

```powershell
python scripts\report.py --prepare-evaluation --input runs\e2e-demo.json --config configs\examples\cost-evaluation.json --output runs\evaluation-cost-input.json
python scripts\report.py --input runs\evaluation-cost-input.json --output runs\evaluation-cost-report
```

最初の入力は本ページ冒頭の評価②デモ report です。`teacher` / `base` / `fine_tuned` の3者をまとめた report が必要です。モデル別の実 run では先に同じケース・条件の records をまとめて評価②を再採点し、その report を渡します。価格・通貨・本番業務量・稼働条件等は config に明示し、評価データから勝手に推定しません。

準備 adapter は source report/config の SHA-256、ケースの同一性・重複回数、usage の合計/既知小計/観測数、確認済み結果、row の runtime provenance を保持します。runtime の根拠は `row.provenance.runtime.kind` であり、価格 config から実行環境を補いません。同一 cohort を確認できない、使用量や runtime が不明、レビュー未完了、模擬例しかない等の場合は **hold（判断保留）** にします。未知を無料や成功扱いにしません。

このデモは合成証跡を使うため、bridge や SVG が生成できても実運用での採用根拠にはなりません。この費用 adapter は `mode:e2e` 専用で、評価① (`next-action`) の report は入力段階で拒否します。費用 adapter の詳細と本番条件の記入は第8章および reporting の reference を参照してください。

## 任意の live 境界: Hosted Agent ではない

`--send --approval <path> --run-dir <new-path>` を加えた場合だけ live 分岐を使います。

1. `configs\examples\evaluation-live.json` は**未完成の計画テンプレート**。cases/tools/費用上限を埋めない限り検証失敗します。
2. 同一入力 bundle に targets、endpoint、token 上限、timeout、loop 上限、`reserve_per_request` を固定します。後者は入力・出力・最大履歴を含む保守的な1要求あたり費用上限で、approval の通貨と一致させます。自動で現行価格を調べません。
3. 承認の operation は `eval-next-action` または `eval-e2e`。入力ファイル全体の hash、対象、期限、リクエスト数、費用上限を `Approval.load` で確認します。live 入力はケース・messages・tools・endpoint・deployment を埋め込んだ自己完結 bundle で、外部データセットを実行時に参照しません。準備元の hash は出所情報であり、元ファイルを読み直して実行する指定ではありません。最初に読み込んだ bytes の hash と承認値も照合し、読み込み後のファイル差し替えで異なる payload を承認することを防ぎます。
4. 現在の `Approval` は**単一 target**を承認するため、live では models/targets を1モデルに絞った入力を作り、モデルごとに別途承認してください。3モデルの比較はオフライン集約で行います。
5. 評価②は実際の RetailSession と完全一致する tools と、各ケースの `system_prompt` を入力に含めます。サンプルの縮小 schema をそのまま live 実行することはできません。
6. 最悪時の要求数・予約総額が承認内に入るか事前確認します。毎要求の直前にも `assert_target` / `reserve` を呼びます。次に `Journal.start` を排他的に永続化してから送信します。start が残った attempt は finish がなくても再送禁止です。
7. `Journal.finish` に受信結果または unknown を記録。CLI は不明/エラー/上限/blocked のケースで run 全体を停止し、残りは未試行にします。新しい run ディレクトリを使って不明な操作を自動再開する機能はありません。
8. OpenAI SDK / Azure Identity は送信時だけ import。DefaultAzureCredential の token provider、`max_retries=0`、明示 timeout、Chat Completions の `/openai/v1/` endpoint を使います。API キーを取得・保存しません。

既存 output/run-dir は拒否します。失敗で空 output が残ることもありますが、削除して自動再送せず、run 内の `input.json`, `record-*.json`, `evidence.json`, `attempts` を確認してください。学習データや prompt に機微情報があれば、これらの証跡も公開しないでください。

**この live 経路はローカルで tool を実行し、モデルに直接 API を呼ぶ比較です。Hosted Agent の transport・基盤費・起動時間・監視・分散状態を観測したものではありません。** Hosted Agent のテンプレートは別の実行経路であり、同じ性能条件と表現しません。期限は新規要求の admission を止めるだけで、送信中の要求やクラウド課金の停止を保証しません。

## 検証範囲と限界

標準ライブラリ unittest により、厳密な引数/schema 検査、欠けた証跡、使用量 unknown、順序・最終状態、レビューhash、分母、注入モデルでの会話分離、呼び出し上限、再送禁止、CLI の上書き拒否を検証します。実際の RetailSession / Approval / Journal と Hosted Capture クラスとのローカル結合もテストし、SDK / HTTP 境界は mock で確認します。サンプルだけでは返品・交換・境界条件のカバレッジを満たしません。ケース数を増やす前に独立した期待値とカテゴリ・境界の実験計画を追加してください。

クラウド認証・課金・モデル利用可否・SDKの実サービス互換・Hosted Agent での通し実行・品質同等性・採用成功はこの実装作業では**未検証**です。価格・費用の計算と採用判断は別章で行います。旧実験の cloud ID、固定のケース数、過去 run、5モデル前提には依存しません。
