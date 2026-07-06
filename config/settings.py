"""
配置文件 - 所有可调参数
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent

# ==================== 爬虫配置 ====================
# 并发设置
MAX_WORKERS = 10  # 最大并发浏览器数量
REQUEST_TIMEOUT = 30000  # 请求超时(毫秒)
PAGE_LOAD_TIMEOUT = 20000  # 页面加载超时(毫秒)

# 重试设置
MAX_RETRIES = 3  # 最大重试次数
RETRY_DELAY = 2  # 重试延迟(秒)
RETRY_BACKOFF = 2  # 重试延迟倍数

# 延迟设置(反爬虫)
MIN_DELAY = 1.0  # 最小延迟(秒)
MAX_DELAY = 3.0  # 最大延迟(秒)

# ==================== OCR配置 ====================
OCR_ENGINE = "paddleocr"  # 可选: paddleocr, easyocr, tesseract
OCR_LANG = "en"  # OCR语言
OCR_USE_GPU = False  # 是否使用GPU加速
OCR_CONFIDENCE_THRESHOLD = 0.6  # OCR置信度阈值

# ==================== 截图配置 ====================
SCREENSHOT_ENABLED = True  # 是否启用截图
SCREENSHOT_QUALITY = 90  # 截图质量(1-100)
FULL_PAGE_SCREENSHOT = True  # 是否全页截图
SCREENSHOT_DIR = BASE_DIR / "output" / "screenshots"

# ==================== 浏览器配置 ====================
BROWSER_TYPE = "chromium"  # chromium, firefox, webkit
HEADLESS = True  # 无头模式
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
]

# 视口大小
VIEWPORT = {
    "width": 1920,
    "height": 1080,
}

# ==================== 解析配置 ====================
# 价格相关选择器(常见模式)
PRICE_SELECTORS = [
    ".price",
    ".product-price",
    "[class*='price']",
    "[data-price]",
    ".amount",
    ".cost",
    "span[itemprop='price']",
]

# 价格正则表达式
PRICE_PATTERNS = [
    r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # $24.99, $1,234.99
    r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*USD',  # 24.99 USD
    r'USD\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # USD 24.99
    r'(\d+\.\d{2})',  # 24.99
]

# 库存状态关键词
IN_STOCK_KEYWORDS = ["in stock", "available", "add to cart", "buy now"]
OUT_STOCK_KEYWORDS = ["out of stock", "sold out", "unavailable", "notify me"]

# ==================== 数据输出配置 ====================
OUTPUT_DIR = BASE_DIR / "output" / "results"
OUTPUT_FORMAT = ["csv", "json"]  # 输出格式
OUTPUT_ENCODING = "utf-8-sig"  # CSV编码(支持中文Excel)

# ==================== 日志配置 ====================
LOG_DIR = BASE_DIR / "output" / "logs"
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_ROTATION = "500 MB"  # 日志文件大小限制
LOG_RETENTION = "30 days"  # 日志保留时间

# ==================== 代理配置(可选) ====================
USE_PROXY = False  # 是否使用代理
PROXY_POOL = [
    # "http://proxy1.example.com:8080",
    # "http://proxy2.example.com:8080",
]

# ==================== Firecrawl 配置 ====================
FIRECRAWL_CRAWL_MAX_PAGES = int(os.getenv("FIRECRAWL_CRAWL_MAX_PAGES", "50"))
FIRECRAWL_CRAWL_TIMEOUT_SECS = int(os.getenv("FIRECRAWL_CRAWL_TIMEOUT_SECS", "1800"))

# ==================== 智能策略配置 ====================
# 解析策略优先级
PARSE_STRATEGY = {
    "dom_first": True,  # 优先DOM解析
    "wait_dynamic": True,  # 等待动态加载
    "ocr_fallback": True,  # OCR兜底
}

# 动态加载检测
DYNAMIC_LOAD_INDICATORS = [
    "networkidle",  # 网络空闲
    # "load",  # 页面加载完成
    # "domcontentloaded",  # DOM加载完成
]

# 等待时间
WAIT_FOR_SELECTOR_TIMEOUT = 5000  # 等待元素出现超时(毫秒)

# ==================== 输入文件配置 ====================
INPUT_URLS_FILE = BASE_DIR / "input_websites.txt"
INPUT_SEARCH_TERMS_FILE = BASE_DIR / "Uncle Fossil Crawler - Search Term.csv"

# ==================== 创建必要目录 ====================
def ensure_directories():
    """确保所有必要的目录存在"""
    directories = [
        SCREENSHOT_DIR,
        OUTPUT_DIR,
        LOG_DIR,
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

# 初始化目录
ensure_directories()
