import subprocess,os,time,sys,urllib.request
from pathlib import Path
repo=str(Path(__file__).resolve().parents[2])
venv=sys.executable
env={**os.environ,'NO_PROXY':'127.0.0.1,localhost','PYTHONPATH':repo,'UI_TEST_BASE':'http://127.0.0.1:8792'}
proc=subprocess.Popen([venv,'-m','tests.e2e.server','--port','8792'],cwd=repo,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 for i in range(50):
  try:urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8792/api/health',timeout=1);break
  except Exception:time.sleep(.1)
 sys.exit(subprocess.run(['node',str(Path(repo)/'tests/ui/approved_ui.mjs')],cwd=repo,env=env).returncode)
finally:proc.terminate();proc.wait()
