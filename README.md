# Menu Photo Pro 営業アプリ（menu-outreach）

全国の飲食店を集め、**Instagramをよく更新しているのに写真が暗い・古い店**を上位に並べて、
Instagram DM・LINE・メールで Menu Photo Pro を案内するアプリです。反応を記録しながら、文面を自動で改善します。

| 工程 | 内容 | 自動化 |
|---|---|---|
| ①収集 | Googleマップ（Apify）で「エリア×業種」の店を集め、公式サイトからメール・Instagram・LINE・オーナー名を拾う。「営業お断り」表示の店はメール対象外 | 全自動 |
| ②解析 | Instagramの直近投稿から更新頻度と写真の弱点（暗さ・黄ばみ・ピンボケ・メリハリ）を測り、見込み度（0〜100）を付ける | 全自動 |
| ③送信 | 見込み度の高い順に1店1チャネルで送る。**メールは自動送信**、**Instagram DM・LINEは画面でコピー→開く→送信**（規約上、自動送信はしない） | メール全自動 / DM・LINE手動 |
| ④改善 | 文面ごとの反応率を集計し、良い文面を多めに出し分ける。負けた文面は自動で止め、Claude が次の文面を提案（承認したものだけ配信） | 全自動＋承認 |

## 本番環境（Railway）

- URL: https://web-production-2defbe.up.railway.app （ユーザー名 `otis` / パスワードは `DASHBOARD_PASSWORD`）
- `AUTO_DAILY_AT=09:30` により、毎朝「全自動」（返信取り込み → 収集 → 解析 → 送信リスト → メール → 通知）が動く。月曜は振り返りも
- データは Railway のボリューム `/data/outreach.db` に保存
- デプロイ: `railway up`（GitHub 連携の自動デプロイが有効ならプッシュでも反映）

### 環境変数（Railway の Variables）

| 変数 | 内容 |
|---|---|
| `DASHBOARD_PASSWORD` | **必須**。未設定だとダッシュボードは開かない |
| `SENDER_COMPANY` / `SENDER_ADDRESS` / `SENDER_EMAIL` / `SENDER_CONTACT` | メールの送信者表示（公開リポジトリには書かない） |
| `APIFY_API_TOKEN` | **必須**。店舗の収集と Instagram 解析 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | メール送信。`SMTP_USER` は `SENDER_EMAIL` と同じにする |
| `POP3_HOST` / `POP3_PORT`（または `IMAP_HOST`） | 返信・配信停止の自動取り込み |
| `EMAIL_LIVE` | `true` でメールを本番送信（初期値はドライラン） |
| `EMAIL_DAILY_LIMIT` など `<チャネル>_DAILY_LIMIT` | 1日の送信上限を上書き（新しいアドレスは少なめから） |
| `AUTO_DAILY_AT` | 毎朝の全自動の時刻。未設定なら自動実行しない |
| `AUTO_SEARCHES_PER_DAY` / `AUTO_ENRICH_LIMIT` | 毎朝の収集数（エリア×業種の組数）と解析数 |
| `COLLECT_AREAS` | 「実行」画面の収集エリアの初期値 |
| `DISCORD_WEBHOOK_URL` または `LINE_CHANNEL_ACCESS_TOKEN` + `LINE_TO_USER_ID` | 毎朝の通知 |
| `ANTHROPIC_API_KEY` | 任意。文面の自動提案・写真のAI採点 |
| `IG_BD_USER_ID` / `IG_BD_ACCESS_TOKEN` | 任意。Instagram 公式API（Business Discovery）。未設定なら Apify で代用 |
| `TZ=Asia/Tokyo` / `MENU_OUTREACH_DB=/data/outreach.db` | 設定済み |

## ダッシュボード

| 画面 | できること |
|---|---|
| ホーム | 今日の送信数・残り、全体の成果。「今日の送信リストを作る」 |
| Instagram DM / LINE | 「①コピーして開く」→ 貼り付けて送信 →「②送信した」（キーボード `C` / `S`） |
| メール | 本番送信の設定チェック（✓/✗）、送信状況、件名と本文の確認 |
| 店舗リスト | 見込み度順の一覧。店を開いて反応（返信・無料体験・有料・お断り・停止希望）を記録 |
| 改善 | 文面ごとの成績、提案文面の承認・停止、仮説の履歴 |
| 実行 | 全自動・①収集・②解析・③送信だけ実行・④振り返りを今すぐ動かす。ログとエラーを表示 |

## 安全装置

- 1日の上限: Instagram 25 / LINE 20 / メール 100。1店への送信は最大2回、2回目は14日後に別チャネルから
- お断り・停止希望・「配信停止」の返信があった店には、どのチャネルからも送らない
- メールは送信者名・住所・配信停止方法を自動で付ける。送信元と SMTP ユーザーが違えば送らない
- 自動提案の文面は機械チェック（差し込み漏れ・事実の改変・誇大表現・長さ）と承認を通ったものだけ配信

## 手元で動かす

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py demo       # APIキーなしで試す（架空の30店。data/demo.db）
.venv/bin/python cli.py --help     # コマンド一覧（init / collect / enrich / plan / email / inbox / daily / auto / reflect / serve）
```

キーを使うときは `.env.example` を `.env` にコピーして記入します。

## 構成

```
app/
  core.py      設定の読み込み（settings.yaml ＋ 環境変数）・DB・共通処理
  collect.py   ①収集（Apify Googleマップ ＋ 公式サイトのクロール）
  enrich.py    ②解析（Instagram の更新頻度）と見込み度の計算
  photo.py     写真の弱点度（Pillow。任意で Claude の採点）
  outreach.py  ③文面の作成・送信計画・メール送信・返信の取り込み
  bandit.py    文面の出し分け（トンプソン・サンプリング）
  improve.py   ④振り返りと文面の提案
  jobs.py      裏で動く処理と毎朝の自動実行・通知の呼び出し
  notify.py    Discord / LINE 通知
  web.py       ダッシュボードのルーティング（HTML は app/templates/）
  wsgi.py      本番の入口（Procfile から起動）
  demo.py      デモデータ
config/settings.yaml   動作設定（上限・スコアの重み・収集の業種など）
templates/variants.yaml 初期の営業文面
```

元にしたスキル: maps-outreach（収集・フォーム送信）、insta-autopost-kit（Instagram API）、self-improving-agent-os（検証→仮説→選択のループ）。
