"""
页面解析器 - DOM解析提取商品信息
"""
import re
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup

from config.settings import (
    PRICE_SELECTORS, PRICE_PATTERNS, 
    IN_STOCK_KEYWORDS, OUT_STOCK_KEYWORDS
)
from utils.logger import log
from utils.data_cleaner import clean_price, clean_wine_name, extract_year, check_stock_status

class PageParser:
    """页面解析器"""
    
    def parse_html(self, html: str, url: str) -> Dict[str, Any]:
        """
        解析页面内容
        
        Args:
            html: 页面HTML文本
            url: 页面URL
        
        Returns:
            dict: 解析结果
        """
        result = {
            "url": url,
            "wine_name": None,
            "year": None,
            "region": None,
            "price": None,
            "stock_status": "Unknown",
            "merchant": self._extract_domain(url),
            "parse_method": "DOM",
        }
        
        try:
            soup = BeautifulSoup(html or "", 'lxml')
            
            # 1. 提取价格
            result["price"] = self._extract_price(soup)
            
            # 2. 提取酒名
            result["wine_name"] = self._extract_wine_name(soup)
            
            # 3. 提取年份
            result["year"] = self._extract_year_from_soup(soup, result["wine_name"])
            
            # 4. 提取产区
            result["region"] = self._extract_region(soup)
            
            # 5. 检查库存状态
            result["stock_status"] = self._check_stock(soup)
            
            log.info(f"DOM解析成功: {url} - 价格: ${result['price']}")
            
        except Exception as e:
            log.error(f"DOM解析失败: {url} - {e}")
        
        return result
    
    def _extract_price(self, soup: BeautifulSoup) -> Optional[float]:
        """提取价格"""
        # 方法1: 使用BeautifulSoup选择器
        for selector in PRICE_SELECTORS:
            try:
                elements = soup.select(selector)
                for element in elements[:3]:  # 只检查前3个匹配
                    text = element.get_text(strip=True)
                    price = clean_price(text)
                    if price:
                        log.debug(f"通过BeautifulSoup提取价格: {selector} -> ${price}")
                        return price
            except Exception:
                continue
        
        # 方法2: 正则表达式全文搜索
        page_text = soup.get_text()
        for pattern in PRICE_PATTERNS:
            matches = re.findall(pattern, page_text)
            if matches:
                price_str = matches[0]
                price = clean_price(price_str)
                if price and 5 <= price <= 10000:  # 合理价格范围
                    log.debug(f"通过正则提取价格: {pattern} -> ${price}")
                    return price
        
        log.warning("未能提取到价格")
        return None
    
    def _extract_wine_name(self, soup: BeautifulSoup) -> Optional[str]:
        """提取酒名"""
        # 常见酒名选择器
        name_selectors = [
            "h1",
            ".product-title",
            ".product-name",
            "[class*='product-title']",
            "[class*='product-name']",
            "h1[itemprop='name']",
            ".title",
        ]
        
        # 方法1: BeautifulSoup
        for selector in name_selectors:
            try:
                element = soup.select_one(selector)
                if element:
                    text = element.get_text(strip=True)
                    name = clean_wine_name(text)
                    if name and len(name) > 3:
                        log.debug(f"提取酒名: {name}")
                        return name
            except Exception:
                continue
        
        # 方法2: 从title标签提取
        title = soup.find('title')
        if title:
            title_text = title.get_text(strip=True)
            # 移除常见后缀
            for suffix in [" | ", " - ", " – "]:
                if suffix in title_text:
                    title_text = title_text.split(suffix)[0]
            
            name = clean_wine_name(title_text)
            if name and len(name) > 3:
                log.debug(f"从title提取酒名: {name}")
                return name
        
        log.warning("未能提取到酒名")
        return None
    
    def _extract_year_from_soup(self, soup: BeautifulSoup, wine_name: Optional[str]) -> Optional[int]:
        """提取年份"""
        # 先从酒名提取
        if wine_name:
            year = extract_year(wine_name)
            if year:
                return year
        
        # 从页面文本提取
        page_text = soup.get_text()
        year = extract_year(page_text[:500])  # 只检查前500字符
        
        return year
    
    def _extract_region(self, soup: BeautifulSoup) -> Optional[str]:
        """提取产区"""
        # 常见产区关键词
        region_keywords = [
            "Paso Robles", "Sonoma Coast", "Napa Valley", "Monterey",
            "Central Coast", "Adelaida District", "Alto Adige"
        ]
        
        page_text = soup.get_text()
        
        for keyword in region_keywords:
            if keyword in page_text:
                log.debug(f"提取产区: {keyword}")
                return keyword
        
        return None
    
    def _check_stock(self, soup: BeautifulSoup) -> str:
        """检查库存状态"""
        # 常见库存选择器
        stock_selectors = [
            ".stock",
            ".availability",
            "[class*='stock']",
            "[class*='availability']",
            ".inventory",
            "button[type='submit']",
        ]
        
        # 检查元素文本
        for selector in stock_selectors:
            try:
                element = soup.select_one(selector)
                if element:
                    status = check_stock_status(element.get_text(" ", strip=True))
                    if status != "Unknown":
                        return status
            except Exception:
                continue
        
        # 检查页面文本
        page_text = soup.get_text()
        return check_stock_status(page_text)
    
    def _extract_domain(self, url: str) -> str:
        """提取域名"""
        match = re.search(r'https?://([^/]+)', url)
        if match:
            domain = match.group(1)
            domain = re.sub(r'^www\.', '', domain)
            return domain
        return url

