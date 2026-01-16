# pip install beautifulsoup4 requests cloudscraper brotli
import csv
import time
import json
import random
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Set console encoding and disable buffering for real-time output
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Try to import brotli to support Brotli decompression
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    print("[!] brotli not installed, some websites may not decompress correctly")
    print("    Install command: pip install brotli")

# Import requests (for exception handling)
import requests

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
    scraper = requests.Session()
    print("[!] cloudscraper not installed, using regular requests (some websites may be blocked)")
    print("    Install command: pip install cloudscraper")

# Create a backup scraper with SSL verification disabled (for handling SSL error websites)
# WARNING: Disabling SSL verification is a security risk and should only be used as a last resort
import urllib3
backup_scraper = requests.Session()
backup_scraper.verify = False
# Disable SSL warnings (but log the security risk)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
print("[WARNING] SSL verification is disabled for backup scraper. This is a security risk.")


def make_request_with_retry(url: str, headers: dict, max_retries: int = 2):
    """Request function with retry mechanism, handles SSL errors and 403 errors"""
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            # First attempt: use normal scraper
            if attempt == 0:
                response = scraper.get(url, headers=headers, timeout=30)
            # Second attempt: if SSL error encountered, use scraper with SSL verification disabled
            elif attempt == 1:
                print(f"  🔄 Retrying (trying to disable SSL verification)...", flush=True)
                response = backup_scraper.get(url, headers=headers, timeout=30, verify=False)
            # Third attempt: try using http:// instead of https://
            else:
                print(f"  🔄 Retrying (trying to use HTTP)...", flush=True)
                http_url = url.replace('https://', 'http://')
                response = backup_scraper.get(http_url, headers=headers, timeout=30, verify=False)
            
            return response, None
        except requests.exceptions.SSLError as e:
            last_error = f'SSL error: {str(e)[:100]}'
            if attempt < max_retries:
                continue
        except requests.exceptions.ConnectionError as e:
            last_error = f'Connection failed: {str(e)[:100]}'
            if attempt < max_retries:
                continue
        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
            if attempt < max_retries:
                continue
        except requests.exceptions.RequestException as e:
            last_error = f'Request exception: {str(e)[:100]}'
            if attempt < max_retries:
                continue
        except Exception as e:
            last_error = f'Unknown error: {str(e)[:100]}'
            if attempt < max_retries:
                continue
    
    return None, last_error


def normalize_url(url: str) -> str:
    """Normalize URL, automatically add protocol if missing"""
    url = url.strip()
    if not url:
        return url
    
    # Remove parameters and anchors (before adding protocol)
    url = url.split('#')[0].split('?')[0]
    
    # If URL doesn't contain protocol, automatically add https://
    if not url.startswith(('http://', 'https://')):
        # If starts with //, add https:
        if url.startswith('//'):
            url = 'https:' + url
        else:
            url = 'https://' + url
    
    return url


def is_valid_text_content(text: str) -> bool:
    """Check if text is valid readable content (not garbled)"""
    if not text or len(text) < 10:
        return False
    
    # Count proportion of non-printable characters
    non_printable = 0
    total = len(text)
    for char in text[:1000]:  # Only check first 1000 characters
        # Allow common printable characters, Chinese, Japanese, Korean, etc.
        if ord(char) < 32 and char not in '\n\r\t':
            non_printable += 1
        elif ord(char) > 127 and ord(char) < 256:
            # Latin-1 extended characters, might be garbled
            non_printable += 0.5
    
    # If more than 20% are non-printable characters, consider it garbled
    return (non_printable / min(total, 1000)) < 0.2


def decode_response_content(response) -> str:
    """Try multiple methods to decode response content"""
    # First try using response.text (requests handles most cases automatically)
    try:
        text = response.text
        if is_valid_text_content(text):
            return text
    except Exception:
        pass
    
    # Get raw byte content
    content = response.content
    
    # Check if it's Brotli compressed
    if BROTLI_AVAILABLE:
        try:
            decompressed = brotli.decompress(content)
            # Try multiple encodings
            for encoding in ['utf-8', 'latin-1', 'gbk', 'gb2312']:
                try:
                    text = decompressed.decode(encoding)
                    if is_valid_text_content(text):
                        return text
                except Exception:
                    continue
        except Exception:
            pass
    
    # Try gzip decompression
    import gzip
    try:
        decompressed = gzip.decompress(content)
        for encoding in ['utf-8', 'latin-1', 'gbk', 'gb2312']:
            try:
                text = decompressed.decode(encoding)
                if is_valid_text_content(text):
                    return text
            except Exception:
                continue
    except Exception:
        pass
    
    # Try decoding raw content directly with different encodings
    for encoding in ['utf-8', 'latin-1', 'gbk', 'gb2312']:
        try:
            text = content.decode(encoding)
            if is_valid_text_content(text):
                return text
        except Exception:
            continue
    
    # If all failed, return empty string
    return ""


