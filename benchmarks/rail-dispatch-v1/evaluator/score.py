#!/usr/bin/env python3
import argparse,json,os,re,subprocess,sys

def main():
 p=argparse.ArgumentParser();p.add_argument('workspace');p.add_argument('--json',action='store_true');p.add_argument('--timeout',type=float,default=10.0);a=p.parse_args(); root=os.path.abspath(os.path.join(os.path.dirname(__file__),'..')); testfile=os.path.join(root,'evaluator','tests','test_hidden.py'); names=re.findall(r'^    def (test_[A-Za-z0-9_]+)\(',open(testfile,encoding='utf8').read(),re.M); pts=100/len(names); env=os.environ.copy();env['PYTHONPATH']=os.path.abspath(a.workspace)+os.pathsep+env.get('PYTHONPATH',''); out=[]
 for i,t in enumerate(names,1):
  
  try:
   r=subprocess.run([sys.executable,'-m','unittest','test_hidden.HiddenSemantics.'+t],cwd=os.path.dirname(testfile),env=env,text=True,capture_output=True,timeout=a.timeout); passed=r.returncode==0; detail=(r.stderr or r.stdout)[-250:]; status='passed' if passed else 'failed'
  except subprocess.TimeoutExpired as exc:
   passed=False; detail='timeout after '+str(a.timeout)+'s'; status='timeout'
  out.append({'item':f'case_{i:02d}','test':t,'points':pts if passed else 0,'max_points':pts,'passed':passed,'status':status,'detail':detail})
 report={'score':sum(x['points'] for x in out),'max_score':100,'case_count':len(out),'items':out};print(json.dumps(report,sort_keys=True) if a.json else json.dumps(report,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
