# 引き継ぎ仕様書：Instagram分析の公式API切り替え（運用経費の削減）

最終更新：2026-10-07　／　対象：営業アプリ menu-outreach（Menu Photo Pro の新規開拓システム）

---

## 1. 目的

営業アプリの毎朝の「Instagram分析」を、有料の Apify（`apify/instagram-profile-scraper`）から、
**無料の Instagram 公式API（Graph API の Business Discovery）** に切り替え、Apify の利用料を減らす。

- 分析の精度は落とさない（取得する情報は同じ：名前・自己紹介・リンク先・投稿日時・写真）
- 公式APIで取れない店（個人アカウント等）だけ Apify で取る「併用」にする
- 公式APIが使えなくなっても分析が止まらないようにする（自動で Apify に戻る）

---

## 2. 現状（2026-10-07 時点）

### 2-1. 進み具合

| # | 作業 | 状態 |
|---|---|---|
| 1 | 費用の実測（Apify の請求内訳） | ✅ 完了（3章） |
| 2 | アプリ側の切り替え機能・安全装置の実装 | ✅ 完了・本番反映済み（コミット `42148ec`） |
| 3 | Meta のアプリ作成 | ✅ 完了（App ID `1836759181072844`） |
| 4 | システムユーザーとトークンの作成（60日） | ✅ 完了（トークンは加藤あやかさんが保管） |
| 5 | Instagram アカウントIDの取得 | ✅ 完了（`17841448043763697`） |
| 6 | Railway に `IG_BD_USER_ID` を設定 | ✅ 完了 |
| 7 | Railway に `GRAPH_API_VERSION=v26.0` を設定 | ✅ 完了 |
| 8 | Railway に `IG_BD_ACCESS_TOKEN` を設定して Deploy | ✅ 完了（2026-10-07 17:22、権限6つのトークン） |
| 9 | 切り替えの動作確認 | 🔶 本物のトークンで1店ずつの取得は成功。毎朝の自動実行での確認は 10/8 朝 |
| 10 | DM 開始時刻の変更をスタッフに周知（10時半 → 11時） | ⬜ **未完了（次にやること）** |

**2026-10-07 17:22 のデプロイから公式APIに切り替わった。** 公式APIで取れない店だけ Apify で取る。

### 2-2. Meta 側の設定値（秘密情報ではない）

| 項目 | 値 |
|---|---|
| Meta アプリ名 | menu-outreach（タイプ：ビジネス） |
| App ID | `1836759181072844` |
| ビジネスポートフォリオ | 加藤あやかのビジネスポートフォリオ |
| Facebook ページ | 株式会社OTis（ページID `285514155561361`） |
| Instagram アカウント | @otis_company_official（旧 @reatta2021） |
| Instagram ビジネスアカウントID | `17841448043763697`（= `IG_BD_USER_ID`） |
| システムユーザー | menu-outreach（ユーザーID `122095191879509969`） |
| トークンの権限 | `instagram_basic` / `instagram_manage_insights` / `pages_show_list` / `pages_read_engagement` / `ads_read` / `business_management`（**6つ必須**。`instagram_manage_insights` と `ads_read` が無いと Business Discovery が code 10 で失敗する） |
| トークンの有効期限 | **60日**（「無期限」は選べなかった）。現在のトークンは **2026-12-06 17:21 に失効** |
| Graph API バージョン | v26.0（グラフAPIエクスプローラでの確認時と同じ） |
| Meta の権限 | @otis_company_official の管理者は加藤あやかさん。大宮さんも全権限あり |

> ⚠ **トークン本体は、この文書・チャット・LINE・メールに絶対に書かない。** Railway の Variables に直接貼り付けるだけにする。

### 2-3. Railway の環境変数（営業アプリ：プロジェクト `menu-outreach` ／ サービス `web`）

| 変数 | 状態 | 内容 |
|---|---|---|
| `IG_BD_USER_ID` | ✅ 設定済み | `17841448043763697` |
| `GRAPH_API_VERSION` | ✅ 設定済み | `v26.0` |
| `IG_BD_ACCESS_TOKEN` | ✅ 設定済み（2026-10-07） | システムユーザーのトークン（`EAA` で始まる約200文字） |
| `APIFY_API_TOKEN` | ✅ 設定済み | 予備（公式APIで取れない店の取得）と店舗収集に使う。**消さない** |
| `USE_VISION` | ✅ `true` | 写真のAI採点（Claude）。今回は変更しない |
| `AUTO_DAILY_AT` | ✅ `09:30` | 毎朝の自動実行の時刻 |

`IG_BD_USER_ID` と `IG_BD_ACCESS_TOKEN` の**両方**が入ると、自動的に公式APIへ切り替わる（コードの変更は不要）。

