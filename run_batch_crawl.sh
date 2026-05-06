#!/bin/bash
# 批量爬取脚本 - 从input_websites.txt读取URL并批量爬取

# 检查Python版本
if command -v python3 &> /dev/null; then
    PYTHON_CMD=python3
elif command -v python &> /dev/null; then
    PYTHON_CMD=python
else
    echo "错误: 未找到Python,请先安装Python 3.9+"
    exit 1
fi

# 默认参数
HEADLESS=""
MAX_URLS=""
DELAY=2.0
START_FROM=1

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --headless)
            HEADLESS="--headless"
            shift
            ;;
        --max-urls)
            MAX_URLS="--max-urls $2"
            shift 2
            ;;
        --delay)
            DELAY="$2"
            shift 2
            ;;
        --start-from)
            START_FROM="$2"
            shift 2
            ;;
        --help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --headless        使用无头模式(不显示浏览器)"
            echo "  --max-urls N      最多处理N个URL(用于测试)"
            echo "  --delay SECONDS   每个URL之间的延迟(秒),默认2秒"
            echo "  --start-from N    从第N个URL开始处理"
            echo "  --help            显示此帮助信息"
            echo ""
            echo "示例:"
            echo "  $0                                    # 处理所有URL(显示浏览器)"
            echo "  $0 --headless                        # 无头模式处理所有URL"
            echo "  $0 --max-urls 10                      # 只处理前10个URL"
            echo "  $0 --headless --delay 3 --max-urls 5  # 无头模式,延迟3秒,处理5个"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 检查input_websites.txt是否存在
if [ ! -f "input_websites.txt" ]; then
    echo "错误: 未找到 input_websites.txt 文件"
    exit 1
fi

# 运行批量爬取
echo "开始批量爬取..."
echo "参数: headless=$([ -n "$HEADLESS" ] && echo "是" || echo "否"), delay=${DELAY}s, start_from=${START_FROM}${MAX_URLS:+", max_urls=${MAX_URLS#--max-urls }"}"
echo ""

$PYTHON_CMD batch_crawl.py \
    --file input_websites.txt \
    --delay "$DELAY" \
    --start-from "$START_FROM" \
    $HEADLESS \
    $MAX_URLS

echo ""
echo "批量爬取完成! 结果保存在 output/results/ 目录"

