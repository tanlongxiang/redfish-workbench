# 内部 Redfish 工作台

面向内部协作开发的 **Web + CLI + 共享核心** 仓库。当前以 Systems、Chassis、UpdateService 三模块和任务级 Session 为主，保留既有资产统计、内存统计与独立 DCU CPLD 适配。

> 当前版本：Web 0.6.0 / CLI 1.2.0。69项是文档操作目录，不是69项硬件现场验收。本仓库不含真实凭据、节点清单、现场证据或固件。多人开发不等于可直接作为多人生产服务：当前服务仅监听本机回环地址。

## 开发环境

Python 3.11+（验证环境3.12）、Node.js 22.12+（`.nvmrc`固定已验证的22.23.2）、npm、Git、OpenSSL。浏览器测试另需 Playwright 和 Chrome/Chromium。

```bash
# 在仓库根目录；Windows用 py -3 代替 python，激活 .venv\Scripts\activate
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm --prefix web/frontend ci --no-audit --no-fund
python -I scripts/check.py build-web
python -I web/start_web.py
```

打开 `http://127.0.0.1:38080`，进入“Redfish 资源操作”。启动、导入、生成计划均不连接BMC。页面默认手工输入本次凭据；也可显式选后端配置。需要配置文件时从根目录 `.env.example` 复制 `.env`，只能本机保存，不提交Git。

## 命令行

仅运行CLI不需要pip或Node：

```bash
python -I cli/bmc_tool.py --help
python -I cli/bmc_tool.py redfish catalog
python -I cli/bmc_tool.py targets --targets cli/examples/nodes.csv
```

设备读取必须显式指定节点并加 `--live`；写操作另需本次许可、`--execute`及交互确认。测试、构建和建立仓库都不构成真实设备执行许可。

## 目录

| 目录 | 职责 |
|---|---|
| `web/frontend/` | Vue 3 / TypeScript / Element Plus，三模块、任务、资产与审计界面 |
| `web/bmc_web/` | FastAPI、本地登录、一次性计划、统一后台队列、SQLite、SSE |
| `cli/` | 标准库CLI入口、参数解析、交互确认与单文件构建 |
| `core/` | 共享Session、69项操作契约、执行流程、报告、资产及旧DCU适配 |
| `docs/` | 架构、模块边界、开发流程、迁移与接口文档索引 |
| `scripts/` | 测试、前端构建、索引安全检查、发布与独立解包验证 |
| `var/` | **不入Git**：运行数据库、报告、锁、测试截图、本地参考材料 |
| `dist/` | **不入Git**：发布ZIP、pyz、源码交付副本 |

`core/` 目前是共享标准库模块目录，入口通过明确的路径引导导入；不是已发布的Python包。不要分别复制一份核心到Web和CLI里修改。

## 每次提交前

```bash
python -I scripts/check.py test
python -I scripts/check.py build-web
# 修改界面或会话流程时；默认Linux Chrome路径可用REDFISH_CHROMIUM覆盖
python -I scripts/check.py browser

git add <已审查的文件>
python -I scripts/security_check.py
```

可选启用本仓库的提交前检查：`git config --local core.hooksPath .githooks`。检查读取Git索引内容，不输出匹配到的秘密值，但不能代替人工审查。

私有远程仓库：[2505552499/redfish-workbench](https://github.com/2505552499/redfish-workbench)，主分支为 `main`。需由维护者单独确认协作者账号、权限和分支保护；不要使用带密码/Token的远程URL，不要公开仓库。

## 文档入口

- [架构与信任边界](docs/ARCHITECTURE.md)
- [模块职责与建议分工](docs/MODULES.md)
- [开发、验证与发布](docs/DEVELOPMENT.md)
- [协作与评审规范](CONTRIBUTING.md)
- [设备操作边界](AGENTS.md)
- [安全问题处理](SECURITY.md)
- [迁移说明](docs/MIGRATION.md)
- [G50文档来源及勘误](docs/reference/README.md)

GitHub CI模板只执行离线/回环夹具测试和构建，不使用任何现场secret。推送后的实际运行结果以GitHub Actions为准，不能把本地通过等同于远程CI通过。GitLab/Gitee等平台可复用相同 `scripts/` 命令；没有配置部署或真实设备任务。
