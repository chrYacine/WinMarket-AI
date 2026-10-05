"""Explicit publication allowlist and blocking secret/private-artifact scan."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=('src','tests','scripts','qa','templates','static','prompts','migrations','.github','docs/api')
ROOT_FILES=('main.py','README.md','LICENSE','alembic.ini','pytest.ini','requirements.txt','requirements-test.txt',
            'requirements.lock.txt','.env.example','.gitignore','.flake8','docker-compose.yml','render.yaml')
DOC_FILES=('docs/REPRISE_PROJET.md','docs/CONTRIBUTION.md','docs/ARCHITECTURE.md','docs/EVALUATION.md','docs/DEPLOY_RENDER.md')
PATTERNS=[re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          re.compile(rb'(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}'),
          re.compile(rb'sk-ant-api[0-9]*-[A-Za-z0-9_-]{25,}'),
          re.compile(rb'AKIA[0-9A-Z]{16}')]


def publication_files():
    paths=set()
    for directory in DIRECTORIES:
        for path in (ROOT/directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.pyo'):
                paths.add(path.relative_to(ROOT).as_posix())
    for relative in (*ROOT_FILES,*DOC_FILES):
        if (ROOT/relative).is_file():
            paths.add(relative)
    return sorted(paths)


def scan_blob(name, content):
    errors=[]
    suffix=Path(name).suffix.lower()
    if suffix in ('.db','.sqlite','.sqlite3','.dump','.onnx','.token') or Path(name).name=='.env':
        errors.append('private artifact extension')
    if any(pattern.search(content) for pattern in PATTERNS):
        errors.append('credential/private-key pattern')
    if (b'adrien.'+b'khalar@'+b'bizime.com') in content or (b'C:/Users/'+b'adrie/') in content or (b'C:'+bytes([92])+b'Users'+bytes([92])+b'adrie'+bytes([92])) in content:
        errors.append('personal identity/path from source')
    return [name+': '+error for error in errors]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--git-history',action='store_true')
    args=parser.parse_args()
    errors=[];manifest={}
    for relative in publication_files():
        content=(ROOT/relative).read_bytes()
        errors.extend(scan_blob(relative,content))
        manifest[relative]=hashlib.sha256(content).hexdigest()
    if args.git_history:
        objects=subprocess.check_output(['git','rev-list','--objects','--all'],cwd=ROOT,text=True)
        for line in objects.splitlines():
            sha,_,name=line.partition(' ')
            if name and subprocess.check_output(['git','cat-file','-t',sha],cwd=ROOT,text=True).strip()=='blob':
                errors.extend(scan_blob(name,subprocess.check_output(['git','cat-file','blob',sha],cwd=ROOT)))
    if errors:
        raise SystemExit('\n'.join(errors))
    if args.manifest:
        args.manifest.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'scan':'passed','files':len(manifest),'history_scanned':args.git_history,
                      'scope':'allowlisted code/docs, known credential formats and prohibited artifacts; manual review still required'}))


if __name__=='__main__':
    main()
