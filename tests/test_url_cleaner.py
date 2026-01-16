"""
Unit tests for URL cleaning functionality.
"""

import unittest
from clean_websites import clean_url_by_regex, ensure_www_prefix
from utils.url_cleaner import clean_url, clean_url_list


class TestURLCleaning(unittest.TestCase):
    """Test cases for URL cleaning functions."""

    def test_ensure_www_prefix(self):
        """Test ensure_www_prefix function."""
        self.assertEqual(ensure_www_prefix("example.com"), "www.example.com")
        self.assertEqual(ensure_www_prefix("www.example.com"), "www.example.com")
        self.assertEqual(ensure_www_prefix("subdomain.example.com"), "www.subdomain.example.com")

    def test_clean_url_by_regex_preserves_subdomain(self):
        """Test that clean_url_by_regex preserves subdomains."""
        url = "https://botox-it.ueniweb.com/about-us"
        cleaned = clean_url_by_regex(url)
        self.assertIn("botox-it.ueniweb.com", cleaned)
        self.assertTrue(cleaned.startswith("www."))

    def test_clean_url_by_regex_with_query(self):
        """Test clean_url_by_regex with ?q= parameter."""
        url = "https://example.com/?q=https://target.com/page"
        cleaned = clean_url_by_regex(url)
        self.assertIsInstance(cleaned, str)

    def test_clean_url_removes_subdomain(self):
        """Test that clean_url removes subdomains."""
        url = "https://subdomain.example.com/page"
        cleaned = clean_url(url)
        self.assertEqual(cleaned, "www.example.com")

    def test_clean_url_list(self):
        """Test clean_url_list function."""
        urls = [
            "https://example.com",
            "https://www.example.com",
            "https://sub.example.com"
        ]
        cleaned = clean_url_list(urls)
        self.assertIsInstance(cleaned, list)
        self.assertGreater(len(cleaned), 0)

    def test_clean_url_by_regex_empty_input(self):
        """Test clean_url_by_regex with empty input."""
        self.assertEqual(clean_url_by_regex(""), "")
        self.assertEqual(clean_url_by_regex("   "), "")

    def test_clean_url_empty_input(self):
        """Test clean_url with empty input."""
        self.assertEqual(clean_url(""), "")
        self.assertEqual(clean_url("   "), "")


if __name__ == '__main__':
    unittest.main()
