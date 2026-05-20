# 日本小型印トラッカー

毎日、日本郵便の公式サイトから新しい小型印情報を取得し、Discord に通知します。

## セットアップ

```bash
# 依存関係をインストール
pip install -r requirements.txt

# 設定ファイルをコピーして Discord Webhook URL を記入
cp .env.example .env
# .env を編集して DISCORD_WEBHOOK_URL を設定

# 初回実行（既存データを取り込む、通知は送られない）
python tracker.py
```

## Discord Webhook の作成方法

1. Discord サーバーの **設定 → 連携サービス → ウェブフック**
2. 「新しいウェブフック」をクリック
3. 通知を送りたいチャンネルを選択してコピー
4. `.env` の `DISCORD_WEBHOOK_URL` に貼り付け

## 毎日自動実行（cron）

```
# 毎朝 9:00 に実行（crontab -e で設定）
0 9 * * * cd /Users/kongziqing/Desktop/project/kogata-tracker && /usr/bin/python3 tracker.py >> logs/tracker.log 2>&1
```

## ファイル構成

| ファイル | 説明 |
|---|---|
| `tracker.py` | メインスクリプト |
| `state.json` | 既知の小型印 ID を保存（自動生成） |
| `.env` | Discord Webhook URL などの設定 |
| `logs/tracker.log` | 実行ログ（cron 設定時） |

## 通知内容

各小型印について以下の情報を Discord に送信します：

- 記念事項名称
- 使用期間
- 郵便局名・住所
- 開設場所・時間
- 備考
- 図案画像（サムネイル）
