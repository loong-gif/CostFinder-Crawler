# 批量爬取使用说明

## 📋 概述

批量爬取功能允许你从 `input_websites.txt` 文件中读取多个URL，并自动爬取每个页面的商品价格和链接信息。

## 🚀 快速开始

### 方法1: 使用Python脚本(推荐)

```bash
# 处理所有URL(显示浏览器窗口)
python3 batch_crawl.py

# 无头模式(不显示浏览器,更快)
python3 batch_crawl.py --headless

# 只处理前10个URL(用于测试)
python3 batch_crawl.py --max-urls 10

# 自定义延迟时间(每个URL之间等待5秒)
python3 batch_crawl.py --delay 5

# 从第50个URL开始处理
python3 batch_crawl.py --start-from 50

# 组合使用
python3 batch_crawl.py --headless --delay 3 --max-urls 20
```

### 方法2: 使用Shell脚本

```bash
# 处理所有URL
./run_batch_crawl.sh

# 无头模式
./run_batch_crawl.sh --headless

# 只处理前10个URL
./run_batch_crawl.sh --max-urls 10

# 查看所有选项
./run_batch_crawl.sh --help
```

### 方法3: 直接使用flask_price_crawler.py

```bash
# 从文件读取URL
python3 flask_price_crawler.py --file input_websites.txt --headless

# 处理前5个URL
python3 flask_price_crawler.py --file input_websites.txt --headless --max-urls 5
```

## 📝 参数说明

### batch_crawl.py 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--file` | URL列表文件路径 | `input_websites.txt` |
| `--headless` | 使用无头模式(不显示浏览器) | `False` |
| `--output` | 输出CSV文件路径 | 自动生成(带时间戳) |
| `--delay` | 每个URL之间的延迟时间(秒) | `2.0` |
| `--max-urls` | 最大处理URL数量(用于测试) | `None`(处理所有) |
| `--start-from` | 从第几个URL开始处理 | `1` |

## 📊 输出结果

### CSV文件格式

结果保存在 `output/results/flask_prices_YYYYMMDD_HHMMSS.csv`，包含以下字段:

- `product_type`: 商品类型(主商品/推荐商品)
- `product_name`: 商品名称
- `price`: 价格(数字)
- `price_display`: 价格(格式化字符串,如$18.99)
- `availability`: 库存状态
- `sku`: SKU编码
- `url`: 商品链接
- `description`: 商品描述
- `timestamp`: 爬取时间

### 统计信息

爬取完成后会显示:
- 总URL数
- 成功/失败数量
- 总商品数(主商品+推荐商品)
- 失败的URL列表(如果有)

## ⚙️ 使用示例

### 示例1: 测试前5个URL

```bash
python3 batch_crawl.py --headless --max-urls 5
```

**输出:**
```
准备爬取 5 个URL
[1/5] 正在爬取: https://www.saq.com/en/14038771
✓ 成功提取 1 个商品 (主商品: 1, 推荐: 0)
...
批量爬取完成!
总URL数: 5
成功: 4
失败: 1
总商品数: 8
```

### 示例2: 从第100个URL开始,处理20个

```bash
python3 batch_crawl.py --headless --start-from 100 --max-urls 20 --delay 3
```

### 示例3: 恢复中断的爬取

如果爬取中断了,可以从上次停止的位置继续:

```bash
# 假设上次处理到第50个URL,现在从第51个开始
python3 batch_crawl.py --headless --start-from 51
```

## 🔧 注意事项

### 1. 网站兼容性

当前爬虫主要针对 **Shopify** 网站(如 `flaskfinewines.com`)优化。对于其他网站:
- ✅ 可能能提取基本信息(名称、价格)
- ⚠️ 推荐商品提取可能不完整
- ⚠️ 某些网站结构可能无法识别

### 2. 处理速度

- **有浏览器窗口**: 约3-5秒/URL
- **无头模式**: 约2-4秒/URL
- **132个URL**: 预计需要 5-10分钟(取决于网络速度)

### 3. 延迟设置

建议设置适当的延迟(`--delay`):
- **默认2秒**: 适合大多数情况
- **1秒**: 较快,但可能被某些网站限制
- **3-5秒**: 更安全,避免被识别为机器人

### 4. 错误处理

- 单个URL失败不会影响其他URL的处理
- 失败的URL会记录在日志中
- 已爬取的数据会保存,即使中途中断

### 5. 中断恢复

如果爬取中断:
1. 查看日志确定处理到第几个URL
2. 使用 `--start-from` 参数从断点继续
3. 结果会自动追加到新的CSV文件(或合并)

## 📈 性能优化建议

1. **使用无头模式**: `--headless` 可以显著提高速度
2. **批量处理**: 一次性处理所有URL,避免频繁启动浏览器
3. **合理延迟**: 不要设置过小的延迟,避免被网站限制
4. **测试先行**: 使用 `--max-urls` 先测试几个URL

## 🐛 常见问题

### Q: 某些网站无法提取商品信息?

A: 当前爬虫主要针对Shopify网站优化。其他网站可能需要:
- 检查网站是否使用JavaScript动态加载
- 查看浏览器控制台是否有错误
- 可能需要针对特定网站定制选择器

### Q: 爬取速度太慢?

A: 
- 使用 `--headless` 无头模式
- 减少 `--delay` 延迟时间(但不建议<1秒)
- 检查网络连接速度

### Q: 如何只爬取特定网站?

A: 编辑 `input_websites.txt`,只保留需要的URL,或创建新的URL文件:

```bash
# 创建只包含flaskfinewines.com的URL文件
grep flaskfinewines.com input_websites.txt > flask_urls.txt

# 使用新文件
python3 batch_crawl.py --file flask_urls.txt --headless
```

### Q: 结果文件在哪里?

A: 默认保存在 `output/results/flask_prices_YYYYMMDD_HHMMSS.csv`

### Q: 如何合并多个结果文件?

A: 可以使用pandas或Excel手动合并,或编写简单的合并脚本。

## 📞 技术支持

如有问题,请查看:
- [原理说明文档](./FLASK_CRAWLER_PRINCIPLES.md)
- [主README](./README.md)
- 日志文件: `output/logs/`

