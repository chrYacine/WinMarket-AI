"""Local graceful shutdown via an operator-owned file, no HTTP admin route."""
import argparse
import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

async def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--synthetic-recipe',action='store_true')
    args = parser.parse_args()
    from src.core.environment_guard import validate_path, validate_environment
    envfile = validate_path(args.env_file)
    from dotenv import dotenv_values
    values = dotenv_values(envfile,interpolate=False)
    validate_environment(values,ROOT)
    if values.get('BASE_URL') != f'http://127.0.0.1:{args.port}':
        raise ValueError('Application endpoint differs from the selected environment')
    os.environ['WM_ENV_FILE'] = str(envfile)
    if args.synthetic_recipe:
        from tests.recipe_llm import install
        install()
        print('SYNTHETIC LOT56: fact-search LLM adapter simulated; real embeddings and scoring.',flush=True)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config('main:app',host='127.0.0.1',port=args.port,log_level='info'))
    async def shutdown_monitor():
        while not server.should_exit:
            if (envfile.parent/'stop.request').exists():
                server.should_exit=True
            await asyncio.sleep(0.5)
    monitor = asyncio.create_task(shutdown_monitor())
    try:
        await server.serve()
    finally:
        monitor.cancel()

if __name__ == '__main__':
    asyncio.run(run())
