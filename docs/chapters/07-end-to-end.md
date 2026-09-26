# 07 業務全体を3者で比較する

## 現在地
次の行動の評価を終え、実際の業務の流れを評価する段階です。

## 目的
教師モデル・学習前の生徒モデル・学習済みの生徒モデルが、同じ一つの顧客入力から安全に業務を完了、または適切な確認質問で停止できるかを調べます。

## 前提
一回の顧客入力に対して複数のモデル・ツール呼び出しを許します。顧客シミュレーターはありません。確認質問に架空の返事を自動生成して会話を継続させません。

## 実施内容

### 1. 比較条件と、評価するケースを決める

比較するのは、第4章で選び、第5章で対応履歴を用意した教師モデルと、第6章で評価した学習前後の生徒モデルです。モデルの種類・版と、選択した学習済みモデルを引き継ぎます。モデルを変更する場合は、別の実験版として比較条件を決め直します。

モデルの違いを知りたいのに、問い合わせや業務ルールまで変わっていたら、結果の差が何によるものか分からなくなります。[第4章](04-environment.md)の実験計画を基に、同じケース、注文・在庫などの初期状態、業務ルール、指示文、ツール仕様、採点基準を使います。呼び出し数と時間の上限も固定します。

一件の処理結果が次のケースへ持ち越されないよう、会話ごとに状態を初期化します。教材内の業務日は**2026年8月31日**に固定し、実行日の時計で返品期限などを変えません。

問い合わせの種類と構成比も揃えます。返品ばかりを試したモデルと、配送問い合わせが多い別のモデルの結果を、そのまま比較してはいけません。正常、条件の境界、情報不足、対応を断るケース、失敗からの回復を含むか確認し、種類別の件数と未評価の範囲を記録します。

実行順や一時的な混雑によって処理時間が変わることもあります。必要に応じて実行順を入れ替え、同じ条件で繰り返します。モデルごとに対応する設定が違う場合は、無理に同じ値を指定せず、**何を揃え、何が揃わなかったか**を記録します。

なお、「条件を揃える」ことは、実際のツール呼び出し数や処理時間まで同じにすることではありません。同じ仕事を任せた結果として生じる違いは、評価したい対象です。

### 2. 保存済み記録で採点の経路を確認する

```powershell
python scripts\evaluate.py --help
python scripts\deployment.py --help
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-e2e.json --output runs\e2e-scores.json
```

上の実行は**合成の保存済み記録を採点するオフライン例**で、実測成績ではありません。サンプルの`evidence_kind`は`synthetic_illustration_not_measurement`であり、業務全体の評価には未記録・証拠不足が意図的に含まれます。全行が成功しないこと自体は例の不具合ではありません。入力bundleとレビューの契約は`docs\reference\evaluation-contract.md`を参照します。出力JSONは既存ファイルを上書きせず、同じ出力先で再実行するとexit 2で拒否します。

保存済み/模擬結果の採点と、実推論を区別します。**`evaluate.py run --mode e2e`は手元からモデルを直接呼ぶ実行で、Hosted Agentの評価経路ではありません。** 共通の問題と`configs\examples\evaluation.json`から作った設定を使い、`--model`と新しい`--run-dir`で対象を分けます。問題ファイルのモデル一覧や出所ラベルは編集しません。自動採点と比較には第6章と同じ`grade`・`compare`を使います。

評価②では実際の小売ツール仕様と指示文に合う問題を用意し、3者で共用します。教師の配置名は設定の`targets.teacher`に追加し、`--model teacher`で選びます。生徒は`base`と`fine_tuned`です。実行前に対象・呼び出し上限・費用見積りを確認します。最初の通常ケースで接続も確認し、その時間と使用量を本評価へ含めます。

入力・設定と送信記録は自動保存されます。呼び出し上限やエラーで停止し、自動再試行はしません。結果不明時は元の要求を確認します。未試行や欠測も分母に残し、自動採点でも判断できないケースは比較を保留します。

保存済みHosted Agent観測には、別の**オフラインimport**があります。

```powershell
python scripts\evaluate.py --mode e2e --input data\samples\evaluation-hosted-cases.json --import-hosted data\samples\evaluation-hosted-capture.json --model-label teacher --output runs\hosted-import-demo.json
python scripts\evaluate.py --mode e2e --input runs\hosted-import-demo.json --output runs\hosted-import-demo-scores.json
```

この同梱captureも合成例で、実際のHosted Agent実行ではありません。入力・agent/model/endpoint・toolsのidentityと保存済み観測を照合し、欠けたイベントや最終状態を捏造しません。例はteacherの1記録のみで、他の予定枠は未記録です。captureのreviewは元情報として残しますが、取り込み後の内容確認は別です。モデルHTTPだけの旧`foundry-responses-capture`は、実際のツール実行や副作用の証拠を欠くため、この業務全体の記録の取り込みの代替にはできません。

