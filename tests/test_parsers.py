"""
Unit tests for social media parsers.
"""

import unittest
from crawler.parsers import SocialMediaParser


class TestSocialMediaParser(unittest.TestCase):
    """Test cases for SocialMediaParser class."""

    def setUp(self):
        """Set up test fixtures."""
        self.parser = SocialMediaParser()

    def test_parse_instagram_link(self):
        """Test parsing Instagram links."""
        html = '<a href="https://instagram.com/testuser">Instagram</a>'
        result = self.parser.extract_links_from_html(html, "https://example.com")
        self.assertIn("instagram", result)
        if result["instagram"]:
            self.assertIn("username", result["instagram"][0])

    def test_parse_facebook_link(self):
        """Test parsing Facebook links."""
        html = '<a href="https://facebook.com/testpage">Facebook</a>'
        result = self.parser.extract_links_from_html(html, "https://example.com")
        self.assertIn("facebook", result)

    def test_is_valid_username(self):
        """Test username validation."""
        self.assertTrue(self.parser._is_valid_username("testuser"))
        self.assertFalse(self.parser._is_valid_username("login"))
        self.assertFalse(self.parser._is_valid_username(""))
        self.assertFalse(self.parser._is_valid_username("signup"))

    def test_identify_platform(self):
        """Test platform identification."""
        self.assertEqual(self.parser._identify_platform("https://instagram.com/user"), "instagram")
        self.assertEqual(self.parser._identify_platform("https://facebook.com/page"), "facebook")
        self.assertIsNone(self.parser._identify_platform("https://example.com"))


if __name__ == '__main__':
    unittest.main()