---

## 3. 費用の実測と削減見込み

### 3-1. Apify の実績（2026-10-01〜10-07 の7日間、Billing → Current period より）

| 内容 | 件数 | 単価 | 金額 |
|---|---|---|---|
| Googleマップ 店舗取得（`compass/crawler-google-places` Scraped place） | 1,440 | $0.003167 | $4.56 |
| 起動料（Actor Start） | 144 | $0.00005 | $0.01 |
| メール等の取得（Add-on: Company contacts enrichment） | 799 | $0.002 | $1.60 |
| 営業中の店に絞り込み（Add-on: Filter applied） | 1,440 | $0.001 | $1.44 |
| **Instagram 分析（`apify/instagram-profile-scraper` Profile）** | **619** | **$0.002353** | **$1.46** |
| 合計 | | | **$9.07** |

- 月の見込み（×31÷7）：**約 $40（約6,000円）**。Starter プラン（月$19、$19分の利用込み）を **約$21 超過**する見込み。
- 超過しているので、削った分はそのまま請求の減額になる。

### 3-2. 削減見込み（1ドル150円）

| 施策 | 削減額（月） | 状態 |
|---|---|---|
| ① Instagram 分析を公式APIに切り替え | 約 $6.5（**約970円**）。個人アカウントの店は Apify に残るので実際は 7〜9割 | 実施中（本書の対象） |
| ② Apify 側の「営業中の店に絞り込む」をやめる（アプリ側でも閉店を除外しているため） | 約 $5.4（**約800円**、閉店店の取得料増を差し引き後） | **未着手・大宮さんの承認待ち** |
| ③ 写真のAI採点（`USE_VISION`）をやめる | 約900〜2,000円（試算） | 様子見（精度とのトレードオフ） |

①＋②で **月約1,770円、年約2万1,000円**。

---

## 4. 仕組み（実装済みの内容）

コードは `app/enrich.py`。

| 関数 | 役割 |
|---|---|
| `run(limit, refresh_days)` | 毎朝の Instagram 分析の本体。`IG_BD_*` が揃っていれば公式APIを使い、取れなかった店だけ Apify に回す |
| `check_graph_token()` | 分析の最初にトークンを確認（`/debug_token`）。無効なら今回は全部 Apify。**期限が10日以内ならLINEで通知** |
| `via_graph(username, n_media)` | Business Discovery で1店分を取得。エラーを2種類に分ける |
| `GraphFatal` | トークン無効・権限不足・回数制限（code 190/102/10/4/17/32/613/2xx）。**その回の残りは全部 Apify、LINE で通知** |
| `GraphSkip` | その1店だけ取れない（個人アカウント・非公開・存在しない等）。**その店だけ Apify で取得** |
| `_apify_batch(rows, cfg)` | Apify でまとめて取得（50件ずつ） |
| `send_notice(text)` | LINE（と Discord）に通知 |
| `match_score()` / `rescore_ig_matches()` / `website_shared()` | 取得したアカウントがその店のものかの判定（グルメサイト・情報誌・施設のアカウントを除外）。取得元に関係なく同じ |

動作の流れ：

```
毎朝9:30 自動実行
 └ Instagram分析（最大150店、AUTO_ENRICH_LIMIT）
     ├ トークン確認 ─ 無効 → 全部Apify＋LINE通知
     │              └ 期限10日以内 → LINE通知（分析は続行）
     ├ 公式APIで1店ずつ取得（18秒間隔＝1時間200件の制限内）
     │   ├ 取れた → 保存
     │   ├ GraphSkip → その店だけApifyへ
     │   └ GraphFatal → 残り全部Apifyへ＋LINE通知
     └ Apifyに回した店をまとめて取得
```

**テスト済み**（モックで再現）：①正常＋1件だけ個人アカウント、②途中でトークン切れ、③最初からトークン無効、④期限5日前、の4ケースすべてで店が落ちず、必要なときだけ通知されることを確認。
**本物のトークンで確認済み**（2026-10-07）：@starbucks_j・@otis_company_official をフォロワー数・投稿数・投稿日時・写真6枚まで取得できた。最初は権限4つのトークンで code 10（権限不足）になり、`instagram_manage_insights` と `ads_read` を足して解決。

---

## 5. これからやること

### 5-1. トークンを Railway に入れる（作業 8）
トークンを人から人へ渡さないこと。次のどちらかで行う。

