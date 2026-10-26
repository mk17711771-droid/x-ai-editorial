# X AI編集部（@satoshikun_666 自律運用システム）

米国株・半導体 × Hyperliquid の「資金の流れと異変」を数字で追うXアカウントを、AIの編集部で運用するシステムです。

**第1段階（このリポジトリ）**: 情報収集 → 編集長AIの採点 → 原稿作成 → 安全チェック → **LINEで承認** → X投稿
第2段階（分析・週次レポート）、第3段階（自動返信・競合分析）は、運用が安定してから追加します。

---

## しくみ

```
GitHub Actions（定時実行・無料）
  ├ 材料収集   Finnhub（米国株）/ Hyperliquid公式API
  ├ 編集長AI   Gemini（無料枠）が100点満点で採点 → 80点以上だけ残す
  ├ 制作AI     3案作成。数字はAIに書かせず、取得データを差し込む
  ├ 安全チェック 捏造数字・禁止表現・文字数・類似投稿・同じ話題の連投
  └ LINEに承認依頼（1枠1通）
         │ [承認して投稿] [却下:質が低い] [却下:不正確]
         ▼
Cloudflare Worker（無料）→ GitHub Actions
  └ 投稿直前の再確認（期限・1日の上限・最新データとの照合）→ X API で投稿
```

### 投稿枠（日本時間）

| 時刻 | 平日 | 土日 |
|---|---|---|
| 07:30 | 米国株: 引け後まとめ | — |
| 10:00 | Hyperliquid: 24時間の異変 | Hyperliquid |
| 12:15 | 米国株: 異変の解説 | — |
| 20:30 | 米国株: 深掘り・今夜の注目点 | Hyperliquid: 週間まとめ |
| 23:00 | Hyperliquid: 米国市場オープン前後 | — |

平日は株3：Hyperliquid 2。良い材料がない枠は**投稿しません**（無理に埋めない）。
時刻・テーマ・監視銘柄は `config/settings.yaml` で変更できます。

### 安全装置

- **数字の捏造防止**: AIが本文に数字を直接書いた案は自動で破棄します。数字はすべて取得データから差し込みます。
- **禁止表現**: `config/banned_phrases.txt`（売買の勧誘、利益の保証、レバレッジの煽り、取引所への誘導など）
- **小型銘柄の除外**: Hyperliquidは24時間出来高500万ドル未満の銘柄を扱いません。
- **投稿直前の再確認**: 承認時に最新データを取り直し、騰落率が1.5ポイント以上変わっていたら投稿を中止します。
- **承認期限**: 3時間を過ぎた下書きは投稿しません。
- **上限**: 1日8投稿まで。同じ枠からは1件だけ。同じ話題は24時間空けます。
- **緊急停止**: LINEで「停止」と送ると、すべての投稿が止まります。X APIの認証エラーが出たときは自動で停止します。
- **リンク・メンション禁止**: 本文にURLや@メンションは入れません。

### 文字数について

Xの無料アカウントは、日本語でおよそ**140文字**が上限です（全角は2文字として数えるため）。
X Premiumに加入した場合は、GitHubのリポジトリ変数 `X_PREMIUM` を `true` にすると約220文字まで書けるようになります。

---

## 初期設定の手順

所要時間の目安は1〜2時間です。わからないところはClaudeに聞いてください。

### 1. GitHubに**非公開（Private）**リポジトリを作り、このコードを置く

投稿履歴や下書きがリポジトリに保存されるため、**必ずPrivate**にしてください。

### 2. APIキーを取得する（すべて無料で始められます）

| サービス | 取得場所 | 使うもの |
|---|---|---|
| Gemini | https://aistudio.google.com/apikey | APIキー |
| Finnhub | https://finnhub.io/register | APIキー |
| X API | https://developer.x.com | 下の手順を参照 |
| LINE | https://developers.line.biz/console/ | 下の手順を参照 |
| Cloudflare | https://dash.cloudflare.com/sign-up | アカウントのみ |

**X APIの手順**
1. developer.x.com でアプリを作成します（従量課金。最初のクレジットがある間は実質無料です）。
2. 「User authentication settings」で、権限を **Read and write** にします。
3. 「Keys and tokens」で、次の4つを取得します（2つ目のトークンは、権限を変更した**後に**発行し直してください）。
   - API Key と API Key Secret
   - Access Token と Access Token Secret

