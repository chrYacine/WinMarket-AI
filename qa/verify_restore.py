"""Read restored synthetic private resources through normal login and HTTP."""
import argparse
import hashlib
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, Request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.core.environment_guard import validate_path,validate_environment


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--account-file',type=Path,required=True)
    parser.add_argument('--browser-evidence',type=Path,required=True)
    args=parser.parse_args()
    runtime=validate_path(args.runtime)
    meta=json.loads((runtime/'runtime.json').read_text())
    base=f"http://127.0.0.1:{meta['app_port']}"
    validate_environment({'BASE_URL':base},ROOT)
    account=json.loads(validate_path(args.account_file).read_text())
    evidence=json.loads(validate_path(args.browser_evidence).read_text())
    browser=build_opener(HTTPCookieProcessor(CookieJar()))
    for route in ('/healthz','/readyz'):
        assert browser.open(base+route).status==200
    page=browser.open(base+'/login').read().decode()
    csrf=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
    browser.open(Request(base+'/login',data=urlencode({'email':account['email'],'password':account['password'],'csrf_token':csrf,'next':'/app'}).encode()))
    history=json.load(browser.open(base+'/api/history'))
    assert history['total']==2
    original=browser.open(base+f"/api/download/{account['parent']}/pdf").read()
    assert hashlib.sha256(original).hexdigest()==evidence['parent_pdf_sha256']
    assert browser.open(base+f"/api/download/{account['revision']}/docx").read().startswith(b'PK')
    documents=browser.open(base+'/api/knowledge/documents').read().decode()
    assert 'conditions-synthetiques.md' in documents
    search=json.load(browser.open(base+'/api/knowledge/search?'+urlencode({'q':'frequence nettoyage standard'})))
    assert 'hybrid' in json.dumps(search) and 'conditions-synthetiques.md' in json.dumps(search)
    print(json.dumps({'restored_login':True,'history':2,'parent_pdf_identical':True,'revision_docx':True,'private_document':True,'real_hybrid_search':True,'healthz':200,'readyz':200}))


if __name__=='__main__':
    main()
