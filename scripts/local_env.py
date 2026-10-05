"""Independent local PostgreSQL lifecycle; launch never runs migrations."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.core.environment_guard import validate_environment, validate_path, validate_url


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def hidden():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def pg_run(meta, command, *args, check=True, capture=False):
    binary = validate_path(Path(meta['pg_bin']) / (command + ('.exe' if os.name == 'nt' else '')))
    return subprocess.run([str(binary), *map(str, args)], check=check,
                          capture_output=capture, text=True, **hidden())


def read_runtime(runtime):
    runtime = validate_path(runtime)
    meta = json.loads((runtime / 'runtime.json').read_text(encoding='utf-8'))
    if Path(meta['runtime']).resolve() != runtime:
        raise ValueError('Runtime identity mismatch')
    validate_path(meta['project'])
    validate_path(meta['pg_bin'])
    validate_path(runtime / 'pgdata')
    validate_url(f"postgresql://wm56_app@127.0.0.1:{meta['pg_port']}/{meta['database']}")
    validate_environment({'BASE_URL': f"http://127.0.0.1:{meta['app_port']}"}, ROOT)
    from dotenv import dotenv_values
    env = dict(dotenv_values(runtime / '.env', interpolate=False))
    validate_environment(env, ROOT)
    parsed = urlsplit(env['DATABASE_URL'])
    if parsed.port != meta['pg_port'] or parsed.path != '/' + meta['database']:
        raise ValueError('Configuration does not match this independent cluster')
    for name in ('DATA_DIR','OUTPUT_DIR','LOCAL_STORAGE_PATH','LOGS_DIR','EMBEDDING_CACHE_DIR'):
        path = validate_path(env[name])
        if runtime not in path.parents:
            raise ValueError('Runtime storage must remain within this runtime')
    return runtime, meta, env


def start_pg(runtime, meta):
    marker = runtime/'pgdata'/'postmaster.pid'
    identified = check_pg_identity(runtime, meta) if marker.exists() else False
    if port_open(meta['pg_port']):
        if not identified:
            raise ValueError('Unidentified PostgreSQL listener; operation refused')
        return
    pg_run(meta, 'pg_ctl', '-D', runtime/'pgdata', '-l', runtime/'postgres.log',
           '-o', f"-h 127.0.0.1 -p {meta['pg_port']}", '-w', 'start')
    if not check_pg_identity(runtime, meta):
        raise ValueError('PostgreSQL process absent after startup')


def check_pg_identity(runtime, meta):
    import psutil
    lines = (runtime/'pgdata'/'postmaster.pid').read_text().splitlines()
    data = (runtime/'pgdata').resolve()
    if Path(lines[1]).resolve() != data or int(lines[3]) != meta['pg_port']:
        raise ValueError('PostgreSQL identity mismatch; operation refused')
    try:
        process = psutil.Process(int(lines[0]))
        expected = Path(meta['pg_bin']) / ('postgres.exe' if os.name == 'nt' else 'postgres')
        command = process.cmdline()
        data_args = [command[i+1] for i, arg in enumerate(command[:-1]) if arg == '-D']
        if (Path(process.exe()).resolve() != expected.resolve()
                or not any(Path(arg).resolve() == data for arg in data_args)
                or abs(process.create_time() - int(lines[2])) > 3):
            raise ValueError('PostgreSQL process identity changed; operation refused')
    except psutil.NoSuchProcess:
        if port_open(meta['pg_port']):
            raise ValueError('Unidentified PostgreSQL listener; operation refused')
        return False  # Stale marker: never send a signal to its former PID.
    return True


def stop_app(runtime, meta):
    marker = runtime/'app.json'
    if not marker.exists():
        if port_open(meta['app_port']):
            raise ValueError('Unidentified listener; refusing to stop it')
        return
    import psutil
    record = json.loads(marker.read_text())
    if psutil.pid_exists(record['pid']):
        process = psutil.Process(record['pid'])
        command = process.cmdline()
        if str(runtime/'.env') not in command or not any('serve_local.py' in arg for arg in command):
            raise ValueError('Application process identity changed')
        (runtime/'stop.request').touch()
        process.wait(timeout=35)
    marker.unlink()
    if port_open(meta['app_port']):
        raise ValueError('Port still occupied after stop')


def initialize(args):
    runtime = validate_path(args.runtime)
    validate_path(ROOT)
    if not args.database.replace('_','').isalnum():
        raise ValueError('Invalid database identifier')
    if runtime.exists():
        raise ValueError('Initialization requires a nonexistent runtime directory')
    validate_url(f'postgresql://wm56_app@127.0.0.1:{args.pg_port}/{args.database}')
    validate_environment({'BASE_URL':f'http://127.0.0.1:{args.app_port}'},ROOT)
    if args.app_port == args.pg_port or port_open(args.pg_port) or port_open(args.app_port):
        raise ValueError('Distinct unused ports required')
    spec = importlib.util.find_spec('pgserver')
    pg_bin = validate_path(args.pg_bin or Path(spec.origin).parent/'pginstall'/'bin')
    if not (pg_bin/('initdb.exe' if os.name == 'nt' else 'initdb')).exists():
        raise ValueError('Explicit PostgreSQL binaries with pgvector required')
    meta = dict(runtime=str(runtime), project=str(ROOT), pg_bin=str(pg_bin), pg_port=args.pg_port,
                app_port=args.app_port, database=args.database, format=1)
    runtime.mkdir(parents=True)
    if os.name == 'nt':
        import getpass
        subprocess.run(['icacls',str(runtime),'/inheritance:r','/grant:r',
                        getpass.getuser()+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,capture_output=True)
    else:
        runtime.chmod(0o700)
    (runtime/'runtime.json').write_text(json.dumps(meta,indent=2))
    admin_password = secrets.token_urlsafe(40)
    app_password = secrets.token_urlsafe(40)
    pwfile = runtime/'bootstrap-password'
    pwfile.write_text(admin_password)
    try:
        pg_run(meta,'initdb','-D',runtime/'pgdata','-U','wm56_admin','--pwfile',pwfile,
               '--auth-host=scram-sha-256','--auth-local=scram-sha-256','--encoding=UTF8','--no-locale')
    finally:
        pwfile.unlink()
    (runtime/'admin.json').write_text(json.dumps({'user':'wm56_admin','password':admin_password}))
    # Clean example only, no inherited .env or application imports.
    from dotenv import dotenv_values
    values = dict(dotenv_values(ROOT/'.env.example',interpolate=False))
    values.update(DATABASE_URL=f'postgresql+pg8000://wm56_app:{app_password}@127.0.0.1:{args.pg_port}/{args.database}',
                  SESSION_SECRET=secrets.token_urlsafe(48), BASE_URL=f'http://127.0.0.1:{args.app_port}',
                  DATA_DIR=str(runtime/'data'), OUTPUT_DIR=str(runtime/'data'/'outputs'),
                  LOCAL_STORAGE_PATH=str(runtime/'data'), LOGS_DIR=str(runtime/'logs'),
                  EMBEDDING_CACHE_DIR=str(runtime/'models'), RAG_HYBRID_MODE_ENABLED='true',
                  LLM_ENABLED='false', PAPPERS_ENABLED='false', SMTP_HOST='', ADMIN_NOTIFICATION_EMAIL='')
    for key in ('ANTHROPIC_API_KEY','OPENAI_API_KEY','MISTRAL_API_KEY','PAPPERS_API_TOKEN'):
        values[key]=''
    validate_environment(values,ROOT)
    (runtime/'.env').write_text('\n'.join(f'{k}={str(v or "").replace(chr(92),"/")}' for k,v in values.items())+'\n',encoding='utf-8')
    start_pg(runtime, meta)
    import pg8000.dbapi
    conn = pg8000.dbapi.connect(user='wm56_admin',password=admin_password,host='127.0.0.1',port=args.pg_port,database='postgres')
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        # Generated token and validated identifiers contain no SQL quoting characters.
        if not args.database.replace('_','').isalnum():
            raise ValueError('Invalid database identifier')
        cursor.execute("CREATE ROLE wm56_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '"+app_password+"'")
        cursor.execute('CREATE DATABASE "'+args.database+'" OWNER wm56_app')
    finally:
        conn.close()
    conn = pg8000.dbapi.connect(user='wm56_admin',password=admin_password,host='127.0.0.1',port=args.pg_port,database=args.database)
    try:
        conn.cursor().execute('CREATE EXTENSION vector')
        conn.commit()
    finally:
        conn.close()
    print('Independent cluster initialized; run migrate explicitly. Runtime:',runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['init','migrate','start','stop','status'])
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--pg-bin',type=Path)
    parser.add_argument('--pg-port',type=int,default=5546)
    parser.add_argument('--app-port',type=int,default=8056)
    parser.add_argument('--database',default='wm56_app_db')
    parser.add_argument('--synthetic-recipe',action='store_true')
    args = parser.parse_args()
    if args.action == 'init':
        initialize(args)
        return
    runtime, meta, values = read_runtime(args.runtime)
    if args.action == 'stop':
        stop_app(runtime,meta)
        if (runtime/'pgdata'/'postmaster.pid').exists():
            if check_pg_identity(runtime,meta):
                pg_run(meta,'pg_ctl','-D',runtime/'pgdata','-m','fast','-w','stop')
    elif args.action == 'status':
        print(json.dumps({'postgres_listening':port_open(meta['pg_port']),'app_listening':port_open(meta['app_port']),
                          'database':meta['database'],'app_url':values['BASE_URL'],'runtime':str(runtime)}))
    elif args.action == 'migrate':
        start_pg(runtime,meta)
        env = dict(os.environ,WM_ENV_FILE=str(runtime/'.env'))
        env.pop('WM_DB_TEST_MODE',None)
        subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT,env=env,check=True)
    elif args.action == 'start':
        if (runtime/'maintenance.lock').exists():
            raise ValueError('Runtime is in backup/restore maintenance')
        start_pg(runtime,meta)
        if port_open(meta['app_port']):
            raise ValueError('Application port already occupied')
        (runtime/'stop.request').unlink(missing_ok=True)
        env = dict(os.environ,WM_ENV_FILE=str(runtime/'.env'))
        env.pop('WM_DB_TEST_MODE',None)
        with (runtime/'application.log').open('ab') as output:
            process = subprocess.Popen([sys.executable,str(ROOT/'scripts'/'serve_local.py'),
                         '--env-file',str(runtime/'.env'),'--port',str(meta['app_port']),
                         *(['--synthetic-recipe'] if args.synthetic_recipe else [])],
                         cwd=ROOT,env=env,stdout=output,stderr=output,**hidden())
        (runtime/'app.json').write_text(json.dumps({'pid':process.pid}))
        for _ in range(60):
            if port_open(meta['app_port']):
                print(values['BASE_URL']); return
            if process.poll() is not None:
                raise RuntimeError('Startup failed; consult private application.log')
            time.sleep(0.5)
        raise TimeoutError('Startup timeout; inspect status and private log')


if __name__ == '__main__':
    main()
