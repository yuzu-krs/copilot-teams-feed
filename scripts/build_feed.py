#!/usr/bin/env python3
"""Build rss/copilot.xml from the GitHub Copilot changelog feed.

取得窓は「前回成功実行時刻 < 記事のpubDate <= 今回実行時刻」の連続窓。
data/state.json の last_run_end に前回成功実行時刻を保存する。

- 初回(state.jsonが存在しない): 直近24時間を取得
- 成功時: 今回の実行時刻を last_run_end に保存
- 失敗時: last_run_end を更新しない(次回が同じ窓を再取得)
- state.json が破損している場合は exit 1。24時間への自動フォールバックは
  通知済み記事の重複通知を招くため行わない(手動修復が必要)
- 記事の判定は必ずソースRSSの pubDate を基準にする(内部はUTC、出力はJST)

Python 3.10+ 標準ライブラリのみで動作する。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

FEED_URL = "https://github.blog/changelog/label/copilot/feed/"
SOURCE_LINK = "https://github.blog/changelog/label/copilot/"
PAGES_FEED_URL = "https://yuzu-krs.github.io/copilot-teams-feed/rss/copilot.xml"

STATE_PATH = "data/state.json"
RSS_PATH = "rss/copilot.xml"

# 日本はDSTが無いので固定オフセットで十分(zoneinfoはWindowsのローカル環境で
# tzdata欠如のために使わない)
UTC = timezone.utc
JST = timezone(timedelta(hours=9), "JST")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# 無料モデルは共有上流のレート制限で429になりやすいため、複数モデルを
# 順に試す。廃止された場合は repo variable OPENROUTER_MODEL で差し替えるか、
# この既定値を更新する(https://openrouter.ai/collections/free-models)
DEFAULT_MODEL_CHAIN = (
    "z-ai/glm-5.2:free,"
    "google/gemma-4-31b-it:free,"
    "google/gemma-4-26b-a4b-it:free,"
    "nvidia/nemotron-3-super-120b-a12b:free"
)

SCHEMA_VERSION = 1
PUBLISHED_CAP = 500
FETCH_TIMEOUT = 30
LLM_TIMEOUT = 90
WARN_WINDOW_DAYS = 7
EXCERPT_LIMIT = 1500
FALLBACK_SUMMARY_LIMIT = 200
NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def log(message: str) -> None:
    print(f"[build_feed] {message}", flush=True)


@dataclass
class Article:
    guid: str  # == 記事URL(RSSのguidはこれを安定キーとして使う)
    title: str
    pub: datetime  # aware UTC
    description_html: str
    content_html: str


class StateError(Exception):
    """state.json が破損している(手動修復が必要)。"""


# ---------------------------------------------------------------- state ----

def _fresh_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_run_end": None,
        "published": [],
        "updated_at": None,
    }


def _parse_iso8601_utc(value: str) -> datetime:
    """ISO8601文字列をaware UTC datetimeへ。解析不能ならValueError。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: str) -> dict:
    """state.jsonを読み込む。

    - ファイルが存在しない -> 初回実行としてデフォルト値を返す
    - 破損(JSON解析不能・必須フィールド欠損・不正な値) -> StateError
      (24時間へフォールバックすると通知済み記事を再取得して重複通知に
       なるため、自動フォールバックはしない)
    """
    if not os.path.exists(path):
        log(f"state file not found: {path} -> first run (last 24h window)")
        return _fresh_state()

    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raise StateError(f"cannot read state file {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StateError(
            f"state file {path} is corrupted (invalid JSON: {e}). "
            "Manual repair required."
        ) from e

    if not isinstance(data, dict):
        raise StateError(f"state file {path} is corrupted: top level must be an object")

    problems = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")

    last_run_end = data.get("last_run_end", "")
    if last_run_end is None:
        pass  # 初回実行相当
    elif isinstance(last_run_end, str):
        try:
            _parse_iso8601_utc(last_run_end)
        except ValueError as e:
            problems.append(f"last_run_end is not a valid ISO8601 UTC value: {e}")
    else:
        problems.append(f"last_run_end must be a string or null, got {type(last_run_end).__name__}")

    published = data.get("published")
    if not isinstance(published, list) or not all(isinstance(g, str) for g in published):
        problems.append("published must be a list of strings")

    if problems:
        raise StateError(
            f"state file {path} is corrupted ({'; '.join(problems)}). Manual repair required."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "last_run_end": None if last_run_end is None else _parse_iso8601_utc(last_run_end),
        "published": list(published),
        "updated_at": data.get("updated_at"),
    }


def new_state(prev: dict, now: datetime, new_guids: list[str]) -> dict:
    published = (prev["published"] + new_guids)[-PUBLISHED_CAP:]
    return {
        "schema_version": SCHEMA_VERSION,
        "last_run_end": _iso(now),
        "published": published,
        "updated_at": _iso(now),
    }


# --------------------------------------------------------- fetch / parse ----

