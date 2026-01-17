"""
Async base crawler class
Provides async HTTP request and page fetching functionality using aiohttp
"""

import asyncio
import time
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup
from utils.logger import Logger
from utils.url_validator import URLValidator
from utils.rate_limiter import RateLimiter
import config


class AsyncBaseCrawler:
    """Async base crawler class"""

    def __init__(self):
        """Initialize async crawler"""
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Initialize rate limiter
        self.rate_limiter = RateLimiter(
            requests_per_second=config.RATE_LIMIT_REQUESTS_PER_SECOND,
            requests_per_minute=config.RATE_LIMIT_REQUESTS_PER_MINUTE,
            requests_per_hour=config.RATE_LIMIT_REQUESTS_PER_HOUR,
        )
        
        # Track last request time for delay between requests
        self.last_request_time = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
            self.session = aiohttp.ClientSession(
                headers=config.DEFAULT_HEADERS,
                timeout=timeout
            )
        return self.session

    async def fetch_page(self, url: str, timeout: Optional[int] = None) -> Optional[str]:
        """
        Fetch page HTML content asynchronously.
        
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

        # Apply rate limiting
        self.rate_limiter.acquire(wait=True)
        
        # Apply delay between requests
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time
        if time_since_last_request < config.CRAWL_DELAY_BETWEEN_REQUESTS:
            sleep_time = config.CRAWL_DELAY_BETWEEN_REQUESTS - time_since_last_request
            self.logger.debug(f"Sleeping {sleep_time:.2f} seconds before request")
            await asyncio.sleep(sleep_time)

        # Define retryable exceptions
        retryable_exceptions = [
            asyncio.TimeoutError,
            aiohttp.ClientError,
            aiohttp.ServerTimeoutError,
            aiohttp.ClientConnectorError,
            TimeoutError,
        ]

        # Define the actual request function
        async def _make_request():
            """Internal async function to make HTTP request"""
            self.logger.info(f"Fetching page: {url}")
            session = await self._get_session()
            
            try:
                async with session.get(url, allow_redirects=True) as response:
                    response.raise_for_status()
                    
                    # Update last request time
                    self.last_request_time = time.time()
                    
                    # Check content type
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" not in content_type.lower():
                        self.logger.warning(f"Page content type is not HTML: {content_type}")
                    
                    html = await response.text()
                    self.logger.info(f"Successfully fetched page: {url}")
                    return html
            except aiohttp.ClientResponseError as e:
                # HTTP errors (4xx, 5xx) are not retryable
                self.logger.error(f"HTTP error for {url}: {e.status} - {str(e)}")
                raise
            except Exception as e:
                self.logger.error(f"Request failed: {url}, error: {type(e).__name__}: {str(e)}")
                raise

        # Execute with retry mechanism (async version)
        last_exception = None
        for attempt in range(config.MAX_RETRIES):
            try:
                return await _make_request()
            except aiohttp.ClientResponseError as e:
                # HTTP errors (4xx, 5xx) are not retryable
                self.logger.error(f"HTTP error for {url}: {e.status} - {str(e)}")
                return None
            except Exception as e:
                last_exception = e
                
                # Check if exception is retryable
                is_retryable = isinstance(e, tuple(retryable_exceptions))
                
                if not is_retryable:
                    self.logger.warning(f"Non-retryable exception: {type(e).__name__}: {str(e)}")
                    return None
                
                # Check if we have more retries
                if attempt < config.MAX_RETRIES - 1:
                    self.logger.warning(
                        f"Attempt {attempt + 1}/{config.MAX_RETRIES} failed for {url}: "
                        f"{type(e).__name__}: {str(e)}. Retrying in {config.RETRY_DELAY} seconds..."
                    )
                    await asyncio.sleep(config.RETRY_DELAY)
                else:
                    self.logger.error(
                        f"All {config.MAX_RETRIES} attempts failed for {url}. "
                        f"Last error: {type(e).__name__}: {str(e)}"
                    )
        
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

    async def close(self):
        """Close Session"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.logger.info("Session closed")

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
