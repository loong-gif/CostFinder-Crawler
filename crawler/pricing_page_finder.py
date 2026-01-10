"""
Pricing page finder
Used to find pages containing price information in websites
"""

import re
from typing import List, Dict, Optional, Set
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from crawler.base_crawler import BaseCrawler
import requests
import config


class PricingPageFinder(BaseCrawler):
    """Pricing page finder class"""

    # Price-related keywords (in URL path and page title)
    PRICING_KEYWORDS = [
        'pricing', 'prices', 'price', 'service', 'services',
        'treatment', 'treatments', 'specials',
        'package', 'packages', 'special', 'promotion',
        'offer', 'offers', 'discount', 'discounts', 'deal', 'deals',
    ]

    # Excluded keywords (to avoid false positives)
    EXCLUDE_KEYWORDS = [
        'login', 'signin', 'signup', 'register', 'cart', 'checkout',
        'account', 'profile', 'logout', 'search', 'contact-form',
        'privacy', 'terms', 'policy', 'cookie', 'legal'
    ]

    # Price symbol patterns (for detecting page content)
    PRICE_PATTERNS = [
        r'\$\s*\d+(?:,\d{3})*(?:\.\d{2})?',  # $100, $1,000.00
        r'\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:USD|usd|dollars?)',  # 100 USD
        r'from\s+\$\d+',  # from $100
        r'starting\s+(?:at\s+)?\$\d+',  # starting at $100
    ]

    def __init__(self):
        """Initialize pricing page finder"""
        super().__init__()
        self.visited_urls: Set[str] = set()
        self.max_pages_per_site = 20  # Maximum number of pages to check per site

    def find_pricing_pages(self, domain: str) -> Dict:
        """
        Find pricing pages in website.
        
        Args:
            domain: Domain name (e.g., example.com)
            
        Returns:
            Dict: Dictionary containing found pricing page information
        """
        # Normalize domain
        if not domain.startswith('http'):
            base_url = f"https://{domain}"
        else:
            base_url = domain

        self.logger.info(f"Starting to find pricing pages: {base_url}")
        self.visited_urls.clear()

        result = {
            'domain': domain,
            'base_url': base_url,
            'pricing_pages': [],
            'status': 'success',
            'error': None
        }

        try:
            # Step 1: Get homepage
            html = self.fetch_page(base_url)
            if not html:
                result['status'] = 'failed'
                result['error'] = 'Unable to access website homepage'
                return result

            soup = self.parse_html(html)
            if not soup:
                result['status'] = 'failed'
                result['error'] = 'HTML parsing failed'
                return result

            # Step 2: Collect all possible price-related links
            candidate_links = self._extract_candidate_links(soup, base_url)
            self.logger.info(f"Found {len(candidate_links)} candidate links")

            # Step 3: Check each candidate link
            for link_info in candidate_links[:self.max_pages_per_site]:
                url = link_info['url']
                
                if url in self.visited_urls:
                    continue
                    
                self.visited_urls.add(url)
                
                # Check if page contains price information
                page_result = self._check_pricing_page(url, link_info)
                if page_result:
                    result['pricing_pages'].append(page_result)
                    self.logger.info(f"Found pricing page: {url}")

            # Step 4: If no dedicated pricing pages found, check homepage
            if not result['pricing_pages']:
                homepage_has_pricing = self._check_page_content_for_prices(soup)
                if homepage_has_pricing:
                    result['pricing_pages'].append({
                        'url': base_url,
                        'title': self._extract_page_title(soup),
                        'link_text': 'Homepage',
                        'confidence': 'low',
                        'reason': 'Homepage contains price information'
                    })

            result['total_found'] = len(result['pricing_pages'])
            self.logger.info(f"Search completed, found {result['total_found']} pricing pages")

        except (ValueError, AttributeError, requests.exceptions.RequestException) as e:
            self.logger.error(f"Error during search: {str(e)}")
            result['status'] = 'error'
            result['error'] = str(e)
        except Exception as e:
            self.logger.error(f"Unknown error: {str(e)}")
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def _extract_candidate_links(self, soup: BeautifulSoup, base_url: str) -> List[Dict]:
        """
        Extract candidate price-related links from page.
        
        Args:
            soup: BeautifulSoup object
            base_url: Base URL
            
        Returns:
            List[Dict]: Candidate link list
        """
        candidates = []
        seen_urls = set()

        # Get all links
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if not href:
                continue

            # Convert to full URL
            full_url = urljoin(base_url, href)
            
            # Ensure link belongs to same domain
            if not self._is_same_domain(full_url, base_url):
                continue

            # Avoid duplicates
            if full_url in seen_urls:
                continue

            # Get link text
            link_text = link.get_text(strip=True).lower()
            
            # Check if URL and link text contain price keywords
            url_lower = full_url.lower()
            
            # Exclude irrelevant links
            if any(exclude in url_lower for exclude in self.EXCLUDE_KEYWORDS):
                continue

            # Check if matches price keywords
            score = 0
            matched_keywords = []
            
            for keyword in self.PRICING_KEYWORDS:
                if keyword in url_lower:
                    score += 2
                    matched_keywords.append(keyword)
                if keyword in link_text:
                    score += 1
                    matched_keywords.append(keyword)

            if score > 0:
                seen_urls.add(full_url)
                candidates.append({
                    'url': full_url,
                    'link_text': link.get_text(strip=True),
                    'score': score,
                    'matched_keywords': list(set(matched_keywords))
                })

        # Sort by score
        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates

    def _check_pricing_page(self, url: str, link_info: Dict) -> Optional[Dict]:
        """
        Check if page contains price information.
        
        Args:
            url: Page URL
            link_info: Link information
            
        Returns:
            Optional[Dict]: Returns page information if it's a pricing page, otherwise None
        """
        try:
            html = self.fetch_page(url)
            if not html:
                return None

            soup = self.parse_html(html)
            if not soup:
                return None

            # Check if page content contains prices
            has_prices = self._check_page_content_for_prices(soup)
            
            if has_prices:
                # Determine confidence
                confidence = 'high' if link_info['score'] >= 3 else 'medium'
                
                return {
                    'url': url,
                    'title': self._extract_page_title(soup),
                    'link_text': link_info['link_text'],
                    'confidence': confidence,
                    'matched_keywords': link_info['matched_keywords'],
                    'reason': f"URL and content both contain price-related information"
                }

        except (requests.exceptions.RequestException, ValueError, AttributeError) as e:
            self.logger.warning(f"Error checking page {url}: {str(e)}")
        except Exception as e:
            self.logger.warning(f"Unknown error {url}: {str(e)}")

        return None

    def _check_page_content_for_prices(self, soup: BeautifulSoup) -> bool:
        """
        Check if page content contains price information.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            bool: Whether contains price information
        """
        # Get page text
        text_content = soup.get_text()
        
        # Check if contains price symbols
        for pattern in self.PRICE_PATTERNS:
            if re.search(pattern, text_content, re.IGNORECASE):
                return True

        return False

    def _extract_page_title(self, soup: BeautifulSoup) -> str:
        """
        Extract page title.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            str: Page title
        """
        # Try to get from <title> tag
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text(strip=True)

        # Try to get from <h1> tag
        h1_tag = soup.find('h1')
        if h1_tag:
            return h1_tag.get_text(strip=True)

        return "No title"

    def _is_same_domain(self, url: str, base_url: str) -> bool:
        """
        Check if URL belongs to the same domain.
        
        Args:
            url: URL to check
            base_url: Base URL
            
        Returns:
            bool: Whether same domain
        """
        try:
            url_domain = urlparse(url).netloc
            base_domain = urlparse(base_url).netloc
            
            # Remove www prefix for comparison
            url_domain = url_domain.replace('www.', '')
            base_domain = base_domain.replace('www.', '')
            
            return url_domain == base_domain
        except:
            return False

