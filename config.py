"""
Configuration file
Contains global configuration parameters for crawler
"""

# HTTP request configuration
REQUEST_TIMEOUT = 30  # Request timeout (seconds)
MAX_RETRIES = 3  # Maximum retry count
REQUEST_DELAY = 1  # Request delay (seconds)

# User-Agent configuration
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# HTTP request headers
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",  # Removed br (Brotli) as it may cause decoding issues
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Social media platform configuration
SOCIAL_MEDIA_PLATFORMS = {
    "instagram": {
        "domains": [
            "instagram.com",
            "www.instagram.com",
            "instagr.am",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9._]+)",
            r"(?:https?://)?(?:www\.)?instagr\.am/([a-zA-Z0-9._]+)",
        ],
        "enabled": True,
    },
    "facebook": {
        "domains": [
            "facebook.com",
            "www.facebook.com",
            "fb.com",
            "m.facebook.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.|m\.)?facebook\.com/([a-zA-Z0-9.]+)",
            r"(?:https?://)?(?:www\.)?fb\.com/([a-zA-Z0-9.]+)",
            r"(?:https?://)?(?:www\.|m\.)?facebook\.com/profile\.php\?id=(\d+)",
        ],
        "enabled": True,
    },
}

# Logging configuration
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FILE = "social_media_finder.log"

