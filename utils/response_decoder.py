"""
Response decoder utility
Provides common functionality for decoding HTTP responses with various encodings and compression formats.
"""

import gzip
from typing import Optional
import requests

# Try to import brotli
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False


def is_valid_text_content(text: str) -> bool:
    """
    Check if text is valid readable content (not garbled).
    
    Args:
        text: Text content to validate
        
    Returns:
        bool: True if text is valid, False otherwise
    """
    if not text or len(text) < 10:
        return False
    
    # Count non-printable characters
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


def decode_response_content(response: requests.Response) -> str:
    """
    Try multiple methods to decode response content.
    
    Args:
        response: requests.Response object
        
    Returns:
        str: Decoded text content, empty string if decoding fails
    """
    # First try using response.text (requests handles most cases automatically)
    try:
        text = response.text
        if is_valid_text_content(text):
            return text
    except (UnicodeDecodeError, AttributeError):
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
                except (UnicodeDecodeError, ValueError):
                    continue
        except (brotli.error, OSError):
            pass
    
    # Try gzip decompression
    try:
        decompressed = gzip.decompress(content)
        for encoding in ['utf-8', 'latin-1', 'gbk', 'gb2312']:
            try:
                text = decompressed.decode(encoding)
                if is_valid_text_content(text):
                    return text
            except (UnicodeDecodeError, ValueError):
                continue
    except (gzip.BadGzipFile, OSError):
        pass
    
    # Try decoding raw content directly with different encodings
    for encoding in ['utf-8', 'latin-1', 'gbk', 'gb2312']:
        try:
            text = content.decode(encoding)
            if is_valid_text_content(text):
                return text
        except (UnicodeDecodeError, ValueError):
            continue
    
    # If all failed, return empty string
    return ""
