# copilot-teams-feed

GitHub Copilot Changelogの更新を毎朝、日本語タイトル・要約に変換してRSSで公開するリポジトリです。
Power AutomateがこのRSSを取得し、既存のAIニュースと同じ朝にTeamsへ通知します。

```
毎朝 06:50 JST (cron "50 21 * * *" UTC)
  ↓ GitHub Actions
scripts/build_feed.py
  1. Changelog RSSを取得し、「前日0:00〜当日0:00(UTC/米国時間)」に公開された記事を抽出
  2. OpenRouter(無料モデル)で日本語タイトル・要約を生成
  3. rss/copilot.xml を生成
  ↓ mainへコミット&push (github-actions[bot])
  ↓ GitHub Pages が自動再ビルド
https://yuzu-krs.github.io/copilot-teams-feed/rss/copilot.xml
  ↓ Power Automate (RSS新着トリガー)
  ↓ Teams AI Lab へ通知
```

## 公開URL

- RSSフィード: https://yuzu-krs.github.io/copilot-teams-feed/rss/copilot.xml

## セットアップ

1. **OpenRouter APIキーをsecretに登録**
   - https://openrouter.ai/keys でキーを作成(無料モデルのみの利用なら利用料0円)
   - リポジトリの Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `OPENROUTER_API_KEY` / Secret: キーの値
2. **(任意) モデルチェーンをvariableに登録**
   - 同じページの Variables タブで `OPENROUTER_MODEL` を作成
   - 値はカンマ区切りのモデルID(例: `z-ai/glm-5.2:free,google/gemma-4-31b-it:free`)
   - 未設定時はコード既定値(`scripts/build_feed.py` の `DEFAULT_MODEL_CHAIN`)が使われる
   - OpenRouterの無料モデルは頻繁に入れ替わるため、LLMが失敗し始めたら
     [無料モデル一覧](https://openrouter.ai/collections/free-models)を確認してここだけ差し替える
3. **GitHub Pagesを有効化**(設定済み)
   - Settings → Pages → Source: **Deploy from a branch** / Branch: `main` / `(root)`

## スケジュール

- GitHub Actionsは `cron: "50 21 * * *"`(**UTC基準**)で毎日1回実行 = **毎朝06:50 JST**
- GitHubのキュー都合で数分遅れて開始することがあるが、取得窓は実行時刻基準なので問題ない
- 手動実行: Actions → update-feed → **Run workflow**
  - 任意の `window_start`(ISO8601 UTC)を指定すると、昨日1日分の代わりに
    その時刻から実行時刻までを再取得できる。**実行に失敗した日の記事の
    復旧**や、LLMの出力が壊れていた時の再生成に使う

## 取得窓の仕組み

**前日0:00〜当日0:00(UTC/米国時間)に公開された記事**、つまり**米国時間の前日1日分**を取得する。
毎朝06:50 JSTに実行し、07:00前後にTeamsへ届く。

- Changelogサイトの日付表記(9月16日など)も米国時間ベースのため、**フィードの対象日とサイトの表示日が一致**する
- 日本時間では窓は「前日09:00〜当日09:00」に相当する。
  **日本時間X日の朝の配信は、米国時間の(X−2)日分**(例: 9/18の朝 → 米国時間9/16分)
- 06:50 JSTの実行時点では対象日の窓はすでに終わっている(締め切り済み)ため、
  取りこ抜しは発生しない
- 履歴ファイル(state等)は**持たない**。前日分だけを毎朝生成するシンプルな設計
- 重複排除は guid=記事URL に基づき **Power Automate側の重複排除**に任せる
- 暦日で区切るため、cronの実行が数分遅れても窓は変わらない
- **注意**: 実行が失敗した日はその日分が丸ごと飛ぶ。手動実行の `window_start` に
  該当日の0:00 UTCを指定して再取得すれば復旧できる

## RSS仕様

- `guid` は元記事のURL(`isPermaLink="true"`、安定キー)。Power Automateの
  重複排除はこのguidに依存するため、再実行しても二重投稿されない
- フィードには**米国時間の前日1日分の記事のみ**を掲載する(アーカイブではない)
- **記事が0件の日も正常系**。アイテム0件の有効なRSSを生成する。
  Power Automateには新着が無いため通知も行われない
- `lastBuildDate` はフィード生成時刻(JST)

## 日本語生成の仕様

- OpenRouterの無料モデルで、タイトルは簡潔な日本語に(製品名・機能名は英語のまま)、
  要約は2〜3文(何が変わったか・誰に影響するか)で生成する
- モデルは `DEFAULT_MODEL_CHAIN` の順に試行。無料モデルは共有上流のレート制限で
  429になりやすいため、2/10/30秒のバックオフでリトライし、だめなら次のモデルへ
- **LLMが全滅した場合・APIキーが未設定の場合**も、英語タイトル+本文抜粋で
  記事を掲載する(記事を落とさない、ジョブも落とさない)

## Power Automate側の設定(リポジトリ外)

1. トリガー: **「フィードのアイテムが公開されたとき」**(RSS)
   - RSSフィードのURL: `https://yuzu-krs.github.io/copilot-teams-feed/rss/copilot.xml`
   - 頻度(recurrence): 15分推奨(毎朝07:00前後の取得に間に合う)
2. アクション: **「チャットまたはチャネルでカードを投稿」**(Teams)
   - タイトル = RSSアイテムのタイトル / 本文 = 概要(description) / リンク = リンク
3. 記事が無い日は新着アイテムが無いため、通知は行われない

## ローカルテスト

Python 3.10+ があれば動作します(標準ライブラリのみ使用)。

```powershell
# ヘルプ(利用できるフラグ一覧)
python scripts/build_feed.py --help

# ネットワーク取得せず、保存済みのRSSファイルで試す
python scripts/build_feed.py --feed-file tmp\feed.xml --out tmp\out.xml

# 「現在時刻」と「窓の開始」を固定して0件フィードを試す
python scripts/build_feed.py --feed-file tmp\feed.xml --now 2026-09-17T00:00:00Z --window-start 2026-09-17T00:00:00Z --out tmp\out.xml
```

- `--feed-file`: ネットワークの代わりにファイルからソースRSSを読む
- `--window-start`: 取得窓の開始時刻を上書き(ISO8601、UTC)
- `--now`: 現在時刻を上書き(ISO8601、UTC。テスト用)
- `--out`: 出力RSSのパス(既定は `rss/copilot.xml`)

ローカルで実フィードを試す場合は、先に本物のRSSを保存しておく:

```powershell
mkdir tmp -Force; curl.exe -sL -o tmp\feed.xml https://github.blog/changelog/label/copilot/feed/
```

`OPENROUTER_API_KEY` を設定せずに実行すると、すべての記事が英語フォールバックに
なるため、LLM無しでの動作確認ができる。

## 注意

- 毎日の実行で `github-actions[bot]` が `rss/copilot.xml` を更新・コミットする(正常な挙動)
- OpenRouterの無料モデルにはレート制限があるが、0〜3件/日の利用では実質問題にならない
- ソースフィードは https://github.blog/changelog/label/copilot/feed/ (直近約10件)
