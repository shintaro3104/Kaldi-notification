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
from bs4 import BeautifulSoup

# --- Configuration & Constants ---
BASE_URL = "https://map.kaldi.co.jp/kaldi/articleList"
DB_FILE = "seen.db"
KEYWORDS = ["浦和", "赤羽", "川口", "レイクタウン", "与野", "戸田", "銀座"]
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

def build_url() -> str:
    """現在 JST のタイムスタンプを kkw001 に付けた URL を返す"""
    jst_now = datetime.datetime.now()
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
        logger.error(f"Failed to fetch Kaldi website: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select(SELECTORS["row"])
    
    if not rows:
        logger.warning(f"No rows found using selector '{SELECTORS['row']}'. Has the website layout changed?")
        return

    logger.info(f"Found {len(rows)} table rows. Processing...")

    for row in rows:
        try:
            name_tag = row.select_one(SELECTORS["name"])
            if not name_tag:
                continue
            
            store = name_tag.text.strip()
            
            # 店舗名が KEYWORDS のどれにもヒットしなければスキップ
            if not any(k in store for k in KEYWORDS):
                continue

            def get_text(selector, default=""):
                el = row.select_one(selector)
                return el.text.strip() if el else default

            addr = get_text(SELECTORS["address"])
            title = get_text(SELECTORS["title"])
            term = get_text(SELECTORS["date"])
            detail = get_text(SELECTORS["detail"])
            notes = get_text(SELECTORS["notes"])

            body = textwrap.dedent(f"""\
                🛒 {store}
                {addr}
                {title}（{term}）
                {detail}
                {notes}""").rstrip()

            art_id = f"{store}_{term}"
            yield art_id, body, url
            
        except Exception as e:
            logger.error(f"Error parsing a row: {e}")
            continue

def diff_since_last_run(records):
    new_msgs = []
    page_url = None

    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY)")
            for art_id, msg, url in records:
                exists = conn.execute("SELECT 1 FROM seen WHERE id=?", (art_id,)).fetchone()
                if not exists:
                    new_msgs.append(msg)
                    conn.execute("INSERT INTO seen(id) VALUES(?)", (art_id,))
                page_url = url
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")

    return new_msgs, page_url

def broadcast_line(msgs, page_url):
    if not msgs:
        logger.info("No new sale info to broadcast.")
        return

    token = os.environ.get("LINE_TOKEN")
    if not token:
        logger.warning("LINE_TOKEN not set. Skipping broadcast.")
        return

    text = HEADLINE + "\n\n".join(msgs) + f"\n\n🔗 一覧ページはこちら\n{page_url}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text}]}

    try:
        r = requests.post("https://api.line.me/v2/bot/message/broadcast",
                          json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        logger.info(f"Successfully broadcasted {len(msgs)} sale(s).")
    except requests.RequestException as e:
        logger.error(f"Failed to broadcast to LINE: {e}")

if __name__ == "__main__":
    fresh_msgs, list_page_url = diff_since_last_run(fetch_target_articles())
    broadcast_line(fresh_msgs, list_page_url)
