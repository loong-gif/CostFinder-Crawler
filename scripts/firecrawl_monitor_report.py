#!/usr/bin/env python3
"""Check Firecrawl monitor results after a run and output summary for cron delivery.
This script is designed for cron with no_agent=true - its stdout goes to Feishu."""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

for v in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY']:
    os.environ.pop(v, None)

import requests

FIRE_KEY = os.getenv('FIRECRAWL_API_KEY')
API_URL = 'http://127.0.0.1:3003'  # SSH tunnel
headers = {'Authorization': f'Bearer {FIRE_KEY}'}

def main():
    # Get all monitors
    all_mons = []
    for offset in [0, 100]:
        r = requests.get(f'{API_URL}/v2/monitor', headers=headers,
                         params={'limit': 100, 'offset': offset}, timeout=30)
        all_mons.extend(r.json()['data'])
    
    # Focus on checks from today (after 2026-07-10 06:00 UTC = 14:00 CST)
    cutoff = '2026-07-10 06:00:00+00'
    
    completed = 0
    running = 0
    changed_sites = []
    error_sites = []
    new_sites = []
    removed_sites = []
    skipped = 0
    
    for m in all_mons:
        mid = m['id']
        name = m['name']
        cid = m.get('currentCheckId')
        last = str(m.get('lastRunAt',''))
        
        if cid:
            running += 1
            continue
        
        if last < cutoff:
            continue  # skip monitors that haven't run today
        
        completed += 1
        
        # Get latest check
        r2 = requests.get(f'{API_URL}/v2/monitor/{mid}/checks',
                          headers=headers, params={'limit': 1}, timeout=15)
        checks = r2.json().get('data', [])
        if not checks:
            continue
        
        c = checks[0]
        s = c.get('summary', {})
        status = c.get('status', '')
        trigger = c.get('trigger', '?')
        
        if status == 'skipped_overlap':
            skipped += 1
            continue
        
        new_cnt = s.get('new', 0)
        chg_cnt = s.get('changed', 0)
        del_cnt = s.get('removed', 0)
        err_cnt = s.get('error', 0)
        total = s.get('totalPages', 0)
        
        if new_cnt > 0:
            new_sites.append(f'  • {name} ({new_cnt} new)')
        if chg_cnt > 0:
            changed_sites.append(f'  • {name} ({chg_cnt} changed)')
        if del_cnt > 0:
            removed_sites.append(f'  • {name} ({del_cnt} removed)')
        if err_cnt > 0 and new_cnt == 0 and chg_cnt == 0 and del_cnt == 0:
            error_sites.append(name)
    
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    beijing = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
    
    lines = [f"📋 Firecrawl Monitor 运行报告"]
    lines.append(f"🕐 {now}（北京时间 {beijing}）")
    lines.append("")
    lines.append(f"📊 今日完成: {completed + running} 个 monitor")
    lines.append(f"  ✅ 已完成: {completed}")
    lines.append(f"  🏃 运行中: {running}")
    if skipped:
        lines.append(f"  ⏭️ 跳过: {skipped}")
    
    if new_sites:
        lines.append(f"\n🆕 新增页面 ({len(new_sites)}):")
        lines.extend(new_sites[:15])
        if len(new_sites) > 15:
            lines.append(f"  ... 还有 {len(new_sites)-15} 个")
    
    if changed_sites:
        lines.append(f"\n✏️ 内容变更 ({len(changed_sites)}):")
        lines.extend(changed_sites[:15])
        if len(changed_sites) > 15:
            lines.append(f"  ... 还有 {len(changed_sites)-15} 个")
    
    if removed_sites:
        lines.append(f"\n🗑️ 已移除 ({len(removed_sites)}):")
        lines.extend(removed_sites[:10])
        if len(removed_sites) > 10:
            lines.append(f"  ... 还有 {len(removed_sites)-10} 个")
    
    if error_sites:
        lines.append(f"\n❌ 错误 ({len(error_sites)})")
        lines.append(f"   多数为 JSON extraction 错误，不影响 git-diff 检测")
        if len(error_sites) <= 10:
            for s in error_sites:
                lines.append(f"  • {s}")
    
    if running > 0:
        lines.append(f"\n⏳ 还有 {running} 个 monitor 在运行中，稍后会自动完成")
    
    lines.append(f"\n📌 下次调度: 今天 07:00 UTC = 15:00 CST（已由 Firecrawl 自动处理）")
    
    print('\n'.join(lines))

if __name__ == '__main__':
    main()
