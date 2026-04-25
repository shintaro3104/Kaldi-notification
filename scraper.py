# -*- coding: utf-8 -*-
"""
KALDI セール一覧をクロール（LINE Messaging API: Broadcast 版）
  1) 店名に KEYWORDS が含まれる行を抽出（部分一致）
  2) 店舗名 / 住所 / 期間 / 内容 / 補記 をまとめて1メッセージ化
  3) (店舗 + 期間) が未送信なら LINE Broadcast
  4) Broadcast 冒頭に固定ヘッダ、末尾にセール一覧ページの URL

必要な環境変数:
  - LINE_TOKEN: Messaging API チャネルアクセストークン（長期）
"""

import os
import sqlite3
import urllib.parse
import requests
import datetime
import textwrap
from bs4 import BeautifulSoup

BASE_URL = "https://map.kaldi.co.jp/kaldi/articleList"
DB_FILE = "seen.db"

# ───────── 店舗名部分一致（埼玉近辺の例） ──────────
KEYWORDS = ["浦和", "赤羽", "レイクタウン", "与野", "戸田"]
# ──────────────────────────────────────────────

HEADLINE = "☕️ KALDIの新着セール情報が届いたよ！\n\n"


def build_url() -> str:
    """現在 JST のタイムスタンプを kkw001 に付けた URL を返す"""
    jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    ts = jst_now.strftime("%Y-%m-%dT%H:%M:%S")
    params = dict(account="kaldi", accmd=1, ftop=1, kkw001=ts)
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def fetch_target_articles():
    url = build_url()
    html = requests.get(url, timeout=15).text
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.select("table.cz_sp_table tr"):
        name_tag = row.select_one("span.salename")
        if not name_tag:
            continue

        store = name_tag.text.strip()

        # 店舗名が KEYWORDS のどれにもヒットしなければスキップ
        if not any(k in store for k in KEYWORDS):
            continue

        # ── 必要な要素を抽出 ───────────────────
        addr_el = row.select_one("span.saleadress")
        title_el = row.select_one("span.saletitle, span.saletitle_f")
        term_el = row.select_one("p.saledate, p.saledate_f")
        detail_el = row.select_one("p.saledetail")

        # 想定外のHTML変化に備え、要素が欠けたらスキップ
        if not (addr_el and title_el and term_el and detail_el):
            continue

        addr = addr_el.text.strip()
        title = title_el.text.strip()
        term = term_el.text.strip()

        detail = detail_el.text.strip()
        note_el = row.select_one("p.saledetail_notes")
        notes = note_el.text.strip() if note_el else ""

        # 1店舗ぶんのテキスト
        body = textwrap.dedent(f"""\
            🛒 {store}
            {addr}
            {title}（{term}）
            {detail}
            {notes}""").rstrip()

        # 既読判定用ID（同一店舗・同一期間は二重送信しない）
        art_id = f"{store}_{term}"

        # url は末尾リンク用に返す（同じURLが続くのでどれでもOK）
        yield art_id, body, url


def diff_since_last_run(records):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY)")

    new_msgs = []
    page_url = None

    for art_id, msg, url in records:
        exists = conn.execute("SELECT 1 FROM seen WHERE id=?", (art_id,)).fetchone()
        if not exists:
            new_msgs.append(msg)
            conn.execute("INSERT INTO seen(id) VALUES(?)", (art_id,))
        page_url = url

    conn.commit()
    conn.close()
    return new_msgs, page_url


def broadcast_line(msgs, page_url):
    if not msgs:
        print("No new sale info.")
        return

    token = os.environ.get("LINE_TOKEN")
    if not token:
        raise RuntimeError("Environment variable LINE_TOKEN is not set.")

    # ① ヘッダ ②店舗ごとの塊 ③末尾リンク を結合
    text = HEADLINE + "\n\n".join(msgs) + f"\n\n🔗 一覧ページはこちら\n{page_url}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Broadcastは to が不要（友だち全員宛て）
    payload = {"messages": [{"type": "text", "text": text}]}

    r = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        json=payload,
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()
    print(f"Broadcasted {len(msgs)} sale(s).")


if __name__ == "__main__":
    fresh, page = diff_since_last_run(fetch_target_articles())
    broadcast_line(fresh, page)
