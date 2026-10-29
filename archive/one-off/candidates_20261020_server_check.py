import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.deploy_monitor_price_sync import remote_python

code = r'''import json,subprocess,os
from pathlib import Path
from datetime import datetime
base=Path('/home/loong/costfinder_monitor')
print('time',datetime.now().astimezone().isoformat())
for name in ['retry.log','monitor.log','crawl_due.log']:
 p=base/'logs'/name
 if p.exists():
  print(name,p.stat().st_mtime)
  print('\n'.join(p.read_text(errors='replace').splitlines()[-8:]))
processes=subprocess.check_output(['ps','-eo','pid,etimes,pcpu,pmem,args'],text=True)
print('\n'.join(line for line in processes.splitlines() if 'costfinder_monitor/.venv/bin/python' in line))
recent=sorted((p for p in (base/'state').rglob('*.json') if p.is_file()),key=lambda p:p.stat().st_mtime,reverse=True)[:5]
print('recent_state_updates',json.dumps([{'path':str(p.relative_to(base)), 'updated_at':datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat()} for p in recent]))
schedule=subprocess.check_output(['schtasks.exe','/query','/fo','CSV'],text=True,errors='replace')
print('task_schedule','\n'.join(line for line in schedule.splitlines() if 'CostFinder' in line))
'''
result = remote_python(code)
(ROOT / 'one-off/outputs/candidates_20261020/server_activity.txt').write_text(result)
print(result)
