# 出典とライセンス

この教材は Microsoft の公式教材ではありません。元のデモを基礎に、
日本語の小売サポート、評価・費用計算と章別ガイドを構成した派生教材です。

## 基になったリポジトリ

- [microsoft-foundry/fine-tuning](https://github.com/microsoft-foundry/fine-tuning)
- [TracesDistillation（参照元固定版）](https://github.com/microsoft-foundry/fine-tuning/tree/2de8f491026472358919b35e473ab42393a686a7/Demos/TracesDistillation)
- [日本語化済みforkの基準版](https://github.com/shitada/fine-tuning/tree/7a06f181eba475e05e608924de8e84d79a1c5978/Demos/TracesDistillation)

原著作権表示は `Copyright (c) 2025 Azure AI Foundry`、ライセンスはMITです。
元リポジトリの参照commitに収録された `LICENSE` を確認し、全文を本リポジトリの
`LICENSE` に保持しています。これは参照元リポジトリの最新版を確認したという意味ではありません。
MITは複製・改変・頒布・商用利用を認め、著作権表示と許諾文の保持を条件とします。

日本語のインストラクション、ツール仕様、架空商品の名称・金額・ポリシーは、
元デモを日本語化したforkを再利用しています。Function名やprotocol識別子は維持します。
`synthetic_store.py` とcontractは移植時に内容を変更せず、会話状態分離は外側のwrapperで行っています。

整形・評価・学習・費用計算の実装は、同forkで行った実験の仕組みを参照し、
固定環境・過去runへの依存を除去して再構成しています。
未commitの実験資産は参照元リポジトリのcommitに含まれるとは表現せず、
`docs/reference/provenance.json` に採用時のファイルhashと変更区分を記録します。

## 公開サンプルと本文

今回追加したコード・教材本文・スクリプト生成した架空サンプルもMITで提供します。
将来出版する書籍そのものの利用条件は本リポジトリから自動的には定まりません。
過去の個人環境のrun、生ログ、認証状態、学習済みモデルを再配布していません。
別ライセンスの他デモ・サンプルデータセットも移植していません。

依存ライブラリは各配布元のライセンスに従います。モデルやFoundryサービスの利用条件、
モデル出力を学習・公開する権利は、このコードのMIT Licenseとは別に確認してください。
出典URLを掲示することは、元の許諾文を保持する義務の代わりにはなりません。
