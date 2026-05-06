"""
主程序入口
"""
import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import json

from config.settings import (
    INPUT_URLS_FILE, INPUT_SEARCH_TERMS_FILE, 
    OUTPUT_DIR, OUTPUT_FORMAT, OUTPUT_ENCODING,
    MAX_WORKERS
)
from crawler.base_crawler import WineCrawler
from utils.logger import log

def load_urls(file_path: Path) -> list[str]:
    """加载URL列表"""
    if not file_path.exists():
        log.error(f"文件不存在: {file_path}")
        return []
    
    urls = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            url = line.strip()
            if url and url.startswith('http'):
                urls.append(url)
    
    log.info(f"加载了{len(urls)}个URL")
    return urls

def load_search_terms(file_path: Path) -> pd.DataFrame:
    """加载搜索词CSV"""
    if not file_path.exists():
        log.warning(f"搜索词文件不存在: {file_path}")
        return pd.DataFrame()
    
    df = pd.read_csv(file_path, encoding='utf-8')
    log.info(f"加载了{len(df)}条搜索词")
    return df

def save_results(results: list[dict], output_formats: list[str] = None):
    """保存结果"""
    if not results:
        log.warning("没有结果可保存")
        return
    
    if output_formats is None:
        output_formats = OUTPUT_FORMAT
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 转换为DataFrame
    df = pd.DataFrame(results)
    
    # 添加时间戳
    df['crawl_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 重新排列列顺序
    columns_order = [
        'merchant', 'wine_name', 'year', 'region', 
        'price', 'stock_status', 'url', 'parse_method', 'crawl_time'
    ]
    
    # 只选择存在的列
    columns_order = [col for col in columns_order if col in df.columns]
    df = df[columns_order]
    
    # 保存为不同格式
    saved_files = []
    
    if 'csv' in output_formats:
        csv_file = OUTPUT_DIR / f"wine_prices_{timestamp}.csv"
        df.to_csv(csv_file, index=False, encoding=OUTPUT_ENCODING)
        saved_files.append(csv_file)
        log.info(f"CSV文件已保存: {csv_file}")
    
    if 'json' in output_formats:
        json_file = OUTPUT_DIR / f"wine_prices_{timestamp}.json"
        df.to_json(json_file, orient='records', force_ascii=False, indent=2)
        saved_files.append(json_file)
        log.info(f"JSON文件已保存: {json_file}")
    
    if 'excel' in output_formats:
        excel_file = OUTPUT_DIR / f"wine_prices_{timestamp}.xlsx"
        df.to_excel(excel_file, index=False, engine='openpyxl')
        saved_files.append(excel_file)
        log.info(f"Excel文件已保存: {excel_file}")
    
    # 打印统计信息
    print_statistics(df)
    
    return saved_files

def print_statistics(df: pd.DataFrame):
    """打印统计信息"""
    log.info("=" * 60)
    log.info("爬取统计")
    log.info("=" * 60)
    
    total = len(df)
    log.info(f"总URL数: {total}")
    
    if 'price' in df.columns:
        price_extracted = df['price'].notna().sum()
        log.info(f"成功提取价格: {price_extracted} ({price_extracted / total * 100:.1f}%)")
        
        if price_extracted > 0:
            log.info(f"价格范围: ${df['price'].min():.2f} - ${df['price'].max():.2f}")
            log.info(f"平均价格: ${df['price'].mean():.2f}")
    
    if 'parse_method' in df.columns:
        log.info("\n解析方法分布:")
        method_counts = df['parse_method'].value_counts()
        for method, count in method_counts.items():
            log.info(f"  {method}: {count} ({count / total * 100:.1f}%)")
    
    if 'stock_status' in df.columns:
        log.info("\n库存状态分布:")
        stock_counts = df['stock_status'].value_counts()
        for status, count in stock_counts.items():
            log.info(f"  {status}: {count}")
    
    log.info("=" * 60)

async def main_async(args):
    """异步主函数"""
    log.info("=" * 60)
    log.info("CostFinder - 葡萄酒价格爬虫")
    log.info("=" * 60)
    
    # 1. 加载URL
    urls = load_urls(INPUT_URLS_FILE)
    
    if not urls:
        log.error("没有可爬取的URL")
        return
    
    # 如果指定了URL数量限制
    if args.limit:
        urls = urls[:args.limit]
        log.info(f"限制爬取数量: {args.limit}")
    
    # 2. 创建爬虫
    async with WineCrawler() as crawler:
        # 3. 开始爬取
        results = await crawler.crawl_urls(urls, max_workers=args.workers)
        
        # 4. 保存结果
        if results:
            save_results(results, args.format)
        else:
            log.warning("没有爬取到任何结果")
    
    log.info("程序执行完成")

def main():
    """主函数"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="葡萄酒价格爬虫")
    
    parser.add_argument(
        '--workers',
        type=int,
        default=MAX_WORKERS,
        help=f'并发数量 (默认: {MAX_WORKERS})'
    )
    
    parser.add_argument(
        '--format',
        nargs='+',
        default=['csv', 'json'],
        choices=['csv', 'json', 'excel'],
        help='输出格式 (默认: csv json)'
    )
    
    parser.add_argument(
        '--limit',
        type=int,
        help='限制爬取数量(用于测试)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='调试模式(显示浏览器)'
    )
    
    parser.add_argument(
        '--ocr-only',
        action='store_true',
        help='仅使用OCR模式'
    )
    
    args = parser.parse_args()
    
    # 如果是调试模式,修改配置
    if args.debug:
        from config import settings
        settings.HEADLESS = False
        log.info("调试模式: 浏览器可见")
    
    # 如果是OCR-only模式
    if args.ocr_only:
        from config import settings
        settings.PARSE_STRATEGY["dom_first"] = False
        settings.PARSE_STRATEGY["ocr_fallback"] = True
        log.info("OCR模式: 仅使用截图+OCR")
    
    # 运行异步主函数
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log.warning("程序被用户中断")
        sys.exit(0)
    except Exception as e:
        log.error(f"程序异常退出: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