実際のcapture実装とRetailSessionを合成HTTP fixtureで接続し、出力した`hosted-retail-evidence`をimport・採点するオフライン結合テストがあります。これはHostedサーバーやSDK・認証の実環境確認ではありません。`final_state_scope: observed_tool_returns_not_store_snapshot`が示す通り、最終状態は観測したツール戻り値の範囲であり、ストア全体のsnapshotや実システムの永続化を証明しません。

実際の配置前に、第4章の対象環境と保持費を確認します。Hosted Agentの配置設計でも、先に定めた比較条件と会話ごとの状態分離を守り、配置内容に学習データ・評価用の正解・採点コードを入れません。

モデル配置の計画だけなら、次のコマンドでローカルJSONを作れます。

```powershell
python scripts\deployment.py prepare --config configs\examples\cloud-deployment.json --output runs\deployment-plan.json
```

出力には対象resource ID、runの所有者識別子、SKU/model、API version等が入ります。これはARM要求の**計画作成**で、資源作成やリージョンの利用可否確認ではありません。`deploy`・`status`・`cleanup`はネットワーク操作です。新しい所有者識別子を生成しただけで、既存資源を自分の所有物として扱ってはいけません。Hosted Agentのstageは小売実行に必要なファイルだけをallowlistで収録しますが、Hosted Agent配置の成功をこのモデル配置計画で検証したとは言えません。

### 3. 業務品質と、重大な違反を確認する

各ケースで、呼び出し順序・ツールへ渡した情報・結果、最終状態、日本語の説明、開始から最終回答までの時間、全呼び出しの使用量を保存します。次の観点を、事前に決めた成功条件に照らして確認します。

| 観点 | 確認すること |
|---|---|
| 必要な確認と順序 | 注文照会、返品条件の確認、計算などの必須手順を省かず、前提を満たして操作したか |
| 業務としての成功 | 許可された対応、最終状態、日本語の説明が正しいか |
| 確認質問 | 情報不足のときに必要な質問を返したか。解決可能な場面で不要な質問を返して先送りしていないか |
| 重大な違反 | 未承認の返金、別注文の操作、存在しない確認結果の捏造などがないか |

教師モデルと全く同じ道筋をたどったかではなく、必須条件を守って正しい結果になったかを判断します。ただし、業務上定めた必須順序まで自由に変えてよいわけではありません。

10件のうち9件を正しく処理しても、残り1件で顧客が承認していない返金を実行したなら、「だいたい成功した」とは評価できません。**重大な違反は、ほかの成功や平均点で埋め合わせない**基準にします。

その際、モデルが不正な操作を要求したこと、ツール側が拒否して実行を防いだこと、実際に不正な状態変更が行われたことを、別々に数えます。ツールが被害を防いだことは重要ですが、それだけでモデルの判断まで正しかったことにはなりません。

ツールや状態のルール検査に加え、採点用モデルが説明の内容を評価します。注文番号や金額の形式が合っていても、返金できない理由の説明が誤っていることがあります。利用者は自動採点後の比較表で代表例を読み、形式と内容の両方を確認します。

### 4. 成功・失敗・未確認を分けて記録する

成績表では、成功した例だけでなく、試せなかった例や判定できない例も残します。

| 記録する状態 | 意味 |
|---|---|
| 確認済みの成功 | 決めた基準で内容・状態を確認できた |
| 業務上の失敗 | 確認漏れや不適切な対応など、仕事としての問題があった |
| 技術的な障害 | 通信や実行基盤などに問題があり、通常の評価を完了できなかった |
| 確認中・成否不明 | 証拠の不足やレビュー未完了で、成功とも失敗とも確定できない |
| 未実施 | そのケースをまだ実行していない |

自動比較では`automatic_decision`と`assessment_source`で判定とその出所を区別します。従来の`status`は`technical_failure`（技術的失敗）、`quality_failure`（品質不合格）、`review_pending`（人間確認待ち）、`confirmed_success`（確認済み成功）のまま残ります。自動判定だけで`confirmed_success`にはなりません。実行記録や判定理由も一緒に確認し、未実施や証拠不足をモデルの業務上の失敗へ読み替えません。

