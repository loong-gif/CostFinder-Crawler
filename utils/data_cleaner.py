"""
数据清洗工具
"""
import re
from typing import Optional, Union

def clean_price(price_text: str) -> Optional[float]:
    """
    清洗价格文本,提取数值
    
    Args:
        price_text: 原始价格文本,如 "$24.99", "24.99 USD", "1,234.99"
    
    Returns:
        float: 清洗后的价格数值,失败返回None
    """
    if not price_text:
        return None
    
    # 移除空白字符
    price_text = price_text.strip()
    
    # 常见价格模式
    patterns = [
        r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # $24.99
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*USD',  # 24.99 USD
        r'USD\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # USD 24.99
        r'(\d+(?:,\d{3})*\.\d{2})',  # 1,234.99
    ]
    
    for pattern in patterns:
        match = re.search(pattern, price_text, re.IGNORECASE)
        if match:
            price_str = match.group(1)
            # 移除逗号
            price_str = price_str.replace(',', '')
            try:
                return float(price_str)
            except ValueError:
                continue
    
    return None

def clean_wine_name(name: str) -> str:
    """清洗酒名"""
    if not name:
        return ""
    
    # 移除多余空白
    name = re.sub(r'\s+', ' ', name)
    name = name.strip()
    
    # 移除HTML标签
    name = re.sub(r'<[^>]+>', '', name)
    
    return name

def extract_year(text: str) -> Optional[int]:
    """从文本中提取年份"""
    if not text:
        return None
    
    # 查找年份模式 (1900-2099)
    match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
    if match:
        return int(match.group(1))
    
    return None

def check_stock_status(text: str) -> str:
    """
    检查库存状态
    
    Returns:
        str: "In Stock", "Out of Stock", "Unknown"
    """
    if not text:
        return "Unknown"
    
    text_lower = text.lower()
    
    # 有货关键词
    in_stock_keywords = [
        "in stock", "available", "add to cart", "buy now", 
        "add to bag", "purchase", "order now"
    ]
    
    # 缺货关键词
    out_stock_keywords = [
        "out of stock", "sold out", "unavailable", "notify me",
        "coming soon", "pre-order", "back order"
    ]
    
    for keyword in in_stock_keywords:
        if keyword in text_lower:
            return "In Stock"
    
    for keyword in out_stock_keywords:
        if keyword in text_lower:
            return "Out of Stock"
    
    return "Unknown"

def extract_domain(url: str) -> str:
    """提取域名"""
    if not url:
        return ""
    
    match = re.search(r'https?://([^/]+)', url)
    if match:
        domain = match.group(1)
        # 移除www
        domain = re.sub(r'^www\.', '', domain)
        return domain
    
    return url

def normalize_text(text: str) -> str:
    """标准化文本"""
    if not text:
        return ""
    
    # 移除多余空白
    text = re.sub(r'\s+', ' ', text)
    
    # 移除首尾空白
    text = text.strip()
    
    # 移除特殊字符(保留基本标点)
    text = re.sub(r'[^\w\s\-\.,\$]', '', text)
    
    return text


