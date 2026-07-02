#!/usr/bin/env python3
"""
使用 Lightpanda 爬取 needs_ocr=true 的页面，提取文本和图片内容，分块处理后写回数据库
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.page_content_processor import process_page_content

# ─── Supabase 客户端 ────────────────────────────────────────────

class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        })

    def fetch_rows(self, table: str, filters: str, select: str = "*") -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.base_url}/{table}",
            params={"select": select, **dict(kv.split("=") for kv in filters.split("&") if "=" in kv)},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def update_row(self, table: str, row_id: int, data: Dict[str, Any], id_col: str = "promo_website_id") -> Dict[str, Any]:
        resp = self.session.patch(
            f"{self.base_url}/{table}",
            params={id_col: f"eq.{row_id}"},
            json=data,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


# ─── Lightpanda 爬虫 ─────────────────────────────────────────────

LIGHTPANDA_BIN = os.path.expanduser("~/.cache/lightpanda-node/lightpanda")


def crawl_with_lightpanda(url: str) -> Optional[str]:
    """使用 Lightpanda fetch 获取页面 HTML"""
    try:
        result = subprocess.run(
            [LIGHTPANDA_BIN, "fetch", "--dump", "html", "--wait-ms", "5000", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        html = result.stdout.strip()
        if html and len(html) > 100 and "Navigation failed" not in html:
            return html
    except Exception as e:
        print(f"  [!] Lightpanda fetch 失败: {e}")

    # 备用: curl
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "15", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.stdout and len(result.stdout) > 100:
            return result.stdout
    except Exception as e:
        print(f"  [!] curl 失败: {e}")

    return None


def extract_image_urls(html: str) -> List[str]:
    """从 HTML 中提取促销相关图片 URL"""
    img_pattern = re.compile(r'<img[^>]*src=["\']([^"\']+)["\']', re.IGNORECASE)
    all_imgs = img_pattern.findall(html)

    # 过滤: 只保留可能包含促销信息的图片
    promo_keywords = ["promo", "special", "offer", "deal", "discount", "sale", "banner", "flyer", "copy%20of"]
    promo_imgs = []
    for img_url in all_imgs:
        lower = img_url.lower()
        if any(kw in lower for kw in promo_keywords):
            promo_imgs.append(img_url)
        # 也保留较大的 CDN 图片 (可能是促销图)
        elif "cdn" in lower and (".png" in lower or ".jpg" in lower or ".jpeg" in lower or ".webp" in lower):
            # 排除小图标
            if "icon" not in lower and "logo" not in lower and "svg" not in lower:
                promo_imgs.append(img_url)

    return list(set(promo_imgs))[:5]  # 最多5张


def download_image(url: str) -> Optional[str]:
    """下载图片到临时文件"""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        suffix = ".png" if ".png" in url.lower() else ".jpg"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"  [!] 图片下载失败: {e}")
        return None


def ocr_image_with_vision(image_path: str) -> str:
    """使用 Vision AI 提取图片中的文本 (通过 Hermes vision_analyze)"""
    # 这里我们返回空字符串，实际 OCR 由外部调用完成
    # 因为我们无法直接调用 Hermes 的 vision 工具
    return ""


# ─── 主流程 ──────────────────────────────────────────────────────

def main():
    load_dotenv(PROJECT_ROOT / ".env")

    base_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_key:
        print("❌ 缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)

    client = SupabaseRestClient(base_url, service_key)

    # 查询 needs_ocr=true 的记录
    print("🔍 查询 needs_ocr=true 的记录...")
    rows = client.fetch_rows(
        "promo_website_staging",
        "needs_ocr=eq.true",
        "promo_website_id,subpage_url,domain_name,name,page_content"
    )
    print(f"📋 找到 {len(rows)} 条记录\n")

    results = []

    for i, row in enumerate(rows, 1):
        row_id = row["promo_website_id"]
        url = row["subpage_url"]
        domain = row["domain_name"]
        name = row["name"]

        print(f"{'='*60}")
        print(f"[{i}/{len(rows)}] {name} ({domain})")
        print(f"  URL: {url}")

        # 1. 爬取页面
        html = crawl_with_lightpanda(url)
        if not html:
            print(f"  ❌ 页面获取失败，跳过")
            results.append({"id": row_id, "status": "failed", "reason": "fetch_failed"})
            continue

        print(f"  ✅ HTML 获取成功 ({len(html)} 字符)")

        # 2. 提取图片 URL
        image_urls = extract_image_urls(html)
        print(f"  🖼️  发现 {len(image_urls)} 张促销相关图片")

        # 3. 使用项目代码分块处理 HTML 文本
        processed = process_page_content(html, source_type="html")
        page_content = processed["page_content"]
        page_content_llm = processed["page_content_llm"]
        quality_flags = processed["content_quality_flags"]

        print(f"  📊 分块处理完成:")
        print(f"     - 原始段落: {len(processed['page_segments_raw'])} 个")
        print(f"     - 过滤后段落: {len(processed['page_segments_filtered'])} 个")
        print(f"     - LLM 内容: {len(page_content_llm)} 字符")
        print(f"     - 质量标志: {quality_flags}")

        # 4. 图片 URL 信息附加到内容
        if image_urls:
            img_section = "\n\n[IMAGE_URLS]\n" + "\n".join(image_urls)
            page_content = page_content + img_section

        # 5. 写回数据库
        try:
            update_data = {
                "page_content": page_content,
                "needs_ocr": False,
                "processed_status": True,
            }
            client.update_row("promo_website_staging", row_id, update_data)
            print(f"  ✅ 数据库更新成功")
            results.append({
                "id": row_id,
                "status": "success",
                "content_length": len(page_content),
                "segments": len(processed["page_segments_filtered"]),
                "image_count": len(image_urls),
            })
        except Exception as e:
            print(f"  ❌ 数据库更新失败: {e}")
            results.append({"id": row_id, "status": "db_error", "reason": str(e)})

        print()

    # 汇总
    print("=" * 60)
    print("📊 汇总:")
    success = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] != "success")
    print(f"  ✅ 成功: {success}")
    print(f"  ❌ 失败: {failed}")

    # 保存结果到 JSON
    output_path = PROJECT_ROOT / "output" / "lightpanda_crawl_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n📁 结果已保存: {output_path}")


if __name__ == "__main__":
    main()
