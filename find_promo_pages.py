"""
Promo/Price page finder
Finds subpages that may contain price or promotion information from input URL list
Output format is CSV, containing promo_website_id, business_id, subpage_url, page_content, crawl_timestamp
"""

import csv
import uuid
import time
import json
import random
import re
import sys
import io
from datetime import datetime
from typing import List, Dict, Set, Optional
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# Set console encoding and disable buffering for real-time output
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Try to import brotli to support Brotli decompression
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    print("[!] brotli not installed, some websites may not decompress correctly")

# Try to use cloudscraper (can bypass Cloudflare), fallback to requests if failed
try:
    import cloudscraper
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    print("[OK] Using cloudscraper (can bypass Cloudflare protection)")
except ImportError:
    import requests
    scraper = requests.Session()
    print("[!] cloudscraper not installed, using regular requests")


class PromoPageFinder:
    """Promo/Price page finder"""

    # Keywords related to price and promotion (in URL path and page title)
    PROMO_KEYWORDS = [
        # Price related
        'pricing', 'prices', 'price', 'cost', 'costs', 'fee', 'fees', 'rate', 'rates',
        # Service related
        'service', 'services', 'treatment', 'treatments', 'menu',
        # Promotion related
        'promo', 'promotion', 'promotions', 'special', 'specials',
        'offer', 'offers', 'deal', 'deals', 'discount', 'discounts',
        'sale', 'sales', 'coupon', 'coupons', 'savings',
        # Package related
        'package', 'packages', 'bundle', 'bundles', 'membership', 'memberships',
        # Other
        'shop', 'store', 'buy', 'booking', 'book-now', 'appointment'
    ]

    # Excluded keywords (to avoid false positives)
    EXCLUDE_KEYWORDS = [
        'login', 'signin', 'signup', 'register', 'cart', 'checkout',
        'account', 'profile', 'logout', 'search', 'contact-form',
        'privacy', 'terms', 'policy', 'cookie', 'legal', 'careers',
        'job', 'jobs', 'blog', 'news', 'about-us', 'team', 'staff',
        'gallery', 'testimonial', 'review', 'faq', 'help', 'support'
    ]

    # Price symbol patterns (for detecting page content)
    PRICE_PATTERNS = [
        r'\$\s*\d+(?:,\d{3})*(?:\.\d{2})?',  # $100, $1,000.00
        r'\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:USD|usd|dollars?)',  # 100 USD
        r'from\s+\$\d+',  # from $100
        r'starting\s+(?:at\s+)?\$\d+',  # starting at $100
        r'\$\d+\s*[-–]\s*\$\d+',  # $100 - $200 price range
        r'(?:only|just)\s+\$\d+',  # only $100
        r'\d+%\s*off',  # 20% off
        r'save\s+\$?\d+',  # save $100 or save 20
    ]

    def __init__(self, max_pages_per_site: int = 15):
        """
        Initialize finder.
        
        Args:
            max_pages_per_site: Maximum number of pages to check per site
        """
        self.max_pages_per_site = max_pages_per_site
        self.visited_urls: Set[str] = set()
        
        # HTTP request headers
        accept_encoding = "gzip, deflate"
        if BROTLI_AVAILABLE:
            accept_encoding += ", br"
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Accept-Encoding": accept_encoding,
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    def _is_valid_text(self, text: str) -> bool:
        """Check if text is valid readable content"""
        if not text or len(text) < 10:
            return False
        non_printable = sum(1 for char in text[:1000] if ord(char) < 32 and char not in '\n\r\t')
        return (non_printable / min(len(text), 1000)) < 0.2

    def _decode_response(self, response) -> str:
        """Try multiple methods to decode response content"""
        try:
            text = response.text
            if self._is_valid_text(text):
                return text
        except Exception:
            pass

        content = response.content
        
        # Try Brotli decompression
        if BROTLI_AVAILABLE:
            try:
                decompressed = brotli.decompress(content)
                for encoding in ['utf-8', 'latin-1', 'gbk']:
                    try:
                        text = decompressed.decode(encoding)
                        if self._is_valid_text(text):
                            return text
                    except Exception:
                        continue
            except Exception:
                pass
        
        # Try gzip decompression
        import gzip
        try:
            decompressed = gzip.decompress(content)
            for encoding in ['utf-8', 'latin-1', 'gbk']:
                try:
                    text = decompressed.decode(encoding)
                    if self._is_valid_text(text):
                        return text
                except Exception:
                    continue
        except Exception:
            pass
        
        # Try direct decoding
        for encoding in ['utf-8', 'latin-1', 'gbk']:
            try:
                text = content.decode(encoding)
                if self._is_valid_text(text):
                    return text
            except Exception:
                continue
        
        return ""

    def _fetch_page(self, url: str, timeout: int = 30) -> Optional[str]:
        """Fetch page content"""
        try:
            response = scraper.get(url, headers=self.headers, timeout=timeout)
            if response.status_code == 200:
                return self._decode_response(response)
        except Exception as e:
            print(f"    ⚠️ Failed to fetch page {url}: {str(e)[:50]}")
        return None

    def _extract_text_content(self, html: str) -> Dict:
        """Extract text content from HTML"""
        soup = BeautifulSoup(html, 'html.parser')

        # Extract page title
        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        # Extract meta description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        description = meta_desc.get('content', '').strip() if meta_desc else ""

        # Remove unwanted tags
        for tag in soup(['script', 'style', 'noscript', 'header', 'footer', 'nav', 'aside', 'iframe']):
            tag.decompose()

        # Extract text content
        text_content = soup.get_text(separator='\n', strip=True)

        # Clean up extra blank lines
        lines = []
        prev_line = None
        for line in text_content.split('\n'):
            line = line.strip()
            if line and line != prev_line:
                lines.append(line)
                prev_line = line

        clean_content = '\n'.join(lines)

        return {
            'title': title,
            'description': description,
            'content': clean_content,
            'soup': soup
        }

    def _is_same_domain(self, url: str, base_url: str) -> bool:
        """Check if URL belongs to the same domain"""
        try:
            url_domain = urlparse(url).netloc.replace('www.', '')
            base_domain = urlparse(base_url).netloc.replace('www.', '')
            return url_domain == base_domain
        except:
            return False

    def _has_price_content(self, text: str) -> bool:
        """Check if text contains price information"""
        for pattern in self.PRICE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _extract_candidate_links(self, soup: BeautifulSoup, base_url: str) -> List[Dict]:
        """Extract candidate price/promotion related links from page"""
        candidates = []
        seen_urls = set()

        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if not href:
                continue

            # Convert to full URL
            full_url = urljoin(base_url, href)
            
            # Clean URL (remove anchor and parameters)
            full_url = full_url.split('#')[0].split('?')[0]
            
            # Ensure link belongs to same domain
            if not self._is_same_domain(full_url, base_url):
                continue

            # Avoid duplicates
            if full_url in seen_urls:
                continue

            # Get link text
            link_text = link.get_text(strip=True).lower()
            url_lower = full_url.lower()

            # Exclude irrelevant links
            if any(exclude in url_lower for exclude in self.EXCLUDE_KEYWORDS):
                continue

            # Calculate match score
            score = 0
            matched_keywords = []
            
            for keyword in self.PROMO_KEYWORDS:
                if keyword in url_lower:
                    score += 2
                    matched_keywords.append(keyword)
                if keyword in link_text:
                    score += 1
                    if keyword not in matched_keywords:
                        matched_keywords.append(keyword)

            if score > 0:
                seen_urls.add(full_url)
                candidates.append({
                    'url': full_url,
                    'link_text': link.get_text(strip=True),
                    'score': score,
                    'matched_keywords': matched_keywords
                })

        # Sort by score
        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates

    def find_promo_pages(self, domain: str) -> List[Dict]:
        """
        Find price/promotion pages in website.
        
        Args:
            domain: Domain name (e.g., example.com)
            
        Returns:
            List[Dict]: List of found promotion pages
        """
        # Normalize domain
        if not domain.startswith('http'):
            base_url = f"https://{domain}"
        else:
            base_url = domain
        
        # Clean URL
        base_url = base_url.rstrip('/')
        
        self.visited_urls.clear()
        promo_pages = []

        # Step 1: Get homepage
        print(f"  📄 Fetching homepage: {base_url}")
        html = self._fetch_page(base_url)
        if not html:
            print(f"  ❌ Unable to access website homepage")
            return []

        result = self._extract_text_content(html)
        soup = result['soup']

        # Check if homepage contains price information
        homepage_has_prices = self._has_price_content(result['content'])
        if homepage_has_prices:
            promo_pages.append({
                'url': base_url,
                'title': result['title'],
                'content': result['content'],
                'confidence': 'medium',
                'reason': 'Homepage contains price information'
            })
            print(f"    ✅ Homepage contains price information")

        # Step 2: Collect candidate links
        candidate_links = self._extract_candidate_links(soup, base_url)
        print(f"  🔍 Found {len(candidate_links)} candidate links")

        # Step 3: Check candidate links
        checked_count = 0
        for link_info in candidate_links:
            if checked_count >= self.max_pages_per_site:
                break
                
            url = link_info['url']
            
            if url in self.visited_urls:
                continue
            
            self.visited_urls.add(url)
            checked_count += 1

            print(f"    Checking: {url[:60]}...")
            
            # Get page content
            page_html = self._fetch_page(url)
            if not page_html:
                continue

            page_result = self._extract_text_content(page_html)
            
            # Check if contains price information
            if self._has_price_content(page_result['content']):
                confidence = 'high' if link_info['score'] >= 3 else 'medium'
                promo_pages.append({
                    'url': url,
                    'title': page_result['title'],
                    'content': page_result['content'],
                    'confidence': confidence,
                    'matched_keywords': link_info['matched_keywords'],
                    'reason': f"URL and content contain price-related information"
                })
                print(f"    ✅ Found promo page: {page_result['title'][:40]}")
            
            # Random delay to avoid requests too fast
            time.sleep(random.uniform(0.5, 1.5))

        return promo_pages


