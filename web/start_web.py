"""Development launcher. No device operation is performed at startup."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import argparse
import threading
import webbrowser
import uvicorn
from bmc_web.app import create_app
from bmc_web.service import Settings

def main():
    parser=argparse.ArgumentParser(description='BMC 本地 Web 工作台；启动不连接 BMC')
    parser.add_argument('--port',type=int,default=38080)
    parser.add_argument('--host',default='127.0.0.1',help='监听地址；局域网访问使用 0.0.0.0')
    parser.add_argument('--reload',action='store_true',help='开发模式：源码变更后自动重载')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'var'/'web')
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('端口须在 1024～65535')
    display_host='127.0.0.1' if args.host in ('0.0.0.0','::') else args.host
    origin=f'http://{display_host}:{args.port}'
    if args.reload:
        import os
        os.environ['REDFISH_DATA_DIR']=str(args.data_dir.resolve())
        os.environ['REDFISH_ORIGIN']=origin
        print('BMC 开发 Web 工作台：'+origin+'（热更新已启用）')
        uvicorn.run('bmc_web.dev_app:app',host=args.host,port=args.port,access_log=False,log_level='warning',reload=True,workers=1)
        return
    app=create_app(Settings(args.data_dir.resolve(),origin=origin))
    print('BMC 本地 Web 工作台：'+origin)
    print('正式工作台：启动不连接 BMC；电源和固件操作在界面逐次确认。')
    print('本机访问口令保存在数据目录 access-key 文件；不会输出到日志。')
    if not args.no_browser:
        # The one-use bootstrap is in a fragment: never sent in a URL to the server/logs.
        url=origin+'/#bootstrap='+app.state.sessions.bootstrap
        timer=threading.Timer(1.5,lambda:webbrowser.open(url));timer.daemon=True;timer.start()
    uvicorn.run(app,host=args.host,port=args.port,access_log=False,log_level='warning',workers=1)

if __name__=='__main__':main()
