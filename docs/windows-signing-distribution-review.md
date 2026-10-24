# Windows 签名与分发审查 — 2026-10-19

> 历史候选记录，不代表当前 RC2 通过。2026-10-23 的实际依赖修补、包清单与待验项见
> [安全复核](release-security-review.md)和[发布准备状态](release-readiness.md)。
> 沿用维护者暂不购买签名的决定；签名不是本轮采购前提，但必须披露未签名状态、
> 提供固定哈希并实测正常系统防护下的可接受安装路径，不能诱导关闭安全防护。

范围：候选 WU-20261019-r2，完整安装器与 bundle-r6。工程核查不等于法律意见或公开发布授权。

## 签名

本机 CurrentUser/My 与 LocalMachine/My 均未找到代码签名证书；Windows SDK SignTool 可用。
安装器与自有启动器目前未签名。未建立自签根证书，未购买或调用收费签名服务。
内置 Python 等供应商二进制的签名不等于 ProjectWatch 已签名。

取得可用签名身份后，顺序必须是：

1. 冻结候选源码、依赖和版本号。
2. 签名自有 ProjectWatch.exe（SHA-256，可信 RFC3161 时间戳），验证证书链、发布者和时间戳。
3. 用签名后的启动器重新生成 bundle 文件哈希，不得沿用旧清单。
4. 在 NSIS 的卸载器构建阶段签名 Uninstall.exe，再嵌入安装器。不能只签最终外壳。
5. 签名最终安装器，再生成 SHA-256 与构建回执，重新跑安装/升级/卸载验收。
6. 有效签名与 SmartScreen 无提示是不同条件；两者分别记录，发布前由维护者批准。

此顺序是待证书就绪后的执行清单，不宣称已完成签名流水线验收。
个人签名通常需要真实身份核验，公开昵称 qzqzqz 不等于证书主体。
微软 Artifact Signing 当前地区资格有限，不能假定中国个人可申请。证书采购及账号注册另行确认。

依据：[微软签名选项](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options)、
[SmartScreen 声誉说明](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)。

## 发行清单与边界

- 从实际内置运行时读取依赖版本、许可声明、许可文件哈希，而不是读取开发者 venv。脚本 `scripts/audit_windows_distribution.py` 离线核验清单及额外文件，发现不在清单内的文件即失败。
- 完整保留应用、解析、已有分析、OCR/MCP/团队/PostgreSQL 可选依赖。没有以审核为理由裁剪功能。
- Python PSF、NSIS COPYING、字体 OFL、前端第三方通知及各 Python 包原许可继续适用。自有社区许可不能覆盖第三方权利。
- React Bits 继续保留现有通知及限制；本次未取得新的上游授权，不宣称组件独立再分发或源码再分发边界已获得额外许可。
- pystray 0.19.5、psycopg 3.3.6、psycopg-binary 3.3.6、psycopg-pool 3.3.3 的 LGPL 义务需单列，不因属于可选功能而消失。
- pystray 使用未冻结的 Python 文件；Psycopg 亦从 site-packages 加载。启动不做逐文件签名白名单验证。安装时完整性校验、升级/卸载对定制文件的保留，不应被用来剥夺库替换/调试权利。
- 与 LGPL 库修改有关的替换和调试权利按原许可保留。关闭应用、备份数据后，可在活动版本的 runtime/Lib/site-packages 中替换接口兼容库；二进制扩展需匹配 CPython 3.13 x64 及原生依赖。维护安装遇到定制必须停止交由维护者处理，而不是覆盖。
- 已取得版本对应的 pystray 上游源码、psycopg 与 psycopg-pool sdist，哈希列入私有验收包。psycopg-binary 原生构建、libpq/加密库等的完整对应源码与构建来源仍须补齐；这不是“已满足所有 LGPL 要求”的结论。
- 实物盘点为 71 个 Python 分发包、6282 个清单文件。RapidOCR/FlatBuffers 的 dist-info 声明了 Apache 许可，但没有随 wheel 携带独立许可文本，已从各自上游版本补到验收包的 third-party-sources；公开安装器内也须随最终构建补齐。
- 更正此前“权重未预置”的描述：本候选含 RapidOCR 三份中文默认模型，另有 ONNX Runtime 三份示例模型；pywin32.pth 是 Python 路径文件，不是权重。安装后运行时以禁止 Python 网络连接的方式完成合成图片识别，结果为 ProjectWatch Revenue 2026。不同语言/可视化字体仍可能触发下载，模型出处和分发权利继续单独核查。

依据：[pystray 原许可](https://github.com/moses-palmer/pystray/blob/v0.19.5/COPYING.LGPL)、
[Psycopg 安装与二进制边界](https://www.psycopg.org/psycopg3/docs/basic/install.html)。

## 安全核查

候选安装器 SHA-256：`3b464720a350b45c7c82812647c48317bef0249c5f978640297df05350719fd3`。
上一批 128 项回归、10 项真实安装生命周期检查通过；本批增加发行清单测试。
本机 Defender 用原生 Windows 路径进行自定义只扫描（DisableRemediation），返回 0、found no threats。
第一次使用正斜杠路径返回 0x80508023，不作为通过证据；改为原生路径后重新扫描成功。
单引擎扫描不保证无恶意代码，不替代依赖漏洞扫描、签名验证或新电脑的 SmartScreen 检查。
未上传二进制到公开多引擎分析网站，未修改系统防护设置。

本批没有修改安装器二进制或已验证运行时；补充材料作为私有验收包附件交付，不能把这些附件当成已嵌入未来公开安装器。

## 公开发布前仍需闭合

可信签名身份与全链签名验收；原生依赖与 LGPL 对应源码材料；OCR 下载/分发边界；依赖漏洞扫描；干净 Windows 使用者反馈；维护者发布批准。
未闭合事项不阻止同一维护者在自己的干净电脑上私下验收，但不得据此宣布已获公开分发全面许可。
