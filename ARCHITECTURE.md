# CostFinder 架构设计文档

## 1. 系统架构

### 1.1 整体架构
```
┌─────────────────────────────────────────────────────────┐
│                     Main Entry                          │
│                    (main.py)                            │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │                         │
        ▼                         ▼
┌──────────────┐          ┌──────────────┐
│   Browser    │          │  URL Queue   │
│   Manager    │          │   Manager    │
└──────┬───────┘          └──────┬───────┘
       │                         │
       │      ┌─────────────────┘
       │      │
       ▼      ▼
┌─────────────────────────────────┐
│      WineCrawler                │
│   (Core Crawler Logic)          │
│                                 │
│  ┌──────────────────────────┐  │
│  │  Parse Strategy          │  │
│  │  1. DOM Parser (First)   │  │
│  │  2. Wait Dynamic Load    │  │
│  │  3. OCR Parser (Backup)  │  │
│  └──────────────────────────┘  │
└─────┬───────────────────┬───────┘
      │                   │
      ▼                   ▼
┌─────────────┐   ┌──────────────┐
│ Page Parser │   │  OCR Parser  │
│  (DOM/CSS)  │   │  (Screenshot)│
└─────┬───────┘   └──────┬───────┘
      │                   │
      └────────┬──────────┘
               │
               ▼
        ┌─────────────┐
        │Data Cleaner │
        │  & Exporter │
        └─────────────┘
```

## 2. 核心模块

### 2.1 Browser Manager (browser_manager.py)
**职责**: 管理Playwright浏览器实例

**关键功能**:
- 浏览器生命周期管理
- 浏览器上下文(Context)创建与复用
- 反爬虫配置(User-Agent, Stealth)
- 资源拦截优化(阻止图片/字体加载)

**反爬虫策略**:
```python
- 随机User-Agent
- 覆盖navigator.webdriver
- 注入chrome对象
- 修改permissions API
- 禁用自动化特征检测
```

### 2.2 Wine Crawler (base_crawler.py)
**职责**: 核心爬取逻辑与策略控制

**三层解析策略**:
1. **Level 1**: DOM直接解析(最快)
   - 使用CSS选择器定位价格元素
   - 优先级最高

2. **Level 2**: 等待动态加载 + DOM解析
   - 等待JavaScript执行
   - 等待网络空闲(networkidle)
   - 动态元素出现后再解析

3. **Level 3**: 截图 + OCR识别(兜底)
   - 全页截图
   - OCR文字识别
   - 正则提取价格

**并发控制**:
- 使用asyncio.gather并发处理
- 分批处理(每批max_workers个)
- 自动错误恢复与重试

### 2.3 Page Parser (page_parser.py)
**职责**: DOM解析与数据提取

**提取目标**:
- 价格: 多选择器 + 正则匹配
- 酒名: H1标签、产品标题、meta标签
- 年份: 正则提取(1900-2099)
- 库存: 关键词匹配
- 产区: 关键词列表匹配

**选择器优先级**:
```python
Price: .price > .product-price > [data-price] > 正则全文搜索
Name: h1 > .product-title > title标签
```

### 2.4 OCR Parser (ocr_parser.py)
**职责**: 图像文字识别

**支持的OCR引擎**:
- **PaddleOCR** (推荐): 高精度,支持多语言
- EasyOCR: 易用,准确率高
- Tesseract: 开源经典

**OCR流程**:
1. 读取截图
2. OCR识别全部文本
3. 置信度过滤(>0.6)
4. 正则匹配价格模式
5. 清洗与验证

**价格提取正则**:
```regex
\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)    # $24.99
(\d+\.\d{2})\s*USD                     # 24.99 USD
Price[:\s]+\$?\s*(\d+\.\d{2})          # Price: $24.99
```

### 2.5 Data Cleaner (data_cleaner.py)
**职责**: 数据清洗与标准化

