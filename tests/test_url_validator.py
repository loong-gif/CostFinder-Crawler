"""
Unit tests for URL validation functionality.
"""

import unittest
from utils.url_validator import URLValidator


class TestURLValidator(unittest.TestCase):
    """Test cases for URLValidator class."""

    def test_is_valid_url(self):
        """Test URL validation."""
        self.assertTrue(URLValidator.is_valid_url("https://example.com"))
        self.assertTrue(URLValidator.is_valid_url("http://example.com"))
        self.assertFalse(URLValidator.is_valid_url("not-a-url"))
        self.assertFalse(URLValidator.is_valid_url(""))
        self.assertFalse(URLValidator.is_valid_url(None))

    def test_normalize_url(self):
        """Test URL normalization."""
        self.assertEqual(URLValidator.normalize_url("example.com"), "https://example.com")
        self.assertEqual(URLValidator.normalize_url("http://example.com"), "http://example.com")
        self.assertEqual(URLValidator.normalize_url("https://example.com"), "https://example.com")
        self.assertEqual(URLValidator.normalize_url(""), "")

    def test_get_domain(self):
        """Test domain extraction."""
        self.assertEqual(URLValidator.get_domain("https://example.com/page"), "example.com")
        self.assertEqual(URLValidator.get_domain("http://www.example.com"), "www.example.com")
        self.assertEqual(URLValidator.get_domain("invalid"), "")

    def test_is_same_domain(self):
        """Test same domain check."""
        self.assertTrue(URLValidator.is_same_domain(
            "https://example.com/page1",
            "https://example.com/page2"
        ))
        self.assertFalse(URLValidator.is_same_domain(
            "https://example.com",
            "https://other.com"
        ))


if __name__ == '__main__':
    unittest.main()
