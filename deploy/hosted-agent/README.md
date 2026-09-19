# Hosted retail trace collector（cloud未検証）

元デモのResponsesHostServer / FoundryChatClient方式を小売packageへ接続した
**追試用skeleton**です。配置済みサービスでも、クラウド動作確認済み実装でもありません。

1. rootから`python deploy\hosted-agent\stage.py --output runs\hosted-build`でallowlistだけをstageする。
2. `staging-manifest.json`を確認する。rootのMIT `LICENSE`もstage/imageへ保持する。rootをbuild contextにしない。
3. 別途承認した環境でのみ専用requirementsを導入し、image/managed identity/Hosted入力/永続volumeを検証する。
4. collection plan/approvalをmountし、`COLLECTION_PLAN` / `COLLECTION_APPROVAL` /
   `COLLECTION_RUN_DIR`を指定する。**単一replica + 永続volume必須**。
5. 現行azdのflags/内部retry動作を検証した後、`scripts\collect.py --prepare-invocation`で
   version付きagent endpointと1入力を固定し、別承認の`--invoke --execute --approval ...`で送る。
   このwrapperの詳細CLIは下記referenceに記載。実際のcaptureファイルを別途回収する。

`main.py --help`とstageはSDK不要です。main起動には`--execute`と`collect`承認が必要です。
containerのCMDもこのguardを迂回しません。data/approval/eval/cases/oracleはimageへ含めません。
templateの依存はrootのOpenAI 3/Projects 2.5系列とは別です。

詳細、停止条件、SDK差、capture形式、実行コマンドは
[cloud-execution.md](../../docs/reference/cloud-execution.md)を参照してください。
