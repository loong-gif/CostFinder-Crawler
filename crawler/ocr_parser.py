"""
OCR解析器 - 使用OCR从截图中提取文本
"""
import os
from pathlib import Path
from typing import Optional, List, Tuple
from PIL import Image
import re

from config.settings import (
    OCR_ENGINE, OCR_LANG, OCR_USE_GPU, OCR_CONFIDENCE_THRESHOLD,
    SCREENSHOT_DIR
)
from utils.logger import log
from utils.data_cleaner import clean_price

class OCRParser:
    """OCR解析器"""
    
    def __init__(self):
        self.engine = None
        self._init_engine()
    
    def _init_engine(self):
        """初始化OCR引擎"""
        try:
            if OCR_ENGINE == "paddleocr":
                from paddleocr import PaddleOCR
                self.engine = PaddleOCR(
                    use_angle_cls=True,
                    lang=OCR_LANG,
                    use_gpu=OCR_USE_GPU,
                    show_log=False,
                )
                log.info("PaddleOCR引擎初始化成功")
                
            elif OCR_ENGINE == "easyocr":
                import easyocr
                self.engine = easyocr.Reader([OCR_LANG], gpu=OCR_USE_GPU)
                log.info("EasyOCR引擎初始化成功")
                
            elif OCR_ENGINE == "tesseract":
                import pytesseract
                self.engine = pytesseract
                log.info("Tesseract引擎初始化成功")
                
            else:
                raise ValueError(f"不支持的OCR引擎: {OCR_ENGINE}")
                
        except Exception as e:
            log.error(f"OCR引擎初始化失败: {e}")
            raise
    
    def extract_text_from_image(self, image_path: str) -> List[Tuple[str, float]]:
        """
        从图片中提取文本
        
        Args:
            image_path: 图片路径
        
        Returns:
            List[Tuple[str, float]]: [(文本, 置信度), ...]
        """
        if not os.path.exists(image_path):
            log.error(f"图片不存在: {image_path}")
            return []
        
        try:
            if OCR_ENGINE == "paddleocr":
                result = self.engine.ocr(image_path, cls=True)
                texts = []
                
                if result and result[0]:
                    for line in result[0]:
                        text = line[1][0]
                        confidence = line[1][1]
                        if confidence >= OCR_CONFIDENCE_THRESHOLD:
                            texts.append((text, confidence))
                
                return texts
                
            elif OCR_ENGINE == "easyocr":
                result = self.engine.readtext(image_path)
                texts = []
                
                for bbox, text, confidence in result:
                    if confidence >= OCR_CONFIDENCE_THRESHOLD:
                        texts.append((text, confidence))
                
                return texts
                
            elif OCR_ENGINE == "tesseract":
                import pytesseract
                from PIL import Image
                
                img = Image.open(image_path)
                text = pytesseract.image_to_string(img)
                
                # Tesseract不返回置信度,使用默认值
                return [(text, 1.0)]
            
        except Exception as e:
            log.error(f"OCR提取失败: {e}")
            return []
    
    def extract_price_from_image(self, image_path: str) -> Optional[float]:
        """
        从图片中提取价格
        
        Args:
            image_path: 图片路径
        
        Returns:
            float: 价格,失败返回None
        """
        texts = self.extract_text_from_image(image_path)
        
        if not texts:
            log.warning(f"未能从图片中提取到文本: {image_path}")
            return None
        
        # 合并所有文本
        full_text = " ".join([text for text, _ in texts])
        
        log.debug(f"OCR提取的文本: {full_text[:100]}...")
        
        # 查找价格模式
        price_patterns = [
            r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # $24.99
            r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*USD',  # 24.99 USD
            r'USD\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # USD 24.99
            r'Price[:\s]+\$?\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # Price: $24.99
        ]
        
        for pattern in price_patterns:
            matches = re.findall(pattern, full_text, re.IGNORECASE)
            if matches:
                # 取第一个匹配
                price_str = matches[0]
                price = clean_price(price_str)
                if price:
                    log.info(f"从OCR中提取到价格: ${price}")
                    return price
        
        log.warning(f"未能从OCR文本中提取价格: {image_path}")
        return None
    
    def extract_all_info_from_image(self, image_path: str) -> dict:
        """
        从图片中提取所有信息
        
        Returns:
            dict: {
                "price": float,
                "wine_name": str,
                "year": int,
                "stock_status": str,
                "all_text": str,
            }
        """
        texts = self.extract_text_from_image(image_path)
        
        if not texts:
            return {
                "price": None,
                "wine_name": None,
                "year": None,
                "stock_status": "Unknown",
                "all_text": "",
            }
        
        # 合并所有文本
        full_text = " ".join([text for text, _ in texts])
        
        # 提取价格
        price = self.extract_price_from_image(image_path)
        
        # 提取年份
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', full_text)
        year = int(year_match.group(1)) if year_match else None
        
        # 检查库存状态
        stock_status = "Unknown"
        if re.search(r'(in stock|available|add to cart)', full_text, re.IGNORECASE):
            stock_status = "In Stock"
        elif re.search(r'(out of stock|sold out|unavailable)', full_text, re.IGNORECASE):
            stock_status = "Out of Stock"
        
        return {
            "price": price,
            "wine_name": None,  # 酒名较难从OCR准确提取,建议从URL或HTML获取
            "year": year,
            "stock_status": stock_status,
            "all_text": full_text,
        }


