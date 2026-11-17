# 模块与协作分工

以下是职责边界和建议，不是已经指定给某位同事的负责人。团队确认后再添加真实CODEOWNERS；不要填写虚构账号。

| 模块 | 主要入口 | 建议维护职责 |
|---|---|---|
| Session/协议 | `core/redfish_session.py` | 认证、同源、异常清理、响应边界、脱敏 |
| Systems | `redfish_catalog_data.py`中3.3、`redfish_ops.py`、`redfish_workflow.py` | 系统/CPU/内存/存储/BIOS/电源/日志的契约和验证 |
| Chassis | 同上3.4 | 热管理、磁盘、PCIe、网卡、功率、传感器 |
| UpdateService | 同上3.7、workflow升级分支 | 镜像、组件映射、Task关联、状态及版本读回 |
| Web后端 | `web/bmc_web/` | 请求模型、一次性计划、队列、SSE、SQLite和审计 |
| Web前端 | `web/frontend/src/` | 范围可见性、参数表单、确认交互、状态与错误展示 |
| CLI/交付 | `cli/`、`scripts/release.py` | 参数、隐藏输入、TTY确认、单文件与发布白名单 |
| 测试/质量 | `core/test_*.py`、`cli/tests/`、`web/tests/` | 合成夹具、回环HTTPS、浏览器、构建与秘密检查 |

Systems/Chassis/UpdateService目前仍共享目录和执行文件。不同同事若同时修改这两个热点文件，先拆小任务、及时同步，避免各自复制整份文件；后续可把操作契约按模块拆分，但必须保留唯一目录入口和回归测试。

新增操作顺序：确认文档与适用机型 → 定义契约/风险/前后置条件 → 核心实现 → 回环夹具与异常测试 → Web/CLI入口 → 文档与兼容性标记。真实现场验证是另一个明确授权的活动，不是合并PR或跑CI的附带步骤。
