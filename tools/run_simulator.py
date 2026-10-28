"""Local-only simulator using the real task runner with an isolated data store."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=28742)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Port must be 1024–65535')
    directory=ROOT/'data/simulator'
    directory.mkdir(parents=True,exist_ok=True)
    key=directory/'openai.key'
    if not key.exists():
        for source in (ROOT/'openai_key.txt',ROOT.parent/'openai_key.txt',ROOT/'data/openai.key'):
            if source.is_file():
                shutil.copyfile(source,key);key.chmod(0o600);break
    config=directory/'settings.json'
    if not config.exists():
        config.write_text(json.dumps({'simulation':True,'transport':'lan','device_url':'','device_key':'',
            'ai_provider':'openai' if key.exists() else 'gemma','openai_model':'gpt-6-luna',
            'navigation_policy':'sensor','thinking_enabled':False,'floor_calibrated':True,
            'floor_angle':55,'camera_rotate_180':False,'name':'Kvído · simulátor'},ensure_ascii=False,indent=2),encoding='utf-8')
    os.environ['ROVER_DATA']=str(directory)
    os.environ['ROVER_SIMULATION_ONLY']='1'
    sys.path.insert(0,str(ROOT))
    import uvicorn
    print(f'Simulator: http://127.0.0.1:{args.port}/',flush=True)
    print('Access key file: data/simulator/access.key (created on startup).',flush=True)
    uvicorn.run('server.app:app',host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
