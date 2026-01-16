#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
URL cleaning tool (preserves main site and subdomains, avoids misjudging sites like ueniweb.com as main site).

NOTE: This module preserves subdomains (e.g., botox-it.ueniweb.com).
For root domain only cleaning, use utils.url_cleaner.clean_url() instead.

Features:
1. Attempts to extract target URL from ?q= parameter
2. Cleans links to root domain + subdomain (supports .com, .co, .net, .site, .edu, .org, .us, .store, .la, .beauty, .biz, etc.)
3. Strips leading http:// or https:// protocol
4. Does not incorrectly remove multi-level subdomains, subdomains must be preserved, e.g., botox-it.ueniweb.com
5. If hostname does not start with www., adds www. prefix
"""

import re
from urllib.parse import unquote, urlparse
import sys

# Supported top-level domains
SUPPORTED_TLDS = (
    "com", "co", "net", "site", "edu", "org", "us", "store",
    "la", "beauty", "biz", "clinic", "salon", "spa", "center"
)

def ensure_www_prefix(host: str) -> str:
    """Ensure host starts with www."""
    if not host.lower().startswith("www."):
        return "www." + host
    return host

def clean_url_by_regex(url: str) -> str:
    """
    Clean URL to standardized host (e.g., www.example.com or www.botox-it.ueniweb.com), remove protocol and path parameters
    and determine whether to add www. after cleaning.

    Args:
        url: URL string to be cleaned

    Returns:
        str: Cleaned hostname URL (e.g., www.example.com or www.botox-it.ueniweb.com)
    """
    if not url or not url.strip():
        return ""

    target_url = url.strip()

    # 1. Extract target URL - check if there is a ?q= parameter
    q_param_regex = r'[?&]q=([^&]+)'
    q_match = re.search(q_param_regex, url)

    if q_match:
        try:
            target_url = unquote(q_match.group(1))
        except (UnicodeDecodeError, ValueError):
            target_url = q_match.group(1)

    try:
        # Parse URL
        if not re.match(r'^https?://', target_url, re.I):
            target_url_for_parse = "http://" + target_url  # Compatible with urlparse when protocol is missing
        else:
            target_url_for_parse = target_url
        parsed = urlparse(target_url_for_parse)
        host = parsed.hostname

        if not host:
            raise ValueError("Unable to parse host")

        # After cleaning, finally check if it starts with www
        host = ensure_www_prefix(host)
        return host

    except (ValueError, AttributeError, Exception) as e:
        print(f"Warning: Failed to clean '{url}': {e}, returning original link.", file=sys.stderr)
        return url


def clean_url_file(input_file: str, output_file: str = None) -> None:
    """
    Batch clean URL file and maintain original input order (deduplicate, keep first occurrence)

    Args:
        input_file: Input file path
        output_file: Output file path (optional, defaults to input_file with _cleaned suffix)
    """
    if output_file is None:
        # Generate default output filename
        if input_file.endswith('.txt'):
            output_file = input_file.replace('.txt', '_cleaned.txt')
        else:
            output_file = input_file + '_cleaned'

    # Read input file
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            urls = f.readlines()
    except FileNotFoundError:
        print(f"Error: File not found '{input_file}'", file=sys.stderr)
        return
    except (IOError, OSError, UnicodeDecodeError) as e:
        print(f"Error: Failed to read file - {e}", file=sys.stderr)
        return

    # Clean URLs and deduplicate, maintain original order
    cleaned_urls_ordered = []
    seen = set()
    skipped_count = 0

    for url in urls:
        url = url.strip()
        if not url:  # Skip empty lines
            skipped_count += 1
            continue

        cleaned = clean_url_by_regex(url)
        # Check "www." again to prevent manual concatenation omissions
        cleaned = ensure_www_prefix(cleaned) if cleaned else cleaned
        if cleaned and cleaned.strip() and cleaned not in seen:  # Ensure not empty string and not duplicate
            cleaned_urls_ordered.append(cleaned)
            seen.add(cleaned)

    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            for url in cleaned_urls_ordered:
                f.write(url + '\n')

        print(f"[OK] Cleaning completed!")
        print(f"  Original URL count: {len(urls)}")
        print(f"  Empty line count: {skipped_count}")
        print(f"  Cleaned URL count: {len(cleaned_urls_ordered)} (deduplicated, order preserved)")
        print(f"  Output file: {output_file}")

    except (IOError, OSError, PermissionError) as e:
        print(f"Error: Failed to write file - {e}", file=sys.stderr)


if __name__ == "__main__":
    # Test single URL
    test_urls = [
        "https://www.socoplasticsurgery.com/?utm_source=GMBlisting&utm_medium=organic&utm_campaign=gmb-irvine",
        "http://www.californiaplasticsurgerygroup.com/",
        "https://orangetwist.com/center/tustin/",
        "https://healthy.kaiserpermanente.org/southern-california/physicians/ryan-wong-3762893",
        "https://botox-it.ueniweb.com/about-us/best-medical-spa-in-tustin-6389335",
        "ueniweb.com",  # Special test
    ]

    print("=" * 60)
    print("Testing URL cleaning functionality:")
    print("=" * 60)
    for url in test_urls:
        cleaned = clean_url_by_regex(url)
        cleaned = ensure_www_prefix(cleaned) if cleaned else cleaned  # Check again at the end
        print(f"Original: {url}")
        print(f"Cleaned: {cleaned}")
        print("-" * 60)

    print("\n" + "=" * 60)
    print("Starting file cleaning...")
    print("=" * 60)

    # Clean file
    clean_url_file('input_website_list.txt', 'input_website_list_cleaned.txt')

