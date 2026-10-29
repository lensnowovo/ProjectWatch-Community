# Windows 候选安全复核

## 2026-10-29：rc3 当前输入与边界

本轮最小修补固定 pypdf 6.21.2、Undici 8.12.2，并将五十个嵌套 Zod 4.4.3 去重到既有 4.6.7；没有跨 Zod 主版本。Python 3.13.17、Node 24.22.1、DSH 0.2.1-rc.2 主体版本不变。当前固定 Python 依赖及两套 npm 锁的审计无已知漏洞报告；该结论绑定输入和检查日期，不保证不存在未知问题。

R2 二进制扫描及多项本地检查通过后，完整门禁仍发现旧库升级失败，因此 R2 不交付。修复后的新资产须有自己的安装态迁移、文件清单和二进制扫描回执。已过且未受影响的协议/前端检查可以注明来源复用，不能冒称新资产已完成全部验证。

psycopg-binary 3.3.6 Windows wheel 的同版本 Psycopg、PostgreSQL 18.4、OpenSSL 3.6.4 源码和许可已准备；缺的是该精确 wheel 的 vcpkg port/补丁、工具链及打包构建记录。2026-10-29 核对[官方 PyPI](https://pypi.org/project/psycopg-binary/3.3.6/#files)仍无 provenance metadata。这是构建来源/可复现性局限，不是新增漏洞或自动的许可失败结论；不宣称逐字节可复现，源码附件和原始许可仍须随发行提供。

下文为 2026-10-23 历史复核。旧扫描、旧候选的 No-Go 或批准均不能替代本次精确资产裁决。

更新：2026-10-23。范围是隔离 Windows 候选；现用实例未升级。**总体仍为 No-Go**，本文不是发布授权，也不保证不存在未知漏洞。Mac 验收已按维护者要求暂缓。

## 依赖发现与处理

| 项目 | 原输入与发现 | 处置与边界 |
| --- | --- | --- |
| Pillow | 12.3.0 命中 35 条安全公告，包括图片解码和内存安全问题；PDF/Office/图片链路可能加载它 | 升至 13.0.0；基础依赖也设安全下限，不能只修 desktop extra。没有执行危险漏洞样本 |
| certifi | 扫描器把 2026.10.18 报为受影响，建议 2026.10.19；上游公告却已把 18 列为修复版 | 保留这个元数据差异，不直接认定 18 仍有漏洞。统一使用 19，并复扫 |
| Vite | 6.4.3 的开发服务器命中安全公告 | 固定 6.4.5。此为开发/构建工具风险，不把它说成产品内置的服务端 |
| DSH MCP client | 2.2.0 的跨源重定向可能泄露请求头和请求体 | 显式 override 到 2.3.0，core 随同升级；DSH 本身仍固定 0.2.1-rc.2，重新跑真实协议测试 |
| fflate | LibreOffice 子树的 0.8.2 命中 ZIP64 无限循环问题 | override 到 0.8.3。LibreOffice 已从桌面运行时裁剪，这条不是已证实的桌面可达漏洞 |
| Node.js | 原 Windows 内置 24.13.1 未含 10 月 21 日安全发布 | 固定 24.22.1 LTS，校验官方 SHA-256，重新验证 DSH。没有替换本机全局 Node |
| Python | 原 Windows 内置 3.13.16 | 固定同系列的 3.13.17 特别安全版，不跨 Python ABI；该版主要更新 pip/certifi。产品嵌入包本来不附 pip，不把这项描述成已复现的产品漏洞 |

npm 初扫的 12 项 DSH 结果包含传递依赖传播，不能解释成 12 个独立漏洞。复扫对完整锁文件进行，包括最终裁剪掉的组件。Python 扫描依据 70 项完整 Windows wheel 依赖清单，不扫描用户配置，不把私有产品名提交到公共漏洞服务。

截至本次记录，两套 npm 锁文件以及修补后 70 项 Python 依赖均为 **无已知漏洞报告**。这只代表当日数据库与这些固定版本的匹配结果；二进制内嵌库、应用逻辑、权限与秘密扫描仍有独立验收门槛。

## 复现与证据要求

- 安装使用逐文件 SHA-256 锁定的离线 wheelhouse，不在用户安装时执行依赖解析或 `npm install`。
- 新候选为 `0.2.0rc2`；RC1 保留为历史证据，不替换其文件，不继承为 RC2 全量通过。
- 前端依赖修补后：296 项测试、类型检查与生产构建通过；产物未出现业务代码变化。
- Pillow 修补后的相关 Python：136 通过 / 5 跳过；跳过的是未提供本地 Docker 解析镜像，不能计通过。随后运行时锁契约 65 通过；两组有重叠，不合计。
- DSH 新锁重新安装、裁剪和生成许可材料。已有 15 项补件记录与上版逐项相同，沿用仓库已批准清单，未新增批准。
- MCP client/core 2.3.0 附完整上游许可文件，包含许可迁移说明、Apache-2.0、保留的 MIT 和文档 CC-BY-4.0 说明；原文留在分发 notices，不以单一包元数据抹去旧版权。
- 新 Node / Python 的实际安装态、最终安装生命周期、全量回归和完整许可审查仍须在唯一看板记录结果。

## 仍需完成

1. 固定安装包的原生组件、模型文件、完整 notices 和文件清单核对。
2. 秘密扫描及应用边界回归（本机访问、上传、SSRF、导出脱敏、unknown 不自动重试）；扫描器告警逐项归因，不静默加入豁免。
3. 未签名 Windows 包的下载/首次启动提示与真实用户验收；无证书不冒称已签名。
4. 发布前再次核对公告数据库与最终候选输入；不是每次自动更新所有依赖。

分发用途与仍需确认的边界另见[第三方分发范围](release-third-party-scope.md)：
完整应用与独立组件再分发要区分，Psycopg Windows 对应源码/port 的未知项不能
用版本号猜测补齐。该记录不代替最终资产核验或维护者发布确认。

## 上游依据

- [Pillow 13.0.0 安全与兼容性说明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)
- [certifi 上游公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
- [MCP HTTP 重定向公告](https://github.com/modelcontextprotocol/typescript-sdk/security/advisories/GHSA-6prh-2h8m-c8cw)
- [Vite / launch-editor 公告](https://github.com/advisories/GHSA-587h-7wmj-h5pm)
- [fflate ZIP64 公告](https://github.com/advisories/GHSA-px8p-9vwx-vf98)
- [Node 2026-10-21 安全发布](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases) / [24.22.1](https://nodejs.org/en/blog/release/v24.22.1)
- [Python 3.13.17](https://www.python.org/downloads/release/python-31317/)
