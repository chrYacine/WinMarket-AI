"""Small explicit synthetic reference evaluation, real pgvector and pinned embeddings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    os.environ['WM_DB_TEST_MODE']='1'
    os.environ['WM_REQUIRE_POSTGRES']='1'
    os.environ['EMBEDDING_CACHE_DIR']=str(args.cache)
    from src.core.environment_guard import validate_path
    output=validate_path(args.output)
    validate_path(args.cache)
    from src.core import config
    from tests import pg_support
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from src.web.database.repositories import users
    from src.web.auth.service import create_private_organization_for_user
    from src.web.knowledge.documents_service import upload_document
    from src.rag import hybrid_search
    url=pg_support.postgres_url_or_skip()
    pg_support.assert_disposable_target(url,'')
    pg_support.upgrade(url,'head')
    storage=Path(tempfile.mkdtemp(prefix='wm56_eval_files_'))
    config.LOCAL_STORAGE_PATH=storage
    config.DATA_DIR=storage
    config.RAG_HYBRID_MODE_ENABLED=True
    engine=create_engine(url)
    dataset=json.loads((ROOT/'qa/evaluation_corpus.json').read_text(encoding='utf-8'))
    try:
        with Session(engine,expire_on_commit=False) as db:
            user=users.create_user(db,email='synthetic-evaluation@example.test',password_hash='unused',first_name='Synthetic',last_name='Evaluation',status='pending')
            org=create_private_organization_for_user(db,user)
            for name,content in dataset['documents'].items():
                upload_document(db,organization_id=org.id,owner_user_id=user.id,original_filename=name,raw=content.encode())
            db.commit()
            rows=[];hits=expected=citations=valid=0
            for case in dataset['queries']:
                result=hybrid_search.search(db,organization_id=org.id,owner_user_id=user.id,query=case['query'],top_k=3)
                assert result.mode=='hybrid'
                found={e.source for e in result.evidences}
                expected+=len(case['expected_references'])
                recalled=len(found.intersection(case['expected_references']))
                hits+=recalled
                for evidence in result.evidences:
                    citations+=1
                    source=dataset['documents'][evidence.source]
                    valid+=int(evidence.content==source[evidence.start_char:evidence.end_char])
                rows.append({'id':case['id'],'expected':case['expected_references'],'retrieved':sorted(found),'recalled':recalled})
            assert hits==expected and valid==citations
            from src.rag.model_artifact import MANIFEST
            result={'synthetic':True,'llm':'not used','database':'real PostgreSQL/pgvector disposable',
                    'model_revision':MANIFEST['revision'],'reference_recall_at_3':hits/expected,
                    'citation_integrity':valid/citations,'expected_references':expected,'citations_checked':citations,
                    'cases':rows,'corpus_sha256':hashlib.sha256((ROOT/'qa/evaluation_corpus.json').read_bytes()).hexdigest(),
                    'limitation':'Small targeted corpus; no claim of general gain over lexical search'}
            output.write_text(json.dumps(result,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k!='cases'}))
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
