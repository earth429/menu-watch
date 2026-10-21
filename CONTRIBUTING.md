# 開発と検証

Python 3.11以上を使います。実行・テストとも標準ライブラリのみで動作します。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q menu_watch.py scripts tests
python3 scripts/update_schedule.py --check
```

GitHubのCIではPython 3.11と3.14で確認します。
設定の頻度・時刻を変更した場合は `python3 scripts/update_schedule.py` でworkflowも更新します。

## 構成

| ファイル | 役割 |
| --- | --- |
| `menu_watch.py` | 設定・取得・抽出・通知・履歴管理・CLI |
| `menu-watch.toml` | スケジュール、取得上限、通知形式 |
| `scripts/update_schedule.py` | 日本時間の設定をActionsのUTC cronへ反映 |
| `.github/workflows/notify-bampire.yml` | 定期・手動通知 |
| `tests/` | オフラインテストと必要最小限の公開投稿データ |

抽出の変更には、期待する正例だけでなく、誤って通知してはいけない負例も追加してください。
実投稿のデータは元投稿ID・取得元・必要なフィールドを明記し、プロフィール・画像など不要な情報を含めません。
合成データは実際の投稿と区別します。テスト中に外部APIやDiscordへ通信しないよう、I/Oはモックを使います。

通知履歴が欠損した場合に空で続行しないこと、予約の保存前に送信しないこと、
結果不明の送信を自動でやり直さないことを維持してください。
新しい対象アカウントを追加する場合は、抽出ルール、投稿者ID、履歴・Environmentの分離も設計します。

認証情報やWebhook URLは、コード・テストデータ・Issue・ログに含めないでください。
