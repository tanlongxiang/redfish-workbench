"""Run the normal launcher and verify the daily UI without any device task.
Only an inert TEST-NET address is loaded. Power plans are inspected then cancelled.
No correct phrase is entered, no job is submitted, and no device is contacted.
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
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=ROOT/'var/artifacts/browser'/('Web正式入口验证_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
OUTPUT.mkdir(parents=True)
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
origin=f'http://127.0.0.1:{port}'
errors=[]
with tempfile.TemporaryDirectory(prefix='bmc_production_ui_') as directory:
    data=Path(directory)/'data'
    env={**os.environ,'BMC_USERNAME':'fixture-user','BMC_PASSWORD':'fixture-password'}
    with (OUTPUT/'server.log').open('w') as log:
        command=[sys.executable,'-I','-B',str(ROOT/'web/start_web.py'),'--no-browser','--data-dir',str(data),'--port',str(port)]
        process=subprocess.Popen(command,cwd='/tmp',env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                if process.poll() is not None:raise AssertionError('Launcher exited')
                try:
                    with opener.open(origin,timeout=1) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise AssertionError('Startup timeout')
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('REDFISH_CHROMIUM','/usr/bin/google-chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':1600,'height':1100},locale='zh-CN')
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                def restrict(route):
                    if route.request.url.startswith(origin+'/'):route.continue_()
                    else:errors.append('External browser request');route.abort()
                page.route('**/*',restrict)
                page.goto(origin)
                page.locator('input[data-testid=access-key], [data-testid=access-key] input').fill((data/'access-key').read_text().strip())
                page.get_by_role('button',name='进入工作台').click()
                expect(page.get_by_test_id('settings-button')).to_be_visible()
                config=context.request.get(origin+'/api/config').json()
                assert config['mode']=='live' and config['confirmation_required'] is True
                assert 'live_writes_enabled' not in config
                for test_id in ('load-demo','mode-switch','asset-demo'):
                    expect(page.get_by_test_id(test_id)).to_have_count(0)
                for text in ('加载演示','离线模拟','真实写入默认锁定'):
                    assert text not in page.locator('body').inner_text(),text
                page.locator('textarea[data-testid=source-text], [data-testid=source-text] textarea').fill('fixture-node 192.0.2.21')
                page.get_by_test_id('load-source').click()
                expect(page.get_by_test_id('normal-off')).to_be_enabled()
                expect(page.get_by_test_id('force-off')).to_be_enabled()
                expect(page.get_by_test_id('power-on')).to_be_enabled()
                expect(page.get_by_test_id('upgrade')).to_be_disabled()
                page.get_by_test_id('settings-button').click()
                expect(page.get_by_test_id('model-input')).to_have_count(0)
                assert '要求机型' not in page.get_by_role('dialog',name='连接与维护设置').inner_text()
                page.get_by_role('dialog',name='连接与维护设置').locator('.el-drawer__close-btn').click()
                for action in ('normal-off','force-off','power-on'):
                    with page.expect_response(lambda r:r.url.endswith('/api/plans') and r.request.method=='POST') as response:
                        page.get_by_test_id(action).click()
                    assert response.value.status==200,response.value.text()
                    assert 'model' not in response.value.json()
                    dialog=page.get_by_role('dialog')
                    expect(dialog).to_be_visible()
                    expect(dialog).to_contain_text('192.0.2.21')
                    expect(dialog.get_by_test_id('confirm-execute')).to_be_disabled()
                    dialog.locator('input[data-testid=confirm-phrase], [data-testid=confirm-phrase] input').fill('wrong phrase')
                    expect(dialog.get_by_test_id('confirm-execute')).to_be_disabled()
                    dialog.get_by_role('button',name='取消，不执行').click()
                    expect(dialog).not_to_be_visible()
                assert context.request.get(origin+'/api/jobs').json()==[]
                assert context.request.get(origin+'/api/images').json()==[]
                page.locator('.el-message').evaluate_all('(es)=>es.forEach(e=>e.style.visibility="hidden")')
                page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(OUTPUT/'正式工作台_电源可用_未执行.png'),full_page=True)
                page.get_by_role('button',name='资产统计管理',exact=True).click()
                expect(page.get_by_test_id('asset-manager')).to_be_visible()
                expect(page.get_by_test_id('asset-demo')).to_have_count(0)
                assert '模拟台账' not in page.locator('body').inner_text()
                ledger=context.request.get(origin+'/api/asset-records')
                assert ledger.status==200 and ledger.json()['total']==0
                assert not errors,errors
                (OUTPUT/'验证结果.json').write_text(json.dumps({'entry':'start_web.py (normal defaults)','default_mode':'live','simulation_controls':False,'extra_unlock_required':False,'required_model':False,'power_buttons_enabled':True,'cancelled_plans':3,'submitted_jobs':0,'real_device_operations':0,'page_errors':errors},ensure_ascii=False,indent=2))
                browser.close()
            print('Production UI PASS: normal launch; no simulation controls/unlock; power plans cancelled; zero jobs/device operations.')
            print('Evidence:',OUTPUT)
        finally:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
