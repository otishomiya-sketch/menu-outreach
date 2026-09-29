# Menu Photo Pro 営業アプリ（menu-outreach）

全国の飲食店を集めて、**Instagramは頻繁に更新しているのに写真が暗い・古い店**を上位に並べ、
Instagram DM・LINE・公開メールで Menu Photo Pro を案内し、反応を見ながら文面を自動で改善します。

| 工程 | 中身 | 自動化 | 元にしたスキル |
|---|---|---|---|
| ①集める | Googleマップ（Apify）で「地域×業種」の店を収集。公式サイトからメール・Instagram・LINE・オーナー名を補完。「営業お断り」表示の店はメール対象外 | 全自動 | maps-outreach / gmaps-lead-scraper |
| ②調べる | Instagramの直近投稿から更新頻度（30日の投稿数・最終投稿日）と写真の弱点（暗さ・黄ばみ・メリハリのなさ・ピンボケ）を測定し、見込み度（0〜100）を算出 | 全自動 | insta-autopost-kit（Graph API）＋新規 |
| ③送る | 見込み度順に1店1チャネルで送信リストを作成。**メールは自動送信**、**Instagram DM・LINEはダッシュボードでコピー→開く→送信を押すだけ**（1件10秒） | メール全自動 / DM・LINE半自動 | maps-outreach / contact-form-sender を改修 |
| ④改善 | 文面ごとの反応率を集計し、良い文面を多めに出し分け（トンプソン・サンプリング）。負けた文面は自動で引退、Claude が次の文面を提案 → 承認したものだけ配信 | 全自動＋承認 | self-improving-agent-os |

DM・LINEを自動送信にしていないのは、Instagramの規約違反でアカウント凍結や商品URLのブロックを避けるためです。

## セットアップ

```bash
cd menu-outreach
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # APIキーを自分で記入
.venv/bin/python cli.py init
```

`config/settings.yaml` で最低限ここを埋めます。
- `sender.company` / `sender.address` / `sender.email`：**メール送信に必須**（特定電子メール法の表示義務）。空の間はメールを送りません
- `collect.areas` / `collect.keywords`：最初は1〜2エリアで試す（Apifyは件数に応じて課金）

## 毎日の使い方

```bash
.venv/bin/python cli.py collect      # ①収集（初回・エリアを増やすとき）
.venv/bin/python cli.py enrich       # ②Instagram解析とスコア付け
.venv/bin/python cli.py daily        # 返信の取り込み → 今日の送信リスト作成 → メール送信
.venv/bin/python cli.py serve        # ダッシュボード http://127.0.0.1:8765
```

ダッシュボードでは次のことができます。
1. **Instagram DM / LINE**：「①コピーして開く」→ 貼り付けて送信 →「②送信した」。キーボードの `C` / `S` でも操作できます
2. **店舗リスト → 店名**：返信あり・無料体験・有料契約・お断り・停止希望を記録（DM・LINEの返信はここで手入力）
3. **改善**：文面ごとの成績の確認、提案文面の承認、振り返りの実行

## 安全装置

- 1日の上限：Instagram 25件 / LINE 20件 / メール 100件（`channels.daily_limit`）。Instagram は30件を超えないでください
- 1店舗に送るのは最大2回まで。2回目は14日間反応がないときだけ、別のチャネルから送ります
- お断り・停止希望・「配信停止」の返信があった店には、どのチャネルからも二度と送りません
- メールは `email_live: false`（ドライラン）が初期値です。文面を確認してから `true` にするか、`cli.py email --live` で送信します
- メールには送信者名・住所・配信停止の方法を自動で入れます
- 自動提案された文面は、機械チェック（差し込み漏れ・事実の改変・誇大表現・長さ）を通ったうえで、承認したものだけが配信されます

## 成果の計測（任意・推奨）

`service.append_ref: true` にすると、お試しURLの末尾に `&ref=店舗コード-チャネル文面ID` が付きます（例：`ref=a1b2c3d4-in1`）。
Menu Photo Pro 側で `st.query_params.get("ref")` を記録すれば、どの店がどの文面で無料体験を始めたかを突き合わせられます。

## Instagram解析の取得元

- **推奨：Business Discovery API（公式）**。`IG_BD_USER_ID` と `IG_BD_ACCESS_TOKEN` を使います。Facebookページに紐づけたプロアカウントで、Facebookログイン方式のトークンが必要です。insta-autopost-kit の「Instagram業務用ログイン」トークンでは動きません。1時間に約200件まで取得できます
- **予備：Apify `instagram-profile-scraper`**。上の2つが空のときに使います

## 本番環境（Railway）

GitHub の `main` に push すると Railway に自動でデプロイされます。データは Railway のボリューム（`/data/outreach.db`）に保存されます。
Railway の Variables で設定する値:

| 変数 | 内容 |
|---|---|
| `DASHBOARD_PASSWORD` | **必須**。未設定だとダッシュボードは開きません（ユーザー名は `otis`、`DASHBOARD_USER` で変更可） |
| `SENDER_COMPANY` / `SENDER_ADDRESS` / `SENDER_EMAIL` / `SENDER_CONTACT` | メールの送信者表示。リポジトリが公開なので、settings.yaml ではなくここに書く |
| `APIFY_API_TOKEN`、`IG_BD_*`、`SMTP_*`、`IMAP_*`、`ANTHROPIC_API_KEY` | `.env.example` と同じ |
| `EMAIL_LIVE` | `true` でメールを本番送信（初期値 `false`） |
| `AUTO_DAILY_AT` | 例 `09:30`。毎日この時刻に「毎日の実行」を自動で動かす（月曜は振り返りも）。未設定なら自動実行しない |
| `COLLECT_AREAS` | 収集エリア（カンマ区切り）。settings.yaml より優先 |

収集・解析・毎日の実行・振り返りは、ダッシュボードの「実行」画面から動かせます。
