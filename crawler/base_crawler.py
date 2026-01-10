"""
Base crawler class
Provides common HTTP request and page fetching functionality
"""

import time
import requests
from typing import Optional
from bs4 import BeautifulSoup
from utils.logger import Logger
from utils.url_validator import URLValidator
import config


class BaseCrawler:
    """Base crawler class"""

    def __init__(self):
        """Initialize crawler"""
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(config.DEFAULT_HEADERS)

    def fetch_page(self, url: str, timeout: int = None) -> Optional[str]:
        """
        Fetch page HTML content.
        
        Args:
            url: Target URL
            timeout: Timeout in seconds
            
        Returns:
            Optional[str]: HTML content, returns None on failure
        """
        # Validate URL
        if not URLValidator.is_valid_url(url):
            self.logger.error(f"Invalid URL: {url}")
            return None

        # Normalize URL
        url = URLValidator.normalize_url(url)
        
        if timeout is None:
            timeout = config.REQUEST_TIMEOUT

        # Retry mechanism
        for attempt in range(config.MAX_RETRIES):
            try:
                self.logger.info(f"Fetching page: {url} (attempt {attempt + 1}/{config.MAX_RETRIES})")
                
                response = self.session.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True
                )
                
                response.raise_for_status()
                
                # Check content type
                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type.lower():
                    self.logger.warning(f"Page content type is not HTML: {content_type}")
                
                self.logger.info(f"Successfully fetched page: {url}")
                return response.text

            except requests.exceptions.Timeout:
                self.logger.warning(f"Request timeout: {url}")
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Request failed: {url}, error: {str(e)}")
            except Exception as e:
                self.logger.error(f"Unknown error: {url}, error: {str(e)}")

            # Wait before retry
            if attempt < config.MAX_RETRIES - 1:
                wait_time = config.REQUEST_DELAY * (attempt + 1)
                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)

        self.logger.error(f"Reached max retry count, giving up: {url}")
        return None

    def parse_html(self, html: str, parser: str = "lxml") -> Optional[BeautifulSoup]:
        """
        Parse HTML content.
        
        Args:
            html: HTML string
            parser: Parser type (lxml, html.parser, html5lib)
            
        Returns:
            Optional[BeautifulSoup]: BeautifulSoup object, returns None on failure
        """
        try:
            soup = BeautifulSoup(html, parser)
            return soup
        except Exception as e:
            self.logger.error(f"HTML parsing failed: {str(e)}")
            return None

    def close(self):
        """Close Session"""
        if self.session:
            self.session.close()
            self.logger.info("Session closed")

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()