def extract_text_content(html: str) -> dict:
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

    # Clean up extra blank lines, remove consecutive duplicate lines
    lines = []
    prev_line = None
    for line in text_content.split('\n'):
        line = line.strip()
        if line and line != prev_line:  # Skip empty lines and consecutive duplicate lines
            lines.append(line)
            prev_line = line

    clean_content = '\n'.join(lines)

    return {
        'title': title,
        'description': description,
        'content': clean_content
    }


# Temporarily disabled subpage crawling feature
# def find_subpages(base_url, html):
#     """Find and return all subpage URLs under base_url domain (one level only, to avoid full site crawling)"""
#     soup = BeautifulSoup(html, 'html.parser')
#     subpages = set()
#     base_parsed = urlparse(base_url)
#     base_scheme_netloc = f"{base_parsed.scheme}://{base_parsed.netloc}"
#
#     for a in soup.find_all('a', href=True):
#         href = a['href'].strip()
#         # Construct absolute URL
#         full_url = urljoin(base_url, href)
#         parsed = urlparse(full_url)
#         # Only keep links under same domain
#         if (parsed.scheme, parsed.netloc) == (base_parsed.scheme, base_parsed.netloc):
#             # Must be subpage of base_url
#             # Note: Allow slashes or deep paths as subpages (exclude self)
#             if full_url != base_url and full_url.startswith(base_url):
#                 # Remove anchors and parameters
#                 url_no_query = full_url.split('#')[0].split('?')[0]
#                 subpages.add(url_no_query)
#     return subpages


