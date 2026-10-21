# 運用ガイド

## 通知の流れ

```text
投稿取得 → 対象判定 → 通知履歴を確認
→ GitHubに送信予約を保存 → Discordへ1回送信 → 結果を保存
```

取得をすべて完了してから対象を選び、投稿日時の古い順に送信します。
初期値は最大3ページ・3投稿／回です。上限を超えた候補は未処理のまま残り、次回も対象期間内なら処理します。
本文の訂正や通知形式の変更で、同じ投稿IDを再通知することはありません。

手動の `--post-id`（workflow入力 `post_id`）は、明示した1件に限って初期化前の投稿も対象にします。
確認期間・投稿者・共通告知・重複防止の条件は維持し、送信予約に `manual: true` を記録します。
通常の開始時刻は書き換えません。

## 履歴

通知先ごとに、同じリポジトリの専用ブランチへ `state.json` を保存します。

| 通知先 | Environment | 履歴ブランチ |
| --- | --- | --- |
| `bampire-test` | `menu-watch-test` | `menu-watch-state-bampire-test` |
| `bampire-prod` | `menu-watch-prod` | `menu-watch-state-bampire-prod` |

履歴はスキーマバージョン、投稿者ID、通知先の別名、Webhook ID、開始時刻と投稿ごとの試行結果を持ちます。
Webhook URL・トークン・投稿本文は保存しません。公開リポジトリでは、履歴ブランチの情報も公開されます。
履歴には512KiBの読み書き上限があり、自動削除はしません。

| 状態 | 意味 | 自動再送 |
| --- | --- | --- |
| `reserved` | 送信予約を確定。送信前の停止や結果保存失敗の可能性あり | しない |
| `sent` | DiscordメッセージIDを取得して記録済み | しない |
| `unknown` | 送信が失敗、または到達したか不明 | しない |

履歴の読み取り失敗・破損・ブランチ消失では停止します。空の履歴として続行したり、自動初期化したりしません。
`init` はWebhookの有効性を読み取り専用で確認してから、現在時刻を開始時刻として新規ブランチを作る操作です。
既存ブランチがある場合は拒否します。

## 並行実行と障害

workflowのconcurrencyで定期・手動・初期化の実行を直列化します。
GitHub Git Data APIでは、読み込んだコミットを親とする変更を `force: false` で確定します。
競合する更新やブランチ消失はエラーとし、送信予約を保存できなければDiscordを呼び出しません。
予約にはランダムな試行IDを付け、同時刻の別実行を区別します。

- HTTP通信に自動リトライやリダイレクトはありません。
- 通常の通信は各15秒、GitHub APIは各10秒、レスポンスは最大2MiBです。
- 通知workflow全体の上限は8分です。
- 429・5xx・不正JSON・不正構造・cursor循環は送信前に停止します。
- 送信タイムアウトや結果保存失敗でも、先に確定した予約が次回の再送を防ぎます。

APIの取得失敗は、次の定期実行で取得し直します。すでに送信予約したIDは対象外です。
`reserved` や `unknown` を調べる場合は、Actionsログ・Discordの実際の投稿・履歴を照合してください。
履歴の削除や開始時刻の巻き戻しは、重複や取りこぼしにつながります。

## 判定と通知の制限

対象アカウントの数値ID・ハンドル、投稿形式・日時、定型見出しを検証します。
店舗限定・予告・終了・訂正、画像だけの価格、対応が曖昧な複数商品は見送ります。
未対応の表記には、数字を含む商品名、税表記や単価注記、複数の価格段落などがあります。
元投稿へのURLは検証したIDから組み立てます。

Discordへの本文はMarkdownをエスケープし、`allowed_mentions` でメンションを無効にします。
2000 UTF-16単位を超えるメッセージは見送ります。送信は `wait=true` で結果を確認します。
URLは `https://discord.com/api[/v10]/webhooks/...` の標準形式だけを許可し、クエリやフラグメントは受け付けません。

手動の `test-webhook` はテスト先だけに、明示的な接続確認メッセージを送ります。
投稿通知の履歴は変更せず、再実行するともう1件送信します。

## 元投稿のカード

`notification.link_preview = true` では、検証した元投稿のURL・本文・投稿時刻をカードとして送ります。
最初の有効な写真があれば表示します。画像URLは `https://pbs.twimg.com/media/` の形式だけを許可します。
カード本文は3000 UTF-16単位で省略し、メッセージ全体とカードの文字数上限内に収めます。
`style=original` では本文を重複させず、カードはリンクと画像を中心に表示します。

手動の `refresh-preview --post-id 投稿ID` は、送信履歴のDiscordメッセージIDを使って既存カードを更新します。
同じWebhookのメッセージで元投稿URLが一致することを確認し、本文と送信履歴を保ったままPATCHします。
新しいメッセージは作りません。失敗時の自動リトライもありません。

## Webhookを変更するとき

同じWebhook IDのトークンを再発行した場合は、対応するEnvironmentのSecretを更新できます。
別のWebhook IDへ変えると、初期化済み履歴との不一致で停止します。
別チャンネルへの移行は、定期実行を無効にし、既存履歴と移行先を確認してから行ってください。

## ログと権限

通常のエラーは認証情報やレスポンス本文を含まない短いメッセージです。
dry-runでは投稿ID・通知候補・判定理由を表示します。
`heading` は見出し対象外、`outside-window` は期間外、`already-reserved` は予約済み、
`incomplete-price-block` などは抽出できない形式を表します。

PRのCIはオフラインで、Webhook Secretは渡しません。
通知workflowはデフォルトブランチからだけ実行し、dry-runは `contents: read`、
初期化・送信jobだけ `contents: write` を要求します。外部Actionsは完全なコミットSHAに固定しています。

## 関連資料

- [GitHub Git references API](https://docs.github.com/en/rest/git/refs#update-a-reference)
- [GitHub Git trees API](https://docs.github.com/en/rest/git/trees#create-a-tree)
- [GitHub Actionsのトラブルシューティング](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
