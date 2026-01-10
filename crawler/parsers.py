"""
Social media link parser
Used to extract and parse social media links from HTML
"""

import re
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from utils.logger import Logger
import config


class SocialMediaParser:
    """Social media link parser"""

    def __init__(self):
        """Initialize parser"""
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.platforms = config.SOCIAL_MEDIA_PLATFORMS

    def extract_links_from_html(self, html: str, base_url: str) -> Dict[str, List[Dict[str, str]]]:
        """
        Extract all social media links from HTML.
        
        Args:
            html: HTML content
            base_url: Base URL (for handling relative links)
            
        Returns:
            Dict: Social media information categorized by platform
        """
        soup = BeautifulSoup(html, "lxml")
        results = {
            "instagram": [],
            "facebook": [],
        }

        # Extract all links
        all_links = self._extract_all_links(soup, base_url)
        
        # Extract links from text
        text_links = self._extract_links_from_text(soup)
        all_links.update(text_links)

        # Categorize by platform (use set for deduplication)
        seen_usernames = {
            "instagram": set(),
            "facebook": set(),
        }
        
        for link in all_links:
            platform = self._identify_platform(link)
            if platform and self.platforms[platform]["enabled"]:
                account_info = self._parse_link(link, platform)
                if account_info:
                    username = account_info['username']
                    # Check if this username has already been added
                    if username not in seen_usernames[platform]:
                        results[platform].append(account_info)
                        seen_usernames[platform].add(username)

        self.logger.info(
            f"Extraction completed - Instagram: {len(results['instagram'])}, "
            f"Facebook: {len(results['facebook'])}"
        )
        
        return results

    def _extract_all_links(self, soup: BeautifulSoup, base_url: str) -> Set[str]:
        """
        Extract all links from HTML.
        
        Args:
            soup: BeautifulSoup object
            base_url: Base URL
            
        Returns:
            Set[str]: Set of all links
        """
        links = set()
        
        # Extract from <a> tags
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "").strip()
            if href:
                absolute_url = urljoin(base_url, href)
                links.add(absolute_url)

        # Extract from other attributes that may contain links
        for tag in soup.find_all(attrs={"data-url": True}):
            data_url = tag.get("data-url", "").strip()
            if data_url:
                absolute_url = urljoin(base_url, data_url)
                links.add(absolute_url)

        return links

    def _extract_links_from_text(self, soup: BeautifulSoup) -> Set[str]:
        """
        Extract links from text content.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            Set[str]: Links extracted from text
        """
        links = set()
        text = soup.get_text()
        
        # Use regex to extract links for all platforms
        for platform, settings in self.platforms.items():
            if not settings["enabled"]:
                continue
                
            for pattern in settings["patterns"]:
                matches = re.finditer(pattern, text, re.IGNORECASE)
                for match in matches:
                    # Reconstruct full URL
                    if "instagram" in platform:
                        link = f"https://instagram.com/{match.group(1)}"
                    elif "facebook" in platform:
                        if "profile.php" in match.group(0):
                            link = f"https://facebook.com/profile.php?id={match.group(1)}"
                        else:
                            link = f"https://facebook.com/{match.group(1)}"
                    else:
                        continue
                    links.add(link)

        return links

    def _identify_platform(self, url: str) -> Optional[str]:
        """
        Identify which social media platform the link belongs to.
        
        Args:
            url: Link URL
            
        Returns:
            Optional[str]: Platform name, returns None if not identified
        """
        try:
            parsed = urlparse(url.lower())
            domain = parsed.netloc
            
            for platform, settings in self.platforms.items():
                if any(d in domain for d in settings["domains"]):
                    return platform
            
            return None
        except Exception:
            return None

    def _parse_link(self, url: str, platform: str) -> Optional[Dict[str, str]]:
        """
        Parse social media link and extract account information.
        
        Args:
            url: Social media link
            platform: Platform name
            
        Returns:
            Optional[Dict]: Account information dictionary, returns None on parsing failure
        """
        try:
            settings = self.platforms[platform]
            
            for pattern in settings["patterns"]:
                match = re.search(pattern, url, re.IGNORECASE)
                if match:
                    username = match.group(1)
                    
                    # Clean username
                    username = username.rstrip("/").split("?")[0].split("#")[0]
                    
                    # Filter invalid usernames
                    if not self._is_valid_username(username):
                        return None
                    
                    return {
                        "username": username,
                        "profile_url": self._normalize_profile_url(url, platform),
                    }
            
            return None
        except (ValueError, AttributeError, re.error) as e:
            self.logger.debug(f"Link parsing failed: {url}, error: {str(e)}")
            return None
        except Exception as e:
            self.logger.debug(f"Unknown error: {url}, error: {str(e)}")
            return None

    def _is_valid_username(self, username: str) -> bool:
        """
        Validate if username is valid.
        
        Args:
            username: Username
            
        Returns:
            bool: Whether valid
        """
        if not username or len(username) < 1:
            return False
        
        # Filter common invalid usernames
        invalid_keywords = [
            "login", "signup", "explore", "search", "home",
            "about", "contact", "privacy", "terms", "help",
            "settings", "profile", "notifications", "messages"
        ]
        
        return username.lower() not in invalid_keywords

    def _normalize_profile_url(self, url: str, platform: str) -> str:
        """
        Normalize social media profile URL.
        
        Args:
            url: Original URL
            platform: Platform name
            
        Returns:
            str: Normalized URL
        """
        # Remove query parameters and anchors (preserve Facebook's id parameter)
        if platform == "facebook" and "profile.php?id=" in url:
            return url.split("&")[0].split("#")[0]
        
        return url.split("?")[0].split("#")[0]

