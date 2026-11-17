# 开发、测试与发布

## 环境与启动

推荐Python3.12、Node22.23.2，最低Python3.11/Node22.12。`.python-version`、`.nvmrc`只是版本提示，不自动安装运行时。后端依赖精确固定在web/requirements.txt，测试依赖在web/requirements-test.txt，前端依赖由package-lock.json锁定。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm --prefix web/frontend ci --no-audit --no-fund
python -I scripts/check.py doctor
python -I scripts/check.py build-web
python -I web/start_web.py
```

Windows激活 `.venv\Scripts\activate`，可运行web/启动Web工作台.bat；Windows原生未验收。不要提交虚拟环境、node_modules或dist。构建不需要厂商PDF、现场表格或.env。

本机修改前端后执行build-web；也可用 `npm --prefix web/frontend run build -- --watch`。后端仍使用正常本机入口，避免用不同Origin绕过既有安全检查。若旧服务仍有活动任务，先核对结果，不要为了重载源码强杀烧写。

## 验证层级

```bash
python -I scripts/check.py test                    # 全部：核心、CLI、Web
python -I scripts/check.py test --suite core
python -I scripts/check.py test --suite cli
python -I scripts/check.py test --suite web
python -I scripts/check.py browser                 # 新Redfish+正常入口
python -I scripts/check.py browser --legacy        # 另加旧工作台完整流程
```

测试统一清除继承的BMC_*凭据环境，CLI测试显式选择当前源码。OpenSSL为HTTPS夹具所需；不提供真实设备测试开关。浏览器默认 `/usr/bin/google-chrome`，可通过 `REDFISH_CHROMIUM` 指定本机可用可执行文件。截图/测试日志写var/artifacts/browser，不入Git。

若开发环境与测试解释器不同，可用 `python -I scripts/check.py --python /path/to/python test`；所有源码仍来自本仓库，不借用旧目录。完整构建还需要前端依赖。

原始迁移基线包含259个测试；其中两项依赖现场工作簿的历史断言改成合成工作簿测试，不能把CI通过当现场表已核验。每次实际结果以命令输出为准；不得硬编码“所有测试通过”的发布声明。

## 安全检查与Git

```bash
git status --short
git add <本次已审查的源码和文档>
python -I scripts/security_check.py
git diff --cached --stat
# 需要看具体变更时在本机审查 git diff --cached，勿外发秘密
```

检查读取索引里的blob，所以能发现“暂存了秘密后又只改工作区”的情况。它拦截私钥、部分已知Token格式、非空.env.example和不应入库的文件，但不是完整秘密扫描。现场URL、日志和误贴普通密码仍需要人工审查；禁止`git add -f`绕过。

可选 `git config --local core.hooksPath .githooks` 启用pre-commit。GitHub Actions模板可直接跑相同脚本；其他平台迁移命令即可。本地设置不等于远程分支保护、审批或CI已经启用。

## 发布

发布从审核后的Git索引读取源码，要求工作区与索引一致，并只额外纳入已编译前端和新生成的CLI。输出目录必须在dist/下且为空。

```bash
python -I scripts/check.py build-web
git add <经过审查的变更>
python -I scripts/release.py --output dist/dev-001
python -I scripts/check_release.py dist/dev-001
```

发布包包含web/cli/core/docs及单文件bmc_tool.pyz；不包含.git、.env、var、节点清单、数据库、日志、镜像或厂商PDF。MANIFEST按内容校验，build-info只说明源树与构建，不伪造新的硬件验证。包不入Git，后续通过团队批准的私有Release/制品库分发。

`cli/tools/build_release.py --output dist/cli-only` 可独立生成标准库CLI；不依赖Web、前端或现场目录。重建会拒绝覆盖已有非空输出。

## 团队接入远程

私有远程已按维护者确认建立为 `https://github.com/2505552499/redfish-workbench`，origin使用不带凭据的HTTPS地址，主分支为main。获邀同事可运行 `git clone https://github.com/2505552499/redfish-workbench.git`。协作者邀请、权限和分支保护尚需单独确认，本次建仓不自动邀请成员。建议main受保护、功能分支PR/MR合并，涉及写入和凭据至少另一人复核。网络需要代理时仅在本机配置或限定本次命令，不把私人代理、密码或Token写进仓库。

## 迁移时的依赖来源核对

原前端锁文件的103个resolved地址来自镜像站，已规范化为registry.npmjs.org；版本和integrity均未改变。原始离线安装曾被本机npm策略拒绝，未修改全局策略。规范化后在独立临时目录使用本机缓存全新离线安装通过；前端构建和三个浏览器流程也通过。团队内网镜像的访问及Windows环境仍应按实际环境验证，不把当前机器缓存可用当作其他机器网络已配置。