**LINEの手順**
1. LINE Developersでプロバイダーを作り、**Messaging APIチャネル**を作成します。
2. チャネルシークレットと、チャネルアクセストークン（長期）を取得します。
3. 作成した公式アカウントを、自分のLINEで友だち追加します。
4. LINE Official Account Managerで、「応答メッセージ」をオフ、「Webhook」をオンにします。

### 3. Cloudflare Workerを公開する（LINEのボタンの受け口）

```bash
cd worker
npx wrangler login
# wrangler.toml の GITHUB_REPO に "あなたのGitHub名/リポジトリ名" を書く
npx wrangler secret put LINE_CHANNEL_SECRET
npx wrangler secret put LINE_CHANNEL_ACCESS_TOKEN
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

`GITHUB_TOKEN` には、GitHubの Settings → Developer settings → **Fine-grained tokens** で、このリポジトリだけを対象にしたトークンを作って設定します。必要な権限は次の2つです。
- Contents: Read and write
- Variables: Read and write

公開すると `https://x-ai-editorial-line.<名前>.workers.dev` というURLが表示されます。これをLINE Developersの「Webhook URL」に設定してください。

続いて、自分のLINEユーザーIDを確認して登録します。
1. LINEで公式アカウントに「ID」と送ると、あなたのユーザーIDが返ってきます。
2. そのIDを登録します。
   ```bash
   npx wrangler secret put LINE_ADMIN_USER_ID
   ```

### 4. GitHubにキーを登録する

リポジトリの Settings → Secrets and variables → Actions で登録します。

**Secrets**
`GEMINI_API_KEY` `FINNHUB_API_KEY` `X_API_KEY` `X_API_SECRET` `X_ACCESS_TOKEN` `X_ACCESS_TOKEN_SECRET` `LINE_CHANNEL_ACCESS_TOKEN` `LINE_ADMIN_USER_ID`

**Variables**

| 名前 | 値 | 意味 |
|---|---|---|
| `BOT_ENABLED` | `true` | `true` にすると運用開始。`false` で停止（LINEの「停止」でも切り替わります） |
| `APPROVAL_MODE` | `true` | `false` で全自動投稿（承認の精度を確認してから切り替えます） |
| `X_PREMIUM` | `false` | X Premiumに加入したら `true` |
| `GEMINI_MODEL` | （空欄） | AI Studioで使えるモデル名に変える場合のみ |

### 5. 動作確認

GitHubの Actions → **generate-drafts** → Run workflow で、slotに `1000`、forceにチェックを入れて実行します。
LINEに承認依頼が届けば完成です。

---

## LINEのコマンド

| 送る言葉 | 動作 |
|---|---|
| 停止 | すべての自動投稿を止める |
| 再開 | 再開する（異常検知による停止も解除） |
| 状態 | 稼働状態とモードを表示 |
| ID | 自分のLINEユーザーIDを表示 |

## 月額費用の目安

| 項目 | 月額 |
|---|---|
| X API（月110〜150投稿 × 約0.015ドル） | 約200〜300円（最初のクレジットで実質0円） |
| Gemini / GitHub Actions（Private: 月2,000分まで無料） / Cloudflare Workers / Finnhub / Hyperliquid API | 0円 |
| LINE（無料プランは月200通。1枠1通なので月130通程度） | 0円 |

## 開発者向け

```bash
pip install -r requirements.txt pytest
python -m pytest            # 外部APIを使わないテスト
PYTHONPATH=src python -m aiedit.cli preview --kind hl   # Hyperliquidの材料収集だけ試す
```

データは `data/` に保存されます。

| ファイル | 内容 |
|---|---|
| `drafts/` | 下書き |
| `posts.jsonl` | 投稿履歴 |
| `decisions.jsonl` | 承認・却下の記録（採点の改善に使用） |
| `facts/` | 収集した材料 |
| `state.json` | 停止状態 |

## 注意

- このアカウントの投稿は情報提供を目的としたもので、投資助言ではありません。プロフィールと固定投稿に免責文を入れてください。
- Hyperliquidは日本の金融庁に登録された事業者ではありません。利用の勧誘・紹介リンクは投稿しない設計にしています。
- Xの規約に従い、自動化されたアカウントのラベル設定を検討してください（X → 設定 → アカウント情報 → 自動化）。
