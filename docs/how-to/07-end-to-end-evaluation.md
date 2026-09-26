# 第7章の実践手順：三つのモデルに問い合わせ対応を任せて比べる

[第7章](../chapters/07-end-to-end.md)の評価②を実行します。教師モデル・学習前の生徒モデル・学習済みの生徒モデルに同じ依頼を渡し、業務ツールを使った対応、最後の回答、時間、使用量を比較します。

| 段階 | 行うこと | 得られるもの |
|---|---|---|
| 1 | 第6章のモデルと設定を引き継ぐ | 三つの評価対象と採点用モデルの接続設定 |
| 2 | 評価用の問い合わせを準備する | 小売サポートの主要な対応を含む共通の問題 |
| 3 | 対応の実行から自動採点・比較まで進める | 三つのモデルの応答・判定・比較表 |
| 4 | 比較表を読む | 品質・時間・使用量の違い |
| 5 | 記録を保存して次章へ進む | 第8章で費用計算に使う記録 |

PowerShellを開き、リポジトリのルートフォルダーで操作します。Pythonは第4章で作成した仮想環境のものを使います。

## 1. モデルと設定を準備する

第6章で使った学習前後のモデルと採点用モデルに、第5章の教師モデルを加えます。Foundryの **Build → Models** で、それぞれの配置名と利用できる状態になっていることを確認します。

`runs\evaluation-config.json`をエディターで開き、`targets`に教師モデルの配置名を`teacher`として追加します。教師の配置名が`teacher-model`の場合は、次のようになります。

```json
{
  "base_url": "https://<リソース>.openai.azure.com/openai/v1/",
  "targets": {
    "teacher": "teacher-model",
    "base": "student-base",
    "fine_tuned": "student-fine-tuned"
  },
  "max_completion_tokens": 1024,
  "timeout_seconds": 60,
  "max_model_calls": 12,
  "max_tool_calls": 24
}
```