def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "copilot-teams-feed/1.0 (+https://github.com/yuzu-krs/copilot-teams-feed)",
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def parse_pubdate(value: str | None) -> datetime | None:
    """RFC 822形式のpubDateをaware UTCへ。欠損/解析不能はNone(推測しない)。"""
    if not value or not value.strip():
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_feed(data: bytes) -> list[Article]:
    root = ET.fromstring(data)
    articles: list[Article] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate")
        description = item.findtext("description") or ""
        content = item.findtext(NS_CONTENT) or ""

        if not link:
            log(f"WARN: skip item without link: {title!r}")
            continue
        pub = parse_pubdate(pub_raw)
        if pub is None:
            log(f"WARN: skip item with missing/unparseable pubDate ({pub_raw!r}): {title!r}")
            continue

        articles.append(
            Article(guid=link, title=title, pub=pub, description_html=description, content_html=content)
        )
    return articles


# ----------------------------------------------------------- html / text ----

class _TextExtractor(HTMLParser):
    """HTMLからテキスト部分だけを集める(script/style内は無視)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)


def strip_html(html_text: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html_text)
        extractor.close()
    except Exception:
        pass  # 不正なHTMLでも集めたテキストは使う
    return "".join(extractor._chunks)


# WordPressがdescription末尾に付ける定型文
_BOILERPLATE_RE = re.compile(r"The post .*? appeared first on .*?\.", re.DOTALL)


def clean_excerpt(html_text: str, limit: int = EXCERPT_LIMIT) -> str:
    text = strip_html(html_text or "")
    text = _BOILERPLATE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        boundary = cut.rfind(" ")
        if boundary > limit // 2:
            cut = cut[:boundary]
        text = cut.rstrip() + "…"
    return text


# ---------------------------------------------------- window / selection ----

def window_bounds(state: dict, now: datetime) -> tuple[datetime, datetime]:
    start = state["last_run_end"] or (now - timedelta(hours=24))
    end = now
    if start >= end:
        start = end
    return start, end


def select_articles(
    articles: list[Article], start: datetime, end: datetime, published: set[str]
) -> list[Article]:
    """窓内(start < pub <= end)かつ未掲載の記事を古い順で返す。"""
    selected = [a for a in articles if start < a.pub <= end and a.guid not in published]
    selected.sort(key=lambda a: a.pub)
    return selected


# ------------------------------------------------------------------ LLM ----

SYSTEM_PROMPT = "あなたは開発者向けニュースの編集者です。有効なJSONのみを出力します。"
USER_PROMPT_TMPL = """次のGitHub変更履歴(changelog)記事の情報を日本語にしてください。

タイトル: {title}

本文(抜粋):
{excerpt}