**清洗规则**:
- 价格: 移除货币符号、千分位逗号
- 酒名: 移除HTML标签、多余空白
- 年份: 验证范围(1900-2099)
- 库存: 标准化为3种状态(In Stock/Out of Stock/Unknown)

## 3. 性能优化

### 3.1 速度优化
- **资源拦截**: 阻止图片、字体、媒体加载
- **并发处理**: asyncio异步并发(10个/批)
- **上下文复用**: 浏览器实例复用,减少启动开销
- **智能等待**: 最小化等待时间,避免固定延迟

### 3.2 准确性优化
- **多选择器**: 使用多个CSS选择器提升覆盖率
- **正则兜底**: 全文正则搜索作为后备方案
- **OCR备选**: DOM失败时使用OCR
- **数据验证**: 价格范围校验($5-$10000)

### 3.3 稳定性优化
- **自动重试**: 失败自动重试3次,指数退避
- **异常捕获**: 全局异常处理,不中断整体流程
- **日志记录**: 详细日志,便于调试
- **优雅关闭**: 确保资源正确释放

## 4. 数据流

```
Input URLs (txt)
    ↓
Load & Queue
    ↓
[Batch 1] → [Browser 1] → Parse → Extract → Clean → Result 1
[Batch 1] → [Browser 2] → Parse → Extract → Clean → Result 2
...
[Batch 1] → [Browser N] → Parse → Extract → Clean → Result N
    ↓
Aggregate Results
    ↓
Export (CSV/JSON/Excel)
```

## 5. 配置系统

### 5.1 可配置项
- **并发**: MAX_WORKERS
- **超时**: REQUEST_TIMEOUT, PAGE_LOAD_TIMEOUT
- **重试**: MAX_RETRIES, RETRY_DELAY
- **延迟**: MIN_DELAY, MAX_DELAY
- **OCR**: OCR_ENGINE, OCR_CONFIDENCE_THRESHOLD
- **解析策略**: PARSE_STRATEGY

### 5.2 策略开关
```python
PARSE_STRATEGY = {
    "dom_first": True,      # 优先DOM解析
    "wait_dynamic": True,   # 等待动态加载
    "ocr_fallback": True,   # OCR兜底
}
```

## 6. 扩展性

### 6.1 新增OCR引擎
实现OCRParser接口:
```python
def extract_text_from_image(self, image_path: str) -> List[Tuple[str, float]]
```

### 6.2 新增解析策略
继承PageParser并重写:
```python
async def parse_page(self, page: Page, url: str) -> Dict[str, Any]
```

### 6.3 新增数据输出格式
在main.py的save_results()中添加格式处理

## 7. 错误处理

### 7.1 错误分类
- **网络错误**: 自动重试
- **超时错误**: 降低期望,继续尝试解析
- **解析错误**: 记录并标记为Failed
- **OCR错误**: 记录并返回空结果

### 7.2 容错机制
- 单个URL失败不影响整体
- 批次失败自动跳过
- 全局异常捕获

## 8. 监控与调试

### 8.1 日志级别
- **DEBUG**: 详细调试信息
- **INFO**: 一般运行信息
- **WARNING**: 警告(未提取到数据等)
- **ERROR**: 错误(网络失败、解析异常等)

### 8.2 调试模式
```bash
python main.py --debug  # 显示浏览器窗口
python main.py --limit 5  # 仅测试5个URL
```

## 9. 未来优化方向

### 9.1 功能增强
- [ ] 代理IP池支持
- [ ] 验证码识别
- [ ] 登录支持(需要账号的网站)
- [ ] 分布式爬取(多机器)

### 9.2 智能化
- [ ] 自动识别页面结构(AI辅助)
- [ ] 智能选择器学习
- [ ] 价格变化监控与通知

### 9.3 可视化
- [ ] Web控制面板
- [ ] 实时进度展示
- [ ] 数据可视化图表


