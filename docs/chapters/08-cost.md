# 08 総費用・分岐点・投資回収

## 現在地
品質の証拠を確認してから、同じ業務量で経済性を比較します。品質未確認でも計算練習はできますが、採用判断とは切り離します。

## 目的
推論のtoken代だけでなく、固定費・初期費用・失敗を含めて検討します。

## 前提
レポーターは**集計済みJSON**を読みます。手入力のほか、保存済み評価reportからusageと最終判定を取り込む限定adapterがあります。Azure価格取得、請求照合、生traceの直接集計、通貨換算はしません。実測用テンプレートの`actual`は利用者の申告区分で、証拠が揃ったことの認証ではありません。

## 実施内容
```powershell
python scripts\report.py --input examples\illustrative\cost-input.json --output runs\cost-example
```

出力は`report.json`、`costs.csv`、`volume.csv/svg`、`cumulative.csv/svg`、`payback.csv/svg`、`input.json`、`decision.md`です。再実行には新しい出力先を選びます。[スキーマと架空図表](../../examples/illustrative/README.md)を参照し、実測時は`configs\examples\cost-actual-template.json`をコピーして別ファイルへ記入します。

第7章の採点reportへ接続する場合は次を使います。最初のコマンドは費用入力JSONを新規作成するだけ、次が図表生成です。

```powershell
python scripts\report.py --prepare-evaluation --input runs\e2e-scores.json --config configs\examples\cost-evaluation.json --output runs\evaluation-cost-input.json
python scripts\report.py --input runs\evaluation-cost-input.json --output runs\evaluation-cost-report
```

adapterは元reportと設定のbytesをSHA256へ結び付け、3者の行数・状態件数・usage合計/既知小計/観測数を照合します。分母は失敗・未記録を含む**予定されたcase×model枠**で、各caseの出現回数に加え、`case_sha256`と`tool_contract_sha256`を3者で比較します。同じcase名でも内容が違えば比較しません。一つの項目でも欠測があればそのtoken平均は`null`です。確認済み成功は最終判定だけでなく、freshな`review`が行の`evidence_sha256`に結び付いていることを確認します。未知/未レビューが残れば成功件数の確定と成功単価を保留し、履歴の`source_review`を流用しません。これらのidentity hashがない旧reportは実測へ格上げしません。

これは均等な評価枠の平均であって、運用カテゴリ比で再重み付けした推計ではありません。供給された行のcategoryは監査に残しますが、欠けたカテゴリを補作せず、集計カテゴリ比は`null`、運用の仮定は`evaluation_projection.production_category_mix`へ別記します。観測した`origin: local_tool_loop`と明示的な`provenance.runtime.kind: local_direct_model`を照合し、Hostedでは`origin: hosted_capture_import`と`provenance.runtime.kind: foundry_hosted_agent`を照合します。注入runnerの`origin`だけでは実モデル呼出しを証明できず、provenanceがなければunknownです。未知・混在・設定した想定runtimeとの不一致なら分岐点/回収期間も保留します。cohortやcase/tool条件が違う場合も同様です。

評価①は次の行動1回の測定なので、顧客要求全体のtoken平均や業務成功率へ拡大できません。adapterは**`mode=e2e`のreportだけを受け付け、評価①を拒否**します。合成例や由来不明の記録を実測へ格上げせず、すべての接続出力を**評価cohortの予測、本番の証拠ではない**と表示します。価格・固定費・初期費用・運用件数・品質ゲートは設定で別途確定する必要があります。

| 区分 | 集計方法 |
|---|---|
| 推論 | 1顧客要求に属する全モデル呼出しを合計。失敗・回復も含む |
| cache | input tokenの内数。非cache分だけ通常入力単価、cache分はcache単価 |
| 変動費 | agent、tool/log/storageの要求当たり費用。推論と重複計上しない |
| 固定費 | model hosting、agent、tool/log/storageの月額。稼働時間を根拠にする |
| 初期費用 | teacher生成、学習、初期評価、その他。共通費の配賦を明記 |

価格の単位・通貨・取得日・モデル/version・tier/region、稼働時間、月間件数・カテゴリ比を別々に記録します。欠測は`null`、非該当でゼロと確認したものだけ`0`です。欠測費目を除いた合計を総費用と表示しません。

月額は「要求当たり変動費×月間要求数＋月固定費」です。分岐件数はteacherとstudentの月額が同じになる件数。回収期間は「studentの追加初期費用÷月間節約額」で、月間節約がなければ未定義です。元から安いケース、交点なし、不明を区別します。

冒頭の架空値では500件/月が運用分岐、1,000件/月で9,000円/月の節約、追加初期18,000円で2か月回収です。累積運用費の図には初期費用を含めず、回収図には含めます。baseとfine_tunedの推論費が同じでも、学習初期費用が違うので回収曲線は別です。

成功単価は人間確認済み成功率を月間件数へ適用した**予測値**です。レビュー未完了、成功0件、対象件数0なら未定義です。レビュー対象と運用カテゴリ比が異なればその予測は使えません。

## 出力
入力根拠と再計算可能なCSV/JSON/SVG、常に`hold`から始まる判断ドラフトです。図にはILLUSTRATIVEまたはACTUALを表示します。

## 解釈
節約があっても品質不合格なら採用しません。初期費用が不明でも月額分岐は計算可能ですが、回収期間は不明です。費用だけでbaseよりfine_tunedを優先しません。

## 完了条件
すべての費目に根拠または不明理由があり、品質条件・件数・期間を変えた感度分析を別runで残します。

## 限界
線形モデルは価格段階、混雑、autoscale、税、為替、割引、将来価格を自動で扱いません。月間一定の需要・単価・成功率を仮定するため、条件変化時は再集計が必要です。
