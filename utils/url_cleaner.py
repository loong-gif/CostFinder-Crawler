"""
URL cleaning utility module
Provides URL cleaning functionality that normalizes URLs to root domain format.

NOTE: This module removes subdomains and keeps only root domain (e.g., www.example.com).
For preserving subdomains (e.g., botox-it.ueniweb.com), use clean_websites.clean_url_by_regex() instead.
"""

import re
from urllib.parse import unquote


def clean_url(url: str) -> str:
    """
    Clean URL to root domain format (removes subdomains).
    
    This function:
    1. Attempts to extract target URL from ?q= parameter
    2. Cleans link to root domain only (supports .com, .co, .net, .site, .edu, .org, .us, .store)
    3. Strips leading http:// or https:// protocol
    4. Ensures domain starts with www. and removes all subdomains
    
    Args:
        url: URL string to be cleaned
        
    Returns:
        Cleaned root domain URL (starts with www., no trailing slash)
        
    Note:
        This function removes subdomains. To preserve subdomains, use clean_websites.clean_url_by_regex()
    """
    if not url or not url.strip():
        return ""
    
    target_url = url.strip()
    
    # 1. Extract target URL (if contains ?q= parameter)
    q_param_pattern = r'[?&]q=([^&]+)'
    q_match = re.search(q_param_pattern, url)
    
    if q_match:
        try:
            target_url = unquote(q_match.group(1))
        except (UnicodeDecodeError, ValueError):
            target_url = q_match.group(1)
    
    # 2. Extract root domain (supports multiple top-level domains)
    domain_pattern = r'^(https?://[^/]+\.(?:com|co|net|site|edu|org|us|store))(?:/.*)?$'
    domain_match = re.match(domain_pattern, target_url)
    
    if domain_match:
        cleaned_domain = domain_match.group(1)
        
        # 3. Strip leading http:// or https:// protocol
        cleaned_domain = re.sub(r'^https?://', '', cleaned_domain)
        
        # 4. Ensure domain starts with www. and remove other subdomains
        
        # Find last dot position
        last_dot_index = cleaned_domain.rfind('.')
        
        if last_dot_index == -1:
            # No dot found, return original domain
            return cleaned_domain
        
        # Find second-to-last dot position
        second_last_dot_index = cleaned_domain.rfind('.', 0, last_dot_index)
        
        if second_last_dot_index == -1:
            # Bare domain, e.g., "example.com" or "square.site"
            main_domain = cleaned_domain
        else:
            # Find first dot position (start of any subdomain)
            first_dot_index = cleaned_domain.find('.')
            
            # Extract from after first dot, get "main_domain.tld"
            main_domain = cleaned_domain[first_dot_index + 1:]
        
        # Force add 'www.' prefix
        cleaned_domain = 'www.' + main_domain
        
        # 5. Return cleaned domain (no trailing slash)
        return cleaned_domain
    else:
        # Failed to match domain pattern, return original URL
        return url


def clean_url_list(urls: list) -> list:
    """
    Batch clean URL list.
    
    Args:
        urls: List of URL strings
        
    Returns:
        Cleaned URL list (removes empty lines and duplicates)
    """
    cleaned_urls = []
    seen = set()
    
    for url in urls:
        cleaned = clean_url(url)
        if cleaned and cleaned not in seen:
            cleaned_urls.append(cleaned)
            seen.add(cleaned)
    
    return cleaned_urls

