# Hosted retail trace collector（cloud未検証）

元デモのResponsesHostServer / FoundryChatClient方式を小売packageへ接続した
**追試用skeleton**です。配置済みサービスでも、クラウド動作確認済み実装でもありません。

1. rootから`python deploy\hosted-agent\stage.py --output runs\hosted-build`でallowlistだけをstageする。
2. `staging-manifest.json`を確認する。rootのMIT `LICENSE`もstage/imageへ保持する。rootをbuild contextにしない。
3. 専用環境へrequirementsを導入し、image/managed identity/Hosted入力/永続volumeを検証する。
4. collection planをmountし、`COLLECTION_PLAN` / `COLLECTION_RUN_DIR`を指定する。
   **単一replica + 永続volume必須**。入力・対象・費用見積りと、1会話あたりの`max_model_calls: 12`、`max_tool_calls: 24`を確認する。
5. 現行azdのflags/内部retry動作を検証した後、`scripts\collect.py --prepare-invocation`で
   version付きagent endpointと1入力を固定し、`--invoke`で1回送る。
   このwrapperの詳細CLIは下記referenceに記載。実際のcaptureファイルを別途回収する。

`main.py --help`とstageはSDK不要です。`main.py`はplanと保存先を読み、送信を記録します。
成否不明の要求を自動再送しません。data/eval/cases/oracleはimageへ含めません。
templateの依存はrootのOpenAI 3/Projects 2.5系列とは別です。

詳細、停止条件、SDK差、capture形式、実行コマンドは
[cloud-execution.md](../../docs/reference/cloud-execution.md)を参照してください。