def process_urls(input_file: str, output_file: str, api_key: str = None):
    """Batch process URLs and output CSV (automatic subpage crawling feature disabled)"""

    # Read URL list
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            raw_urls = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"❌ Error: Input file not found '{input_file}'")
        return
    except Exception as e:
        print(f"❌ Error: Failed to read input file: {e}")
        return

    if not raw_urls:
        print("❌ Error: Input file is empty")
        return

    # Preprocessing: normalize URLs and deduplicate
    all_urls_set = set()
    for u in raw_urls:
        try:
            normalized = normalize_url(u)
            if normalized:  # Only add non-empty URLs
                all_urls_set.add(normalized)
        except Exception as e:
            print(f"⚠️ Warning: Skipping invalid URL '{u}': {e}")
            continue
    
    main_urls = list(all_urls_set)

    print(f"📋 Total {len(main_urls)} main URLs to process (deduplicated)")
    print("=" * 60)

    # Only process main pages (URLs already normalized, no need to process again)
    visited_urls = set()
    to_process_urls = main_urls.copy()

    # Prepare CSV output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"page_contents_{timestamp}.csv"

    # Initialize counters
    success_count = 0
    fail_count = 0

    try:
        with open(output_file, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['URL', 'Title', 'Description', 'Content', 'Status'])

            # Simulate real browser complete request headers
            # Decide whether to request br encoding based on whether brotli library is installed
            accept_encoding = "gzip, deflate"
            if BROTLI_AVAILABLE:
                accept_encoding += ", br"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": accept_encoding,
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }

            processing_idx = 1
            try:
                while to_process_urls:
                    url = to_process_urls.pop(0)
                    
                    # URL already normalized, use directly
                    if url in visited_urls:
                        continue
                    
                    print(f"[{processing_idx}] Processing: {url}", flush=True)
                    processing_idx += 1

                    try:
                        # Use request function with retry mechanism
                        response, error = make_request_with_retry(url, headers, max_retries=2)
                        
                        if response is None:
                            writer.writerow([url, '', '', '', f'Error: {error}'])
                            fail_count += 1
                            print(f"  ❌ {error}", flush=True)
                            visited_urls.add(url)
                            continue

                        if response.status_code == 200:
                            # Try to decode response content
                            try:
                                html_content = decode_response_content(response)
                            except Exception as decode_error:
                                writer.writerow([url, '', '', '', f'Decode error: {str(decode_error)[:100]}'])
                                fail_count += 1
                                print(f"  ❌ Decode error: {decode_error}", flush=True)
                                visited_urls.add(url)
                                continue
                            
                            if not html_content:
                                writer.writerow([url, '', '', '', 'Decode failed: Unable to parse response content'])
                                fail_count += 1
                                print(f"  ❌ Decode failed - Unable to parse response content", flush=True)
                            else:
                                # Extract text content
                                try:
                                    result = extract_text_content(html_content)
                                except Exception as extract_error:
                                    writer.writerow([url, '', '', '', f'Extract error: {str(extract_error)[:100]}'])
                                    fail_count += 1
                                    print(f"  ❌ Extract error: {extract_error}", flush=True)
                                    visited_urls.add(url)
                                    continue
                                
                                # Validate if extracted content is valid
                                if not is_valid_text_content(result['content']):
                                    writer.writerow([url, result['title'], result['description'], '', 'Content invalid: may be garbled'])
                                    fail_count += 1
                                    print(f"  ⚠️ Content invalid - Title: {result['title'][:50] if result['title'] else 'None'}...", flush=True)
                                else:
                                    # Stringify content
                                    try:
                                        stringified_content = json.dumps(result['content'], ensure_ascii=False)
                                        writer.writerow([
                                            url,
                                            result['title'],
                                            result['description'],
                                            stringified_content,
                                            'Success'
                                        ])
                                        success_count += 1
                                        print(f"  ✅ Success - Title: {result['title'][:50] if result['title'] else 'No title'}...", flush=True)
                                    except Exception as json_error:
                                        writer.writerow([url, result['title'], result['description'], '', f'JSON serialization error: {str(json_error)[:100]}'])
                                        fail_count += 1
                                        print(f"  ❌ JSON serialization error: {json_error}", flush=True)

                            # Temporarily disabled subpage crawling feature
                            # subpages = find_subpages(url, response.text)
                            # new_subs = [u for u in subpages if u not in visited_urls and u not in to_process_urls]
                            # if new_subs:
                            #     print(f"  🔍 Found {len(new_subs)} subpages, adding to queue")
                            #     to_process_urls.extend(new_subs)
                        elif response.status_code == 403:
                            # For 403 errors, try multiple strategies
                            print(f"  ⚠️ HTTP 403, trying multiple strategies to bypass...", flush=True)
                            
                            # Strategy 1: Try accessing homepage to get cookies, then access target page
                            try:
                                parsed_url = urlparse(url)
                                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                                
                                # First access homepage
                                print(f"  🔄 Strategy 1: Accessing homepage to get cookies...", flush=True)
                                home_response = scraper.get(base_url, headers=headers, timeout=30)
                                if home_response.status_code == 200:
                                    # Wait a bit, simulate real browsing
                                    time.sleep(random.uniform(2, 4))
                                    # Use obtained cookies to access target page
                                    retry_response = scraper.get(url, headers=headers, timeout=30)
                                    if retry_response.status_code == 200:
                                        html_content = decode_response_content(retry_response)
                                        if html_content:
                                            result = extract_text_content(html_content)
                                            if is_valid_text_content(result['content']):
                                                stringified_content = json.dumps(result['content'], ensure_ascii=False)
                                                writer.writerow([
                                                    url,
                                                    result['title'],
                                                    result['description'],
                                                    stringified_content,
                                                    'Success (Strategy 1: Homepage cookies)'
                                                ])
                                                success_count += 1
                                                print(f"  ✅ Success (Strategy 1) - Title: {result['title'][:50] if result['title'] else 'No title'}...", flush=True)
                                                visited_urls.add(url)
                                                continue
                            except Exception as e:
                                print(f"  ⚠️ Strategy 1 failed: {str(e)[:50]}", flush=True)
                            
                            # Strategy 2: Use enhanced request headers
                            print(f"  🔄 Strategy 2: Using enhanced request headers...", flush=True)
                            enhanced_headers = headers.copy()
                            parsed_url = urlparse(url)
                            base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
                            
                            # Multiple different User-Agent options
                            user_agents = [
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                            ]
                            
                            success_with_strategy2 = False
                            for ua in user_agents:
                                try:
                                    enhanced_headers.update({
                                        "User-Agent": ua,
                                        "Referer": base_domain + "/",
                                        "Origin": base_domain,
                                        "DNT": "1",
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                                        "Accept-Language": "en-US,en;q=0.9",
                                        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                                        "Sec-Ch-Ua-Mobile": "?0",
                                        "Sec-Ch-Ua-Platform": '"Windows"',
                                    })
                                    
                                    time.sleep(random.uniform(1, 3))
                                    retry_response = scraper.get(url, headers=enhanced_headers, timeout=30)
                                    
                                    if retry_response.status_code == 200:
                                        html_content = decode_response_content(retry_response)
                                        if html_content:
                                            result = extract_text_content(html_content)
                                            if is_valid_text_content(result['content']):
                                                stringified_content = json.dumps(result['content'], ensure_ascii=False)
                                                writer.writerow([
                                                    url,
                                                    result['title'],
                                                    result['description'],
                                                    stringified_content,
                                                    'Success (Strategy 2: Enhanced headers)'
                                                ])
                                                success_count += 1
                                                print(f"  ✅ Success (Strategy 2) - Title: {result['title'][:50] if result['title'] else 'No title'}...", flush=True)
                                                visited_urls.add(url)
                                                success_with_strategy2 = True
                                                break
                                except Exception as e:
                                    continue
                            
                            if not success_with_strategy2:
                                # All strategies failed
                                writer.writerow([url, '', '', '', f'HTTP 403 - Access denied (multiple strategies tried)'])
                                fail_count += 1
                                print(f"  ❌ Failed - HTTP 403 (multiple strategies tried, access denied)", flush=True)
                        else:
                            writer.writerow([url, '', '', '', f'HTTP {response.status_code}'])
                            fail_count += 1
                            print(f"  ❌ Failed - HTTP {response.status_code}", flush=True)

                    except KeyboardInterrupt:
                        # User interrupted, save progress and exit
                        print("\n\n⚠️ User interrupted operation, saving progress...", flush=True)
                        raise  # Re-raise to be caught by outer handler
                    except requests.exceptions.Timeout:
                        writer.writerow([url, '', '', '', 'Error: Request timeout'])
                        fail_count += 1
                        print(f"  ❌ Error: Request timeout", flush=True)
                    except requests.exceptions.ConnectionError as e:
                        writer.writerow([url, '', '', '', f'Error: Connection failed - {str(e)[:80]}'])
                        fail_count += 1
                        print(f"  ❌ Error: Connection failed - {e}", flush=True)
                    except requests.exceptions.RequestException as e:
                        writer.writerow([url, '', '', '', f'Error: Request exception - {str(e)[:80]}'])
                        fail_count += 1
                        print(f"  ❌ Error: Request exception - {e}", flush=True)
                    except Exception as e:
                        error_msg = str(e)[:100]
                        writer.writerow([url, '', '', '', f'Error: {error_msg}'])
                        fail_count += 1
                        print(f"  ❌ Error: {e}", flush=True)

                    visited_urls.add(url)

                    # Calculate progress (avoid division by zero)
                    total = len(visited_urls) + len(to_process_urls)
                    if total > 0:
                        progress = len(visited_urls) / total * 100
                        print(f"  📊 Progress: {progress:.1f}% (Processed: {len(visited_urls)}; Pending: {len(to_process_urls)})", flush=True)
                    
                    if to_process_urls:
                        # Random delay 2-5 seconds, simulate human behavior
                        try:
                            delay = random.uniform(2, 5)
                            time.sleep(delay)
                        except KeyboardInterrupt:
                            print("\n\n⚠️ User interrupted operation, saving progress...", flush=True)
                            raise
            except KeyboardInterrupt:
                print("\n\n⚠️ Processing interrupted by user", flush=True)
            except Exception as e:
                print(f"\n\n❌ Unexpected error occurred: {e}", flush=True)
                import traceback
                traceback.print_exc()
    except Exception as e:
        print(f"❌ Error: Unable to create or write output file '{output_file}': {e}")
        return

    print("\n" + "=" * 60)
    print(f"✅ Processing completed!")
    print(f"   Success: {success_count}")
    print(f"   Failed: {fail_count}")
    print(f"   Output file: {output_file}")


if __name__ == "__main__":
    INPUT_FILE = "input_website_list_cleaned.txt"
    OUTPUT_FILE = "page_contents.csv"

    # Temporarily disabled automatic subpage crawling, only process main URLs
    process_urls(INPUT_FILE, OUTPUT_FILE)
