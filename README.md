# menu-watch

公式Xアカウントの新メニュー告知を取得し、商品名・価格と元投稿のリンクをDiscordへ通知します。
GitHub ActionsとDiscord Webhookで動作し、常駐サーバーやDiscord Botアカウントは不要です。

現在は晩杯屋公式アカウント（[@bampirePR](https://x.com/bampirePR)）の共通告知に対応しています。
晩杯屋・X・Discord・FxEmbedの公式サービスではありません。

```text
晩杯屋 新メニュー
・ヤゲン軟骨　200円
元投稿：https://x.com/bampirePR/status/2112689033075929439
```

## 動作

- 公開APIから投稿を取得し、「晩杯屋新メニュー」の定型見出しがある告知を抽出します。
- 店舗別のおすすめ、店舗限定商品、返信・リポスト・引用は通知しません。
- 復活メニューも対象です。商品名と価格の対応が曖昧な投稿や、未対応の形式は見送ります。
- 初期設定は**毎日15:17（日本時間）**。新しい告知がなければ何も送りません。
- 元投稿へ移動できるカードを表示し、本文や取得できた写真を載せます。
- 通知を始めた時刻以降、かつ過去48時間の投稿を確認し、同じ投稿の重複送信を防ぎます。

定型見出しを共通告知の目印として使います。全店舗での提供、税込・税別、販売開始日は推測しません。
Xの有料APIは使いませんが、無料APIやGitHub Actionsの稼働・取得範囲によって取りこぼす場合があります。
投稿APIの一時エラーは取得だけ1回再試行します。通知送信は自動で繰り返しません。

## 必要なもの

- GitHubアカウントと、Actionsを実行できるリポジトリ
- 通知先のDiscordテキストチャンネルと、そのWebhookを作成できる権限
- ローカルで確認・設定変更する場合はPython 3.11以上（追加パッケージ不要）

## セットアップ

### 1. リポジトリを用意する

このリポジトリをForkするか、ファイルを自分のリポジトリに配置します。
Forkした場合はGitHubの **Actions** タブからworkflowを有効にしてください。

ローカルで編集する場合：

```sh
git clone https://github.com/earth429/menu-watch.git
cd menu-watch
python3 -m unittest discover -s tests -v
```

### 2. Discord Webhookを登録する

テスト用と本番用のWebhookを、それぞれDiscordの通常のテキストチャンネルに作成します。
GitHubリポジトリの **Settings → Environments** に次の2つを作成し、各EnvironmentへSecretを登録します。

| Environment | Secret名 | 値 |
| --- | --- | --- |
| `menu-watch-test` | `DISCORD_WEBHOOK_URL` | テスト用Webhook URL |
| `menu-watch-prod` | `DISCORD_WEBHOOK_URL` | 本番用Webhook URL |

Environmentのdeployment branchはデフォルトブランチに制限してください。
Webhook URLはチャンネルへ投稿できる認証情報です。コードやIssueには書かず、EnvironmentのSecretだけに保存します。
repository共通の同名Secretは設定しません。フォーラム・スレッド用Webhookは未対応です。

### 3. 送信せずに候補を確認する

**Actions → 晩杯屋の新メニュー通知 → Run workflow** を開き、デフォルトブランチから実行します。
最初は `mode=dry-run`、`destination=bampire-test`、`history=none` を選びます。
ログに通知候補と判定理由が出ます。この操作ではDiscordへの送信も履歴更新も行いません。

### 4. テスト用チャンネルで確認する

`mode=test-webhook`、`destination=bampire-test` で実行すると、接続確認メッセージを1件送信します。
この操作は手動専用で、投稿の通知履歴は変更しません。再実行すると、もう1件テストメッセージを送信します。

投稿の通知も試す場合は、`mode=init`、`destination=bampire-test` で履歴を初期化します。
その時刻より後に出た対象告知を、`mode=notify`、`destination=bampire-test` で通知できます。
`init` 自体は何も送らず、過去の投稿をまとめて通知しません。

### 5. 本番の定期通知を有効にする

1. 本番用Secretの登録後、`mode=init`、`destination=bampire-prod` を一度実行します。
2. 成功したら **Settings → Secrets and variables → Actions → Variables** に、
   Repository Variable `MENU_WATCH_ENABLED` を値 `true` で作成します。

設定した頻度で、本番への通知が始まります。停止する場合は変数を `false` に変更します。
手動で `mode=notify`、`destination=bampire-prod` を実行することもできます。
初期化時はWebhookが有効か、投稿せずに確認します。履歴の初期化は既存ブランチを上書きしません。

### 特定の投稿を手動で通知する

初期化前の告知も1件だけ送りたい場合は、Run workflowの `post_id` に元投稿URL末尾の数値IDを入力します。
最初に `mode=dry-run`、通知先と `history=github` を選んで内容を確認し、送る場合は `mode=notify` で同じIDを指定します。

明示した1投稿だけを個別取得し、通常と同じ公式アカウント・共通告知・対象期間・通知済み履歴の確認を行います。
この操作だけは初期化前の投稿も対象にできますが、確認期間外や対象外の投稿は送れません。
送信前後に通常と同じ履歴へ記録するため、手動の再実行や定期実行でも同じ投稿を再送しません。
開始時刻や定期通知の範囲は変更されません。通常の定期実行では `post_id` は使いません。

すでに送った投稿のカードを更新する場合は、`mode=refresh-preview` と `post_id` を指定します。
履歴に記録した同じDiscordメッセージを編集し、再投稿や通知履歴の変更は行いません。
確認期間内の対象投稿で、送信成功したメッセージがある場合だけ利用できます。

## 設定を変更する

[menu-watch.toml](menu-watch.toml) に実行設定をまとめています。

| 設定 | 初期値 | 内容 |
| --- | --- | --- |
| `schedule.frequency` | `daily` | `daily`は1日1回、`hourly`は毎時 |
| `schedule.hour_jst` / `minute` | `15` / `17` | 日本時間の時・分。毎時の場合は分だけ使用 |
| `watch.include_revivals` | `true` | 復活メニューを含める |
| `watch.lookback_hours` | `48` | 何時間前まで確認するか（1〜168） |
| `watch.max_pages` | `3` | 取得するページ数の上限（1〜5） |
| `watch.max_notifications` | `3` | 1回の通知投稿数の上限（1〜3） |
| `notification.style` | `summary` | 商品名・価格＋リンク。`original`なら投稿本文＋リンク |
| `notification.link_preview` | `true` | 元投稿へのリンク付きカードを表示。`false`で非表示 |

`original`も画像の転載ではなくテキストです。どちらの形式でも、対象判定と価格抽出を通った投稿だけを通知します。
形式を変えても通知済み投稿は再送しません。対応する投稿の範囲は現在 `watch.scope = "common"` のみです。
カードには検証した公式投稿の本文と、取得できた最初の写真を使います。
画像のない投稿はテキストのカードになり、長い本文はカード内だけ省略します。

頻度や時刻を変えたら、次のコマンドでworkflowのcronにも反映し、TOMLとworkflowの両方をコミットします。

```sh
python3 scripts/update_schedule.py
```

毎時17分にする場合は `frequency = "hourly"` に変更して実行します。
CIが設定とworkflowの一致を確認します。TOMLだけの編集では、GitHub側の起動頻度は変わりません。

## ローカルで確認する

```sh
# オフラインテスト
python3 -m unittest discover -s tests -v

# 保存したデータを再生（外部通信なし）
python3 menu_watch.py dry-run --fixture tests/fixtures/bampire/timeline-2026-10-21.json --now 2026-10-21T14:00:00+09:00

# 公開APIから現在の候補を取得（送信・履歴更新なし）
python3 menu_watch.py dry-run

# スケジュール設定の整合性
python3 scripts/update_schedule.py --check
```

`dry-run` が既定の動作です。既存履歴を参照するには `--state github --destination bampire-prod` を指定し、
`GITHUB_REPOSITORY=所有者/リポジトリ名` と読み取り権限のある `GH_TOKEN` を環境変数に設定します。
ローカルからの `notify`・`init` は書き込み権限のあるトークンとWebhookの環境変数も必要です。
通常の運用では、これらの認証情報をGitHub Actionsが渡します。

## 制約と運用

通知前に送信予約をリモートへ保存し、予約したIDは自動再送しません。
重複する送信試行を防ぐため、予約後の停止やタイムアウトでは取りこぼしを許容します。確実な配信を保証する仕組みではありません。

GitHub Actionsの定期実行は遅延・実行漏れがあり、公開リポジトリでは非活動により停止する場合があります。
確認期間やページ上限を超える過去投稿は自動回収しません。
運用中はActionsの実行結果とDiscordの通知を確認してください。

履歴の構造、障害時の扱い、Webhook変更手順は [運用ガイド](docs/operations.md) を参照してください。
開発・検証手順は [CONTRIBUTING.md](CONTRIBUTING.md) にまとめています。

## 参照

- [FxEmbed 投稿一覧API](https://docs.fxembed.com/api/twitter/operations/2profilehandlestatuses/)
- [Discord Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook)
- [GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
