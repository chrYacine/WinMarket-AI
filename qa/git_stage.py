"""Stage only reviewed publication files, preserve target history, scan all reachable blobs."""
import json
from pathlib import Path
import sys
from dulwich import porcelain
from dulwich.repo import Repo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.check_publication import publication_files,scan_blob
from src.core.environment_guard import validate_path
validate_path(ROOT)
repo=Repo(str(ROOT))
config=repo.get_config()
assert config.get((b'remote',b'origin'),b'url')==b'https://github.com/chrYacine/WinMarket-AI.git'
errors=[];seen=set()
for entry in repo.get_walker(include=list(repo.refs.as_dict().values())):
    for item in repo.object_store.iter_tree_contents(entry.commit.tree):
        if item.sha in seen:continue
        seen.add(item.sha)
        obj=repo[item.sha]
        if obj.type_name==b'blob':errors.extend(scan_blob(item.path.decode(),obj.data))
if errors:raise SystemExit('\n'.join(errors))
head=repo.head()
if b'refs/heads/dev' not in repo.refs:
    repo.refs[b'refs/heads/dev']=head
repo.refs.set_symbolic_ref(b'HEAD',b'refs/heads/dev')
files=publication_files()
porcelain.add(repo,paths=files)
print(json.dumps({'staged_allowlisted_files':len(files),'history_blobs_scanned':len(seen),'parent':head.decode(),'branch':'dev'}))
