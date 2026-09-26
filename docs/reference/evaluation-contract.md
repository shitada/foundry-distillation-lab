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

`data\samples\traces.jsonl` は20件の SCRIPTED な架空の配送問い合わせで、teacher 生成・性能実績ではありません。60/15/15/残りのグループ分割から development のみを評価①の準備に使い、final は調整に使いません。変換直後の bundle を通常採点すれば、全モデルが未試行と表示されるのが正しい挙動です。実測には、共通の評価設定と対象モデルを指定する`run`を使います。単一カテゴリのサンプルだけで業務カバレッジを満たしたとは判断できません。

## 共通 JSON bundle

| 項目 | 契約 |
|---|---|
| `models` | 一意なラベルの配列。省略時は `teacher`, `base`, `fine_tuned`。モデル数・ケース数の固定はない |
| `tools` | OpenAI function schema の配列。ケースの `tools` で明示的に上書きできる |
| `cases` | 一意で空でない `case_id` を持つケース配列 |
| `records` | 保存済み結果配列。`case_id` × `model` ごとに最大1件 |
| `evidence_kind` | 架空例・新規実測などの出所。未指定なら `unspecified` |

全 `cases × models` が分母です。欠けた結果も `technical_failure` / `missing_record` として残し、試行済みの良い結果だけを分母にしません。`run`は指定モデルの全ケースを対象にし、別々のrunは`compare`で比較します。共通の問題・ツール・設定を自動照合するため、recordsの転記は不要です。重複レコードや予定外のケース・モデルは入力エラーです。

JSON の重複キー、NaN、Infinity、オーバーフロー数値は拒否します。採点用 schema は標準の JSON Schema 実装全体ではなく、業務ツール用の閉じた部分集合です:

- `type`（単一の object/array/string/integer/number/boolean/null）、`properties`、`required`
- `items`、`minItems`、`maxItems`、`uniqueItems`
- `enum`、`const`、`minimum`、`maximum`、`minLength`、`maxLength`、`pattern`
- 説明用 `description`、`title`、`$schema`、`default`、`examples`

`$ref` / `oneOf` 等の未対応キーワードは黙って無視せず契約エラーです。object は `additionalProperties` の省略時も**厳格に未知キーを拒否**します。`additionalProperties: true` は扱いません。bool を整数として受け入れません。スキーマの追加機能が必要なら評価器・テスト・評価契約を一緒に更新します。

## 評価①: 次の行動

### 第6章の標準設定

[第6章の実践手順](../how-to/06-training-and-evaluation.md)では、共通の設定ファイルに接続先・配置名・生成条件を記入します。問題ファイルは学習前後で共用します。

### 実測結果のラベル

`evidence_kind`は、保存する結果の種類を示す記録用ラベルです。モデルへの指示や学習パラメーターではありません。

実行コードがラベルを設定するため、利用者の編集は不要です。実測ラベルはモデルを呼び出したことを示すだけで、問題の代表性や品質合格を意味しません。同梱サンプルにモデルが答えた場合も、サンプル由来という出所情報を保持します。

### 費用の見積りと実測

評価①は1問につき1回のモデル呼び出しです。対象モデル・地域・配置方式の単価、入力トークン数の見込み、出力上限から費用を見積もります。学習・配置の保持費は別に確認します。料金は[Azure OpenAI料金表](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)で実行時に確認してください。

実行後はAPIの`usage`と請求を確認します。見積りは実費ではなく、使用量や請求が未取得なら不明のまま残します。比較条件の問題・ツール・生成設定を費用に合わせて途中で変えません。

### 入出力と採点

ケースは `messages` と `expected: {"kind":"tool"|"text", "calls":[...]}` を持ちます。正解 call は `{"name":"...", "arguments":{...}}`。実行時の `messages` は既存履歴そのものを使い、ツールは実行しません。

