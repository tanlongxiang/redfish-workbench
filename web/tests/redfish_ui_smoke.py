"""Browser end-to-end: loopback only, including inert fixture power action."""
from pathlib import Path
import json,os,socket,subprocess,sys,tempfile,time,urllib.request
from datetime import datetime
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/artifacts/browser'/('Redfish浏览器验证_'+datetime.now().strftime('%Y%m%d_%H%M%S'));OUT.mkdir(parents=True)
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
origin=f'http://127.0.0.1:{port}';errors=[]
with tempfile.TemporaryDirectory(prefix='rf_ui_') as tmp:
 data=Path(tmp)/'state'
 with (OUT/'server.log').open('w') as log:
  process=subprocess.Popen([sys.executable,'-I','-B',str(ROOT/'web/tests/serve_redfish_fixture.py'),'--port',str(port),'--data-dir',str(data)],stdout=log,stderr=subprocess.STDOUT,cwd='/tmp')
  try:
   opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
   for _ in range(150):
    if process.poll() is not None:raise AssertionError('fixture exited')
    try:
     with opener.open(origin,timeout=1) as r:
      if r.status==200:break
    except OSError:time.sleep(.1)
   else:raise AssertionError('startup timed out')
   address=json.loads((data/'fixture.json').read_text())['address']
   with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('REDFISH_CHROMIUM','/usr/bin/google-chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1600,'height':1100},locale='zh-CN');page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
    page.goto(origin);page.locator('input[data-testid=access-key], [data-testid=access-key] input').fill((data/'access-key').read_text().strip());page.get_by_role('button',name='进入工作台').click()
    page.get_by_role('button',name='Redfish 资源操作',exact=True).click();expect(page.get_by_test_id('redfish-workbench')).to_be_visible()
    page.locator('textarea[data-testid=rf-targets], [data-testid=rf-targets] textarea').fill('localhost-fixture '+address)
    page.get_by_test_id('rf-load').click();expect(page.get_by_text('localhost-fixture',exact=True)).to_be_visible()
    page.locator('[data-testid=redfish-workbench] .el-table__body-wrapper .el-checkbox').first.click()
    def input_(name):return page.locator(f'input[data-testid={name}], [data-testid={name}] input')
    input_('rf-username').fill('fixture-user');input_('rf-password').fill('fixture-password')
    page.locator('.el-checkbox').filter(has_text='跳过TLS证书校验（降低设备身份保障）').click()
    with page.expect_response(lambda r:r.url.endswith('/api/redfish/jobs') and r.request.method=='POST') as response:
     page.get_by_test_id('rf-submit').click();page.get_by_role('button',name='确认只读连接',exact=True).click()
    assert response.value.status==200;job_id=response.value.json()['id']
    expect(page.get_by_text('已确认注销',exact=True)).to_be_visible(timeout=15000);expect(input_('rf-password')).to_have_value('')
    page.screenshot(path=str(OUT/'三模块_Session注销_模拟.png'),full_page=True)
    # Change to a power action, confirm that clearing the password does not cancel the plan.
    page.get_by_test_id('rf-operation').click();page.get_by_role('option',name='3.3.23 服务器开关机操作',exact=True).click()
    input_('rf-model').fill('R6250A0');input_('rf-password').fill('fixture-password')
    page.locator('textarea[data-testid=rf-payload], [data-testid=rf-payload] textarea').fill('{"ResetType":"On"}')
    with page.expect_response(lambda r:r.url.endswith('/api/redfish/plans') and r.request.method=='POST') as response:page.get_by_test_id('rf-submit').click()
    assert response.value.status==200;plan=response.value.json();dialog=page.get_by_role('dialog',name='核对本次变更计划')
    expect(dialog).to_be_visible();expect(page.get_by_test_id('rf-confirm')).to_be_disabled();expect(input_('rf-password')).to_have_value('')
    input_('rf-confirm-input').fill(plan['phrase'])
    with page.expect_response(lambda r:r.url.endswith('/confirm') and r.request.method=='POST') as response:page.get_by_test_id('rf-confirm').click()
    assert response.value.status==200
    expect(dialog).not_to_be_visible();expect(page.get_by_text('电源动作已提交并读回目标状态',exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_text('已确认注销',exact=True)).to_be_visible()
    page.screenshot(path=str(OUT/'一次性计划_仅本机模拟.png'),full_page=True)
    # Dedicated BIOS secret inputs must clear without cancelling the offline plan.
    page.get_by_test_id('rf-operation').click();page.get_by_role('option',name='3.3.22 修改 BIOS 密码',exact=True).click()
    page.locator('textarea[data-testid=rf-payload], [data-testid=rf-payload] textarea').fill('{"PasswordName":"SetupAttribute"}')
    input_('rf-password').fill('fixture-password');input_('rf-bios-old').fill('old-bios-private');input_('rf-bios-new').fill('new-bios-private');input_('rf-bios-repeat').fill('new-bios-private')
    with page.expect_response(lambda r:r.url.endswith('/api/redfish/plans') and r.request.method=='POST') as response:page.get_by_test_id('rf-submit').click()
    assert response.value.status==200
    assert 'new-bios-private' not in response.value.text() and 'old-bios-private' not in response.value.text()
    expect(dialog).to_be_visible()
    for field in ['rf-password','rf-bios-old','rf-bios-new','rf-bios-repeat']:expect(input_(field)).to_have_value('')
    dialog.get_by_role('button',name='取消并清除计划').click();expect(dialog).not_to_be_visible()
    assert len(context.request.get(origin+'/api/jobs').json())==2
    # Module switching and a narrow viewport must remain usable.
    page.get_by_text('Chassis',exact=True).click();expect(page.get_by_test_id('rf-operation')).to_contain_text('查询机箱资源集合')
    page.get_by_text('UpdateService',exact=True).click();expect(page.get_by_test_id('rf-operation')).to_contain_text('查询升级服务资源信息')
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(OUT/'窄屏_模拟.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'horizontal page overflow'
    page.set_viewport_size({'width':1600,'height':1100})
    page.get_by_role('button',name='任务中心',exact=True).click()
    page.locator('.records-panel .el-table__row').first.get_by_role('button',name='详情').click()
    expect(page.get_by_role('dialog',name='任务范围与操作记录')).to_be_visible()
    assert not errors,errors
    (OUT/'验证结果.json').write_text(json.dumps({'web_ui':True,'https_bmc':'127.0.0.1 only','read_session_deleted':True,'password_cleared':True,'bios_secret_plan_cancelled_and_inputs_cleared':True,'write_plan_persists_after_password_clear':True,'inert_fixture_power_action':True,'module_navigation':True,'narrow_viewport_no_overflow':True,'page_errors':errors,'real_bmc_connections':0},ensure_ascii=False,indent=2))
    browser.close()
   print('Redfish browser PASS; loopback only; evidence:',OUT)
  finally:
   process.terminate()
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:process.kill();process.wait()
