#!/usr/bin/env python3
"""Isolated, read-only image evidence capture; never calls a monitor or writer."""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from utils.monitor_price_store import atomic_json, now, run_lock
from utils.monitor_rendered_browser import render_page
from scripts.firecrawl_self_hosted_monitor import SelfHostedMonitor, load_config


def capture(config, directory):
    directory.mkdir(parents=True,exist_ok=True)
    rows=[{'monitor':name,'url':url} for name,group in config['monitors'].items()
          for target in group.get('targets',[]) for url in target.get('urls',[])]
    urls=list(dict.fromkeys(r['url'] for r in rows));started=time.monotonic();started_at=now();results={}
    def one(url):
        page=directory/'pages'/uuid4().hex;page.mkdir(parents=True)
        client=SelfHostedMonitor(os.getenv('FIRECRAWL_SELF_HOSTED_API_URL','http://127.0.0.1:3002'),
            os.getenv('FIRECRAWL_SELF_HOSTED_API_KEY',''),45)
        error=None;raw={};markdown=''
        try:
            raw=client.scrape_document(url)
            markdown=str(raw.get('data',{}).get('markdown') or '')
        except Exception as exc:error=type(exc).__name__;raw=getattr(exc,'raw_response',{})
        artifact={'url':url,'captured_at':now(),'markdown':markdown,'raw_response':raw,'firecrawl_error':error}
        atomic_json(page/'artifact.json',artifact)
        try:
            browser=render_page(url,page)
            result={'url':url,'captured_at':browser['captured_at'],'browser_path':str(page/'browser.json'),
                'artifact_path':str(page/'artifact.json'),'accessible':browser['accessible'],'complete':browser['complete'],
                'http_status':browser['http_status'],'firecrawl_error':error,'browser_fallback':bool(error and browser['accessible']),
                'image_required':browser['image_review_required'],'tiles_a':len(browser['image_tiles']['a']),
                'tiles_b':len(browser['image_tiles']['b']),'failures':browser['failures']}
        except Exception as exc:
            result={'url':url,'captured_at':now(),'artifact_path':str(page/'artifact.json'),
                'accessible':False,'complete':False,'error':type(exc).__name__,'firecrawl_error':error}
        atomic_json(page/'result.json',result);return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(one,url) for url in urls]):
            result=future.result();results[result['url']]=result
            atomic_json(directory/'capture_progress.json',{'completed':len(results),'total_unique':len(urls),'last':result,'updated_at':now()})
            print(json.dumps({'captured':len(results),'total':len(urls),'url':result['url'],'accessible':result['accessible']}),flush=True)
    coverage=[{**row,**results[row['url']]} for row in rows]
    receipt={'started_at':started_at,'finished_at':now(),'configured_pages':len(rows),'unique_urls':len(urls),'seconds':time.monotonic()-started,
             'coverage':coverage,'database_writes':0,'production_baseline_changes':0}
    atomic_json(directory/'coverage.json',receipt)
    fields=sorted(set(k for row in coverage for k in row))
    with (directory/'page_coverage.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(coverage)
    return receipt


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=ROOT/'config/firecrawl_monitor_migration.json')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    with run_lock(ROOT/'state'):
        capture(load_config(a.config),a.output)

if __name__=='__main__':main()
