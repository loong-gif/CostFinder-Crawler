"""
基础爬虫类 - 核心爬取逻辑
"""
import asyncio
import base64
import os
import random
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional

from config.settings import (
    MIN_DELAY, MAX_DELAY, SCREENSHOT_ENABLED, SCREENSHOT_DIR,
    PARSE_STRATEGY, BROWSER_ARGS, BROWSER_TYPE, HEADLESS, BASE_DIR
)
from config.user_agents import get_random_user_agent
from crawler.page_parser import PageParser
from crawler.ocr_parser import OCRParser
from utils.logger import log
from utils.retry import async_retry

# crawl4ai 默认写入 ~/.crawl4ai，沙箱环境下可能无权限。
# 将缓存目录限制到项目内，避免权限问题。
os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(BASE_DIR))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / ".playwright_browsers"))

class WineCrawler:
    """葡萄酒爬虫"""
    
    def __init__(self, max_concurrent_pages: int = 3):
        """
        初始化爬虫
        
        Args:
            max_concurrent_pages: 最大并发页面数，默认3（防止浏览器崩溃）
        """
        self.crawler: Optional[Any] = None
        self._crawl4ai_cache_mode_bypass: Optional[Any] = None
        self.page_parser = PageParser()
        self.ocr_parser = None
        if PARSE_STRATEGY.get("ocr_fallback"):
            try:
                self.ocr_parser = OCRParser()
            except Exception as e:
                log.warning(f"OCR 引擎初始化失败，自动关闭 OCR 回退: {e}")
        self.results: List[Dict[str, Any]] = []
        self.page_semaphore = asyncio.Semaphore(max_concurrent_pages)  # 限制并发页面数
        
    async def start(self):
        """启动爬虫"""
        if sys.version_info < (3, 10):
            raise RuntimeError("crawl4ai 需要 Python 3.10+，当前环境不兼容。")
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode
        except Exception as e:
            raise RuntimeError(f"无法导入 crawl4ai: {e}") from e

        cdp_url = os.getenv("CRAWL4AI_CDP_URL", "").strip() or None
        browser_config = BrowserConfig(
            browser_type=BROWSER_TYPE,
            headless=HEADLESS,
            user_agent=get_random_user_agent(),
            extra_args=BROWSER_ARGS,
            cdp_url=cdp_url,
            use_managed_browser=bool(cdp_url),
            verbose=False,
        )
        self.crawler = AsyncWebCrawler(config=browser_config)
        self._crawl4ai_cache_mode_bypass = CacheMode.BYPASS
        await self.crawler.start()
        log.info("爬虫初始化完成")
    
    async def close(self):
        """关闭爬虫"""
        if self.crawler:
            await self.crawler.close()
            self.crawler = None
        log.info("爬虫已关闭")
    
    @async_retry(max_attempts=3)
    async def crawl_url(self, url: str) -> Dict[str, Any]:
        """
        爬取单个URL
        
        Args:
            url: 目标URL
        
        Returns:
            dict: 爬取结果
        """
        # 使用信号量控制并发
        async with self.page_semaphore:
            if not self.crawler:
                raise RuntimeError("爬虫未初始化，请先调用 start()")
            log.info(f"开始爬取: {url}")
            try:
                run_config = CrawlerRunConfig(
                    cache_mode=self._crawl4ai_cache_mode_bypass,
                    screenshot=SCREENSHOT_ENABLED and PARSE_STRATEGY.get("ocr_fallback", False),
                )
                crawl_result = await self.crawler.arun(url=url, config=run_config)

                if not getattr(crawl_result, "success", False):
                    error_message = getattr(crawl_result, "error_message", "unknown crawl error")
                    raise RuntimeError(error_message)

                # 3. 尝试DOM解析
                result = None
                if PARSE_STRATEGY.get("dom_first"):
                    html = self._extract_html(crawl_result)
                    result = self.page_parser.parse_html(html, url)
                    
                    # 如果DOM解析成功(提取到价格),直接返回
                    if result and result.get("price"):
                        log.success(f"DOM解析成功: {url} - ${result['price']}")
                        return result
                
                # 4. 如果DOM解析失败,尝试OCR
                if PARSE_STRATEGY.get("ocr_fallback") and self.ocr_parser:
                    log.info(f"DOM解析未提取到价格,尝试OCR: {url}")
                    result = await self._parse_with_ocr(crawl_result, url)
                    
                    if result and result.get("price"):
                        log.success(f"OCR解析成功: {url} - ${result['price']}")
                        return result
                
                # 5. 如果都失败,返回基础信息
                if not result:
                    result = {
                        "url": url,
                        "wine_name": None,
                        "year": None,
                        "region": None,
                        "price": None,
                        "stock_status": "Unknown",
                        "merchant": self._extract_domain(url),
                        "parse_method": "Failed",
                    }
                
                log.warning(f"爬取完成但未提取到价格: {url}")
                return result
                
            except Exception as e:
                log.error(f"爬取失败: {url} - {e}")
                return {
                    "url": url,
                    "wine_name": None,
                    "year": None,
                    "region": None,
                    "price": None,
                    "stock_status": "Error",
                    "merchant": self._extract_domain(url),
                    "parse_method": "Error",
                    "error": str(e),
                }

            finally:
                # 随机延迟(反爬虫)
                await self._random_delay()
    
    def _extract_html(self, crawl_result: Any) -> str:
        """从 crawl4ai 结果中提取最可用的HTML文本。"""
        for attr in ("html", "cleaned_html"):
            value = getattr(crawl_result, attr, "")
            if isinstance(value, str) and value.strip():
                return value

        markdown_obj = getattr(crawl_result, "markdown", None)
        if markdown_obj is not None:
            for attr in ("raw_markdown", "fit_markdown"):
                value = getattr(markdown_obj, attr, "")
                if isinstance(value, str) and value.strip():
                    return value

        extracted_content = getattr(crawl_result, "extracted_content", "")
        if isinstance(extracted_content, str) and extracted_content.strip():
            return extracted_content
        return ""
    
    async def _parse_with_ocr(self, crawl_result: Any, url: str) -> Optional[Dict[str, Any]]:
        """使用OCR解析页面"""
        if not SCREENSHOT_ENABLED or not self.ocr_parser:
            return None
        
        try:
            # 1. 截图
            screenshot_path = self._save_crawl4ai_screenshot(crawl_result, url)
            
            if not screenshot_path:
                return None
            
            # 2. OCR提取
            ocr_result = self.ocr_parser.extract_all_info_from_image(screenshot_path)
            
            # 3. 构造结果
            result = {
                "url": url,
                "wine_name": ocr_result.get("wine_name"),
                "year": ocr_result.get("year"),
                "region": None,
                "price": ocr_result.get("price"),
                "stock_status": ocr_result.get("stock_status", "Unknown"),
                "merchant": self._extract_domain(url),
                "parse_method": "OCR",
                "screenshot": screenshot_path,
            }
            
            return result
            
        except Exception as e:
            log.error(f"OCR解析失败: {url} - {e}")
            return None
    
    def _save_crawl4ai_screenshot(self, crawl_result: Any, url: str) -> Optional[str]:
        """保存 crawl4ai 返回的 base64 截图。"""
        try:
            screenshot_base64 = getattr(crawl_result, "screenshot", "")
            if not isinstance(screenshot_base64, str) or not screenshot_base64.strip():
                return None

            # 生成文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            domain = self._extract_domain(url)
            filename = f"{domain}_{timestamp}.png"
            filepath = SCREENSHOT_DIR / filename

            image_bytes = base64.b64decode(screenshot_base64)
            filepath.write_bytes(image_bytes)
            
            log.debug(f"截图保存: {filepath}")
            return str(filepath)
            
        except Exception as e:
            log.error(f"截图失败: {url} - {e}")
            return None
    
    async def _random_delay(self):
        """随机延迟"""
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        await asyncio.sleep(delay)
    
    def _extract_domain(self, url: str) -> str:
        """提取域名"""
        import re
        match = re.search(r'https?://([^/]+)', url)
        if match:
            domain = match.group(1)
            domain = re.sub(r'^www\.', '', domain)
            return domain
        return url
    
    async def crawl_urls(self, urls: List[str], max_workers: int = 10) -> List[Dict[str, Any]]:
        """
        并发爬取多个URL
        
        Args:
            urls: URL列表
            max_workers: 最大并发数
        
        Returns:
            List[dict]: 爬取结果列表
        """
        results = []
        
        # 分批处理
        for i in range(0, len(urls), max_workers):
            batch = urls[i:i + max_workers]
            log.info(f"处理批次 {i // max_workers + 1}/{(len(urls) + max_workers - 1) // max_workers}, 共{len(batch)}个URL")
            
            # 并发爬取
            tasks = [self.crawl_url(url) for url in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            for result in batch_results:
                if isinstance(result, Exception):
                    log.error(f"任务异常: {result}")
                else:
                    results.append(result)
        
        self.results = results
        log.info(f"爬取完成,共{len(results)}个结果")
        
        return results
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
