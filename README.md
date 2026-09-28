# 🏨 東横イン空室ウォッチ

満室で取れなかった東横インを登録しておくと、キャンセルが出た瞬間に Gmail で知らせてくれる仕組み。

```
スマホ（Streamlit画面） ──登録──▶ watchlist.json（このリポジトリ）
                                        │
GitHub Actions（約50分ずつリレーで常時起動、中で1分おきにチェック）
        │  東横イン公式の検索結果ページから空室数を読み取る
        ├─ 満室 → 空室 に変わった → スマホにプッシュ通知(ntfy)＋Gmail（予約ページのリンク付き）
        └─ 結果を state.json に保存 → スマホ画面に表示
```

| ファイル | 役割 |
|---|---|
| `checker.py` | 空室チェックと Gmail 通知の本体 |
| `app.py` | スマホ用の登録・確認画面（Streamlit） |
| `.github/workflows/watch.yml` | 定期実行の設定 |
| `watchlist.json` | 登録した監視条件（画面から追加・削除） |
| `state.json` | 最新の空室状況（自動更新） |
| `hotels.json` | 全国の東横イン一覧（367軒、韓国含む） |

## 仕様メモ

- 判定は「条件に合う部屋タイプのどれか1つでも空室が1以上」で空室ありとする
- 部屋タイプは部屋名の部分一致（「ダブル」→ ダブル＋エコノミーダブル）
- 通知は **満室→空室に変わった時だけ**。空いたまま続いても再送しない。一度埋まってまた空けば再通知
- 宿泊日を過ぎた条件は自動で「期限切れ」扱い（削除は画面から）
- 5回連続で取得できなかったら「監視が止まっている」メールを1回送る
- サイト負荷を避けるため、1リクエストごとに3秒あける。同じ条件が複数あっても取得は1回

## セットアップ

### 1. Gmail のアプリパスワードを作る
1. Google アカウントで2段階認証をオンにする（済みなら不要）
2. https://myaccount.google.com/apppasswords を開き、名前「toyoko」で作成
3. 表示された16桁をメモ（あとで GitHub に登録する）

### 2. GitHub Secrets を登録
リポジトリの Settings → Secrets and variables → Actions → New repository secret

| 名前 | 値 |
|---|---|
| `GMAIL_ADDRESS` | 送信に使う Gmail アドレス |
| `GMAIL_APP_PASSWORD` | 手順1の16桁 |
| `NOTIFY_TO` | 通知先（省略可。カンマ区切りで複数可） |

### 3. 動作確認
Actions タブ →「東横イン空室ウォッチ」→ Run workflow。
ログに `空室 None -> 0` のような行が出ていれば取得できている。
`HTTP 403` などが出る場合は、GitHub のサーバーからのアクセスが制限されている（→ 下の「PCで動かす」へ）。

### 4. スマホ画面（Streamlit Cloud）
1. GitHub → Settings → Developer settings → Fine-grained tokens で新規作成
   - Repository access: このリポジトリだけ
   - Permissions: Contents = Read and write
2. https://share.streamlit.io で New app → このリポジトリ / `app.py` を指定
3. Advanced settings → Secrets に貼る
   ```toml
   GITHUB_TOKEN = "github_pat_xxxxx"
   GITHUB_REPO  = "tsutomu333/toyoko-watch"
   APP_PASSWORD = "好きな合言葉"
   ```
4. 発行された URL をスマホのホーム画面に追加

## PC で動かす場合（GitHub から取得できないとき用）
```
pip install -r requirements.txt
set GMAIL_ADDRESS=xxx@gmail.com
set GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
python checker.py --loop 1440      # 24時間、3分おきにチェック
streamlit run app.py               # 画面（ローカルのファイルを直接読み書き）
```

## 注意
- GitHub の定期実行は混雑時に数分〜十数分遅れることがある
- 空室データは公式サイトの検索結果に基づく。予約は必ず公式サイトで行う
- 公式サイトの仕組みが変わると動かなくなる可能性がある（その時は取得失敗メールが届く）