保存済み record は `case_id`, `model`, `status:"completed"`, `message`, `usage`, `latency_seconds` を持ちます。message は `content` と `tool_calls` を持つ OpenAI 型で、tool call は function 内の名前と JSON 文字列 arguments、または上記の正規化形を受け付けます。

- `strict_tool_match`: ツール名・引数の完全一致。引数オブジェクトのキー順は不問。並列 call の順番は不問だが重複回数は保持。
- `schema_valid`: 既知のツール名か、必須項目・型・列挙値・未知キーなどに違反していないか。
- `predicted_call_count`: 出力された call 数。壊れた message は件数も未知。
- ルール検査はテキストの存在を確認します。意味・日本語の正確さ・適切な確認質問かどうかは、`grade`による採点用モデルの評価へ分離します。人による確認は別に記録できます。

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

teacher の説明文の丸暗記を要求しないため、評価②に限り required call / allowed mutation に `match:"subset"` を明示できます。名前は厳密一致、arguments/result の指定したオブジェクト項目だけ再帰的に比較します。配列は順序と長さを維持し、未指定の自由文は採点用モデルによる内容の評価へ残します。subset での許可送信には少なくとも arguments の非空 `order_id` / `calculation_id` と、result 内の同一ID、`status:"処理シミュレーション完了"`, `external_side_effect:false` が必須です。単に空オブジェクトで全操作を許可することはできません。元のモデル出力引数は省略なしの完全なツール schema で検証します。

`final_state_match:"subset"` も明示的に選べます。terminal と送信件数・順番は維持し、各送信結果の業務フィールドを独立して期待値へ書きます。自由な `resolution_summary` やそれから作られる受付IDを期待値から外せます。**未指定フィールドはルール検査していません**。ポリシー・計算・必要な実行内容を required calls の結果で別途確認し、説明文は`grade`で内容を評価します。評価①には subset 一致はありません。

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

### 標準の自動採点

標準の流れは`run`で応答を保存し、`grade`で内容を自動採点し、`compare`で読むための比較表を作る順です。CSVへの判定入力は不要です。`grade`は未採点・人による確認前のrunを入力にし、新しい保存先を作ります。すでに`review`または`model_review`があるrunは受け付けません。

