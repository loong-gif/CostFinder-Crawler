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
    def normalize_url(url: str, default_protocol: str = "https") -> str:
        """
        Normalize URL, ensure it contains protocol.
        Preserves original protocol if present, otherwise adds default protocol.
        
        Args:
            url: Original URL
            default_protocol: Default protocol to use if URL has no protocol (default: "https")
            
        Returns:
            str: Normalized URL with protocol
        """
        if not url:
            return ""
        
        url = url.strip()
        
        # If URL already has protocol, preserve it
        if url.startswith("http://") or url.startswith("https://"):
            return url
        
        # If URL starts with //, add protocol
        if url.startswith("//"):
            return f"{default_protocol}:" + url
        
        # Otherwise, add protocol prefix
        return f"{default_protocol}://{url}"

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
    def normalize_domain(domain: str) -> str:
        """
        Normalize domain name by removing www. prefix and converting to lowercase.
        
        Args:
            domain: Domain name (e.g., www.example.com or example.com)
            
        Returns:
            str: Normalized domain name (e.g., example.com)
        """
        if not domain:
            return ""
        
        # Convert to lowercase
        domain = domain.lower().strip()
        
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        
        return domain

    @staticmethod
    def get_normalized_domain(url: str) -> str:
        """
        Extract and normalize domain from URL.
        
        Args:
            url: Full URL
            
        Returns:
            str: Normalized domain name
        """
        domain = URLValidator.get_domain(url)
        return URLValidator.normalize_domain(domain)

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
        domain1 = URLValidator.get_normalized_domain(url1)
        domain2 = URLValidator.get_normalized_domain(url2)
        return domain1 == domain2 and domain1 != ""










