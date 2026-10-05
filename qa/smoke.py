"""HTTP smoke against an explicitly disposable database; no provider credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['WM_DB_TEST_MODE']='1'
from src.core.db_target import assert_disposable_test_target
assert_disposable_test_target(os.environ['DATABASE_URL'])
process=subprocess.Popen([sys.executable,'-m','uvicorn','main:app','--host','127.0.0.1','--port','8056'],cwd=ROOT)
try:
    for attempt in range(60):
        try:
            with urlopen('http://127.0.0.1:8056/readyz',timeout=2) as response:
                assert response.status==200
            break
        except OSError:
            if process.poll() is not None:
                raise RuntimeError('Application exited')
            time.sleep(0.5)
    else:
        raise TimeoutError('Readiness timeout')
    with urlopen('http://127.0.0.1:8056/healthz') as response:
        print(json.dumps({'healthz':json.load(response),'readyz':200}))
finally:
    process.terminate()
    process.wait(timeout=35)
