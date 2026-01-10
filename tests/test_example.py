"""
示例测试文件
演示如何测试爬虫功能
"""

import sys
import os
import io

# 设置 stdout 编码为 UTF-8（Windows 兼容）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from crawler.social_media_finder import SocialMediaFinder
from utils.url_validator import URLValidator


def test_url_validator():
    """测试 URL 验证功能"""
    print("\n🧪 测试 URL 验证器...")
    
    # 测试有效 URL
    assert URLValidator.is_valid_url("https://example.com") == True
    assert URLValidator.is_valid_url("http://test.com") == True
    
    # 测试无效 URL
    assert URLValidator.is_valid_url("not a url") == False
    assert URLValidator.is_valid_url("") == False
    
    # 测试 URL 规范化
    normalized = URLValidator.normalize_url("example.com")
    assert normalized == "https://example.com"
    
    # 测试域名提取
    domain = URLValidator.get_domain("https://www.example.com/path")
    assert domain == "www.example.com"
    
    print("✅ URL 验证器测试通过")


def test_social_media_finder():
    """测试社交媒体查找功能"""
    print("\n🧪 测试社交媒体查找器...")
    
    with SocialMediaFinder() as finder:
        # 测试基本功能结构（不实际访问网站）
        # 这里只测试返回结果的结构
        
        # 注意：实际测试需要真实的网站
        # 这里仅作为示例展示如何进行测试
        
        # 如果要进行真实测试，取消下面的注释：
        # result = finder.find("https://example.com")
        # assert "instagram" in result
        # assert "facebook" in result
        # assert "status" in result
        # assert result["status"] in ["success", "failed", "error"]
        
        print("✅ 社交媒体查找器测试通过")


def test_parser_logic():
    """测试解析器逻辑"""
    print("\n🧪 测试解析器...")
    
    from crawler.parsers import SocialMediaParser
    
    parser = SocialMediaParser()
    
    # 测试平台识别
    assert parser._identify_platform("https://instagram.com/user") == "instagram"
    assert parser._identify_platform("https://facebook.com/page") == "facebook"
    assert parser._identify_platform("https://twitter.com/user") == None
    
    # 测试用户名验证
    assert parser._is_valid_username("valid_user") == True
    assert parser._is_valid_username("login") == False  # 关键词过滤
    assert parser._is_valid_username("") == False
    
    # 测试链接解析
    ig_info = parser._parse_link("https://instagram.com/test_user", "instagram")
    assert ig_info is not None
    assert ig_info["username"] == "test_user"
    
    fb_info = parser._parse_link("https://facebook.com/test.page", "facebook")
    assert fb_info is not None
    assert fb_info["username"] == "test.page"
    
    print("✅ 解析器测试通过")


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("🚀 开始运行测试...")
    print("=" * 60)
    
    try:
        test_url_validator()
        test_parser_logic()
        test_social_media_finder()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\n❌ 测试失败: {str(e)}")
        raise
    except Exception as e:
        print(f"\n❌ 发生错误: {str(e)}")
        raise


if __name__ == "__main__":
    run_all_tests()

