"""Explicit public embedding download/copy, immutable revision and file hashes."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.core.environment_guard import validate_path
from src.rag.model_artifact import MANIFEST, verified_artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--from-snapshot',type=Path)
    args = parser.parse_args()
    cache = validate_path(args.cache)
    source = validate_path(args.from_snapshot) if args.from_snapshot else None
    target = cache/MANIFEST['revision']
    target.mkdir(parents=True,exist_ok=True)
    for name in MANIFEST['files']:
        if source:
            file = source/name
        else:
            from huggingface_hub import hf_hub_download
            file = hf_hub_download(repo_id=MANIFEST['repository'],filename=name,
                                   revision=MANIFEST['revision'],cache_dir=str(cache/'download'))
        shutil.copyfile(file,target/name)
    verified_artifact(cache)
    print('Pinned artifact verified:',MANIFEST['revision'])


if __name__ == '__main__':
    main()
