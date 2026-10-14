#!/usr/bin/env python3
"""Export server evidence, frozen plans, protections, and receipts over SSH."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.deploy_monitor_price_sync import remote_python
from utils.monitor_price_store import atomic_bytes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",default="firecrawl")
    parser.add_argument("--destination",type=Path,default=ROOT / "output/monitor_price_sync/server_state")
    args=parser.parse_args()
    code='''import io,tarfile,json,sys,os
from pathlib import Path
sys.path.insert(0,"/home/loong/costfinder_monitor")
from utils.monitor_price_store import run_lock
base=Path("/home/loong/costfinder_monitor/state")
export=Path("/mnt/c/Users/loong/costfinder_exports/monitor_price_sync_state.tar.gz")
export.parent.mkdir(parents=True,exist_ok=True)
temporary=export.with_suffix(".pending")
with run_lock(base):
    with tarfile.open(temporary,mode="w:gz",compresslevel=1) as archive:
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.name.startswith(".") or path.suffix==".lock": continue
            content=path.read_bytes()
            member=tarfile.TarInfo(str(path.relative_to(base))); member.size=len(content); member.mode=0o600
            archive.addfile(member,io.BytesIO(content))
    os.replace(temporary,export)
parts=[]
with export.open("rb") as source:
    while content:=source.read(8*1024*1024):
        part=export.with_name(export.name+".part_"+str(len(parts)).zfill(4))
        part.write_bytes(content)
        parts.append({"archive":"C:/Users/loong/costfinder_exports/"+part.name,"size_bytes":len(content)})
print(json.dumps({"parts":parts,"size_bytes":export.stat().st_size}))
'''
    metadata=json.loads(remote_python(code,timeout=120,host=args.host))
    download=args.destination.parent / "server_state.tar.gz"
    download.parent.mkdir(parents=True,exist_ok=True)
    temporary=download.with_suffix(".pending")
    parts_dir=args.destination.parent / "export_parts"
    parts_dir.mkdir(exist_ok=True)
    def fetch(part):
        path=parts_dir / Path(part["archive"]).name
        subprocess.run(["scp","-q","-o","BatchMode=yes","-o","ConnectTimeout=10",
                        args.host+":"+part["archive"],str(path)],check=True,timeout=1800)
        if path.stat().st_size != part["size_bytes"]:
            raise RuntimeError("incomplete_export_archive")
        return path
    with ThreadPoolExecutor(max_workers=8) as executor:
        parts=list(executor.map(fetch,metadata["parts"]))
    with temporary.open("wb") as target:
        for part in parts:
            with part.open("rb") as source:
                while content:=source.read(1024*1024): target.write(content)
    if temporary.stat().st_size != metadata["size_bytes"]: raise RuntimeError("incomplete_export_archive")
    temporary.replace(download)
    archive=tarfile.open(download,mode="r:gz")
    count=0
    for member in archive.getmembers():
        relative=Path(member.name)
        if not member.isfile() or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("invalid_export_archive")
        atomic_bytes(args.destination / relative,archive.extractfile(member).read())
        count+=1
    print(json.dumps({"exported_files":count,"destination":str(args.destination)}))
    for part in parts: part.unlink()


if __name__ == "__main__":
    main()
