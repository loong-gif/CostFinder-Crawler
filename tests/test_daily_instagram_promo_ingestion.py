import unittest
from argparse import Namespace
from datetime import date

from scripts import daily_instagram_promo_ingestion as ingestion


class DailyInstagramPromoIngestionTests(unittest.TestCase):
    def test_resolve_target_date_window_single_day(self) -> None:
        start_date, end_date = ingestion.resolve_target_date_window(date(2026, 5, 5), 1)

        self.assertEqual(start_date, date(2026, 5, 5))
        self.assertEqual(end_date, date(2026, 5, 5))

    def test_resolve_target_date_window_last_seven_days(self) -> None:
        start_date, end_date = ingestion.resolve_target_date_window(date(2026, 5, 5), 7)

        self.assertEqual(start_date, date(2026, 4, 29))
        self.assertEqual(end_date, date(2026, 5, 5))

    def test_resolve_only_posts_newer_than_defaults_to_window_days(self) -> None:
        args = Namespace(only_posts_newer_than=None, lookback_days=7)

        self.assertEqual(ingestion.resolve_only_posts_newer_than(args), "7 days")

    def test_instagram_target_is_explicitly_instantiable(self) -> None:
        target = ingestion.InstagramTarget(
            master_id=1,
            business_id=2,
            name="Demo Spa",
            instagram_url="https://www.instagram.com/demo_spa",
        )

        self.assertEqual(target.master_id, 1)
        self.assertEqual(target.business_id, 2)
        self.assertEqual(target.name, "Demo Spa")
        self.assertEqual(target.instagram_url, "https://www.instagram.com/demo_spa")

    def test_collect_posts_in_window_keeps_only_last_seven_days_and_dedupes(self) -> None:
        raw_posts = [
            {
                "inputUrl": "https://www.instagram.com/demo_spa/",
                "url": "https://www.instagram.com/p/WITHIN1/",
                "caption": "$99 intro facial special",
                "timestamp": "2026-05-05T01:00:00+00:00",
            },
            {
                "inputUrl": "https://www.instagram.com/demo_spa/",
                "url": "https://www.instagram.com/p/WITHIN1/?utm_source=ig_web_copy_link",
                "caption": "$99 intro facial special",
                "timestamp": "2026-05-05T01:00:00+00:00",
            },
            {
                "inputUrl": "https://www.instagram.com/demo_spa/",
                "url": "https://www.instagram.com/p/WITHIN2/",
                "caption": "Members only discount this week",
                "timestamp": "2026-04-29T03:00:00+00:00",
            },
            {
                "inputUrl": "https://www.instagram.com/demo_spa/",
                "url": "https://www.instagram.com/p/TOO_OLD/",
                "caption": "$79 peel special",
                "timestamp": "2026-04-28T15:00:00+00:00",
            },
        ]

        collected = ingestion.collect_posts_in_window(
            raw_posts,
            start_date=date(2026, 4, 29),
            end_date=date(2026, 5, 5),
            timezone_name="Asia/Shanghai",
        )

        self.assertEqual(len(collected), 2)
        self.assertEqual(
            {(post["url"], post["local_post_date"]) for post in collected},
            {
                ("https://www.instagram.com/p/WITHIN1", "2026-05-05"),
                ("https://www.instagram.com/p/WITHIN2", "2026-04-29"),
            },
        )
        self.assertTrue(all(post["inputUrl"] == "https://www.instagram.com/demo_spa" for post in collected))

    def test_fetch_existing_post_keys_uses_local_post_date_window_filter(self) -> None:
        class StubClient:
            def __init__(self) -> None:
                self.calls = []

            def fetch_rows(self, table, select, **kwargs):
                self.calls.append({"table": table, "select": select, **kwargs})
                return []

        client = StubClient()

        keys = ingestion.fetch_existing_post_keys(
            client,
            start_date=date(2026, 4, 29),
            end_date=date(2026, 5, 5),
            timezone_name="Asia/Shanghai",
            available_columns={"local_post_date", "post_url"},
        )

        self.assertEqual(keys, set())
        self.assertEqual(client.calls[0]["filters"], {"and": "(local_post_date.gte.2026-04-29,local_post_date.lte.2026-05-05)"})


if __name__ == "__main__":
    unittest.main()