業務全体の確認済み成功には、そのケースの**証拠に結び付いた明示的な人間のレビュー記録**が必要です。自動採点による推定とは別の扱いです。必要な場合の人による確認手順は[評価仕様](../reference/evaluation-contract.md#必要な場合の人による確認)を参照できます。証拠が不足している場合は、人が合格と書くだけで確認済みにしてはいけません。

### 5. 時間・使用量・評価した範囲を集計する

処理時間は、問い合わせ開始から最終回答までを測り、モデルの応答だけでなく、ツール待ちや回復のための処理も含めます。起動直後と稼働中の違いや、一時的な混雑など、時間に影響する条件を記録します。失敗して早く終了しただけの処理を、適切な業務を短時間で終えた実績として扱いません。

平均だけでは、まれに非常に遅くなるケースを見落とします。観測数と**中央値**に加え、遅い側の目安として**95パーセンタイル**（p95）を記録します。現実装では、値を小さい順に並べ、観測数の95%に達する最初の順位を選ぶ方式（nearest-rank）です。件数が少ない場合の不安定さを隠さず、全予定件数が測れたかも確認します。

全体の集計に加え、返品・交換・配送などの種類別の件数、成功・失敗、代表的な失敗例を残します。未試行や繰り返し回数の不足も示し、一部の成功だけを基に業務全体へ結論を広げません。

### 6. 費用比較へ渡す3者の結果をまとめる

この章で取得した評価②の保存先を`runs\e2e-teacher`、`runs\e2e-base`、`runs\e2e-fine-tuned`とします。[第6章の手順6.1](../how-to/06-training-and-evaluation.md#61-採点用モデルと設定を準備する)で用意した`runs\grading-config.json`を3者で共用し、同じ採点基準で自動採点します。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\e2e-teacher --config runs\grading-config.json --output-dir runs\e2e-teacher-graded
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\e2e-base --config runs\grading-config.json --output-dir runs\e2e-base-graded
.\.venv\Scripts\python.exe scripts\evaluate.py grade --run-dir runs\e2e-fine-tuned --config runs\grading-config.json --output-dir runs\e2e-fine-tuned-graded
```

次のコマンドで学習前後の比較表を作ります。`runs\e2e-comparison\comparison.csv`は読むための表です。第6章と同じ列で、応答・参照例・判定理由、時間・使用量の代表例を確認します。行ごとの判定の書き込みはしません。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py compare --before runs\e2e-base-graded --after runs\e2e-fine-tuned-graded --output-dir runs\e2e-comparison
```

次の`combine`は採点済みの3者のrunをまとめるオフライン処理です。第8章には`runs\e2e-combined\scores.json`を渡します。評価対象モデルを呼び直したり、自動判定を確認済みの業務成功へ変えたりする操作ではありません。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py combine --run-dirs runs\e2e-teacher-graded `
  runs\e2e-base-graded runs\e2e-fine-tuned-graded --output-dir runs\e2e-combined
```

## 出力
ケース・モデル・反復ごとの記録、自動判定と理由、比較表、エラー分類、時間・使用量の表です。人による確認を行った場合は、その記録を自動判定と分けて保持します。`overall`、`per_model`、`per_case`の分母には予定された未記録枠も含めます。基盤障害、未試行、呼び出し上限到達を通常の業務失敗に埋め込まず、母数とともに表示します。未知の使用量は`null`です。

`usage_total`は欠測が一つでもあれば`null`です。`usage_known_subtotal`と`usage_reported_n`は観測済み部分を説明する値で、総費用の根拠としてそのまま使いません。tokenの単位は`input_tokens`、`output_tokens`、`cached_input_tokens`で、cacheは入力の内数です。第8章の`report.py --prepare-evaluation`で保存済み採点reportを費用入力へ接続できます。行と集計を照合し、全予定枠で完全に観測された項目だけを要求当たり平均へ変換します。未知のusage・レビュー、runtime差を埋めたり、価格を取得したりはしません。

## 解釈
例えば100件予定で80件のみ実行し、そのうち60件が確認済みの成功なら「成功率75%」だけでは不十分です。100件予定、80件実行、60件確認済み成功、実行した残り20件の状態、20件未試行の理由を併記します。成功率の分母が実行した80件であることを示し、予定した評価をどれだけ実施できたかとは区別します。

未試行をモデルの失敗として扱わず、確認中のものも成功へ加えません。費用集計では失敗・回復の呼び出しも支出へ含め、使用量が取れなかった処理を無料として扱わないことを、第8章へ引き継ぎます。

## 完了条件
全予定件数の状態が追跡でき、3者の条件が一致し、自動採点後の代表例と主要な境界を確認できています。欠測や条件混在があれば比較不能として保留します。比較を終えたことと、業務成功・採用を確定したことは区別します。

## 限界
実環境での通し評価は未実施です。保存済み模擬結果の採点成功はモデル品質の検証ではありません。反復不足の95パーセンタイルや、小さな種類別集計の成功率は不安定です。最終評価用データを改善へ使わず、不確実性を記録します。

現行のHosted Agentの記録では、ツールの名前・引数による照合と、実際の実行に対応する識別子の直接確認を区別します。後者を確認できないケースは`unverified_framework_tool_call_linkage`という技術的な失敗として扱い、人間レビューで成功へ上書きしません。実環境での検証と証拠の制約は、[評価の契約](https://github.com/shitada/foundry-distillation-lab/blob/main/docs/reference/evaluation-contract.md)を参照してください。
