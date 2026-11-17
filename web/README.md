# Web应用

生产入口：`python -I web/start_web.py`，仅监听127.0.0.1。默认数据在根目录var/web，不进Git。前端源代码在frontend/src，先执行 `npm --prefix web/frontend ci` 和 `python -I scripts/check.py build-web`；Git不保存编译dist，交付包才包含。

安装、架构、测试和发布均以根README及docs/为准。根目录.venv是推荐环境，启动脚本也兼容旧web/.venv。手工BMC凭据只在本次请求内存；显式“后端凭据”读取根目录.env或环境变量。

bmc_web/app.py负责API与安全依赖；service.py是统一工作队列；redfish_service.py管理三模块的临时凭据和确认；store.py持久化非秘密状态。不要另开一套不协调的升级队列。
