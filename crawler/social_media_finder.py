"""
Social media finder
Main crawler logic implementation
"""

from datetime import datetime
from typing import Dict, Optional
from crawler.base_crawler import BaseCrawler
from crawler.parsers import SocialMediaParser
from utils.logger import Logger


class SocialMediaFinder(BaseCrawler):
    """Social media information finder"""

    def __init__(self):
        """Initialize finder"""
        super().__init__()
        self.parser = SocialMediaParser()
        self.logger = Logger.get_logger(self.__class__.__name__)

    def find(self, url: str) -> Dict:
        """
        Find social media information from specified website.
        
        Args:
            url: Target website URL
            
        Returns:
            Dict: Dictionary containing social media information
        """
        self.logger.info(f"Starting to find social media information: {url}")
        
        result = {
            "url": url,
            "instagram": [],
            "facebook": [],
            "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success",
            "message": "",
        }

        try:
            # Get page content
            html = self.fetch_page(url)
            
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

            # Update results
            result["instagram"] = social_media_links.get("instagram", [])
            result["facebook"] = social_media_links.get("facebook", [])

            # Statistics
            total_found = len(result["instagram"]) + len(result["facebook"])
            result["message"] = f"Successfully found {total_found} social media accounts"
            
            self.logger.info(
                f"Search completed - Instagram: {len(result['instagram'])}, "
                f"Facebook: {len(result['facebook'])}"
            )

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Error occurred: {str(e)}"
            self.logger.error(f"Error occurred during search: {str(e)}")

        return result

    def find_multiple(self, urls: list) -> list:
        """
        Batch find social media information from multiple websites.
        
        Args:
            urls: List of target website URLs
            
        Returns:
            list: List containing all results
        """
        results = []
        
        for i, url in enumerate(urls, 1):
            self.logger.info(f"Processing website {i}/{len(urls)}: {url}")
            result = self.find(url)
            results.append(result)

        return results










