import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import scraper


def sale_row(store="浦和店", future=False):
    suffix = "_f" if future else ""
    return (
        '<tr><td><span class="salename">' + store + '</span>'
        '<span class="saleadress">埼玉県</span>'
        f'<span class="saletitle{suffix}">感謝セール</span></td><td>'
        f'<p class="saledate{suffix}">10/20～10/23</p>'
        '<p class="saledetail">10%OFF</p></td></tr>'
    )


def page(*rows):
    return '<table class="cz_sp_table"><tr><th>店舗</th></tr>' + ''.join(rows) + '</table>'


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "seen.db")
        self.db_patch = patch.object(scraper, "DB_FILE", self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.env = patch.dict(os.environ, {
            "LINE_TOKEN": "test-token",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "123",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.get = patch.object(scraper.requests, "get").start()
        self.post = patch.object(scraper.requests, "post").start()
        patch.object(scraper, "logger").start()
        self.addCleanup(patch.stopall)
        self.get.return_value.text = page(sale_row())

    def seen_ids(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return [row[0] for row in conn.execute("SELECT id FROM seen")]

    def test_non_target_sales_are_silent(self):
        self.get.return_value.text = page(sale_row("新宿店"))
        self.assertEqual(scraper.main(), 0)
        self.post.assert_not_called()
        self.assertEqual(self.seen_ids(), [])

    def test_success_is_recorded_only_after_line_accepts_and_not_repeated(self):
        def accept(*args, **kwargs):
            self.assertEqual(self.seen_ids(), [])
            return Mock()
        self.post.side_effect = accept
        self.assertEqual(scraper.main(), 0)
        self.assertEqual(self.seen_ids(), ["浦和店_10/20～10/23"])
        self.assertEqual(scraper.main(), 0)
        self.assertEqual(self.post.call_count, 1)

    def test_http_and_timeout_failures_notify_with_log_link(self):
        for error in (requests.Timeout(), requests.HTTPError()):
            with self.subTest(error=type(error).__name__):
                self.get.side_effect = error
                self.post.reset_mock()
                self.assertEqual(scraper.main(), 1)
                text = self.post.call_args.kwargs["json"]["messages"][0]["text"]
                self.assertIn("取得できません", text)
                self.assertIn("https://github.com/owner/repo/actions/runs/123", text)

    def test_http_status_is_checked(self):
        self.get.return_value.raise_for_status.side_effect = requests.HTTPError()
        self.assertEqual(scraper.main(), 1)
        self.post.assert_called_once()

    def test_missing_table_or_empty_table_is_failure(self):
        for html in ("<html>maintenance</html>", page()):
            with self.subTest(html=html):
                self.get.return_value.text = html
                self.assertEqual(scraper.main(), 1)

    def test_every_required_field_is_validated_on_non_target_rows(self):
        for field, value in (("salename", "新宿店"), ("saleadress", "埼玉県"),
                             ("saletitle", "感謝セール"), ("saledate", "10/20～10/23"),
                             ("saledetail", "10%OFF")):
            for missing_element in (False, True):
                with self.subTest(field=field, missing_element=missing_element):
                    row = sale_row("新宿店")
                    if missing_element:
                        row = row.replace(f'class="{field}"', 'class="changed"')
                    else:
                        row = row.replace(value, "")
                    with self.assertRaises(scraper.ScraperError):
                        scraper.parse_articles(page(row), "url")

    def test_later_invalid_row_prevents_sale_notification(self):
        self.get.return_value.text = page(sale_row(), sale_row("新宿店").replace("10%OFF", ""))
        self.assertEqual(scraper.main(), 1)
        self.post.assert_called_once()
        self.assertIn("必須項目", self.post.call_args.kwargs["json"]["messages"][0]["text"])
        self.assertFalse(Path(self.db).exists())

    def test_future_sales_and_optional_notes(self):
        records = scraper.parse_articles(page(sale_row(future=True)), "url")
        self.assertEqual(len(records), 1)
        self.assertIn("感謝セール（10/20～10/23）", records[0][1])

    def test_line_failure_keeps_sales_unseen_and_retries_next_run(self):
        self.post.return_value.raise_for_status.side_effect = requests.HTTPError()
        self.assertEqual(scraper.main(), 1)
        self.assertEqual(self.seen_ids(), [])
        self.post.return_value.raise_for_status.side_effect = None
        self.assertEqual(scraper.main(), 0)
        self.assertEqual(self.seen_ids(), ["浦和店_10/20～10/23"])

    def test_missing_token_fails_without_marking_seen(self):
        del os.environ["LINE_TOKEN"]
        self.assertEqual(scraper.main(), 1)
        self.post.assert_not_called()
        self.assertEqual(self.seen_ids(), [])

    def test_db_failure_is_not_silently_ignored(self):
        with patch.object(scraper, "DB_FILE", self.temp.name):
            self.assertEqual(scraper.main(), 1)
        self.post.assert_called_once()

    def test_duplicate_rows_are_not_sent_twice(self):
        self.get.return_value.text = page(sale_row(), sale_row())
        self.assertEqual(scraper.main(), 0)
        text = self.post.call_args.kwargs["json"]["messages"][0]["text"]
        self.assertEqual(text.count("🛒 浦和店"), 1)

    def test_long_message_is_not_marked_seen(self):
        self.get.return_value.text = page(sale_row().replace("10%OFF", "あ" * 5000))
        self.assertEqual(scraper.main(), 1)
        self.assertEqual(self.seen_ids(), [])
        self.post.assert_called_once()  # エラー通知のみ


if __name__ == "__main__":
    unittest.main()
