"""Coherent backup/restore of an explicitly stopped independent runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.local_env import read_runtime, validate_path, check_pg_identity, port_open, start_pg, hidden


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files(root):
    result={}
    if root.exists():
        for path in sorted(root.rglob('*')):
            if path.is_file():
                resolved=validate_path(path)
                if root.resolve() not in resolved.parents:
                    raise ValueError('Storage link escapes runtime')
                result[path.relative_to(root).as_posix()]=digest(path)
    return result


def db_snapshot(values):
    from sqlalchemy import create_engine, inspect, text
    engine=create_engine(values['DATABASE_URL'])
    try:
        with engine.connect() as conn:
            tables=inspect(conn).get_table_names(schema='public')
            result={}
            for name in sorted(tables):
                quoted=engine.dialect.identifier_preparer.quote(name)
                rows=sorted(conn.execute(text(f'SELECT to_jsonb(t)::text FROM {quoted} t')).scalars().all())
                result[name]={'count':len(rows),'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
            return result
    finally:
        engine.dispose()


def pg_tool(meta, values, tool, *args):
    url=urlsplit(values['DATABASE_URL'])
    env=dict(os.environ,PGPASSWORD=unquote(url.password),PGHOST='127.0.0.1',PGPORT=str(meta['pg_port']),
             PGUSER=unquote(url.username),PGDATABASE=meta['database'])
    binary=validate_path(Path(meta['pg_bin'])/(tool+('.exe' if os.name=='nt' else '')))
    subprocess.run([str(binary),*map(str,args)],env=env,check=True,**hidden())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore','verify'])
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args()
    runtime,meta,values=read_runtime(args.runtime)
    archive=validate_path(args.archive)
    if archive==runtime or archive in runtime.parents or runtime in archive.parents:
        raise ValueError('Backup must be separate from its runtime')
    if port_open(meta['app_port']) or (runtime/'app.json').exists():
        raise ValueError('Stop this runtime explicitly before backup/restore/verify')
    if args.action=='backup' and archive.exists():
        raise ValueError('Backup destination must not exist')
    lock=runtime/'maintenance.lock'
    with lock.open('x') as handle:
        handle.write(args.action)
    try:
        start_pg(runtime,meta)
        check_pg_identity(runtime,meta)
        data=runtime/'data'
        if args.action=='backup':
            before=db_snapshot(values)
            manifest_files=files(data)
            archive.mkdir(parents=True)
            if os.name!='nt':
                archive.chmod(0o700)
            else:
                import getpass
                subprocess.run(['icacls',str(archive),'/inheritance:r','/grant:r',getpass.getuser()+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,capture_output=True)
            pg_tool(meta,values,'pg_dump','--format=custom','--no-owner','--no-acl','--no-comments','--file',archive/'database.dump')
            if data.exists():
                shutil.copytree(data,archive/'files')
            else:
                (archive/'files').mkdir()
            if before!=db_snapshot(values) or manifest_files!=files(data) or manifest_files!=files(archive/'files'):
                raise ValueError('Concurrent data/file change; incomplete backup is not valid')
            manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'tables':before,'files':manifest_files,
                      'dump_sha256':digest(archive/'database.dump'),'source_runtime':str(runtime),
                      'roles':'wm56_app recreated by local_env init; passwords/config/operator token excluded'}
            (archive/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        else:
            manifest=json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
            if manifest['dump_sha256']!=digest(archive/'database.dump') or manifest['files']!=files(archive/'files'):
                raise ValueError('Backup integrity failure')
            if args.action=='restore':
                if Path(manifest['source_runtime']).resolve()==runtime:
                    raise ValueError('Restore into source runtime refused')
                if db_snapshot(values) or (data.exists() and any(data.iterdir())):
                    raise ValueError('Restore requires a fresh initialized, unmigrated and empty runtime')
                pg_tool(meta,values,'pg_restore','--exit-on-error','--no-owner','--no-acl','--no-comments','--dbname',meta['database'],archive/'database.dump')
                shutil.copytree(archive/'files',data,dirs_exist_ok=True)
            if db_snapshot(values)!=manifest['tables'] or files(data)!=manifest['files']:
                raise ValueError('Restored database/files differ from verified backup')
        print(json.dumps({'operation':args.action,'verified':True,'tables':len(manifest['tables']),
                          'rows':sum(item['count'] for item in manifest['tables'].values()),'files':len(manifest['files'])}))
    finally:
        lock.unlink()


if __name__=='__main__':
    main()
