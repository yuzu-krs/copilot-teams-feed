# copilot-teams-feed

GitHub Copilot Changelogの更新を毎朝、日本語タイトル・要約に変換してRSSで公開するリポジトリです。
Power AutomateがこのRSSを取得し、既存のAIニュースと同じ朝にTeamsへ通知します。

```
毎朝 06:50 JST (cron "50 21 * * *" UTC)
  ↓ GitHub Actions
scripts/build_feed.py
  1. data/state.json の last_run_end から取得窓を確定
  2. Changelog RSSを取得し、窓内の新着記事を抽出
  3. OpenRouter(無料モデル)で日本語タイトル・要約を生成
  4. rss/copilot.xml を生成
  5. data/state.json を更新(成功時のみ)
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
   - 値はカンマ区切りのモデルID(例: `z-ai/glm-5.2:free,google/gemma-4-31b-it:free`)。
     現在の既定は `z-ai/glm-5.2:free` → `google/gemma-4-31b-it:free` →
     `google/gemma-4-26b-a4b-it:free` → `nvidia/nemotron-3-super-120b-a12b:free` の4本
   - 未設定時はコード既定値(`scripts/build_feed.py` の `DEFAULT_MODEL_CHAIN`)が使われる
   - OpenRouterの無料モデルは頻繁に入れ替わるため、LLMが失敗し始めたら
     [無料モデル一覧](https://openrouter.ai/collections/free-models)を確認してここだけ差し替える
3. **GitHub Pagesを有効化**
   - Settings → Pages → Source: **Deploy from a branch**
   - Branch: `main` / `(root)` → Save

## スケジュール

- GitHub Actionsは `cron: "50 21 * * *"`(**UTC基準**)で毎日1回実行 = **毎朝06:50 JST**
- GitHubのキュー都合で数分遅れて開始することがあるが、取得窓は実行時刻ベースのため
  遅延による取りこ抜しは発生しない
- 手動実行: Actions → update-feed → **Run workflow**
  - 任意の `window_start`(ISO8601 UTC)を指定すると、通常の連続窓の代わりに
    その時刻を窓の開始として再取得できる(LLMの出力が壊れていた時の再生成や
    障害復旧のテストに使える。既に掲載済みの記事は `published` リストにより
    重複掲載されない)

## 取得窓の仕組み

**前回成功実行時刻から今回実行時刻までに公開された記事**を取得する
(`前回成功実行時刻 < 記事のpubDate <= 今回実行時刻`)。
固定の期間区切り(「前日07:00〜当日07:00」のような)は使わない。

- `data/state.json` の `last_run_end` に前回成功実行時刻を保存する
- **初回**: `last_run_end` が無い場合は直近24時間を取得
- **成功時**: 今回の実行時刻を `last_run_end` に保存
- **失敗時**: `last_run_end` は更新しない。次回が同じ窓を再取得するため取りこ抜しが無い
- Actionsの実行が数分遅延しても、実際の実行時刻までを取得窓に含める
- 06:50〜07:00に公開された記事は、その日の実行時点では取得できないため、
  翌日の取得窓に自然に含まれる
- 記事の判定は取得時刻ではなく、**必ずソースRSSの `pubDate` を基準**にする
- 内部の窓計算はすべてUTC。RSSに出力する `pubDate` のみJST(+09:00)に変換する

## RSS仕様

- `guid` は元記事のURL(`isPermaLink="true"`、安定キー)。Power Automate側の
  重複排除はこのguidに依存するため、再実行しても二重投稿されない
- フィードには**当該窓の記事のみ**を掲載する(過去200件のようなアーカイブではない)
- **記事が0件の日も正常系**。アイテム0件の有効なRSSを生成し、stateは前進する。
  Power Automateには新着が無いため通知も行われない
- `lastBuildDate` はフィード生成時刻(JST)

## 日本語生成の仕様

- OpenRouterの無料モデルで、タイトルは簡潔な日本語に(製品名・機能名は英語のまま)、
  要約は2〜3文(何が変わったか・誰に影響するか)で生成する
- モデルは `DEFAULT_MODEL_CHAIN` の順に試行。レート制限(429)や一時的な5xxは
  2段階のバックオフでリトライし、だめなら次のモデルへ
- **LLMが全滅した場合・APIキーが未設定の場合**も、英語タイトル+本文抜粋で
  記事を掲載する(記事を落とさない、ジョブも落とさない)

## data/state.json

```json
{
  "schema_version": 1,
  "last_run_end": "2026-09-17T21:50:12Z",
  "published": ["https://github.blog/changelog/..."],
  "updated_at": "2026-09-17T21:50:12Z"
}
```

- `last_run_end`: 前回成功実行時刻(ISO8601 UTC)。`null` なら初回実行(直近24時間)
- `published`: 掲載済み記事のguidリスト(上限500、古いものから削除)。
  窓計算の重複防止のための二重化
- このファイルは毎日の実行で `github-actions[bot]` により更新・コミットされる(正常な挙動)

### state.jsonの修復

`last_run_end` が破損している等の異常がある場合、スクリプトは**何も書き換えずに
異常終了**します(24時間へ自動フォールバックすると、通知済みの記事を再取得して
重複通知する恐れがあるため)。破損時は手動で修復してください:

- 直近の正常なstateに戻す: `git log --oneline -- data/state.json` で直近の正常な
  コミットを探し、`git checkout <commit> -- data/state.json` で復元してコミット
- または手でJSONを修正する(壊れたフィールドだけ直す)

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
python scripts/build_feed.py --feed-file tmp\feed.xml --state tmp\state.json --out tmp\out.xml

# 「現在時刻」と「窓の開始」を固定して0件フィードを試す
python scripts/build_feed.py --feed-file tmp\feed.xml --now 2026-09-17T00:00:00Z --window-start 2026-09-17T00:00:00Z --state tmp\state.json --out tmp\out.xml
```

- `--feed-file`: ネットワークの代わりにファイルからソースRSSを読む
- `--window-start`: 取得窓の開始時刻を上書き(ISO8601、UTC)
- `--now`: 現在時刻を上書き(ISO8601、UTC。テスト用)
- `--state` / `--out`: stateと出力RSSのパス(既定は `data/state.json` / `rss/copilot.xml`)

ローカルで実フィードを試す場合は、先に本物のRSSを保存しておく:

```powershell
mkdir tmp -Force; curl.exe -sL -o tmp\feed.xml https://github.blog/changelog/label/copilot/feed/
```

`OPENROUTER_API_KEY` を設定せずに実行すると、すべての記事が英語フォールバックに
なるため、LLM無しでの動作確認ができる。

## 注意

- 毎日の実行で `github-actions[bot]` が1コミット作成する(stateとRSSの更新)。正常な挙動
- OpenRouterの無料モデルにはレート制限があるが、0〜3件/日の利用では実質問題にならない
- ソースフィードは https://github.blog/changelog/label/copilot/feed/ (直近約10件)