出力は次のJSON形式のみにしてください。説明やコードフェンスは一切付けないこと:
{{"ja_title": "簡潔な日本語タイトル(製品名・機能名は英語のまま)", "ja_summary": "2〜3文の要約(何が変わったか、誰に影響するか)"}}"""


def model_chain() -> list[str]:
    raw = os.environ.get("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL_CHAIN
    return [m for m in (part.strip() for part in re.split(r"[,\s]+", raw)) if m]


def call_openrouter(model: str, user_prompt: str, api_key: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 700,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/yuzu-krs/copilot-teams-feed",
        "X-Title": "copilot-teams-feed",
    }
    delays = (2, 10, 30)
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        if attempt:
            time.sleep(delays[attempt - 1])
        try:
            request = urllib.request.Request(
                OPENROUTER_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            try:
                return body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"unexpected response shape: {json.dumps(body)[:300]}") from e
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            error = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code not in (408, 429, 500, 502, 503, 504):
                raise error  # リトライ不能(401/402/404など) -> 次のモデルへ
            last_error = error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_error = RuntimeError(f"network/parse error: {e}")
    raise last_error  # type: ignore[misc]


def extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    begin, end = cleaned.find("{"), cleaned.rfind("}")
    if begin == -1 or end <= begin:
        return None
    try:
        data = json.loads(cleaned[begin : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ja_title, ja_summary = data.get("ja_title"), data.get("ja_summary")
    if not isinstance(ja_title, str) or not ja_title.strip():
        return None
    if not isinstance(ja_summary, str) or not ja_summary.strip():
        return None
    return {"ja_title": ja_title.strip(), "ja_summary": ja_summary.strip()}


def japanize(article: Article, api_key: str | None, chain: list[str]) -> tuple[str, str, str | None]:
    """(日本語タイトル, 日本語要約, 使用モデル|None)を返す。

    LLMが一切使えない場合も英語フォールバックで記事は落とさない。
    """
    source_html = article.description_html or article.content_html
    fallback_title = article.title
    fallback_summary = clean_excerpt(source_html, FALLBACK_SUMMARY_LIMIT) or article.title

    if api_key is None:
        return fallback_title, fallback_summary, None

    prompt = USER_PROMPT_TMPL.format(title=article.title, excerpt=clean_excerpt(source_html))
    errors: list[str] = []
    for model in chain:
        try:
            content = call_openrouter(model, prompt, api_key)
        except Exception as e:
            errors.append(f"{model}: {e}")
            continue
        parsed = extract_json(content)
        if parsed:
            return parsed["ja_title"], parsed["ja_summary"], model
        errors.append(f"{model}: unparseable/empty JSON output")
    log(f"WARN: all LLM models failed -> English fallback ({'; '.join(errors)})")
    return fallback_title, fallback_summary, None


# ------------------------------------------------------------------ rss ----

def build_rss(items: list[tuple[Article, str, str]], built_at: datetime) -> bytes:
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "GitHub Copilot Changelog(日本語まとめ)"
    ET.SubElement(channel, "link").text = SOURCE_LINK
    ET.SubElement(channel, "description").text = (
        "GitHub Copilotの変更履歴を毎日日本語にまとめたフィードです"
    )
    ET.SubElement(channel, "language").text = "ja"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(built_at.astimezone(JST))
    ET.SubElement(channel, "ttl").text = "60"
    ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link",
        {"href": PAGES_FEED_URL, "rel": "self", "type": "application/rss+xml"},
    )

    for article, ja_title, ja_summary in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ja_title
        ET.SubElement(item, "link").text = article.guid
        # guidは記事URL(安定キー)。Power Automateの重複排除はこれに依存する
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = article.guid
        ET.SubElement(item, "pubDate").text = format_datetime(article.pub.astimezone(JST))
        ET.SubElement(item, "description").text = ja_summary

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ----------------------------------------------------------------- main ----

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the daily Copilot changelog RSS feed (continuous window)."
    )
    parser.add_argument("--feed-file", help="read the source RSS from a file instead of the network")
    parser.add_argument("--window-start", help="override the window start (ISO8601, UTC)")
    parser.add_argument("--now", help="override the current time for testing (ISO8601, UTC)")
    parser.add_argument("--state", default=STATE_PATH, help=f"state file path (default: {STATE_PATH})")
    parser.add_argument("--out", default=RSS_PATH, help=f"output RSS path (default: {RSS_PATH})")
    return parser.parse_args(argv)


def _parse_iso_arg(value: str, flag: str) -> datetime:
    try:
        return _parse_iso8601_utc(value)
    except ValueError:
        raise SystemExit(f"ERROR: invalid ISO8601 value for {flag}: {value!r}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)

    try:
        state = load_state(args.state)
        now = _parse_iso_arg(args.now, "--now") if args.now else datetime.now(UTC)

        if args.feed_file:
            with open(args.feed_file, "rb") as f:
                data = f.read()
        else:
            data = fetch_feed(FEED_URL)
        articles = parse_feed(data)

        window_start, window_end = window_bounds(state, now)
        if args.window_start:
            window_start = _parse_iso_arg(args.window_start, "--window-start")
            if window_start >= window_end:
                window_start = window_end
        if window_end - window_start > timedelta(days=WARN_WINDOW_DAYS):
            log(
                f"WARN: window spans {(window_end - window_start).days} days (> {WARN_WINDOW_DAYS}); "
                "delivering every article in range"
            )

        log(f"window: ({_iso(window_start)} .. {_iso(window_end)}] "
            f"[{window_start.astimezone(JST):%Y-%m-%d %H:%M} JST .. {window_end.astimezone(JST):%Y-%m-%d %H:%M} JST]")

        selected = select_articles(articles, window_start, window_end, set(state["published"]))
        log(f"articles in feed: {len(articles)}, selected: {len(selected)}")

        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or None
        if api_key is None:
            log("WARN: OPENROUTER_API_KEY is not set -> English fallback for all articles")
        chain = model_chain()
        log(f"LLM model chain: {', '.join(chain)}")

        items: list[tuple[Article, str, str]] = []
        for article in selected:
            ja_title, ja_summary, model = japanize(article, api_key, chain)
            if model:
                log(f"LLM ok via {model}: {article.guid}")
            else:
                log(f"fallback (no LLM): {article.guid}")
            items.append((article, ja_title, ja_summary))

        xml_bytes = build_rss(items, now)
        state_out = new_state(state, now, [a.guid for a in selected])

        # ここまで全て成功した場合にのみ書き込む(失敗時は何も書き換えない)
        for path in (args.out, args.state):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        with open(args.out, "wb") as f:
            f.write(xml_bytes)
        with open(args.state, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state_out, f, ensure_ascii=False, indent=2)
            f.write("\n")

        log(f"wrote {args.out} ({len(items)} items)")
        log(f"state updated: last_run_end={state_out['last_run_end']}, "
            f"published={len(state_out['published'])} guids")
        return 0
    except StateError as e:
        print(
            f"ERROR: {e}\n"
            "ERROR: data/state.json が破損しています。自動修復は行いません"
            "(直近24時間へフォールバックすると通知済みの記事を重複通知する恐れがあります)。\n"
            "ERROR: 手動修復が必要です。READMEの「state.jsonの修復」を参照してください。",
            file=sys.stderr,
        )
        return 1
    except ET.ParseError as e:
        print(f"ERROR: failed to parse the source feed XML: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # ネットワークエラー等: 何も書き換えず失敗させる
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
