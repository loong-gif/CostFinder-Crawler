"""
Configuration file
Contains global configuration parameters for crawler
"""

# HTTP request configuration
REQUEST_TIMEOUT = 30  # Request timeout (seconds)
MAX_RETRIES = 3  # Maximum retry count
REQUEST_DELAY = 1  # Request delay (seconds)

# Retry configuration
RETRY_DELAY = 1  # Delay between retries (seconds)
# Exceptions that should trigger retry (only these exceptions will be retried)
RETRY_EXCEPTIONS = [
    "requests.exceptions.Timeout",
    "requests.exceptions.ConnectionError",
    "requests.exceptions.ConnectTimeout",
    "requests.exceptions.ReadTimeout",
    "TimeoutError",
]

# Rate limiting configuration
RATE_LIMIT_REQUESTS_PER_SECOND = 2  # Maximum requests per second
RATE_LIMIT_REQUESTS_PER_MINUTE = 60  # Maximum requests per minute
RATE_LIMIT_REQUESTS_PER_HOUR = 1000  # Maximum requests per hour

# Crawl delay configuration
CRAWL_DELAY_BETWEEN_REQUESTS = 2.0  # Delay between requests (seconds)
CRAWL_DELAY_BETWEEN_DOMAINS = 5.0  # Delay between different domains (seconds)

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
    "twitter": {
        "domains": [
            "twitter.com",
            "www.twitter.com",
            "x.com",
            "www.x.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)",
        ],
        "enabled": True,
    },
    "linkedin": {
        "domains": [
            "linkedin.com",
            "www.linkedin.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9-]+)",
            r"(?:https?://)?(?:www\.)?linkedin\.com/company/([a-zA-Z0-9-]+)",
        ],
        "enabled": True,
    },
    "youtube": {
        "domains": [
            "youtube.com",
            "www.youtube.com",
            "youtu.be",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?youtube\.com/(?:c|channel|user|@)/([a-zA-Z0-9_-]+)",
            r"(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]+)",
        ],
        "enabled": True,
    },
    "tiktok": {
        "domains": [
            "tiktok.com",
            "www.tiktok.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?tiktok\.com/@([a-zA-Z0-9_.]+)",
        ],
        "enabled": True,
    },
    "pinterest": {
        "domains": [
            "pinterest.com",
            "www.pinterest.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?pinterest\.com/([a-zA-Z0-9_]+)",
        ],
        "enabled": True,
    },
    "snapchat": {
        "domains": [
            "snapchat.com",
            "www.snapchat.com",
        ],
        "patterns": [
            r"(?:https?://)?(?:www\.)?snapchat\.com/add/([a-zA-Z0-9_.]+)",
        ],
        "enabled": True,
    },
    "whatsapp": {
        "domains": [
            "wa.me",
            "whatsapp.com",
            "www.whatsapp.com",
        ],
        "patterns": [
            r"(?:https?://)?wa\.me/(\d+)",
            r"(?:https?://)?(?:www\.)?whatsapp\.com/send\?phone=(\d+)",
        ],
        "enabled": True,
    },
}

# Logging configuration
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FILE = "social_media_finder.log"

