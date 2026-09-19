# 06 未学習評価・SFT・次の行動の評価

## 現在地
データが整いました。学習を始める前にbaseの能力を測ります。

## 目的
SFTが何を改善し、何を悪化させたかを局所的に調べます。未学習studentで十分なら学習を見送れます。

## 前提
評価①は固定された履歴の次に何をするかの比較です。業務の最後まで自律的に進める評価②とは分けます。SFTと推論の試行予算は別途承認が必要です。

## 実施内容
次の入口で実装されたオフライン検証・計画と、有料実行の境界を確認します。フラグを推測してクラウド送信しません。

```powershell
python scripts\train.py --help
python scripts\evaluate.py --help
python scripts\evaluate.py --mode next-action --input data\samples\evaluation-next-action.json --output runs\next-action-scores.json
```

第5章の出力を使う場合、学習入力は`runs\data\train.jsonl`、検証入力は`runs\data\validation.jsonl`です。`final.jsonl`を学習、checkpoint選択、改善用評価へ渡してはいけません。サンプルはスクリプト作成の説明用であり、manifestの品質レビュー未承認を、整形済みという理由だけで承認済みに変更しません。

学習の送信前計画はオフラインで作れます。

```powershell
python scripts\train.py prepare --train runs\data\train.jsonl --validation runs\data\validation.jsonl --config configs\examples\training.json --output-dir runs\training-plan
```

`runs\training-plan`にUTF-8 BOM付きへ正規化した`train.jsonl`、`validation.jsonl`と`upload-plan.json`を作ります。trainは10行以上を必要とし、train/validation間の完全一致行を拒否します。入力と設定のhash、行数、bytes、対象を記録しますが、この検査は**構造のみ**です。第5章の漏洩検査や業務レビューを代替しません。設定中のendpoint、モデル名、見積り金額は差替え用の例で、対応モデル・現行料金・支出承認を表しません。

後続の`upload`、`prepare-submit`、`submit`、`status`は別段階です。`prepare-submit`は**実際の先行uploadから得た既知のreceipt**を使うローカル準備であり、最初からオフラインだけで完結するquickstartではありません。receiptやfile IDを作り上げて進めません。`upload`・`submit`・`status`はそれぞれ明示的な承認とSDK確認が必要なネットワーク操作です。まず各サブコマンドの`--help`と安全契約を確認し、この章のオフライン例から自動でlive送信へ進めません。

上の`evaluate.py`は合成bundleに保存済みの模擬応答を採点する**完全オフラインの練習**です。新しいモデル応答を生成せず、出力JSONは既存ファイルを上書きしません。`next-actions.jsonl`は期待行動ケース、`evaluation-next-action.json`はケース・ツール・保存済み応答を含む評価bundleであり、同じ入力形式ではありません。第5章の`--prepare-next-actions`で前者を評価入力へ変換できますが、予測記録は空です。実際の推論記録を供給するまでは、通常採点しても全予定枠が未記録になり、精度や業務成功の証拠にはなりません。詳細は`docs\reference\evaluation-contract.md`を参照します。

1. teacherとbaseに同じ履歴・ツール仕様を与え、次の行動を保存する。
2. ツール選択、引数、必要な確認、日本語説明の形式を評価する。期待する行動が本当に業務上正しいかもレビューする。
3. 学習データhash、モデル/version、学習設定、予算を凍結してSFTを申請する。
4. 選択に使えるvalidation/改善用評価だけでcheckpointを選ぶ。最終holdoutは見ない。
5. fine_tunedを同じ条件で評価し、改善・退行をカテゴリ別に比べる。

差分表にはcase ID、3者の次の行動、期待、判定理由、token、時間、未試行/エラーを並べます。合計点が上がっても、返品の前提確認を省く退行があれば止めます。教師の冗長な呼出しまで無条件に正解にしません。

## 出力
学習計画・jobの証跡、checkpoint選択理由、評価①の保存結果・採点、失敗分類表です。採点JSONには`mode`と`evidence_kind`があり、`per_model`と`per_case`の集計で比較できます。**評価①はbusiness successを確定しません。** 次の行動が合っていることを、人間確認済みの業務全体の成功と混同しないでください。送信が成功したか不明ならjournalを残し、照合するまで再送しません。

## 解釈
①は誤りの所在を絞る診断です。形式や引数の改善は有用ですが、最後まで安全に対応できる証拠にはなりません。「baseより悪化」「teacherも誤っていた」は重要な結果です。

## 完了条件
比較条件が一致し、退行と欠測を隠さず説明できること。品質基準に達しなければ追加支出前に改善案と新しい実験計画を作ります。

## 限界
クラウドでの学習・checkpoint取得の通し検証は未実施です。モデル、学習方式、リージョンで対応機能が異なります。[公式の考慮事項](https://learn.microsoft.com/azure/foundry/openai/concepts/fine-tuning-considerations)と[データ生成](https://learn.microsoft.com/azure/foundry/fine-tuning/data-generation)をlive試行前に確認します。SDKのmock合格を学習成功と表現しません。
