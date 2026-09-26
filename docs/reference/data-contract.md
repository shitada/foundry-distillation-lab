# データ整形・分割の契約

この段階はネットワーク通信なしで動作します。
入力はUTF-8 JSONLで、1行に `conversation_id`、`category`、`messages` を含めます。
`tools` を含める場合は同梱の日本語contractと一致させます。
`source_kind` でモデル収集と説明用スクリプト生成例を区別します。

```powershell
.\.venv\Scripts\python.exe scripts\prepare_data.py --input data\samples\traces.jsonl --output runs\data
```

system promptの欠落は凍結した日本語正本を補い、ツール要求の空contentだけを除きます。
意味のある本文、順番、ツール結果は保存します。任意の重複発話やoverlapping snapshotを
機械的に削除して「修復」しません。不明なrole、call/resultの対応切れ、異なるcontract、
不正な引数、既知の機微情報パターンは `audit.json` に保留理由を残して停止します。
これはPIIや業務品質の完全な検出ではなく、学習前に別途レビューが必要です。

同一注文・会話の連結グループを跨がないようにし、固定seedで
学習60%、検証15%、改善用評価15%、残りを最終用へ分けます。
最低10グループが必要です。比率はグループ単位なので行数比率とは一致しない場合があります。
カテゴリ別件数を報告し、欠落や偏りは明示的にレビューします。
同一テンプレートからの生成物が統計的に独立であることは保証しません。

`train.jsonl` / `validation.jsonl` は学習形式、`development.jsonl` は改善用、
`final.jsonl` は候補選びに使わず保留する最終評価用データです。
`next-actions.jsonl` は改善用会話から作り、教師履歴と正解を別フィールドに保持します。
正解フィールドをモデル入力へ含めないでください。
最終E2Eには、この最終データの独立性を確認しつつ、顧客入力と初期状態、
許容する終状態を明示した別のケース定義が必要です。
`final.jsonl` をそのままagentに渡して最終評価をしたことにはできません。

`manifest.json` は元入力と全出力のhash、分割、カテゴリ、source_kindを記録します。
品質レビューの完了記録は既定でfalseです。これは実行の許可ではなく、内容の確認状態です。構造検査合格を品質合格へ読み替えません。
既存出力ディレクトリは上書きせず、新しい入力版には新しいrunを使います。
