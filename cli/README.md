# 标准库CLI v1.2.0

入口为 `python -I cli/bmc_tool.py`，发布后为单文件 `python -I bmc_tool.pyz`。Python3.11+，无pip/Web/Node运行依赖。设备读取必须明确 `--live`，写入另需本次许可、`--execute`和TTY一次性确认，不支持管道或--yes。

## 功能与最短用法

```bash
python -I cli/bmc_tool.py doctor
python -I cli/bmc_tool.py targets --targets cli/examples/nodes.csv
python -I cli/bmc_tool.py redfish catalog
python -I cli/bmc_tool.py redfish catalog --module Chassis
```

清单支持TXT/CSV/XLSX及显式IP，识别BMCIP，不误用管理IP/IB0IP。默认不启用原机房节点名映射；只有明确需要时用--site-mapping并先核对targets结果。示例192.0.2.x为文档地址，不是可自动采集的范围。

```bash
# 以下仅是操作用法；替换为本次明确许可清单后再执行。
python -I cli/bmc_tool.py collect --live --targets nodes.csv --memory --username admin
python -I cli/bmc_tool.py collect --live --targets nodes.csv --firmware --username admin
python -I cli/bmc_tool.py redfish run --operation 3.3.1 --targets nodes.csv --live --username admin
```

密码隐藏输入；非交互采集可显式环境变量BMC_USERNAME/BMC_PASSWORD或--env-file。CLI不会自动读取根目录或父目录.env，不提供明文--password参数。默认校验证书，只有显式--insecure才跳过身份验证。

## Redfish三模块

69项文档契约，G50写适配限R6250A0/X7450A0，非硬件全面验收。新模块只用任务级Session，不自动降级Basic；取得Token和可注销URI后才执行业务，结束注销本次会话，失败如实标记。

```bash
# params.json: {"memory_id":"实际集合返回的DIMM ID"}
python -I cli/bmc_tool.py redfish run --operation 3.3.7 --params params.json --targets nodes.csv --live --username admin

# power.json: {"ResetType":"GracefulShutdown"}；以下只是离线计划，不关机。
python -I cli/bmc_tool.py redfish plan --operation 3.3.23 --body power.json --target example=192.0.2.11 --model R6250A0
```

参数只接受声明的字段和ID，不是任意HTTP代理。BIOS密码变更的JSON仅包含PasswordName，旧/新密码用专用隐藏输入；不进入计划或报告。ETag/Action/机型/Off条件不满足时拒绝写入。

G50固件用--image绑定本地SHA256：BMC为.ima、BIOS为.bin/.rom、CPLD为.jed；后缀和SHA不证明兼容。3.7.5仅上传，3.7.6计划包含上传、激活、Task跟踪和实际版本读回。主机须Off，不自动关机。--expected-version可选，未填不声称版本一致。多台变更须先完成实际单台验证并显式--canary-confirmed。未知结果不重发、不续刷、不回滚。

## 保留能力

- `asset import/list/set/export`：本机台账，参考SN与现场SN分开，失败不覆盖历史有效固件。
- `stats --input FILE`：离线统计表或采集results.json。
- `power status/off/force-off/on`：独立电源，写动作默认离线计划，需准确--model。
- `cpld precheck/upgrade/verify`：原Lattice四组DCU适配，不是G50单Cpld目标；.svf/.tar镜像不能与G50混用。
- `demo`：只用合成夹具演练，不连真实设备。

每个命令的完整参数见--help。--state-dir指定台账/锁目录，--output指定新增报告目录。现场报告、节点清单和数据库只能本机保存，不能提交或进入分享包。

## 验证与打包

根目录运行 `python -I scripts/check.py test --suite cli`。单独构建：`python -I cli/tools/build_release.py --output dist/cli-only`。生成的source/_engine是交付副本，不要当作第二个开发核心；继续在根core修改。

退出码：0完成，2参数/环境/确认不满足，3设备失败或待核验，4旧资产部分采集，130取消。完成不等于升级生效或业务恢复。更多安全和协作要求见根README及docs。
