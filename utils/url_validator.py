"""
URL validation utility
Provides URL validation and normalization functionality.
"""

import validators
from urllib.parse import urlparse, urlunparse


class URLValidator:
    """URL validation and processing class"""

    @staticmethod
    def is_valid_url(url: str) -> bool:
        """
        Validate if URL is valid.
        
        Args:
            url: URL to validate
            
        Returns:
            bool: True if URL is valid, False otherwise
        """
        if not url:
            return False
        return validators.url(url) is True

    @staticmethod
    def normalize_url(url: str) -> str:
        """
        Normalize URL, ensure it contains protocol.
        
        Args:
            url: Original URL
            
        Returns:
            str: Normalized URL
        """
        if not url:
            return ""
        
        # If URL doesn't contain protocol, add https://
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        
        return url

    @staticmethod
    def get_domain(url: str) -> str:
        """
        Extract domain from URL.
        
        Args:
            url: Full URL
            
        Returns:
            str: Domain name
        """
        try:
            parsed = urlparse(url)
            return parsed.netloc
        except (ValueError, AttributeError):
            return ""

    @staticmethod
    def is_same_domain(url1: str, url2: str) -> bool:
        """
        Check if two URLs belong to the same domain.
        
        Args:
            url1: First URL
            url2: Second URL
            
        Returns:
            bool: True if same domain, False otherwise
        """
        return URLValidator.get_domain(url1) == URLValidator.get_domain(url2)










