"""Run tests with a fresh explicitly allowlisted scratch directory."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scratch-parent', type=Path, required=True)
    parser.add_argument('--embedding-cache',type=Path)
    args, rest = parser.parse_known_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.core.environment_guard import validate_path
    parent = validate_path(args.scratch_parent)
    parent.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix='t', dir=parent)
    env = dict(os.environ, WM_DB_TEST_MODE='1', WM_TEST_DB_EXTRA_ROOTS=scratch)
    if args.embedding_cache:
        env['EMBEDDING_CACHE_DIR']=str(validate_path(args.embedding_cache))
    for key in ('WM_ENV_FILE','DATABASE_URL','ANTHROPIC_API_KEY','OPENAI_API_KEY','MISTRAL_API_KEY','PAPPERS_API_TOKEN'):
        env.pop(key,None)
    print('Fresh disposable test directory:',scratch,flush=True)
    raise SystemExit(subprocess.call([sys.executable,'-m','pytest','--basetemp='+scratch,*rest],env=env))


if __name__ == '__main__':
    main()