def process_urls(input_file: str, output_file: str = None):
    """
    Batch process URLs and output CSV.
    
    Args:
        input_file: Input file path
        output_file: Output file path (optional, auto-generated by default)
    """
    # Read URL list
    with open(input_file, 'r', encoding='utf-8') as f:
        raw_urls = [line.strip() for line in f if line.strip()]

    # Remove duplicates
    unique_urls = list(dict.fromkeys(raw_urls))
    print(f"\n📋 Total {len(unique_urls)} websites to process")
    print("=" * 70)

    # Prepare output file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not output_file:
        output_file = f"promo_pages_{timestamp}.csv"

    # Create finder
    finder = PromoPageFinder(max_pages_per_site=15)

    # Statistics
    total_promo_pages = 0
    success_count = 0
    fail_count = 0

    # Generate a business_id for each domain (simulate foreign key relationship)
    domain_to_business_id = {}

    with open(output_file, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        # Write header row
        writer.writerow([
            'promo_website_id',
            'business_id',
            'domain_name',
            'subpage_url',
            'page_content',
            'crawl_timestamp'
        ])

        for idx, url in enumerate(unique_urls, 1):
            print(f"\n[{idx}/{len(unique_urls)}] Processing: {url}")
            
            # Get domain
            if url.startswith('http'):
                domain = urlparse(url).netloc
            else:
                domain = url.split('/')[0]
            
            # Assign business_id for this domain
            if domain not in domain_to_business_id:
                domain_to_business_id[domain] = str(uuid.uuid4())
            business_id = domain_to_business_id[domain]

            try:
                # Find promo pages
                promo_pages = finder.find_promo_pages(url)
                
                if promo_pages:
                    success_count += 1
                    total_promo_pages += len(promo_pages)
                    print(f"  ✅ Found {len(promo_pages)} promo/price pages")
                    
                    # Write each promo page
                    for page in promo_pages:
                        promo_website_id = str(uuid.uuid4())
                        crawl_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        # Convert content to JSON string (preserve line breaks and formatting)
                        page_content = json.dumps(page['content'], ensure_ascii=False)
                        
                        writer.writerow([
                            promo_website_id,
                            business_id,
                            domain,
                            page['url'],
                            page_content,
                            crawl_timestamp
                        ])
                else:
                    fail_count += 1
                    print(f"  ⚠️ No promo/price pages found")

            except KeyboardInterrupt:
                print("\n\n⚠️ User interrupted, saving processed results...")
                break
            except Exception as e:
                fail_count += 1
                print(f"  ❌ Processing failed: {str(e)}")

            # Delay between websites
            if idx < len(unique_urls):
                delay = random.uniform(2, 4)
                time.sleep(delay)

    # Output domain to business_id mapping table
    mapping_file = f"business_id_mapping_{timestamp}.csv"
    with open(mapping_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['domain', 'business_id'])
        for domain, bid in domain_to_business_id.items():
            writer.writerow([domain, bid])

    # Print statistics summary
    print("\n" + "=" * 70)
    print("📊 Processing Complete - Statistics Summary")
    print("=" * 70)
    print(f"Total websites:        {len(unique_urls)}")
    print(f"Found promo pages:     {success_count} websites")
    print(f"No promo pages found:  {fail_count} websites")
    print(f"Total promo pages:     {total_promo_pages}")
    print("-" * 70)
    print(f"✅ Promo pages saved to: {output_file}")
    print(f"✅ Domain mapping saved to: {mapping_file}")
    print("=" * 70)


if __name__ == "__main__":
    INPUT_FILE = "input_website_list_cleaned.txt"
    process_urls(INPUT_FILE)
