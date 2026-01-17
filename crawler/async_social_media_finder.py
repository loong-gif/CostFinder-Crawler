"""
Async social media finder
Main async crawler logic implementation
"""

import asyncio
from datetime import datetime
from typing import Dict, List
from crawler.async_crawler import AsyncBaseCrawler
from crawler.parsers import SocialMediaParser
from utils.logger import Logger
import config


class AsyncSocialMediaFinder(AsyncBaseCrawler):
    """Async social media information finder"""

    def __init__(self):
        """Initialize async finder"""
        super().__init__()
        self.parser = SocialMediaParser()
        self.logger = Logger.get_logger(self.__class__.__name__)

    async def find(self, url: str) -> Dict:
        """
        Find social media information from specified website asynchronously.
        
        Args:
            url: Target website URL
            
        Returns:
            Dict: Dictionary containing social media information
        """
        self.logger.info(f"Starting to find social media information: {url}")
        
        # Initialize result with all platforms dynamically
        result = {
            "url": url,
            "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success",
            "message": "",
        }
        
        # Initialize all platform lists
        for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
            result[platform] = []

        try:
            # Get page content
            html = await self.fetch_page(url)
            
            if not html:
                result["status"] = "failed"
                result["message"] = "Unable to fetch page content"
                return result

            # Parse HTML
            soup = self.parse_html(html)
            
            if not soup:
                result["status"] = "failed"
                result["message"] = "HTML parsing failed"
                return result

            # Extract social media links
            social_media_links = self.parser.extract_links_from_html(
                str(soup), url
            )

            # Update results for all platforms
            total_found = 0
            platform_counts = []
            
            for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
                platform_links = social_media_links.get(platform, [])
                result[platform] = platform_links
                count = len(platform_links)
                total_found += count
                if count > 0:
                    platform_counts.append(f"{platform.capitalize()}: {count}")

            result["message"] = f"Successfully found {total_found} social media accounts"
            
            if platform_counts:
                self.logger.info(f"Search completed - {', '.join(platform_counts)}")
            else:
                self.logger.info("Search completed - No social media links found")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Error occurred: {str(e)}"
            self.logger.error(f"Error occurred during search: {str(e)}")

        return result

    async def find_multiple(self, urls: List[str]) -> List[Dict]:
        """
        Batch find social media information from multiple websites sequentially.
        Processes one URL at a time to prevent IP blocking.
        
        Args:
            urls: List of target website URLs
            
        Returns:
            List[Dict]: List containing all results
        """
        results = []
        
        for i, url in enumerate(urls, 1):
            self.logger.info(f"Processing website {i}/{len(urls)}: {url}")
            result = await self.find(url)
            results.append(result)
            
            # Add delay between domains
            if i < len(urls):
                self.logger.debug(f"Waiting {config.CRAWL_DELAY_BETWEEN_DOMAINS} seconds before next domain")
                await asyncio.sleep(config.CRAWL_DELAY_BETWEEN_DOMAINS)

        return results
