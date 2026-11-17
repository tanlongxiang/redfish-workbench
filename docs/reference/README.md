# 接口依据与本地参考资料

厂商文档：`BMC Redfish接口说明文档-G50.pdf`，355页，封面适用机型R6250A0/X7450A0。SHA256：`01d367040792038f8d19f6fd9403e044f4103327f44039ea9414d2154df60238`。

PDF不提交Git，也不是构建/测试依赖。通过团队批准渠道取得后，可放在根目录 `var/reference/`（已忽略），不要加入docs或强制提交。原工作目录中的PDF保留原处。

已整理目录保存在core/redfish_catalog_data.py：Systems40、Chassis22、UpdateService7。PDF阅读器页码和正文印刷页码通常相差2页，目录数据记录阅读器页码。

重点章节：SessionService3.1；Systems3.3；Chassis3.4；UpdateService3.7；TaskService3.8作为升级内部依赖。

已知注意：Self/实例ID混用；RAIDStorage_id为需实际发现的日志资源；NetworkAdapters参数拼写有差异；字段单位/类型有疑似笔误。不要照抄固定ID或让文档覆盖现场响应。所有能力仍须区分文档适配、夹具验证和实际设备验证。

需要重新提取目录时，在本机把PDF转成文本后运行 `python -I cli/tools/build_redfish_catalog.py var/reference/G50.txt var/reference/catalog-new.py`，审查差异再更新受控目录；不要直接覆盖源文件或把材料写进Git。
