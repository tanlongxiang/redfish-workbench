"""Drive the production frontend against a separate offline-only fixture.
The normal launcher has no simulation mode. No auth keys are logged.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=ROOT/'var/artifacts/browser'/('Web工作台GUI_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
OUTPUT.mkdir(parents=True)
with socket.socket() as s:s.bind(('127.0.0.1',0));PORT=s.getsockname()[1]
ORIGIN=f'http://127.0.0.1:{PORT}'
errors=[];external=[]

def poll_job(context,job_id):
    for _ in range(150):
        response=context.request.get(ORIGIN+'/api/jobs/'+job_id)
        value=response.json()
        if value['state'] not in ('queued','running'):return value
        time.sleep(.1)
    raise AssertionError('Job timeout')

def screenshot(page,name):
    page.locator('.el-message').evaluate_all('(es)=>es.forEach(e=>e.style.visibility="hidden")')
    page.evaluate('window.scrollTo(0,0)')
    page.wait_for_timeout(350)  # Let checkbox/disabled CSS transitions finish for visual review.
    page.screenshot(path=str(OUTPUT/name),full_page=True)

def wait_gate(page,ready):
    expect(page.get_by_test_id('upgrade')).to_be_enabled(timeout=15000) if ready else expect(page.get_by_test_id('upgrade')).to_be_disabled(timeout=15000)

def submit_read(page,action):
    expect(page.get_by_test_id(action)).to_be_enabled(timeout=15000)
    with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
        page.get_by_test_id(action).click()
    assert response.value.status==200,(response.value.status,response.value.text())
    return response.value.json()

def click_plan(page,button):
    button.click()
    dialog=page.get_by_role('dialog')
    expect(dialog).to_be_visible()
    phrase=dialog.locator('.el-form-item__label').inner_text().split('：',1)[1]
    dialog.locator('input[data-testid=confirm-phrase], [data-testid=confirm-phrase] input').fill(phrase)
    with page.expect_response(lambda r:'/api/plans/' in r.url and r.url.endswith('/confirm') and r.request.method=='POST') as response:
        dialog.get_by_test_id('confirm-execute').click()
    assert response.value.status==200,(response.value.status,response.value.text())
    value=response.value.json()
    expect(dialog).not_to_be_visible()
    return value

with tempfile.TemporaryDirectory(prefix='bmc_web_browser_') as temporary:
    data=Path(temporary)/'data'
    env={**os.environ,'BMC_USERNAME':'fixture-user','BMC_PASSWORD':'fixture-password'}
    with (OUTPUT/'server.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-I','-B',str(ROOT/'web/tests/serve_fixture.py'),'--data-dir',str(data),'--port',str(PORT)],cwd='/tmp',env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                if process.poll() is not None:raise AssertionError('Web launcher exited early; inspect server.log')
                try:
                    with opener.open(ORIGIN,timeout=1) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise AssertionError('HTTP startup timeout')
            key=(data/'access-key').read_text().strip()
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('REDFISH_CHROMIUM','/usr/bin/google-chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':1600,'height':1100},accept_downloads=True,locale='zh-CN')
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('response',lambda r:errors.append('HTTP '+str(r.status)+' '+r.url) if r.status>=500 else None)
                def route(request):
                    if request.request.url.startswith(ORIGIN+'/'):request.continue_()
                    else:external.append(request.request.url);request.abort()
                page.route('**/*',route)
                page.goto(ORIGIN)
                page.wait_for_timeout(500)
                screenshot(page,'Web_登录页.png')
                (OUTPUT/'初始化检查.json').write_text(json.dumps({'page_errors':errors,'access_element':page.locator('[data-testid=access-key]').evaluate_all('(es)=>es.map(e=>e.tagName)')},ensure_ascii=False,indent=2))
                page.locator('input[data-testid=access-key], [data-testid=access-key] input').fill(key)
                page.get_by_role('button',name='进入工作台').click()
                expect(page.get_by_test_id('settings-button')).to_be_visible()
                # Power controls must work with zero images and zero chosen CPLD groups.
                page.locator('textarea[data-testid=source-text], [data-testid=source-text] textarea').fill('DEMO-01 192.0.2.11')
                page.get_by_test_id('load-source').click()
                power=submit_read(page,'power-read');assert poll_job(context,power['id'])['state']=='completed'
                expect(page.get_by_test_id('node-table')).to_contain_text('CY62-T58',timeout=15000)
                assert context.request.get(ORIGIN+'/api/images').json()==[]
                on=click_plan(page,page.get_by_test_id('power-on'));assert poll_job(context,on['id'])['state']=='completed'
                off=click_plan(page,page.get_by_test_id('normal-off'));assert poll_job(context,off['id'])['state']=='completed'
                expect(page.get_by_test_id('settings-button')).to_be_enabled(timeout=15000)
                screenshot(page,'Web_无镜像独立电源.png')
                csrf=context.request.get(ORIGIN+'/api/session').json()['csrf']
                fixture=context.request.post(ORIGIN+'/api/demo',data={},headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})
                assert fixture.status==200
                targets=fixture.json()['targets']
                page.reload();expect(page.get_by_test_id('settings-button')).to_be_visible()
                page.locator('textarea[data-testid=source-text], [data-testid=source-text] textarea').fill('\n'.join(t['name']+' '+t['address'] for t in targets))
                page.get_by_test_id('load-source').click()
                table=page.get_by_test_id('node-table')
                expect(table.locator('.el-table__body-wrapper tbody tr')).to_have_count(3)
                expect(page.get_by_test_id('settings-button')).to_be_enabled()
                expect(page.get_by_test_id('read-state')).to_be_enabled()
                expect(table.locator('.el-table__body-wrapper input[type=checkbox]').first).to_be_checked()
                table.locator('.el-table__header-wrapper .el-checkbox').click()
                expect(table.locator('.el-table__body-wrapper input[type=checkbox]:checked')).to_have_count(3)
                screenshot(page,'Web_导入后.png')
                metadata=page.get_by_test_id('read-state').evaluate('(e)=>({disabled:e.disabled,html:e.outerHTML,parents:[...function*(x){while(x){yield {tag:x.tagName,role:x.getAttribute("role"),disabled:x.getAttribute("aria-disabled")};x=x.parentElement}}(e)]})')
                (OUTPUT/'读取按钮检查.json').write_text(json.dumps({'button':metadata,'jobs':context.request.get(ORIGIN+'/api/jobs').json(),'page_errors':errors},ensure_ascii=False,indent=2))
                power=submit_read(page,'power-read');assert poll_job(context,power['id'])['state']=='completed'
                expect(page.get_by_test_id('node-table')).to_contain_text('CY62-T58',timeout=15000)
                read=submit_read(page,'read-state');assert poll_job(context,read['id'])['state']=='completed'
                expect(table.locator('.power-badge').first).to_contain_text('On',timeout=10000)
                expect(page.get_by_test_id('settings-button')).to_be_enabled(timeout=15000)
                for component in ('Dcu02Cpld','Dcu46Cpld'):
                    page.get_by_test_id('group-'+component).locator('.el-select').click()
                    dropdown=page.locator('.el-select-dropdown:visible').last
                    dropdown.get_by_role('option').filter(has_text='DEMO_LT_NOT_FOR_HARDWARE.svf').click()
                    expect(dropdown).not_to_be_visible()
                    page.get_by_test_id('check-'+component).click()
                pre=submit_read(page,'precheck');assert poll_job(context,pre['id'])['state']=='failed'
                expect(page.get_by_test_id('gate-reason')).to_contain_text('On',timeout=10000)
                wait_gate(page,False);screenshot(page,'Web_开机门禁.png')
                off=click_plan(page,page.get_by_test_id('normal-off'))
                assert poll_job(context,off['id'])['state']=='completed'
                expect(page.get_by_test_id('gate-reason')).to_contain_text('尚未',timeout=15000)
                wait_gate(page,False)
                pre=submit_read(page,'precheck');assert poll_job(context,pre['id'])['state']=='completed'
                wait_gate(page,True)
                screenshot(page,'Web_单页工作台.png')
                assert page.locator('[data-testid^=expected-]').count()==0
                page.get_by_test_id('check-Dcu02Cpld').click();wait_gate(page,False)
                page.get_by_test_id('check-Dcu02Cpld').click();wait_gate(page,False)
                pre=submit_read(page,'precheck');assert poll_job(context,pre['id'])['state']=='completed';wait_gate(page,True)
                with page.expect_download() as download:
                    page.get_by_role('button',name='保存配置',exact=True).click()
                workspace=Path(temporary)/'workspace.json';download.value.save_as(workspace)
                plan_job=click_plan(page,page.get_by_test_id('upgrade'))
                # A browser refresh loses client state, not the background task or immutable scope.
                page.reload();expect(page.get_by_test_id('settings-button')).to_be_visible()
                upgraded=poll_job(context,plan_job['id']);assert upgraded['state']=='completed',upgraded.get('error')
                for result in upgraded['results']:assert [i['component'] for i in result['components']]==['Dcu02Cpld','Dcu46Cpld']
                page.get_by_role('button',name='任务中心',exact=True).click()
                expect(page.locator('.records-panel')).to_contain_text('固件升级',timeout=10000)
                screenshot(page,'Web_任务中心.png')
                page.get_by_role('button',name='维护工作台',exact=True).click()
                page.locator('input[type=file][accept=".json"]').first.set_input_files(str(workspace))
                expect(page.get_by_test_id('node-table').locator('.el-table__body-wrapper tbody tr')).to_have_count(3)
                page.get_by_test_id('node-table').locator('.el-table__header-wrapper .el-checkbox').click()
                verify=submit_read(page,'verify');assert poll_job(context,verify['id'])['state']=='completed'
                expect(page.get_by_test_id('gate-reason')).to_contain_text('预检查',timeout=15000)
                wait_gate(page,False)
                # Verify/readback intentionally snapshots the chosen components only.
                # An explicit full-state read proves the two unselected groups were unchanged.
                final_read=submit_read(page,'read-state');assert poll_job(context,final_read['id'])['state']=='completed'
                cached=context.request.get(ORIGIN+'/api/assets?mode=demo').json()
                for asset in cached:
                    assert {c:v['version'] for c,v in asset['snapshot']['inventory'].items()}=={'Dcu02Cpld':'0.5','Dcu13Cpld':'0.3','Dcu46Cpld':'0.5','Dcu57Cpld':'0.3'},asset
                response=context.request.get(ORIGIN+'/api/jobs/'+plan_job['id']+'/files/report');assert response.status==200
                (OUTPUT/'模拟维护结果.xlsx').write_bytes(response.body())
                screenshot(page,'Web_版本核验.png')
                page.get_by_role('button',name='资产统计管理',exact=True).click()
                ledger=page.get_by_test_id('asset-table')
                expect(ledger.locator('.el-table__body-wrapper tbody tr')).to_have_count(3)
                ledger.locator('.el-table__header-wrapper .el-checkbox').click()
                collect=submit_read(page,'asset-collect');assert poll_job(context,collect['id'])['state']=='completed'
                expect(ledger).to_contain_text('完整采集',timeout=15000)
                collected=context.request.get(ORIGIN+'/api/asset-records?mode=demo').json()
                assert collected['filtered']==3
                assert sorted(r['latest_count'] for r in collected['items'])==[37,38,38]
                ledger.get_by_role('button',name='详情 / 编辑').first.click()
                drawer=page.get_by_role('dialog',name='资产详情与台账管理')
                drawer.locator('input[data-testid=asset-location], [data-testid=asset-location] input').fill('模拟机柜 R01')
                with page.expect_response(lambda r:r.url.endswith('/api/asset-records/metadata') and r.request.method=='PUT') as edited:
                    page.get_by_test_id('asset-save').click()
                assert edited.value.status==200
                drawer.locator('.el-drawer__close-btn').click()
                page.locator('input[type=file][accept=".csv,.xlsx"]').set_input_files({'name':'assets.csv','mimeType':'text/csv','buffer':'节点名,BMC_IP,SN\nDEMO-01,192.0.2.11,001234567890123456789\n'.encode()})
                expect(ledger).to_contain_text('001234567890123456789',timeout=15000)
                expect(ledger).to_contain_text('模拟机柜 R01')
                screenshot(page,'Web_资产统计管理.png')
                page.locator('input[data-testid=asset-search], [data-testid=asset-search] input').fill('DEMO-01')
                expect(ledger.locator('.el-table__body-wrapper tbody tr')).to_have_count(1)
                with page.expect_download() as exported:page.get_by_test_id('asset-export').click()
                exported.value.save_as(OUTPUT/'模拟资产台账.xlsx')
                page.reload();expect(page.get_by_test_id('settings-button')).to_be_visible()
                page.get_by_role('button',name='资产统计管理',exact=True).click()
                expect(page.get_by_test_id('asset-table')).to_contain_text('模拟机柜 R01',timeout=15000)
                page.set_viewport_size({'width':430,'height':900})
                screenshot(page,'Web_资产管理窄屏.png')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),'Asset page horizontal overflow'
                page.set_viewport_size({'width':1600,'height':1100})
                page.get_by_role('button',name='操作审计',exact=True).click();expect(page.locator('.records-panel')).to_contain_text('asset_metadata_updated')
                screenshot(page,'Web_操作审计.png')
                page.get_by_role('button',name='维护工作台',exact=True).click()
                screenshot(page,'Web_刷新后工作台.png')
                assert not errors,errors
                assert not external,external
                result={'entry':'tests/serve_fixture.py (offline test only)','frontend':'Vue 3 + TypeScript + Element Plus','http':'127.0.0.1 only',
                        'checks':['login','demo load','read On','On blocks upgrade','explicit simulated shutdown','Off alone blocked','precheck enables','group edits invalidate','explicit selected-group upgrade','browser reload preserves worker','configuration restore','verify','reports','assets','audit','narrow layout','power without any images','38-field asset collection','asset edit/import/filter/export','asset records survive reload','asset narrow layout'],
                        'upgraded_components':['Dcu02Cpld','Dcu46Cpld'],'unchecked_unchanged':True,'page_errors':errors,'external_requests':external,'real_bmc_operations':0}
                (OUTPUT/'验证结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
                print('Browser PASS: offline fixture + real Vue UI + API worker; selected groups only; reload/report/auth/gates verified; no real BMC access.')
                print('Screenshots:',OUTPUT)
                browser.close()
        finally:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
