"""Reproducible artifact identity; no application import, provider call or private data."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--commit')
    args=parser.parse_args()
    commit=args.commit
    if not commit:
        try:commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        except (OSError,subprocess.CalledProcessError):commit='unpublished-working-tree'
    paths=[ROOT/'requirements.lock.txt',ROOT/'src/core/models.py',ROOT/'src/web/database/models.py',ROOT/'qa/evaluation_corpus.json',ROOT/'src/rag/chunking.py',ROOT/'src/rag/hybrid_search.py']
    paths+=sorted((ROOT/'prompts').glob('*'))
    manifest={'commit':commit,'migration':'0017','model':json.loads((ROOT/'src/rag/model_artifact.json').read_text(encoding='utf-8')),
        'dimension':384,'token_limit':128,'content_tokens':126,'overlap_tokens':32,'rrf_k':60,
        'hashes':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()},
        'llm_evaluation':'synthetic adapter; no external provider qualification'}
    args.output.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('Evaluation artifact manifest written; commit:',commit)


if __name__=='__main__':
    main()
