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
from utils.retry_handler import RetryHandler
from utils.rate_limiter import RateLimiter
import config


class BaseCrawler:
    """Base crawler class"""

    def __init__(self):
        """Initialize crawler"""
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(config.DEFAULT_HEADERS)
        
        # Initialize rate limiter
        self.rate_limiter = RateLimiter(
            requests_per_second=config.RATE_LIMIT_REQUESTS_PER_SECOND,
            requests_per_minute=config.RATE_LIMIT_REQUESTS_PER_MINUTE,
            requests_per_hour=config.RATE_LIMIT_REQUESTS_PER_HOUR,
        )
        
        # Track last request time for delay between requests
        self.last_request_time = 0

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

        # Define retryable exceptions
        retryable_exceptions = [
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            TimeoutError,
        ]

        # Create retry handler
        retry_handler = RetryHandler(
            max_retries=config.MAX_RETRIES,
            timeout=None,  # Timeout is handled by requests library
            retry_delay=config.RETRY_DELAY,
            retryable_exceptions=retryable_exceptions,
            logger=self.logger,
        )

        # Define the actual request function
        def _make_request():
            """Internal function to make HTTP request"""
            # Apply rate limiting
            self.rate_limiter.acquire(wait=True)
            
            # Apply delay between requests
            current_time = time.time()
            time_since_last_request = current_time - self.last_request_time
            if time_since_last_request < config.CRAWL_DELAY_BETWEEN_REQUESTS:
                sleep_time = config.CRAWL_DELAY_BETWEEN_REQUESTS - time_since_last_request
                self.logger.debug(f"Sleeping {sleep_time:.2f} seconds before request")
                time.sleep(sleep_time)
            
            self.logger.info(f"Fetching page: {url}")
            response = self.session.get(
                url,
                timeout=timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            
            # Update last request time
            self.last_request_time = time.time()
            
            # Check content type
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type.lower():
                self.logger.warning(f"Page content type is not HTML: {content_type}")
            
            self.logger.info(f"Successfully fetched page: {url}")
            return response.text

        # Execute with retry mechanism
        try:
            return retry_handler.execute(_make_request)
        except requests.exceptions.HTTPError as e:
            # HTTP errors (4xx, 5xx) are not retryable
            self.logger.error(f"HTTP error for {url}: {e.response.status_code} - {str(e)}")
            return None
        except Exception as e:
            # Other non-retryable exceptions
            self.logger.error(f"Failed to fetch page {url}: {type(e).__name__}: {str(e)}")
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










