import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from scripts import daily_instagram_promo_ingestion as ingestion


class DailyInstagramPromoIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_resolve_report_path_daily_vs_weekly(self) -> None:
        now = datetime(2026, 5, 5, 12, 30, 45, 123456, tzinfo=ZoneInfo("Asia/Shanghai"))

        daily_path = ingestion.resolve_report_path(now, 1)
        weekly_path = ingestion.resolve_report_path(now, 7)

        self.assertTrue(str(daily_path).endswith("instagram_promo_daily_ingestion_20260505_123045_123456.json"))
        self.assertTrue(str(weekly_path).endswith("instagram_promo_weekly_ingestion_20260505_123045_123456.json"))

    def test_main_error_writes_artifact_and_exits_nonzero(self) -> None:
        with mock.patch.object(ingestion, "OUTPUT_DIR", self.output_dir):
            with mock.patch.object(ingestion, "parse_args", return_value=self._base_args()):
                with mock.patch.object(
                    ingestion,
                    "load_supabase_client",
                    side_effect=RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY"),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        ingestion.main()
                    self.assertEqual(ctx.exception.code, 1)

            artifacts = list(self.output_dir.glob("instagram_promo_*_ingestion_*.json"))
            self.assertEqual(len(artifacts), 1)
            report = json.loads(artifacts[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "error")
            self.assertIn("SUPABASE_URL", report["error"])

    def test_main_dry_run_success_exits_zero(self) -> None:
        target = ingestion.InstagramTarget(
            master_id=1,
            business_id=2,
            name="Demo Spa",
            instagram_url="https://www.instagram.com/demo_spa",
        )
        raw_posts = [
            {
                "inputUrl": "https://www.instagram.com/demo_spa/",
                "url": "https://www.instagram.com/p/WITHIN1/",
                "caption": "$99 intro facial special",
                "timestamp": "2026-05-05T01:00:00+00:00",
            }
        ]

        with mock.patch.object(ingestion, "OUTPUT_DIR", self.output_dir):
            with mock.patch.object(ingestion, "parse_args", return_value=self._base_args(dry_run=True)):
                with mock.patch.object(ingestion, "load_supabase_client", return_value=mock.Mock()):
                    with mock.patch.object(
                        ingestion,
                        "detect_table_columns",
                        return_value={"local_post_date", "post_url", "platform"},
                    ):
                        with mock.patch.object(ingestion, "fetch_instagram_targets", return_value=[target]):
                            with mock.patch.object(
                                ingestion,
                                "fetch_posts_from_actor",
                                return_value=(raw_posts, []),
                            ):
                                with mock.patch.object(ingestion, "fetch_existing_post_keys", return_value=set()):
                                    ingestion.main()

        artifacts = list(self.output_dir.glob("instagram_promo_*_ingestion_*.json"))
        self.assertEqual(len(artifacts), 1)
        report = json.loads(artifacts[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["dry_run"])

    def test_insert_rows_with_fallback_tries_second_variant_after_first_failure(self) -> None:
        client = mock.Mock()
        client.insert_rows.side_effect = [RuntimeError("unknown column"), None]
        post = {
            "inputUrl": "https://www.instagram.com/demo_spa/",
            "url": "https://www.instagram.com/p/WITHIN1/",
            "local_post_date": "2026-05-05",
        }
        target = ingestion.InstagramTarget(
            master_id=1,
            business_id=2,
            name="Demo Spa",
            instagram_url="https://www.instagram.com/demo_spa",
        )

        inserted, errors, with_business_id = ingestion.insert_rows_with_fallback(
            client,
            posts=[post],
            target_lookup={"https://www.instagram.com/demo_spa/": target},
            available_columns=set(),
            run_timestamp="2026-05-05T00:00:00+00:00",
            dry_run=False,
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(errors, [])
        self.assertEqual(with_business_id, 1)
        self.assertEqual(client.insert_rows.call_count, 2)

    def _base_args(self, **overrides):
        defaults = {
            "actor_id": "apify/instagram-scraper",
            "results_limit": 12,
            "batch_size": 25,
            "actor_timeout_secs": 1800,
            "only_posts_newer_than": None,
            "timezone": "Asia/Shanghai",
            "local_date": "2026-05-05",
            "lookback_days": 1,
            "limit": None,
            "dry_run": False,
            "fixture_posts_json": None,
            "fixture_direct_urls_json": None,
        }
        defaults.update(overrides)
        return Namespace(**defaults)

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
