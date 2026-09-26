# 初版のオフライン確認記録

2026-09-19、Windows / PowerShell / Python 3.13.15で確認しました。
これは性能評価結果ではなく、公開するコードと教材の限定的な動作確認です。

## 実行した確認

- 新規仮想環境への `pip install -e .` と `pip check`。
- `python -m unittest discover -s tests -q` : 166テスト成功。
- 公開対象だけをGit indexから別ディレクトリへexportし、別の新規仮想環境でも166テスト成功。
- 元のrun・チャット・資格情報を含まないexport側で、データ整形と説明用レポートを生成。
- wheelを作成・インストールし、isolated Pythonからパッケージと日本語contractを読み込み、架空店舗ツールを呼び出し。
- 元のまま移植した4ファイルのhashとMIT表示を照合。Gitの改行変換でhashが変わらない属性を設定。
- 相対リンク・公開対象・既知の個人環境識別子の混入確認。
- [初回GitHub Actions実行](https://github.com/shitada/foundry-distillation-lab/actions/runs/35436920569)でWindows上のテスト、データ準備、説明用レポート、公開対象検査が成功。以後の状態は[workflow履歴](https://github.com/shitada/foundry-distillation-lab/actions/workflows/offline.yml)を参照。

## オフラインでつながる工程

説明用JSONLのimport → provenanceを保持した整形 → 12/3/3/2会話への分割 →
学習upload計画の準備 → 改善用9問の次の行動bundle作成を確認しました。
この20会話は配送照会のスクリプト生成例で、teacherの実行履歴ではありません。

別の合成採点例で、評価①3/3枠、評価②2/3枠を記録として読み込みました。
未記録枠は消さず、評価結果から費用入力・レポートへ接続して判断がholdになることを確認しました。
Hostedの合成captureも1/3枠として取り込み、同じ費用経路でholdを維持しました。
いずれもモデル呼び出し・クラウド操作・新たな実測はありません。

Hosted Agentのstagingは、LICENSEと許可した業務コードだけを含むことを確認しました。
学習データ、評価正解、oracleを配置payloadには含めません。

実際のcaptureコードを模擬HTTP応答で動かした往復テストは、
現在のツール呼び出し対応が診断用の名前・引数照合に留まることを保持し、
`unverified_framework_tool_call_linkage` によるtechnical failureを確認します。
SDKコンテキストからの直接のcall ID対応は未確認であり、人間レビューでこの失敗を
成功に上書きできません。これはHostedツール実行の成功実証ではありません。
この制約を採点に反映した最終コードでも166テスト成功を確認しています。

## 未確認事項

クラウド資格情報・利用可否・quota、実学習、実デプロイ、
モデル推論、Hostedの実protocol・SDK連携・永続volume・capture回収は未確認です。
ARMの条件付き操作とazd内部のretryも、mock成功をサービス保証と扱ってはいけません。
学習効果、品質同等性、応答性能、実価格・請求・運用費優位性は測定していません。

クラウド追試は実行時の対応版・価格・対象・呼び出し上限・保持費を確認して行います。
小規模な通し実行と、本評価の品質・経済性検証を区別してください。
