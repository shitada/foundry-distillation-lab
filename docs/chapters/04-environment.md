# 04 環境・予算・安全な停止

## 現在地
オフライン実行環境を作ります。Azureのリソース作成や推論を始める段階ではありません。

## 目的
再現可能な環境と、支出・再送・削除を制御する手順を用意します。

## 前提
Windows / PowerShell、Python 3.13で確認する構成です。パッケージ宣言は3.11以上ですが、全版の確認済みという意味ではありません。初期インストールにはパッケージ取得の通信が生じ得ます。

## 実施内容
リポジトリのルートで次を実行します。PowerShellの実行ポリシーを緩めず、仮想環境のPythonを直接指定できます。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\collect.py --help
.\.venv\Scripts\python.exe scripts\train.py --help
.\.venv\Scripts\python.exe scripts\deployment.py --help
```

live実行の前に、次の表を別途承認します。設定例の存在は承認ではありません。

| 項目 | 固定する内容 |
|---|---|
| 対象 | subscription/project、region、モデル/version、deployment、tier |
| 入力 | ファイルhash、contract hash、件数、反復、最大step |
| 学習費 | 生成・学習・評価を含む小規模試行上限、本実験上限を分ける |
| 推論費 | teacher/base/fine_tuned別件数、時間・token上限、回復予備費 |
| 固定費 | hosting、agent、tool/log/storage、保持時間 |
| 承認 | 対象・入力・予算・期限・実行者に紐付けた明示承認 |
| 停止・照合 | 送信前journal、成否不明時の照合、再送禁止条件 |
| 所有権 | このrunが作成した資源と既存資源を区別した一覧 |

オフライン検証とネットワークpreflightを区別します。認証はSDKに合うDefaultAzureCredentialを基本にし、APIキーを教材やログへ保存しません。Preview経路、モデル利用可否、quota、SDK majorは実行前に公式資料と照合します。コントロール側とHosted Agentの依存を無理に一環境へ統合しません。

具体的なoperation、承認入力・対象、SDK分離と未検証点は[クラウド実行境界](../reference/cloud-execution.md)を確認します。Hosted skeletonのprotocol、コンテナー、SDKの内部HTTP hook、永続volume、identityは未検証です。現設計のjournal/予算は分散lockではないため、追試でも**永続書込先と単一replica**が必要です。ARMの条件付き更新/削除headerのサービス上の保証も未検証で、確認前の共有資源実行はブロック対象です。

将来の`collect.py --invoke`が起動するazdにも独立した検証が必要です。wrapperの一回起動と、azd内部のHTTP送信回数は同じ保証ではありません。SDK側のretry無効化をazdにも適用済みと誤認せず、拡張のflags・raw出力・内部retryを確認してから別承認へ進みます。

## 出力
Python/SDK版、設定hash、試行計画、承認の所在、所有資源一覧、停止・cleanup手順を非公開runへ保存します。

## 解釈
SDK importや`--help`の成功は認証・配置・課金停止の確認ではありません。ローカル監視の期限切れでクラウドの学習やhostingが自動停止するとは限りません。

## 完了条件
無資格情報でオフラインテストが動き、有料処理には別承認が必要だと説明できます。cleanupはrunが所有すると証明できる対象だけに限定し、既存teacherを削除しません。

## 限界
本版ではクラウド通し実行は未検証です。live確認でSDKや契約を修正するときは、版を記録して新しいrunを開始し、途中の条件をすり替えません。
