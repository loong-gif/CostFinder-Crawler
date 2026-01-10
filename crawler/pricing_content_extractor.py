"""
Pricing content extractor
Used to crawl pricing pages and extract price information from them
"""

import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup, Tag
from crawler.base_crawler import BaseCrawler


class PricingContentExtractor(BaseCrawler):
    """Pricing content extractor class"""

    # Price patterns
    PRICE_PATTERNS = [
        # $100, $1,000, $1,000.00
        (r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', 'USD'),
        # $100-$200 price range
        (r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*[-–—]\s*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', 'USD_RANGE'),
        # Starting at $100, From $100
        (r'(?:starting\s+(?:at\s+)?|from\s+)\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', 'USD_FROM'),
        # 100 USD, 100 dollars
        (r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*(?:USD|dollars?)', 'USD'),
    ]

    # Common patterns for service/item names
    SERVICE_INDICATORS = [
        'treatment', 'service', 'procedure', 'session', 'consultation',
        'facial', 'massage', 'injection', 'filler', 'botox', 'laser',
        'peel', 'therapy', 'package', 'membership'
    ]

    def __init__(self):
        """Initialize extractor"""
        super().__init__()

    def extract_pricing_content(self, url: str) -> Dict:
        """
        Extract content from pricing page.
        
        Args:
            url: Pricing page URL
            
        Returns:
            Dict: Extracted price information
        """
        self.logger.info(f"Extracting pricing content: {url}")

        result = {
            'url': url,
            'domain': urlparse(url).netloc,
            'status': 'success',
            'error': None,
            'page_title': '',
            'prices': [],
            'price_tables': [],
            'service_items': [],
            'raw_price_text': []
        }

        try:
            # Get page
            html = self.fetch_page(url)
            if not html:
                result['status'] = 'failed'
                result['error'] = 'Unable to access page'
                return result

            soup = self.parse_html(html)
            if not soup:
                result['status'] = 'failed'
                result['error'] = 'HTML parsing failed'
                return result

            # Extract page title
            result['page_title'] = self._extract_title(soup)

            # Method 1: Extract prices from tables
            result['price_tables'] = self._extract_price_tables(soup)

            # Method 2: Extract price items from lists
            result['service_items'] = self._extract_service_items(soup)

            # Method 3: Extract all text paragraphs containing prices
            result['prices'] = self._extract_all_prices(soup)

            # Method 4: Extract raw price text (for manual inspection)
            result['raw_price_text'] = self._extract_raw_price_text(soup)

            # Statistics
            result['total_prices_found'] = len(result['prices'])
            result['total_tables_found'] = len(result['price_tables'])
            result['total_items_found'] = len(result['service_items'])

            self.logger.info(f"Extraction completed: found {result['total_prices_found']} prices")

        except Exception as e:
            self.logger.error(f"Error during extraction: {str(e)}")
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title"""
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text(strip=True)
        h1_tag = soup.find('h1')
        if h1_tag:
            return h1_tag.get_text(strip=True)
        return ""

    def _extract_price_tables(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Extract price information from tables.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            List[Dict]: Price table list
        """
        tables = []
        
        for table in soup.find_all('table'):
            table_data = {
                'headers': [],
                'rows': []
            }
            
            # Extract table headers
            header_row = table.find('tr')
            if header_row:
                for th in header_row.find_all(['th', 'td']):
                    table_data['headers'].append(th.get_text(strip=True))
            
            # Extract data rows
            for row in table.find_all('tr')[1:]:  # Skip header row
                cells = row.find_all(['td', 'th'])
                row_data = [cell.get_text(strip=True) for cell in cells]
                
                # Check if contains price
                row_text = ' '.join(row_data)
                if self._contains_price(row_text):
                    table_data['rows'].append(row_data)
            
            if table_data['rows']:
                tables.append(table_data)
        
        return tables

    def _extract_service_items(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Extract service items and prices.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            List[Dict]: Service item list
        """
        items = []
        
        # Find common price list containers
        price_containers = soup.find_all(['div', 'section', 'ul', 'ol'], 
            class_=lambda x: x and any(kw in str(x).lower() for kw in 
                ['price', 'service', 'menu', 'treatment', 'package']))
        
        # Also find all list items
        for li in soup.find_all('li'):
            text = li.get_text(strip=True)
            if self._contains_price(text) and len(text) < 500:
                price_info = self._parse_price_item(text)
                if price_info:
                    items.append(price_info)
        
        # Find price items in div structure
        for div in soup.find_all(['div', 'article']):
            # Check if it's a price card
            class_list = div.get('class', [])
            if isinstance(class_list, list):
                class_str = ' '.join(class_list).lower()
            else:
                class_str = str(class_list).lower()
                
            if any(kw in class_str for kw in ['item', 'card', 'service', 'product', 'price']):
                text = div.get_text(strip=True)
                if self._contains_price(text) and len(text) < 500:
                    price_info = self._parse_price_item(text)
                    if price_info:
                        items.append(price_info)
        
        # Deduplicate
        seen = set()
        unique_items = []
        for item in items:
            key = (item.get('name', ''), item.get('price', ''))
            if key not in seen and key[0]:  # Ensure has name
                seen.add(key)
                unique_items.append(item)
        
        return unique_items[:50]  # Limit quantity

    def _extract_all_prices(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Extract all prices from page.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            List[Dict]: Price list
        """
        prices = []
        text = soup.get_text()
        
        for pattern, price_type in self.PRICE_PATTERNS:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                if price_type == 'USD_RANGE':
                    price_value = f"${match.group(1)} - ${match.group(2)}"
                elif price_type == 'USD_FROM':
                    price_value = f"From ${match.group(1)}"
                else:
                    price_value = f"${match.group(1)}"
                
                # Get context
                start = max(0, match.start() - 50)
                end = min(len(text), match.end() + 50)
                context = text[start:end].strip()
                
                prices.append({
                    'value': price_value,
                    'type': price_type,
                    'context': context
                })
        
        # Deduplicate and limit quantity
        seen = set()
        unique_prices = []
        for p in prices:
            if p['value'] not in seen:
                seen.add(p['value'])
                unique_prices.append(p)
        
        return unique_prices[:100]

    def _extract_raw_price_text(self, soup: BeautifulSoup) -> List[str]:
        """
        Extract raw text paragraphs containing prices.
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            List[str]: Raw text list
        """
        price_texts = []
        
        # Find paragraphs containing prices
        for element in soup.find_all(['p', 'div', 'span', 'li', 'td']):
            text = element.get_text(strip=True)
            if self._contains_price(text) and 10 < len(text) < 300:
                # Clean text
                clean_text = ' '.join(text.split())
                if clean_text not in price_texts:
                    price_texts.append(clean_text)
        
        return price_texts[:50]

    def _contains_price(self, text: str) -> bool:
        """Check if text contains price"""
        for pattern, _ in self.PRICE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _parse_price_item(self, text: str) -> Optional[Dict]:
        """
        Parse price item text.
        
        Args:
            text: Text content
            
        Returns:
            Optional[Dict]: Parsed price item
        """
        # Try to extract price
        price_match = None
        for pattern, _ in self.PRICE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                price_match = match
                break
        
        if not price_match:
            return None
        
        # Extract price value
        price_value = price_match.group(0)
        
        # Try to extract service name (text before price)
        name_text = text[:price_match.start()].strip()
        
        # Clean name
        name_text = re.sub(r'[:\-–—•·]$', '', name_text).strip()
        
        # If name is too short, try using entire text
        if len(name_text) < 3:
            name_text = text
        
        # Limit name length
        if len(name_text) > 100:
            name_text = name_text[:100] + '...'
        
        return {
            'name': name_text,
            'price': price_value,
            'full_text': text[:200] if len(text) > 200 else text
        }