`base_url`には第6章で控えたモデルの接続先を使い、三つの配置名を自分の環境に合わせて保存します。モデルの配置操作は、[第6章のモデル準備](06-training-and-evaluation.md#モデルとfoundryの準備)を参照してください。

採点には、第6章の`runs\grading-config.json`と、そこに指定した採点用モデルを使います。

| ファイル | この章で使う内容 |
|---|---|
| `runs\evaluation-config.json` | 三つのモデルの接続先・配置名と、生成条件 |
| `runs\grading-config.json` | 採点用モデルの接続先・配置名と、採点時の生成条件 |
| `runs\data`フォルダー | 第5章で作ったデータと分割の記録 |

モデルとツールの呼び出し上限は、一件の問い合わせあたりそれぞれ12回と24回です。モデルはツールの実行結果を受け取りながら、最後の回答まで進めます。

## 2. 評価用の問い合わせを準備する

次のコマンドで、教材の業務ルールに基づいた問い合わせと、期待する処理結果を用意します。第5章のデータを読み、そこで使った注文と重ならない注文を選びます。

```powershell
.\.venv\Scripts\python.exe scripts\prepare_evaluation.py `
  --data-dir runs\data --output runs\e2e-input.json
```

作成される`runs\e2e-input.json`を、三つのモデルで共用します。第5章の配送照会サンプルに加えて用意する、次の11件の比較用データです。

| 問い合わせの種類 | 件数 | 確かめる対応 |
|---|---:|---|
| 配送状況の照会 | 1 | 注文と配送状況を照会して説明する |
| 返品・返金 | 3 | 標準・ゴールド・プラチナの会員区分ごとに、手数料と返金額を扱う |
| 交換 | 1 | 在庫と差額を確認して交換する |
| キャンセル | 1 | 発送前の注文をキャンセルする |
| 返品期限を過ぎた依頼 | 1 | 期限を確認して対応可否を説明する |
| 配送中の紛失 | 1 | 紛失時の返金に対応する |
| 遅配 | 1 | 配送遅延クレジットを計算する |
| 破損した最終処分品 | 1 | ストアクレジットで対応する |
| 注文番号がない依頼 | 1 | 注文番号を確認する |

この準備では、問題・業務ルール・ツール仕様・採点基準をファイルにまとめます。

## 3. 対応を実行し、自動採点と比較表を作る

次のコマンドで、三つのモデルによる対応、自動採点、比較表の作成までを順に実行します。問い合わせへの対応には評価対象のモデルを、内容の採点には採点用モデルを呼び出します。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py study `
  --input runs\e2e-input.json --config runs\evaluation-config.json `
  --grading-config runs\grading-config.json --output-dir runs\e2e
```

結果は`runs\e2e`に保存されます。同じコマンドを再実行すると、`runs\e2e-002`のように新しい保存先が作られ、画面に表示されます。以降は、表示されたフォルダーを開きます。

| 保存されるもの | 最初の実行での保存先 |
|---|---|
| 三つのモデルを並べた比較表 | `runs\e2e\comparison.csv` |
| 全体の集計 | `runs\e2e\summary.json` |
| 教師モデルの採点結果 | `runs\e2e\teacher-graded` |
| 学習前モデルの採点結果 | `runs\e2e\base-graded` |
| 学習後モデルの採点結果 | `runs\e2e\fine_tuned-graded` |
| 学習前後の詳細比較 | `runs\e2e\base-vs-fine-tuned\comparison.csv` |
| 教師と学習後の詳細比較 | `runs\e2e\teacher-vs-fine-tuned\comparison.csv` |
| 教師と学習前の詳細比較 | `runs\e2e\teacher-vs-base\comparison.csv` |

## 4. 比較表を読む

`runs\e2e\comparison.csv`をExcelなどで開きます。一行が一つの問い合わせです。三つのモデルの判定、回答、判定理由を並べて読みます。

列名の`teacher_`は教師モデル、`base_`は学習前、`fine_tuned_`は学習後です。

| 読む内容 | 列の例 |
|---|---|
| 問い合わせと参照例 | `user_input`、`reference` |
| 自動判定 | `teacher_verdict`、`base_verdict`、`fine_tuned_verdict` |
| 判定の理由 | `teacher_reason`、`base_reason`、`fine_tuned_reason` |
| 応答とツールの実行過程 | `teacher_response`、`base_response`、`fine_tuned_response` |
| 処理時間（秒） | `teacher_latency_seconds`、`base_latency_seconds`、`fine_tuned_latency_seconds` |
| 入力・出力のトークン数 | `teacher_input_tokens`・`teacher_output_tokens`など |

自動判定は`success`（合格）、`failure`（不合格）、`needs_review`（判断保留）、`unknown`（記録不足や採点エラーなどで不明）です。

まず、次の組み合わせで差を確認します。

| 比べる結果 | 確認すること |
|---|---|
| 学習前と学習後 | 学習によって適切な対応が増えたか。悪化した依頼はあるか |
| 教師と学習後 | 教師と同じく適切に対応できるか。説明や処理に何が違うか |
| 教師と学習前 | 学習前から対応できていた範囲はどこか |

合格・不合格・判断保留の代表例を選び、注文や商品の取り違え、必要な確認、返金額や交換内容、日本語での説明を読みます。二つのモデルを詳しく比べるときは、上の表にある詳細比較の`comparison.csv`を開きます。列の読み方は[第6章の比較表](06-training-and-evaluation.md#63-比較表の代表例を読む)と共通です。

処理時間とトークン使用量も並べて確認します。時間は依頼を渡してから回答まで、使用量は一件の対応で呼び出したモデルの合計です。採点用モデルの使用量は、各採点結果フォルダーの`grading.json`に分けて保存されます。

全体の件数や時間の傾向は、`runs\e2e\summary.json`の`per_model`で確認します。各モデルの`automatic_decision_counts`が判定件数、`latency_seconds`の`median`が中央値、`p95`が95パーセンタイルです。

自動判定は、採点基準に照らした推定です。実際の回答と理由を読んで、どの場面で差が出たかを確認します。

## 5. 記録を保存して第8章へ進む

結果フォルダーを保存します。採点結果、ツールの実行記録、時間、使用量が含まれています。

第8章では、このフォルダー内の三つの採点結果を統合し、モデルごとの料金を使って費用を計算します。統合の操作は[第8章の最初の手順](../chapters/08-cost.md#三つのモデルの評価結果を統合する)で行います。

Foundryの **Build → Models** で、評価に使った配置の保持予定を確認します。実験を終える配置は削除し、残す配置とその保持費を記録します。

## 詳しい仕様

- [第7章：問い合わせへの対応全体を比較する](../chapters/07-end-to-end.md)
- [評価用の問い合わせ・自動採点・集計の仕様](../reference/evaluation-contract.md)
- [実装の確認状況](../reference/verification.md)
