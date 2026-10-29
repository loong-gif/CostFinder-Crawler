import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.deploy_monitor_price_sync import remote_python

REMOTE = "/home/loong/costfinder_monitor/one-off/candidates_20261020_api.py"


def deploy():
    source = (ROOT / "one-off/candidates_20261020_api.py").read_bytes()
    code = f'''import sys
from pathlib import Path
target=Path({REMOTE!r})
source=sys.stdin.buffer.read()
assert not target.exists() or target.read_bytes()==source, 'existing_batch_worker_differs'
target.parent.mkdir(parents=True,exist_ok=True)
if not target.exists(): target.write_bytes(source)
print('batch_worker_ready')
'''
    return remote_python(code, stdin=source)


def request(path, body):
    code = f'''import csv,io,json,sys,subprocess,fcntl
from datetime import datetime
from pathlib import Path
base=Path('/home/loong/costfinder_monitor')
schedule=subprocess.check_output(['schtasks.exe','/query','/fo','CSV'],text=True,errors='replace')
now=datetime.now()
for row in csv.reader(io.StringIO(schedule)):
 if len(row)!=3 or not row[0].startswith('\\\\CostFinder'): continue
 if row[1] in ('N/A','Next Run Time'): continue
 due=datetime.strptime(row[1],'%m/%d/%Y %I:%M:%S %p')
 if 0 <= (due-now).total_seconds() < 180:
  print(json.dumps({{'batch_status':'scheduled_window','task':row[0]}})); sys.exit(0)
lock=open(base/'state/.monitor.lock','a+')
try:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
 print(json.dumps({{'batch_status':'monitor_busy'}})); sys.exit(0)
result=subprocess.run([str(base/'.venv/bin/python'),{REMOTE!r}],input=sys.stdin.buffer.read(),capture_output=True,cwd=base)
if result.returncode:
 print(json.dumps({{'batch_status':'request_failed','error':result.stderr.decode(errors='replace').splitlines()[-1][:200]}})); sys.exit(0)
print(result.stdout.decode())
'''
    return json.loads(remote_python(code, stdin=json.dumps({"path": path, "body": body}).encode(), timeout=170))


if __name__ == "__main__":
    print(deploy())
