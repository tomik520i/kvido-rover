"""Read-only recording of browser-driven simulator tasks; never renews control."""
import argparse
import json
from pathlib import Path
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]


def save_latest(folder,sample):
    temporary=folder/'latest.tmp'
    temporary.write_text(json.dumps(sample,ensure_ascii=False,indent=2),encoding='utf-8')
    # Windows readers/antivirus can briefly hold the destination open. Keep
    # recording the append-only timeline even if this convenience view is busy.
    for attempt in range(5):
        try:
            temporary.replace(folder/'latest.json')
            return True
        except PermissionError:
            time.sleep(.02)
    return False


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--port',type=int,default=28742)
    p.add_argument('--seconds',type=int,default=180)
    p.add_argument('--name',default='simulator-live')
    args=p.parse_args()
    if not args.name.replace('-','').replace('_','').isalnum():p.error('Invalid report name')
    folder=ROOT/'reports'/args.name
    folder.mkdir(parents=True,exist_ok=True)
    samples=[];seen=set();marker=None
    with httpx.Client(base_url=f'http://127.0.0.1:{args.port}',timeout=15,trust_env=False) as c:
        c.post('/api/login',json={'key':(ROOT/'data/simulator/access.key').read_text().strip()}).raise_for_status()
        started=time.monotonic()
        while time.monotonic()-started<args.seconds:
            r=c.get('/api/status');r.raise_for_status();s=r.json()
            r=c.get('/api/simulator');r.raise_for_status();world=r.json()['state']
            t=s['task'];sample={'at':time.time(),'elapsed_s':round(time.monotonic()-started,2),'task':t,
                               'world':world,'armed':s['armed'],'telemetry':s['telemetry']}
            samples.append(sample)
            save_latest(folder,sample)
            with (folder/'timeline.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(sample,ensure_ascii=False)+'\n')
            result=t.get('last_result') or {};version=result.get('id')
            if version and version not in seen:
                seen.add(version)
                for role in ('before','after'):
                    image=c.get('/api/task/frames/'+role,params={'v':version})
                    if image.is_success:(folder/f"step-{result['step']}-{role}.jpg").write_bytes(image.content)
            current=(t['status'],t['phase'],t['current_step'])
            if current!=marker:
                print(json.dumps({'task':current,'action':result.get('action'),
                    'distance_to_door_m':round(world['door_distance'],3),'goal_reached':world['goal_reached'],
                    'collisions':world['collisions'],'crossed_threshold':world['crossed_threshold']}),flush=True)
                marker=current
            if t['status'] not in ('running','reconnecting'):break
            time.sleep(.5)
    (folder/'report.json').write_text(json.dumps({'observer_only':True,'samples':samples},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
