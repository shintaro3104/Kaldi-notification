# -*- coding: utf-8 -*-
"""
KALDI セール一覧をクロール
  1) 店名に KEYWORDS が含まれる行を抽出（部分一致）
  2) 店舗名 / 住所 / 期間 / 内容 / 補記 をまとめて1メッセージ化
  3) (店舗 + 期間) が未送信なら LINE Push
  4) Push 冒頭に固定ヘッダ、末尾にセール一覧ページの URL
"""

import os
import sqlite3
import urllib.parse
import requests
import datetime
import textwrap
import logging
import sys
from contextlib import closing
from bs4 import BeautifulSoup

# --- Configuration & Constants ---
BASE_URL = "https://map.kaldi.co.jp/kaldi/articleList"
DB_FILE = "seen.db"
KEYWORDS = ["浦和", "レイクタウン", "与野"]
HEADLINE = "☕️ KALDIの新着セール情報が届いたよ！\n\n"

# CSS Selectors (Centralized for easy updates)
SELECTORS = {
    "row": "table.cz_sp_table tr",
    "name": "span.salename",
    "address": "span.saleadress",
    "title": "span.saletitle, span.saletitle_f",
    "date": "p.saledate, p.saledate_f",
    "detail": "p.saledetail",
    "notes": "p.saledetail_notes",
}

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """通知に表示してよい、取得・送信処理のエラー。"""


def build_url() -> str:
    """現在 JST のタイムスタンプを kkw001 に付けた URL を返す"""
    jst_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    ts = jst_now.strftime("%Y-%m-%dT%H:%M:%S")
    params = dict(account="kaldi", accmd=1, ftop=1, kkw001=ts)
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"

def fetch_target_articles():
    url = build_url()
    logger.info(f"Fetching URL: {url}")
    
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        raise ScraperError("カルディのセール一覧を取得できませんでした。") from e

    return parse_articles(response.text, url)


def parse_articles(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(SELECTORS["row"])
    
    if not rows:
        raise ScraperError("セール一覧の表が見つかりません。サイトの構成変更を確認してください。")

    logger.info(f"Found {len(rows)} table rows. Processing...")

    records = []
    parsed_count = 0
    for index, row in enumerate(rows, start=1):
        # 見出しだけは除外。店舗名の欠落を「対象なし」と取り違えない。
        if row.find("th") and not row.find("td"):
            continue

        def get_text(key):
            el = row.select_one(SELECTORS[key])
            return el.get_text(strip=True) if el else ""

        fields = {key: get_text(key) for key in
                  ("name", "address", "title", "date", "detail")}
        missing = [key for key, value in fields.items() if not value]
        if missing:
            raise ScraperError(
                f"セール一覧の{index}行目で必須項目が欠落しています: {', '.join(missing)}"
            )
        parsed_count += 1
        store = fields["name"]
        if not any(k in store for k in KEYWORDS):
            continue

        addr, title = fields["address"], fields["title"]
        term, detail = fields["date"], fields["detail"]
        notes = get_text("notes")
        body = textwrap.dedent(f"""\
                🛒 {store}
                {addr}
                {title}（{term}）
                {detail}
                {notes}""").rstrip()

        art_id = f"{store}_{term}"
        records.append((art_id, body, url))

    if not parsed_count:
        raise ScraperError("セール一覧に店舗の行がありません。取得結果を確認してください。")
    logger.info("Validated %d sale rows; %d target sale(s).", parsed_count, len(records))
    return records

def diff_since_last_run(records):
    fresh = []
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY)")
        seen = {row[0] for row in conn.execute("SELECT id FROM seen")}
        for record in records:
            if record[0] not in seen:
                fresh.append(record)
                seen.add(record[0])
    return fresh


def mark_seen(records):
    # LINE API が成功した後にだけ通知済みにする。
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.executemany("INSERT OR IGNORE INTO seen(id) VALUES(?)",
                         [(record[0],) for record in records])


def send_line_text(text):
    token = os.environ.get("LINE_TOKEN")
    if not token:
        raise ScraperError("LINE_TOKEN が設定されていません。")
    # 大きすぎる本文も成功扱いにせず、未送信として次回に残す。
    if len(text.encode("utf-16-le")) // 2 > 5000:
        raise ScraperError("LINE のメッセージ上限（5000文字）を超えています。")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text}]}
    try:
        response = requests.post("https://api.line.me/v2/bot/message/broadcast",
                                 json=payload, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise ScraperError("LINE への送信に失敗しました。通知済みにはしていません。") from e

def broadcast_line(msgs, page_url):
    if not msgs:
        logger.info("No new sale info to broadcast.")
        return

    text = HEADLINE + "\n\n".join(msgs) + f"\n\n🔗 一覧ページはこちら\n{page_url}"
    send_line_text(text)
    logger.info(f"Successfully broadcasted {len(msgs)} sale(s).")


def notify_failure(error):
    # 想定外の例外の本文はLINEに載せず、Actionsのログで確認する。
    reason = str(error) if isinstance(error, ScraperError) else type(error).__name__
    text = f"⚠️ KALDIセール通知でエラーが発生しました。\n{reason}\n\n一覧: {BASE_URL}"
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if repository and run_id:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        text += f"\n\n実行ログ: {server}/{repository}/actions/runs/{run_id}"
    send_line_text(text)


def main():
    try:
        # 全行の検証を終えてから送信する。後続行の欠落で部分送信しない。
        fresh = diff_since_last_run(fetch_target_articles())
        if not fresh:
            logger.info("No new sale info to broadcast.")
            return 0
        broadcast_line([record[1] for record in fresh], fresh[0][2])
        mark_seen(fresh)
        return 0
    except Exception as error:
        logger.exception("KALDI sale check failed.")
        try:
            notify_failure(error)
        except Exception:
            logger.exception("Failed to send the LINE error notification; check Actions logs.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
