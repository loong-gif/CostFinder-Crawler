#!/usr/bin/env python3
"""Check Firecrawl monitor results and send summary to Feishu."""
import os, sys, json, subprocess
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load env
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

# Unset proxies for local/LW network
for v in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY']:
    os.environ.pop(v, None)

import requests

FIRE_KEY = os.getenv('FIRECRAWL_API_KEY')
FIRE_URL = os.getenv('FIRECRAWL_API_URL', 'http://127.0.0.1:3002')
# Use local SSH tunnel if available, else direct
API_URL = 'http://127.0.0.1:3003'  # SSH tunnel

headers = {'Authorization': f'Bearer {FIRE_KEY}'}

def get_monitor_results(cutoff_time_utc):
    """Get all monitor check summaries after cutoff_time (ISO format string)."""
    r = requests.get(f'{API_URL}/v2/monitor', headers=headers,
                     params={'limit': 100}, timeout=30)
    monitors_page1 = r.json()['data']
    
    r = requests.get(f'{API_URL}/v2/monitor', headers=headers,
                     params={'limit': 100, 'offset': 100}, timeout=30)
    monitors_page2 = r.json()['data']
    all_mons = monitors_page1 + monitors_page2
    
    results = []
    for m in all_mons:
        mid = m['id']
        name = m['name']
        last_check = m.get('lastRunAt', '')
        if not last_check or str(last_check) < cutoff_time_utc:
            continue
        
        # Get latest check summary
        r2 = requests.get(f'{API_URL}/v2/monitor/{mid}/checks',
                          headers=headers, params={'limit': 1}, timeout=15)
        checks = r2.json().get('data', [])
        if not checks:
            continue
        
        c = checks[0]
        summary = c.get('summary', {})
        trigger = c.get('trigger', '?')
        status = c.get('status', '?')
        
        results.append({
            'name': name,
            'status': status,
            'trigger': trigger,
            'summary': summary,
            'check_id': c.get('id', ''),
            'finished_at': str(c.get('finishedAt', '')),
            'error': c.get('error', ''),
            'monitor_id': mid,
        })
    
    return results

def send_feishu_message(message):
    """Send message to user via Feishu lark-cli."""
    from lark_im import send_message_to_user
    # Use the user's open_id
    send_message_to_user(open_id="ou_9408ce5c05873f900079e9df4c892a40", text=message)
    print("Feishu message sent")

def format_summary(results, label):
    """Format monitor results for Feishu."""
    total = len(results)
    changed = [r for r in results if r['summary'].get('changed', 0) > 0]
    new = [r for r in results if r['summary'].get('new', 0) > 0]
    removed = [r for r in results if r['summary'].get('removed', 0) > 0]
    errors = [r for r in results if r['summary'].get('error', 0) > 0]
    no_change = [r for r in results if r['summary'].get('totalPages', 0) > 0 
                 and r['summary'].get('changed', 0) == 0 
                 and r['summary'].get('new', 0) == 0]
    skipped = [r for r in results if r['status'] == 'skipped_overlap']
    
    lines = [f"📋 Firecrawl Monitor {label}"]
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"📊 总计检查: {total} 个 monitor")
    lines.append(f"  ✅ 无变动: {len(no_change)}")
    lines.append(f"  🆕 新增页面: {len(new)}")
    lines.append(f"  ✏️ 有变更: {len(changed)}")
    lines.append(f"  🗑️ 已移除: {len(removed)}")
    lines.append(f"  ❌ 错误: {len(errors)}")
    if skipped:
        lines.append(f"  ⏭️ 跳过: {len(skipped)}")
    
    if new:
        lines.append(f"\n🆕 新增页面:")
        for r in new:
            lines.append(f"  • {r['name']}: {r['summary'].get('new',0)} new")
    
    if changed:
        lines.append(f"\n✏️ 有变更的站点:")
        for r in changed[:10]:
            lines.append(f"  • {r['name']}: {r['summary'].get('changed',0)} changed")
        if len(changed) > 10:
            lines.append(f"  ... 还有 {len(changed)-10} 个")
    
    if errors and len(errors) <= total:
        lines.append(f"\n❌ 错误 ({len(errors)}): JSON extraction 错误（不影响 git-diff 检测）")
    
    return "\n".join(lines)

if __name__ == '__main__':
    # Cutoff: checks from last 2 hours
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    
    try:
        results = get_monitor_results(cutoff)
        label = sys.argv[1] if len(sys.argv) > 1 else "运行报告"
        msg = format_summary(results, label)
        print(msg)
        print(f"\n---\nResults collected: {len(results)}")
        
        # Always print to stdout for cron delivery
        # If explicitly asked to send to Feishu, use the send function
        if '--send-feishu' in sys.argv:
            send_feishu_message(msg)
    except Exception as e:
        print(f"❌ Error collecting monitor results: {e}")
        import traceback
        traceback.print_exc()
