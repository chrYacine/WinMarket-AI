"""Real browser recipe with synthetic inputs; only fact-search LLM is simulated."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
PASSWORD='Synthetic-Lot56-Only-123!'
DOC='SYNTHETIQUE - Notre fr\u00e9quence de nettoyage standard est de 5 fois par semaine, ajustable selon les besoins du client.'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--python',type=Path,required=True)
    parser.add_argument('--browser',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(ROOT))
    from src.core.environment_guard import validate_path,validate_environment
    runtime=validate_path(args.runtime)
    output=validate_path(args.output)
    meta=json.loads((runtime/'runtime.json').read_text())
    base=f"http://127.0.0.1:{meta['app_port']}"
    validate_environment({'BASE_URL':base},ROOT)
    output.mkdir(parents=True,exist_ok=True)
    credential=runtime/'operator.token'
    def operator(action,*extra):
        result=subprocess.run([str(args.python),str(ROOT/'scripts/operator_access.py'),
             '--env-file',str(runtime/'.env'),'--credential-file',str(credential),action,*extra],cwd=ROOT,
             capture_output=True,text=True,encoding='utf-8',errors='replace')
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout
    if not credential.exists():
        operator('bootstrap','--actor','lot56-browser-operator')
    def activate(email):
        raw=operator('list')
        rows=json.loads(raw[raw.index('[\n'):])
        row=next(r for r in rows if r['email']==email)
        operator('activate','--user-id',row['id'],'--organization-id',row['organization_id'],
                 '--reason','Synthetic browser qualification','--expires-at',
                 (datetime.now(timezone.utc)+timedelta(days=2)).isoformat(),'--max-analyses','10')
        return row
    checks=[]
    def record(name):
        checks.append(name);print('PASS',name,flush=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path=str(args.browser),headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        page=context.new_page()
        page.set_default_timeout(30000)
        page.on('dialog',lambda dialog:dialog.accept())
        def register(page,email):
            page.goto(base+'/register')
            for name,value in [('first_name','Synthetic'),('last_name','Recipe'),('email',email),('password',PASSWORD),('password_confirm',PASSWORD)]:
                page.locator('[name="'+name+'"]').fill(value)
            page.locator('[name="accept_terms"]').check()
            page.locator('button[type="submit"]').click()
            expect(page.locator('h1')).to_contain_text('En attente')
        email='recipe-'+secrets.token_hex(4)+'@example.test'
        register(page,email)
        assert context.request.get(base+'/api/capacity').status==403
        record('registration_pending_business_denied')
        owner=activate(email)
        # Existing session must see activation without a fresh cookie.
        assert context.request.get(base+'/api/capacity').status==200
        assert context.request.get(base+'/api/history').json()['total']==0
        state=context.request.get(base+'/api/scoring-config').json()
        assert state['policy']['active'] is None and state['policy']['draft'] is None
        record('operator_activation_existing_session_empty_configuration')
        page.goto(base+'/login')
        page.locator('[name="email"]').fill(email)
        page.locator('[name="password"]').fill(PASSWORD)
        page.locator('button[type="submit"]').click()
        page.goto(base+'/app/parametres')
        page.locator('#prof-raison-sociale').fill('Entreprise synthetique LOT56')
        page.locator('#add-fact').click()
        page.locator('[data-f="label"]').fill('Frequence de nettoyage')
        page.locator('[data-f="key"]').fill('frequence_nettoyage')
        page.locator('[data-f="type"]').select_option('number')
        page.locator('[data-f="unit"]').fill('par_semaine')
        page.locator('[data-f="value"] input').fill('5')
        with page.expect_response(lambda r:'/api/scoring-config/profile' in r.url and r.request.method=='PUT') as response:
            page.locator('#save-profile').click()
        assert response.value.status==200,response.value.text()
        page.locator('#new-criterion-evaluator').select_option('numeric_threshold')
        page.locator('#add-criterion').click()
        page.locator('[data-c="label"]').fill('Frequence compatible')
        page.locator('[data-c="id"]').fill('frequence')
        page.locator('[data-c="weight"]').fill('100')
        page.locator('[data-param="fact_key"]').select_option('frequence_nettoyage')
        page.locator('[data-param="comparison"]').select_option('provider_gte_ao')
        page.locator('[data-param="pass_score"]').fill('100')
        page.locator('[data-param="fail_score"]').fill('0')
        page.locator('#pol-threshold-go').fill('50')
        page.locator('#pol-threshold-reserve').fill('20')
        with page.expect_response(lambda r:r.url.split('?')[0].endswith('/api/scoring-config/policy') and r.request.method=='PUT') as response:
            page.locator('#save-draft').click()
        assert response.value.status==200,response.value.text()
        page.locator('#activate-draft').click()
        with page.expect_response(lambda r:'/policy/activate' in r.url) as response:
            page.locator('#activation-confirm-yes').click()
        assert response.value.status==200,response.value.text()
        # Missing provider fact is deliberate; the policy remains the user's chosen one.
        page.locator('[data-f="value"] input').fill('')
        with page.expect_response(lambda r:'/api/scoring-config/profile' in r.url and r.request.method=='PUT') as response:
            page.locator('#save-profile').click()
        assert response.value.status==200
        record('private_profile_and_explicit_policy_via_browser')
        page.goto(base+'/app/analyser')
        page.locator('#capacity-open').click()
        page.locator('#cap-charge').evaluate("e=>{e.value='40';e.dispatchEvent(new Event('input',{bubbles:true}))}")
        page.locator('#cap-projects-count').fill('0')
        page.locator('#cap-min-dispo').fill('10')
        with page.expect_response(lambda r:r.url.split('?')[0].endswith('/api/capacity') and r.request.method=='POST') as response:
            page.locator('#capacity-form button[type="submit"]').click()
        assert response.value.status==200,response.value.text()
        record('private_capacity_via_browser')
        page.goto(base+'/app/base-connaissances')
        page.locator('#knowledge-file').set_input_files({'name':'conditions-synthetiques.md','mimeType':'text/markdown','buffer':DOC.encode()})
        with page.expect_response(lambda r:r.url.split('?')[0].endswith('/api/knowledge/documents') and r.request.method=='POST',timeout=180000) as response:
            page.locator('#knowledge-upload').click()
        assert response.value.status==201,response.value.text()
        document=response.value.json()
        page.locator('#knowledge-search').fill('frequence nettoyage standard')
        with page.expect_response(lambda r:'/api/knowledge/search' in r.url) as response:
            page.locator('#knowledge-search-btn').click()
        search=response.value.json()
        (output/'search.json').write_text(json.dumps(search,indent=2),encoding='utf-8')
        assert 'conditions-synthetiques.md' in json.dumps(search)
        assert 'hybrid' in json.dumps(search),search
        record('private_upload_index_and_real_hybrid_search_via_browser')
        page.goto(base+'/app/analyser')
        page.locator('label:has(input[name="mode"][value="paste"])').click()
        page.locator('#paste-textarea').fill("Appel d'offres de nettoyage a Lyon. 3 fois par semaine. Budget : 120 000 euros.")
        page.locator('#submit-analyze').click()
        page.wait_for_url('**/app/resultats/*',timeout=120000)
        parent=page.url.rsplit('/',1)[1]
        expect(page.locator('body')).to_contain_text('INCOMPLET')
        original_pdf=context.request.get(base+f'/api/download/{parent}/pdf').body()
        assert original_pdf.startswith(b'%PDF')
        record('ao_incomplete_result_and_parent_pdf')
        page.locator('#completion-open').click()
        page.get_by_role('button',name='Chercher dans mes documents').first.click()
        page.get_by_role('button',name='Accepter cette proposition').click()
        page.locator('#completion-confirm-profile').check()
        page.locator('#completion-submit').click()
        page.wait_for_url(lambda url:'/app/resultats/' in str(url) and not str(url).endswith(parent),timeout=120000)
        revision=page.url.rsplit('/',1)[1]
        record('sourced_proposal_accepted_revision_via_browser')
        page.screenshot(path=str(output/'revision.png'),full_page=True)
        for kind,prefix in [('pdf',b'%PDF'),('docx',b'PK')]:
            result=context.request.get(base+f'/api/download/{revision}/{kind}')
            assert result.status==200 and result.body().startswith(prefix)
        assert context.request.get(base+f'/api/download/{parent}/pdf').body()==original_pdf
        assert context.request.get(base+'/api/history').json()['total']==2
        record('history_pdf_docx_parent_preserved')
        second=browser.new_context()
        other=second.new_page()
        other_email='isolation-'+secrets.token_hex(4)+'@example.test'
        register(other,other_email)
        activate(other_email)
        assert second.request.get(base+'/api/history').json()['total']==0
        assert second.request.get(base+f'/api/download/{parent}/pdf').status==404
        assert 'conditions-synthetiques.md' not in second.request.get(base+'/api/knowledge').text()
        record('second_account_documents_and_history_isolated')
        operator('revoke','--user-id',owner['id'],'--organization-id',owner['organization_id'],'--reason','Synthetic session revocation check')
        assert context.request.get(base+f'/api/download/{parent}/pdf').status==403
        record('operator_revocation_existing_cookie_denied')
        # Explicit fresh grant permits restore verification of unchanged private data later.
        activate(email)
        (runtime/'recipe-account.json').write_text(json.dumps({'email':email,'password':PASSWORD,'parent':parent,'revision':revision}))
        (output/'browser.json').write_text(json.dumps({'checks':checks,'count':len(checks),'engine':'Edge headless / Playwright',
            'llm':'synthetic fact-search adapter only; no external call','embeddings':'real pinned ONNX',
            'parent_pdf_sha256':hashlib.sha256(original_pdf).hexdigest()},indent=2))
        browser.close()


if __name__=='__main__':
    main()