採点用モデルの設定は、`configs\examples\grading.json`から作る別ファイルです。受け付ける項目は`base_url`、`deployment`、`max_completion_tokens`、`timeout_seconds`の4項目だけです。接続先はHTTPSのOpenAI v1エンドポイント、`deployment`は構造化出力に対応したChat Completionsモデルの配置名です。例として同じFoundryリソースの`gpt-4.1`を`grader`という名前で配置し、出力上限を`1024`、通信待機上限を`60`秒にします。モデル・版・地域の対応は実行環境で確認します。準備は[実践手順6.1](../how-to/06-training-and-evaluation.md#61-採点用モデルと設定を準備する)に記載しています。

次の2コマンドは、学習前後の保存済み応答を同じ設定で採点し、別々の出力先に保存します。評価対象の生徒モデルは呼び直しません。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\base-before --config runs\grading-config.json --output-dir runs\base-graded
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\fine-tuned-after --config runs\grading-config.json --output-dir runs\fine-tuned-graded
```

次のコマンドは、採点済みの2つのrunをオフラインで比較します。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py compare --before runs\base-graded --after runs\fine-tuned-graded --output-dir runs\comparison
```

採点用モデルには、問題の履歴・業務ルール・ツール仕様・期待値・参照例と、評価対象の応答を渡します。評価②では観測したツールイベントと最終状態も渡します。評価対象モデルの識別情報、配置名、学習前後のラベル、既存の判定、使用量・時間は採点入力に含めません。問題や応答の中にある採点器への指示には従わず、同じ固定の採点基準`retail-blinded-business-quality-v1`を使います。採点用モデルはツールを実行しません。

構造化出力は、`decision`と空でない`reason`だけを持つJSONです。採点基準では、`reason`に根拠や不確実性を簡潔な日本語で示すよう指示します。`decision`は`success`、`failure`、`needs_review`のいずれかです。通信エラー、不正・不完全なJSON、拒否、途中で切れた応答などは`unknown`として保存し、自動再試行しません。未試行も含め予定した分母を維持します。

ルール検査で技術的失敗または品質不合格になった行は、採点用モデルを呼ばずに結果を保持します。内容の自動判定でルール検査の失敗を上書きしません。

| 行の項目 | 意味 |
|---|---|
| `status` | 従来の状態。`technical_failure`、`quality_failure`、`review_pending`、`confirmed_success`。自動判定だけで`confirmed_success`にしない |
| `automatic_decision` | 自動比較用の`success`、`failure`、`needs_review`、`unknown`。技術的失敗は`unknown`、ルール上の品質不合格は`failure` |
| `assessment_source` | `deterministic`（ルール検査）、`model`（採点用モデル）、`invalid_model_review`（無効な自動採点記録）、`human`（人による確認のみ）、`unreviewed`（未採点・未確認） |
| `model_review` | 人の`review`とは独立した採点記録。`source:"model"`、`state`、`decision`、`reason`、元の応答、採点条件と証跡との対応を保持 |
| `confirmed_business_success` | 人が確認した評価②の業務成功だけがtrue。自動採点の合格ではfalse |

`model_review.state`は`received`（採点応答を受理）、`unknown`（採点エラー）、`skipped`（ルール検査の失敗で採点呼び出しを省略）です。採点条件`protocol`には採点基準の版、基準・応答形式の識別値、設定を保持し、証跡・ケース・ツールと対応を検証します。`compare`と`combine`は異なる採点条件や、採点済みと未採点のrunの混在を拒否します。自動採点は推定であり、人による確認や業務成功の確定を代替しません。

同じ設定でも、採点応答に含まれる空でないモデル識別子がrun内またはrun間で異なる場合、`compare`と`combine`は`comparison_grader_response_model_mismatch`で停止します。`grading.json`、`execution.json`の採点概要、`scores.json`の`grading`には、観測した識別子の一覧`response_models`と、識別子を取得できなかった呼び出し数`response_model_unknown_calls`を保持します。`comparison.json`では両側をまとめた`grader_response_identity`に記録します。取得できなかった識別子は不明のままとし、配置名から補ったり、クラウドへ照会して確認したものとして扱ったりしません。

| 採点先に保存するファイル | 内容 |
|---|---|
| `source-evidence.json` | 採点前の観測記録 |
| `grading-config.json`、`grading-rubric.json` | 採点設定、固定の採点基準と構造化出力の仕様 |
| `grading-start.json`、`grading.json` | 採点開始・完了の記録。`grading.json`の`assessments`に各行の判定、`judge_calls`・`judge_failures`に呼び出し・失敗件数 |
| `grading.json`の`usage_total`・`duration_seconds` | 採点用モデルの使用量と採点処理全体の時間。各呼び出しは`assessments`内の`usage`・`duration_seconds` |
| `evidence.json`、`scores.json`、`execution.json` | 自動採点を追加した証跡、集計、元の評価と採点の実行記録 |

個別・統合後の`scores.json`には、採点費用の根拠としてトップレベルの`grading`も保存します。形式は`retail-grading-overhead-v1`で、採点条件、完了状態、呼び出し件数、採点元ごとの`sources`を保持します。`usage_total`、`usage_known_subtotal`、`usage_reported_n`、`usage_unknown_n`は入力・出力・キャッシュ使用量の完全な合計、既知の小計、観測数、欠測数を分けます。`duration_seconds`は各採点元の処理時間の合計で、`duration_scope`は`sum_of_source_grading_wall_clock_seconds`です。実費の`actual_cost`は`null`であり、トークン数だけから価格や請求額を推測しません。

採点の使用量・時間は評価対象の推論使用量・時間へ加算しません。費用に換算するときは、採点用モデルの配置に適用される単価を使い、[第8章](../chapters/08-cost.md)の`initial.evaluation`に計上します。

### 比較表の判定と列

`comparison.json`の`assessment_method:"model"`は自動比較、`legacy_human_review`は従来の確認結果の比較です。自動比較では両側の`automatic_decision`が`success`または`failure`のときだけ、`improvement`・`regression`・`no_change`を判定します。`needs_review`や`unknown`があれば`incomparable`です。全体の`decision_counts`に予定した全ケースを残します。

`comparison.csv`はUTF-8 BOM付きの**閲覧用**の表です。`case_id`、`category`、`messages`、`expected`、`reference`、変化の`decision`に加え、各側の列に`before_`・`after_`を付けます。`response`は応答、`automatic_decision`は自動判定、`assessment_source`は判定の出所、`judge_reason`は採点用モデルの理由、`reasons`は技術的・ルール上の失敗理由、`latency_seconds`・`usage`は評価対象の時間・使用量です。従来の`status`・`review`と自動判定は別に保持します。`comparison.json`には詳細、`comparison.md`には判定と理由の概要を保存します。

### 必要な場合の人による確認

以下は標準の自動採点とは独立した手順です。人による確認を記録する場合、元の未採点runの`reviews.csv`を使います。`status`は優先順に`technical_failure` → `quality_failure` → `review_pending` → `confirmed_success`と判定します。最後の状態にはルール検査合格に加え、明示的な人間の確認が必要です。

次のコマンドで、人による確認専用の`reviews.csv`を各runに出力します。通常の`run`や`grade`ではこのCSVを自動出力しません。

```powershell
python scripts\evaluate.py review-sheet --run-dir runs\base-before
python scripts\evaluate.py review-sheet --run-dir runs\fine-tuned-after
```

出力された`reviews.csv`を開き、応答・参照例・判定理由を読みます。`decision`へ`success`または`failure`、`reviewer`へ確認者、`notes`へ判断理由を記入します。未確認のdecisionは`needs_review`または空欄にします。編集できるのはこの3列だけです。

確認・比較用の出力には`category`と`reference`も含み、日本語の参照回答を読めます。CSVはExcelで開きやすいUTF-8 BOM付きで出力します。

空欄は既存の確認結果を保持します。`needs_review`は既存の人による確認を解除しますが、自動検査の失敗は解除しません。`success`・`failure`には確認者と理由の両方が必要です。記録が欠けている行にはdecisionを付けられないため、空欄にします。

```powershell
python scripts\evaluate.py review --run-dir runs\base-before --output-dir runs\base-reviewed
python scripts\evaluate.py review --run-dir runs\fine-tuned-after --output-dir runs\fine-tuned-reviewed
python scripts\evaluate.py compare --before runs\base-reviewed --after runs\fine-tuned-reviewed --output-dir runs\human-comparison
```

`review`は元の証跡と確認内容の対応を内部で検査し、新しい保存先へ証跡と採点を出力します。識別値を手計算する必要はありません。人間の確認は技術的失敗・自動検査失敗を上書きできません。これは実行の許可ではなく、内容の評価です。確認前のrunも比較できますが、確認待ちはそのまま残ります。`compare`の問題別CSVとJSONで改善・悪化・未試行などを確認します。

`review`は確認日時を自動記録し、取り込んだCSVを`review-input.csv`へ保存します。この人による確認結果の比較では、`compare`が`improvement`・`regression`・`no_change`を判定するのは、両側が`quality_failure`または`confirmed_success`のときだけです。確認待ち・技術的失敗を含む問題は`incomparable`です。

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

どちらの入力も `synthetic_illustration_not_measurement` です。実際の Hosted Agent を実行した証拠ではありません。import の出力は採点用 bundle、次のコマンドの出力が report です。JSON の単一 object / object 配列、または JSONL を受け付けます。上書き・重複するケース×モデルを拒否します。importはオフライン操作で、`run`とは別の入口です。

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

最初の入力は本ページ冒頭の評価②デモ report です。`teacher` / `base` / `fine_tuned`の3者をまとめたreportが必要です。モデル別の実runは、[第7章の自動採点](../chapters/07-end-to-end.md#自動採点して比較表を読む)を終えてから、次のオフライン操作でまとめます。recordsの手作業での転記は不要です。

```powershell
python scripts\evaluate.py combine --run-dirs runs\e2e\teacher-graded `
  runs\e2e\base-graded runs\e2e\fine_tuned-graded --output-dir runs\e2e-combined
```

次のコマンドで、3者分の採点結果から費用入力を作ります。

```powershell
python scripts\report.py --prepare-evaluation --input runs\e2e-combined\scores.json `
  --config configs\examples\cost-evaluation.json --output runs\measured-cost-input.json
```

教師の実行先は共通評価設定の`targets.teacher`へ追加し、`run --model teacher`で選びます。内容の自動採点は各runに同じ設定の`grade`を使います。統合は判定保留や欠測を成功・0へ変えません。自動判定のみでは採用判断と確認済み成功単価を保留します。価格・通貨・本番業務量・稼働条件は費用設定へ明示し、評価データから推測しません。

`combine`は異なるモデルラベルの2個以上のrunを受け取り、共通の問題・ツール・モード・設定を照合します。`input.json`、`evidence.json`、`scores.json`、`execution.json`を新規保存します。採点済みrunでは、統合後の`execution.json`の`sources[i].grading`に各採点元の条件、完了状態、呼び出し・失敗・省略・欠測件数、`usage_total`、`duration_seconds`、`usage_scope`を保持します。個別の採点済みrunでも同じ概要を`execution.json`の`grading`に保存し、詳細な`grading.json`は元の採点先に残します。統合自体は評価①でも使えますが、費用adapterへ渡せるのは3者分の評価②だけです。

準備 adapter は source report/config の SHA-256、ケースの同一性・重複回数、usage の合計/既知小計/観測数、確認済み結果、row の runtime provenance を保持します。runtime の根拠は `row.provenance.runtime.kind` であり、価格 config から実行環境を補いません。同一 cohort を確認できない、使用量や runtime が不明、レビュー未完了、模擬例しかない等の場合は **hold（判断保留）** にします。未知を無料や成功扱いにしません。

このデモは合成証跡を使うため、bridge や SVG が生成できても実運用での採用根拠にはなりません。この費用 adapter は `mode:e2e` 専用で、評価① (`next-action`) の report は入力段階で拒否します。費用 adapter の詳細と本番条件の記入は第8章および reporting の reference を参照してください。

## モデルを直接呼ぶ評価

`configs\examples\evaluation.json`を一度複製し、`base_url`と`targets`を設定します。共通の問題ファイルはそのまま使います。手順は[第6章の実践手順](../how-to/06-training-and-evaluation.md)を参照してください。

```powershell
python scripts\evaluate.py run --mode next-action --input runs\next-action-input.json --config runs\evaluation-config.json --model base --run-dir runs\base-before
python scripts\evaluate.py run --mode next-action --input runs\next-action-input.json --config runs\evaluation-config.json --model fine_tuned --run-dir runs\fine-tuned-after
```

- `--mode`は`next-action`または`e2e`。対象は`--model`で選びます。モデル一覧・records・出所ラベルの手編集は不要です。
- 共通設定は`base_url`、`targets`、`max_completion_tokens: 1024`、`timeout_seconds: 60`、`max_model_calls: 12`、`max_tool_calls: 24`です。出力上限は要求ごと、呼び出し上限はケースごとです。
- 評価①は各ケース1要求で、ツールを実行しません。評価②は実際のRetailSessionに一致するtoolsとsystem promptを使い、模擬業務ツールを実行します。
- 最初の通常ケースで接続も確認します。別の接続試験へ切り出さず、初回の時間・使用量を本評価に含めます。
- runには`input.json`、送信前の`execution-start.json`、`evidence.json`、`scores.json`、結果保存後の`execution.json`を自動保存します。入力はそのまま固定し、選択モデルの観測を別の証跡へ保存します。内部識別値と送信記録もプログラムが管理します。
- エラー・上限・結果不明では停止し、残りを未試行として分母に残します。SDKの自動再試行を無効にし、不明な要求を別runで送り直しません。
- 認証は`DefaultAzureCredential`、推論はChat Completionsの`/openai/v1/`です。入力に機微情報がある場合は出力も公開しません。

費用は対象モデルの単価、入力量、出力上限から見積もり、実行後のusage・請求とは分けます。使用量が欠けた場合は0にしません。学習費や配置の保持費は別に確認します。

**この経路はHosted Agentの基盤費・起動時間・監視・分散状態を測るものではありません。** ローカル停止は送信済み要求やクラウド課金の停止を保証しません。

### 第7章で使う評価②の実行と自動採点

[第7章の実践手順](../how-to/07-end-to-end-evaluation.md)では、教材の比較用の問い合わせを生成し、三つのモデルの実行・自動採点・比較をまとめます。手元のPythonが模擬業務ツールを実行し、Foundryのモデルへ結果を返す構成です。

```powershell
.\.venv\Scripts\python.exe scripts\prepare_evaluation.py --data-dir runs\data --output runs\e2e-input.json
.\.venv\Scripts\python.exe scripts\evaluate.py study --input runs\e2e-input.json `
  --config runs\evaluation-config.json --grading-config runs\grading-config.json --output-dir runs\e2e
```

問い合わせは第3章の業務ルールを基にした教材の比較用データです。準備処理は第5章のデータと照合し、注文が重ならないように選びます。期待する行動・処理結果は評価対象モデルの応答から作りません。これは改善点を調べるための追加の評価であり、未使用の最終評価データによる検証とは区別します。

`study`は`teacher`、`base`、`fine_tuned`の同じ入力・生成条件を確認してから、既存の`run`・`grade`・`compare`の処理を順に実行します。採点基準、ルール検査、ケースごとの状態初期化、モデル・ツール呼び出し上限は共通です。

出力先が既存なら、`runs\e2e-002`のように新しい保存先を割り当てます。利用者が再度コマンドを実行することは新しい実験として記録します。一回の実験内では、応答が不明な要求を自動で再送しません。

各結果フォルダーの`comparison.csv`に三つのモデルの比較、`summary.json`に集計を保存します。`teacher`・`base`・`fine_tuned`に取得結果、対応する`*-graded`に採点結果が残ります。`base-vs-fine-tuned`、`teacher-vs-fine-tuned`、`teacher-vs-base`には従来と同じ詳細比較表を作ります。

三つの採点結果の統合は、[第8章の最初の手順](../chapters/08-cost.md#三つのモデルの評価結果を統合する)で行います。採点用モデルの使用量を評価対象モデルの使用量へ加えることや、自動判定を人による確認済み成功として扱うことはありません。

## 検証範囲と限界

標準ライブラリ unittest により、厳密な引数/schema 検査、欠けた証跡、使用量 unknown、順序・最終状態、確認と証跡の対応、分母、会話分離、呼び出し上限、再送禁止、CLIの上書き拒否を検証します。実際のRetailSession、送信記録、Hosted Captureとのローカル結合と、SDK / HTTP境界のmockを使います。サンプルだけでは返品・交換・境界条件のカバレッジを満たしません。ケース数を増やす前に独立した期待値とカテゴリ・境界の実験計画を追加してください。

クラウド認証・課金・モデル利用可否・SDKの実サービス互換・Hosted Agent での通し実行・品質同等性・採用成功はこの実装作業では**未検証**です。価格・費用の計算と採用判断は別章で行います。旧実験の cloud ID、固定のケース数、過去 run、5モデル前提には依存しません。
