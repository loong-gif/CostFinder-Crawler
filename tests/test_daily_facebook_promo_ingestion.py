import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from scripts import daily_facebook_promo_ingestion as ingestion


class DailyFacebookPromoIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_resolve_report_path_uses_daily_facebook_prefix(self) -> None:
        now = datetime(2026, 5, 5, 12, 30, 45, 123456, tzinfo=ZoneInfo("Asia/Shanghai"))

        report_path = ingestion.resolve_report_path(now)

        self.assertTrue(str(report_path).endswith("facebook_promo_daily_ingestion_20260505_123045_123456.json"))
        self.assertEqual(report_path.parent, ingestion.OUTPUT_DIR)

    def test_fetch_existing_post_keys_uses_local_post_date_eq_filter(self) -> None:
        class StubClient:
            def __init__(self) -> None:
                self.calls = []

            def fetch_rows(self, table, select, **kwargs):
                self.calls.append({"table": table, "select": select, **kwargs})
                return []

        client = StubClient()

        keys = ingestion.fetch_existing_post_keys(
            client,
            target_date=date(2026, 5, 5),
            timezone_name="Asia/Shanghai",
            available_columns={"local_post_date", "post_url"},
        )

        self.assertEqual(keys, set())
        self.assertEqual(client.calls[0]["filters"], {"local_post_date": "eq.2026-05-05"})

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

            artifacts = list(self.output_dir.glob("facebook_promo_daily_ingestion_*.json"))
            self.assertEqual(len(artifacts), 1)
            report = json.loads(artifacts[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "error")
            self.assertIn("SUPABASE_URL", report["error"])

    def test_main_dry_run_success_exits_zero(self) -> None:
        target = ingestion.FacebookTarget(
            master_id=1,
            business_id=2,
            name="Demo Spa",
            facebook_url="https://www.facebook.com/demo_spa",
        )
        raw_posts = [
            {
                "inputUrl": "https://www.facebook.com/demo_spa",
                "url": "https://www.facebook.com/demo_spa/posts/123",
                "text": "$99 intro facial special",
                "time": "2026-05-05T01:00:00+00:00",
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
                        with mock.patch.object(ingestion, "fetch_facebook_targets", return_value=[target]):
                            with mock.patch.object(
                                ingestion,
                                "fetch_posts_from_actor",
                                return_value=(raw_posts, []),
                            ):
                                with mock.patch.object(ingestion, "fetch_existing_post_keys", return_value=set()):
                                    ingestion.main()

        artifacts = list(self.output_dir.glob("facebook_promo_daily_ingestion_*.json"))
        self.assertEqual(len(artifacts), 1)
        report = json.loads(artifacts[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["dry_run"])

    def test_insert_rows_with_fallback_tries_second_variant_after_first_failure(self) -> None:
        client = mock.Mock()
        client.insert_rows.side_effect = [RuntimeError("unknown column"), None]
        post = {
            "inputUrl": "https://www.facebook.com/demo_spa",
            "url": "https://www.facebook.com/demo_spa/posts/123",
            "local_post_date": "2026-05-05",
        }
        target = ingestion.FacebookTarget(
            master_id=1,
            business_id=2,
            name="Demo Spa",
            facebook_url="https://www.facebook.com/demo_spa",
        )

        inserted, errors, with_business_id = ingestion.insert_rows_with_fallback(
            client,
            posts=[post],
            target_lookup={"https://www.facebook.com/demo_spa": target},
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
            "actor_id": "apify/facebook-posts-scraper",
            "results_limit": 20,
            "batch_size": 10,
            "batch_concurrency": 1,
            "actor_timeout_secs": 1800,
            "only_posts_newer_than": "7 days",
            "timezone": "Asia/Shanghai",
            "local_date": "2026-05-05",
            "limit": None,
            "dry_run": False,
            "fixture_posts_json": None,
            "fixture_start_urls_json": None,
        }
        defaults.update(overrides)
        return Namespace(**defaults)


if __name__ == "__main__":
    unittest.main()