- **A（推奨）**：大宮さんが Meta で新しいトークンを作り、同じパソコンで Railway に貼る
  1. https://business.facebook.com → 設定 → ユーザー → システムユーザー → **menu-outreach**
  2. 「新しいトークンを生成」→ アプリ：menu-outreach／期限：60日／権限：上記6つ →「トークンを生成」→ コピー
  3. Railway → menu-outreach → web → Variables → New Variable：`IG_BD_ACCESS_TOKEN` に貼り付け → **Deploy**
  4. あやかさんが保存している古いトークンは削除してもらう
- **B**：あやかさんに、大宮さんのパソコンの Railway 画面へ直接貼り付けてもらう（その後あやかさんの保存分は削除）

> ⚠ **毎朝 9:30〜10:15 ごろは Deploy しない**（自動実行が途中で止まる）。

### 5-2. 動作確認（作業 9）
1. 営業ダッシュボード https://web-production-2defbe.up.railway.app/jobs（ユーザー名 `otis`）
2. 「②Instagram解析」の件数を `5` にして「解析する」
3. ログに **`公式APIで ○店、Apifyに回す分 ○店`** と出れば成功
   - `公式APIを使えません: …` と出たらトークンの問題（LINE にも通知が来る）
4. Railway のログでも確認できる：`railway logs | grep "\[enrich\]"`
5. 翌朝の自動実行のログで、公式APIの件数と Apify に回った件数を確認する
6. 1週間後に Apify の Billing → Current period で `apify/instagram-profile-scraper` の件数が減っているか確認する

### 5-3. スタッフへの周知（作業 10）
公式APIは18秒間隔で取得するため、毎朝の処理の完了が **約10:00 → 約10:30** に遅れる。
DM 送信の手順書の「10時半以降に始めてください」を **「11時以降」** に変える（ほしこさん・森ちゃん・あやかさん向け）。

### 5-4. トークンの更新（60日ごと）
- 期限の10日前から毎朝 LINE に「あと○日で期限切れ」と届く（2026-10-07 作成なら 11月26日ごろから）
- 手順：5-1 の A の 1〜3 と同じ（新しいトークンを作って `IG_BD_ACCESS_TOKEN` を書き換え → Deploy）
- 切れても自動で Apify に戻るので分析は止まらない（その間は Apify の料金がかかる）

### 5-5. 承認待ちの関連施策
- **② Apify の「Filter applied」をやめる**：`app/collect.py` の `run_search()` で Apify に渡している
  `"skipClosedPlaces": True` をやめ（または False）、閉店店はアプリ側の既存処理
  （`permanentlyClosed` / `temporarilyClosed` を見て除外）に任せる。月約800円の削減見込み。**大宮さんの承認後に実施**
- ③ 写真のAI採点をやめるかは、1か月ほど様子を見てから判断

---

## 6. 注意点・既知の問題

- **GitHub に push しても Railway が自動デプロイしないことが何度もあった。**
  反映されない場合は次で手動デプロイできる（最新コミットがデプロイされる）：
  `railway service source connect --repo otishomiya-sketch/menu-outreach --branch main --service web`
  恒久対策として、Railway の web → Settings → Source で自動デプロイの設定を確認してもらう。
- **公式APIで取れるのはビジネス／クリエイターアカウントだけ。** 個人アカウントの店は Apify に回るので、Apify の Instagram 分析費はゼロにはならない（7〜9割減の見込み）。
- **公式APIは1時間約200件まで**（アプリ側で18秒間隔に調整済み）。`AUTO_ENRICH_LIMIT`（既定150）を増やすと処理時間が伸びる。
- **Instagram DM の送信には Meta のアプリは関係ない**（スタッフが手動で送っている）。今回の設定がうまくいかなくても DM 送信は続けられる。
- `APIFY_API_TOKEN` は店舗収集と予備に必須。消さないこと。
- 営業アプリのリポジトリ：`otishomiya-sketch/menu-outreach`（GitHub が正本）。手元の作業フォルダは一時的なものなので、作業は GitHub から clone して行う。

---

## 7. 関連する場所

| もの | 場所 |
|---|---|
| 営業アプリのコード | GitHub `otishomiya-sketch/menu-outreach`（Instagram分析は `app/enrich.py`） |
| 営業アプリの本番 | Railway プロジェクト `menu-outreach` ／ サービス `web` ／ https://web-production-2defbe.up.railway.app |
| Apify の請求 | https://console.apify.com → Billing → Current period（大宮さん個人アカウント HisashiOmiya、Starter） |
| Meta アプリ | https://developers.facebook.com/apps → menu-outreach |
| システムユーザー | https://business.facebook.com → 設定 → ユーザー → システムユーザー → menu-outreach |
| 通知 | LINE（`LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO_USER_ID`、株式会社OTis の公式アカウントから大宮さんへ） |
