# Mac 自带运行时用户版（私有候选）

目标：解压 → 把 `ProjectWatch.app` 拖到“应用程序” → 双击 → 配置一个模型和一个搜索服务。
**使用者不需要安装 Python、Node、Git、pip，也不需要在终端执行安装命令。**
现有网页界面仍在默认浏览器中打开，菜单栏图标负责重开网页、打开资料目录、安全退出。

## 为什么包里仍有 Python

当前后端的材料解析、监控调度、存储及分析由 Python 实现，所以本实现运行时需要解释器。
这不等于必须让使用者安装 Python：解释器、原生依赖和所有分析模块一起放入 `.app`。
前端已经构建为静态文件。DSH 研究引擎需要 Node，当前打包器会把固定 Node 和对应芯片的 DSH 依赖放进 `.app`，不使用系统 Node；npm 仅在维护者构建时使用。
重写成另一种语言并不是解决安装门槛的必要条件。

## 包型与边界

- Apple Silicon（M 系列）：`ProjectWatch-macOS-arm64-candidate.zip`。
- Intel：保留构建代码，首发暂缓；历史 Intel 候选不是本轮正式资产。
- plist 目前声明 macOS 13.0；最终支持下限须经内置运行时及实机共同核对，尚不宣称全部 13+ 已验收。不混用两种芯片的 Python 扩展，不宣称 Universal2 单包。
- 使用已核验哈希的 Astral python-build-standalone CPython 3.12.15，完整功能 wheel、OCR/MCP/team/PostgreSQL 客户端及预构建页面。
- 内置固定 Node v24.13.1、DSH 0.2.1-rc.2，默认启用 DSH；已有显式关闭/自定义路径设置保留。损坏时显示不可用，不静默退回经典流程。当前只完成跨平台组装检查，原生协议与实机验收另行记录。
- 数据独立保存于 `~/Library/Application Support/ProjectWatch-Desktop`；不自动搬入已有开发者实例的数据。
- 删除或替换 `.app` 不删除此目录；替换前从菜单栏正常退出并备份，不能在运行中覆盖应用。
- 保留项目暂停状态及未知付费结果保护。启动时未暂停的项目按已有配置运行，可能调用用户服务。
- 不安装登录自启或隐藏的守护进程。关闭浏览器不停止监控，退出请用菜单栏“退出（等待任务结束）”。
- 内置默认 OCR 依赖与上游随包模型；特殊语言/额外模型和外部 Agent 客户端不保证离线内置。账号及服务额度仍需用户自行提供。

## 当前可验证与不可验证的部分

Windows 可以组装 Mac 目标文件、检查 Mach-O 架构/Python ABI、校验归档哈希和执行位；不能运行 Darwin 二进制。
因此生成 ZIP 不等于 Mac 已通过启动、菜单栏、解析、网络或退出验收。
本批是**未签名、未公证、待 Mac 实机验证的候选**，不是免警告正式安装包。
若系统拦截，请记录提示并停止，交维护者处理；不关闭 Gatekeeper、杀毒或安装不明根证书。
购买签名当前按用户决定推迟；Apple 公证/发行签名与 Windows 签名是不同流程。

## 维护者构建

用独立工作区准备完整 wheel（已有分析和 `web/assets` 不可缺少）。下载 builder 中锁定的上游 runtime，核对 GitHub release 提供的 SHA-256。
构建工具需 Python 3.12、uv、支持 `--os`/`--cpu` 的 npm（本轮 npm 11）；依赖安装只发生在新输出目录，没有修改全局环境。
先按锁文件准备目标 DSH，不可复用 Windows `node_modules`。Node 从 `scripts/build_macos_bundle.py` 指定的官方 URL 下载并核对固定哈希；仅装入执行文件与 LICENSE，不附 npm。

```bash
python -m build --wheel
python -m scripts.prepare_dsh_runtime \
  --target darwin-arm64 \
  --approved packaging/dsh/third-party-approved.json \
  --out /absolute/new/path/dsh-darwin-arm64
python -m scripts.build_macos_bundle \
  --arch arm64 \
  --runtime /absolute/path/python-arm64.tar.gz \
  --wheel /absolute/path/projectwatch_local-0.2.0a1-py3-none-any.whl \
  --dsh /absolute/new/path/dsh-darwin-arm64 \
  --node-archive /absolute/path/node-v24.13.1-darwin-arm64.tar.gz \
  --out /absolute/new/path/arm64-candidate
```

上例 wheel 文件名仅示意，不是最终 RC 版本号；正式候选须从同一冻结源码重建两平台及唯一版本。Intel 若后续恢复，需要 `darwin-x64` DSH、对应 Node/Python 和独立许可/实机验收，本轮不交付。
输出必须全新且在源码外；部分失败产物留作排查，不能当成功包交付。
构建器不下载 Python/Node、不扫描用户 Key/数据库，显式供应归档；npm 准备步骤会下载锁定依赖，uv 仍会解析/下载目标 Python 依赖，最终 RC 的 Python 依赖冻结尚需收口。
Python 本体与第三方原有许可均保留；每个候选记录 runtime/wheel/file 哈希及 `publication_authorized: false`。
审核后发布必须携带对应源码和适用的第三方许可/源码材料，已有许可待核事项不因新包自动关闭。

归档内将 Python 自带的安全相对文件链接实体化，保留可执行权限；没有创建指向电脑外部的符号链接。
没有安装时下载运行环境的后门，也没有自动升级 Python/应用或请求任何模型/搜索 API 的构建步骤。

## Mac 实机验收

首发请在未装 Python/Node 的 Apple Silicon Mac 上验收；Intel 暂缓：

1. 解压并复制到“应用程序”，双击打开。记录系统安全提示，不能假定未签名候选可直接运行。
2. 菜单栏出现图标，浏览器显示首次配置页；系统环境无需添加 Python/Node。
   设置页研究引擎应显示包内 DSH 与 Node 版本；另用合成网关验证真实 DSH 协议，不把 Windows 上的 Mach-O 检查算作已执行。
3. 用自己的账号显式测试连接；确认费用后，名字建项目与合成 BP 上传各一次。真实 BP 须另有披露授权。
4. 暂停项目、保留一份合成原件，从菜单栏退出后重新打开；配置、原件和暂停状态不变。
5. 再次双击只重开现有页面，不开第二个后台任务进程。
6. 验证扫描 PDF/OCR、HTTPS 证书、目录含中文/空格、休眠恢复与浏览器关闭/重开。
7. 关闭应用，验证备份/恢复到新目录及替换 `.app` 后的数据保留；先用合成数据，不动唯一真实副本。

生产版本需要完成实机和分发审查后再批准发布。打包不是测试替代品。

## 资料

- [Python Standalone Builds](https://docs.astral.sh/python-build-standalone/)
- [归档类型说明](https://docs.astral.sh/python-build-standalone/distributions/)
- [pystray 主线程与平台要求](https://pystray.readthedocs.io/en/latest/usage.html)
- [源码开发者入口](macos-developer.md)
- [npm 目标平台配置](https://docs.npmjs.com/cli/v11/using-npm/config/)：`os`/`cpu` 只选择原生依赖，不能在 Windows 上执行 Darwin。
- [固定 Node 官方校验清单](https://nodejs.org/dist/v24.13.1/SHASUMS256.txt)：本轮核验 HTTPS SHA-256，尚未验证其 GPG 签名。
