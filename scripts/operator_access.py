"""Local server administration, no public route or organization-admin shortcut."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,
                         help='Local .env file (default local mode). Omit to read the real process '
                              'environment instead (server-managed config/secrets, e.g. Render) — '
                              'same validation, same operator rules either way.')
    parser.add_argument('--credential-file',type=Path,required=True)
    parser.add_argument('action',choices=['bootstrap','list','activate','revoke'])
    parser.add_argument('--actor')
    parser.add_argument('--user-id',type=uuid.UUID)
    parser.add_argument('--organization-id',type=uuid.UUID)
    parser.add_argument('--reason')
    parser.add_argument('--expires-at',type=datetime.fromisoformat)
    parser.add_argument('--max-analyses',type=int)
    args = parser.parse_args()
    from src.core.environment_guard import validate_path, validate_environment
    credential_path = validate_path(args.credential_file)
    if args.env_file is not None:
        envfile = validate_path(args.env_file)
        if (envfile.parent/'maintenance.lock').exists():
            raise RuntimeError('Runtime is in backup/restore maintenance')
        from dotenv import dotenv_values
        values = dotenv_values(envfile,interpolate=False)
        os.environ['WM_ENV_FILE'] = str(envfile)
    else:
        # Server-managed configuration (e.g. Render): the real process environment IS the
        # configuration, exactly like the running application itself reads it — never a second,
        # divergent source of truth, never a local file assumed to exist.
        values = dict(os.environ)
    validate_environment(values,ROOT)
    if not values.get('DATABASE_URL'):
        parser.error('Explicit isolated database URL required')
    from src.web.database.session import session_scope
    from src.web.auth import manual_access as service
    from src.web.database.models import User, Membership, Subscription
    from sqlalchemy import select
    with session_scope() as db:
        if args.action == 'bootstrap':
            if not args.actor or credential_path.exists():
                parser.error('A new actor and a nonexistent credential file are required')
            actor, token = service.bootstrap_operator(db,actor=args.actor)
            credential_path.parent.mkdir(parents=True,exist_ok=True)
            fd = os.open(credential_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file:
                file.write(token)
            print(json.dumps({'operator':actor.actor,'credential_file':str(credential_path),'secret_printed':False}))
            return
        credential = credential_path.read_text().strip()
        service.authenticate_operator(db,credential)
        if args.action == 'list':
            rows = db.execute(select(User.id,User.email,User.status,Membership.organization_id,Membership.role,Membership.status.label('membership_status'),Subscription.access_origin,Subscription.status.label('access_status'),Subscription.expires_at,Subscription.max_analyses).join(Membership,Membership.user_id==User.id).join(Subscription,Subscription.user_id==User.id)).mappings().all()
            print(json.dumps([dict(row) for row in rows],default=str,indent=2))
        else:
            if not args.user_id or not args.organization_id or not args.reason:
                parser.error('Explicit user, organization and reason are required')
            changed = service.change_access(db,credential=credential,user_id=args.user_id,organization_id=args.organization_id,reason=args.reason,activate=args.action=='activate',expires_at=args.expires_at,max_analyses=args.max_analyses)
            print(json.dumps({'changed':changed,'action':args.action,'user_id':str(args.user_id),'organization_id':str(args.organization_id)}))


if __name__ == '__main__':
    main()
