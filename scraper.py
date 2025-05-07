# -*- coding: utf-8 -*-
"""
KALDI セール一覧をクロールして
  1) 店名に KEYWORDS が含まれる行を抽出（部分一致）
  2) 店舗名 / 住所 / 期間 / 内容 / 補記 をまとめて1メッセージ化
  3) (店舗 + 期間) が未送信なら LINE Push
  4) Push 冒頭に固定ヘッダ、末尾にセール一覧ページの URL
"""

import os, sqlite3, urllib.parse, requests, datetime, textwrap
from bs4 import BeautifulSoup

BASE_URL = "https://map.kaldi.co.jp/kaldi/articleList"
DB_FILE  = "seen.db"

# ───────── 店舗名部分一致（埼玉近辺の例） ──────────
KEYWORDS = ["浦和", "赤羽", "川口", "レイクタウン"]
# ──────────────────────────────────────────────

HEADLINE = "☕️ KALDIの新着セール情報が届いたよ！\n\n"

def build_url() -> str:
    """現在 JST のタイムスタンプを kk w001 に付けた URL を返す"""
    jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    ts = jst_now.strftime("%Y-%m-%dT%H:%M:%S")
    params = dict(account="kaldi", accmd=1, ftop=1, kkw001=ts)
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"

def fetch_target_articles():
    url  = build_url()
    html = requests.get(url, timeout=15).text
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.select("table.cz_sp_table tr"):
        name_tag = row.select_one("span.salename")
        if not name_tag:
            continue
        store = name_tag.text.strip()

        if not any(k in store for k in KEYWORDS):
            continue

        # ── 必要な要素を抽出 ───────────────────
        addr  = row.select_one("span.saleadress").text.strip()
        title = row.select_one("span.saletitle, span.saletitle_f").text.strip()
        term  = row.select_one("p.saledate, p.saledate_f").text.strip()

        detail  = row.select_one("p.saledetail").text.strip()
        note_el = row.select_one("p.saledetail_notes")
        notes   = note_el.text.strip() if note_el else ""

        # 1店舗ぶんのテキスト
        body = textwrap.dedent(f"""\
            🛒 {store}
            {addr}
            {title}（{term}）
            {detail}
            {notes}""").rstrip()

        art_id = f"{store}_{term}"
        yield art_id, body, url            # ← url は末尾リンク用に返す

def diff_since_last_run(records):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY)")
    new_msgs, page_url = [], None
    for art_id, msg, url in records:
        if not conn.execute("SELECT 1 FROM seen WHERE id=?", (art_id,)).fetchone():
            new_msgs.append(msg)
            conn.execute("INSERT INTO seen(id) VALUES(?)", (art_id,))
        page_url = url                     # 同じ URL が続くので最後の値で OK
    conn.commit(); conn.close()
    return new_msgs, page_url

def push_line(msgs, page_url):
    if not msgs:
        print("No new sale info.")
        return
    # ① ヘッダ ②店舗ごとの塊 ③末尾リンク を結合
    text = HEADLINE + "\n\n".join(msgs) + f"\n\n🔗 一覧ページはこちら\n{page_url}"

    headers = {
        "Authorization": f"Bearer {os.environ['LINE_TOKEN']}",
        "Content-Type": "application/json",
    }
    payload = {"to": os.environ["LINE_USER_ID"],
               "messages": [{"type": "text", "text": text}]}
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      json=payload, headers=headers, timeout=10)
    r.raise_for_status()
    print(f"Pushed {len(msgs)} sale(s) to LINE.")

if __name__ == "__main__":
    fresh, page = diff_since_last_run(fetch_target_articles())
    push_line(fresh, page)
