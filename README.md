# Social Media Finder

## 项目简介
Social Media Finder 是一个专门用于从网站中爬取信息的智能工具，支持社交媒体信息提取和价格页面查找两大功能。

## 功能特性

### 社交媒体查找
- ✅ 从目标网站爬取社交媒体链接
- ✅ 重点提取 Instagram (IG) 和 Facebook (FB) 信息
- ✅ 支持多种链接格式识别
- ✅ 自动提取账户名和主页链接
- ✅ 过滤无关信息，提升效率

### 价格页面查找 🆕
- ✅ 智能识别价格相关页面
- ✅ 自动检测页面内容中的价格信息
- ✅ 支持批量处理多个域名
- ✅ 多置信度评级（高、中、低）
- ✅ 生成多种格式报告（JSON、CSV、TXT）

## 技术栈
- **Python 3.8+**
- **requests**: HTTP 请求库
- **BeautifulSoup4**: HTML 解析
- **lxml**: 高性能 XML/HTML 解析器
- **validators**: URL 验证

## 项目结构
```
Social_Media_Finder/
├── README.md                      # 项目说明文档
├── BATCH_PROCESS_README.md        # 社交媒体批量处理指南
├── PRICING_PAGES_README.md        # 价格页面查找使用指南 🆕
├── requirements.txt               # 项目依赖
├── config.py                      # 配置文件
├── main.py                        # 主程序入口（单个网站）
├── batch_process.py               # 社交媒体批量处理脚本
├── find_pricing_pages.py          # 价格页面查找脚本 🆕
├── input_website_list.txt         # 输入URL列表
├── input_website_list_cleaned.txt # 清理后的域名列表
├── crawler/                       # 爬虫核心模块
│   ├── __init__.py
│   ├── base_crawler.py            # 基础爬虫类
│   ├── social_media_finder.py     # 社交媒体查找器
│   ├── pricing_page_finder.py     # 价格页面查找器 🆕
│   └── parsers.py                 # 链接解析器
├── utils/                         # 工具模块
│   ├── __init__.py
│   ├── url_validator.py           # URL 验证工具
│   └── logger.py                  # 日志工具
└── tests/                         # 测试模块
    ├── __init__.py
    └── test_example.py            # 示例测试
```

## 安装步骤
```bash
# 1. 克隆项目
git clone <repository_url>
cd Social_Media_Finder

# 2. 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt
```

## 使用方法

### 功能1：社交媒体查找

#### 单个网站爬取
```python
from crawler.social_media_finder import SocialMediaFinder

# 创建爬虫实例
finder = SocialMediaFinder()

# 爬取目标网站的社交媒体信息
result = finder.find("https://example.com")

# 查看结果
print(result)
```

#### 批量处理
```bash
# 准备包含URL列表的文件 input_website_list.txt
# 每行一个URL

# 运行批量处理脚本
python batch_process.py

# 自动生成3个结果文件：
# - results_YYYYMMDD_HHMMSS.json (完整结果)
# - results_summary_YYYYMMDD_HHMMSS.csv (表格汇总)
# - social_media_found_YYYYMMDD_HHMMSS.txt (找到的账户)
```

详细的批量处理使用说明请查看 [BATCH_PROCESS_README.md](BATCH_PROCESS_README.md)

### 功能2：价格页面查找 🆕

#### 批量查找价格页面
```bash
# 准备包含域名列表的文件 input_website_list_cleaned.txt
# 每行一个域名

# 运行价格页面查找脚本
python find_pricing_pages.py

# 自动生成3个结果文件：
# - pricing_pages_results_YYYYMMDD_HHMMSS.json (完整结果)
# - pricing_pages_summary_YYYYMMDD_HHMMSS.csv (表格汇总)
# - pricing_pages_found_YYYYMMDD_HHMMSS.txt (找到的页面)
```

**主要特性**：
- 智能识别价格相关关键词（pricing, services, menu, rates等）
- 自动检测页面内容中的价格符号（$、USD等）
- 多置信度评级（高、中、低）
- 详细的统计信息和日志

详细的价格页面查找使用说明请查看 [PRICING_PAGES_README.md](PRICING_PAGES_README.md)

## 输出格式
```json
{
  "url": "https://example.com",
  "instagram": [
    {
      "username": "example_user",
      "profile_url": "https://instagram.com/example_user"
    }
  ],
  "facebook": [
    {
      "username": "example.page",
      "profile_url": "https://facebook.com/example.page"
    }
  ],
  "found_at": "2025-11-28 10:00:00"
}
```

## 支持的链接格式

### Instagram
- `https://instagram.com/username`
- `https://www.instagram.com/username`
- `https://instagr.am/username`
- `instagram.com/username`

### Facebook
- `https://facebook.com/username`
- `https://www.facebook.com/username`
- `https://fb.com/username`
- `https://facebook.com/profile.php?id=123456`

## 开发计划

### 已完成功能
- [x] 项目框架搭建
- [x] 基础爬虫功能
- [x] Instagram 链接提取
- [x] Facebook 链接提取
- [x] 价格页面智能查找 🆕
- [x] 批量处理功能
- [x] 多格式结果输出（JSON、CSV、TXT）

### 计划功能
- [ ] 支持更多社交媒体平台（Twitter、LinkedIn等）
- [ ] 添加并发爬取功能
- [ ] 数据持久化存储（数据库）
- [ ] Web API 接口
- [ ] 价格信息提取和结构化
- [ ] 定时任务和监控功能

## 注意事项
1. 请遵守目标网站的 robots.txt 规则
2. 合理控制爬取频率，避免对目标网站造成压力
3. 本工具仅用于学习和研究目的
4. 使用时请遵守相关法律法规

## License
MIT License

