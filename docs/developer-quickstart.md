# 完整开发者版：本机开发与构建

当前为完整开发者预览版，保留已有分析能力。
机构内部自用免费，对外商业服务须授权；具体条款见根目录 LICENSE，第三方许可见 NOTICE。
模型、搜索和商业来源均由使用者自行配置；仓库不带账号或项目材料。

## 环境与安装

建议 Windows x64、Python 3.12、Node.js 22+、Git。以下命令在仓库根运行。
不要复用另一个工作树的环境，也不要在旧环境里同时安装新旧产品包。

```powershell
py -3.12 -m venv .venv-developer
.\.venv-developer\Scripts\python.exe -m pip install --upgrade "pip>=26.2"
.\.venv-developer\Scripts\python.exe -m pip install -e ".[dev,team,mcp,postgres]" build ruff
npm --prefix frontend ci
npm --prefix frontend run build
```

产品分发名为 `projectwatch-local`，Python 导入名仍为 `project_watch`。
该名称用于本地构建，不代表已在公共包索引注册；不要从未知索引安装同名包。
普通安装已包含监控、分析和所有现有页面，不需要另购或安装私有分析包。
上述开发环境包含全量测试需要的 team/MCP/PostgreSQL 客户端依赖，不会自动启动服务器。
只运行个人版可仅装 `.[dev]`，但不能据此运行所有可选模块测试。OCR 按需要追加：

```powershell
.\.venv-developer\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.\.venv-developer\Scripts\python.exe -m pip install -e ".[dev,mcp]"
```

OCR 首次运行可能下载模型；MCP 的依赖安装成功不等于外部客户端已完成授权。
`team`、`postgres` 是可选兼容路线，个人 SQLite 开发不需要数据库服务器或 WSL。

## 空数据启动

```powershell
$env:PROJECT_WATCH_HOME = Join-Path $env:LOCALAPPDATA 'ProjectWatch-developer-sandbox'
.\.venv-developer\Scripts\python.exe -m project_watch.studio --port 8901 --strict-port --no-browser
```

打开 `http://127.0.0.1:8901/`。指定未被使用的端口和独立空目录；
不要为开发关闭别人的进程。没有 Key 可以看空界面，不能据此宣称真实搜索和模型分析已通过。
配置真实服务后，更新可能计费；私有材料外发须符合使用者自己的授权。

## 前端热更新

后端保持上一步运行，另开终端：

```powershell
$env:PW_API_TARGET = 'http://127.0.0.1:8901'
npm --prefix frontend run dev -- --port 5174
```

打开 `http://127.0.0.1:5174/`。React/CSS 修改由 Vite 热更新；
后端修改需重启开发实例。不要把开发服务器暴露到公网。
前端构建写入 `src/project_watch/web/`，wheel 使用这些产物。

## 测试与完整构建

```powershell
.\.venv-developer\Scripts\python.exe -X utf8 -m pytest tests -q
node --test tests/*.test.mjs
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
.\.venv-developer\Scripts\python.exe -m build --wheel
```

不要把排除 Insight 的 Core/Studio probe 当作完整开发者发行包。
独立安装测试必须另建 venv，从 `dist` 中本次构建的确切 wheel 安装并执行
`tests/installed_wheel_smoke.py`；不要从源码导入冒充安装成功。
PostgreSQL 集成测试只有配置专用空 `_test` 数据库才运行；跳过须记录。
合成测试不消耗真实模型额度，不代替真实内容质量验收。

## 从旧包迁移

旧 `greenpine-project-watch` 和新包写入同一个 Python 导入目录，**不能混装**。
先停止自己管理的实例、按备份指南生成并验证备份，再在新的 venv 安装新 wheel。
保留旧 venv 供回滚，不对共享环境执行卸载/覆盖；新环境成功后才能指向原数据。
双击启动器使用独立的 `venv-projectwatch-local`，保留旧 `venv` 和数据目录。
本机测试先用合成副本演练升级、回滚，不能直接拿唯一真实数据试迁移。
备份包含密钥，不能提交 Git、发群或随源码交付。

## 备份与恢复命令

先停止同数据目录实例，使用自己的绝对路径，并选择尚不存在的备份/恢复目标：

```powershell
.\.venv-developer\Scripts\python.exe -m project_watch.studio.host backup --home <data-directory> --dest <new-backup-directory>
.\.venv-developer\Scripts\python.exe -m project_watch.studio.host verify --backup <backup-directory>
.\.venv-developer\Scripts\python.exe -m project_watch.studio.host restore --backup <backup-directory> --dest <new-restore-directory>
```

尖括号内容须替换后再执行，不直接粘贴为命令。恢复到新路径时实例绑定会变化，旧 Agent 凭据需重新授权。
可选托管模式为 `python -m project_watch.studio.host run --home <data-directory> --port <free-port>`，
会启用正常后台任务和已有费用配置；不要与普通实例并行使用同一目录。
托管器仅恢复自己拥有的进程，失败恢复有界；休眠/关机期间不保证监控在线。

## 软件更新（源码安装）

设置 → 更新与网络 → **检查软件更新**。进入页面只读取本地状态，不自动联网或安装。
使用者可在启动前设置公开更新源（不要在 URL 中填写令牌）：

```powershell
$env:PROJECT_WATCH_UPDATE_SOURCE = 'https://github.com/lensnowovo/ProjectWatch-Community'
```

未设置时显示“尚未配置正式更新源”，不会猜测私有 origin。
更新器仅检查 `vX.Y.Z` 稳定标签；本次 `v0.2.0-alpha.1` 不进入自动升级通道。
仅有预览标签时会报告暂无稳定版本；预览版更新请下载独立包或交给 Agent 处理。
预发布、无标签、失败或非 Git 安装不会误报“已是最新”。

- 有未提交修改、未跟踪文件、独有提交、分叉、浅克隆、分离 HEAD、特殊索引或正在合并：交给 Agent。
- 只有当前提交是新版祖先、工作区干净且安全检查通过才允许快进；不 reset、不自动 stash、不覆盖忽略文件。
- “复制 Agent 交接说明”不包含远端凭据、私有文件内容或项目数据；包含版本/提交和安全要求。
- 下载 ZIP/安装 wheel 的非 Git 用户走独立环境安装流程，不直接解压覆盖。
- 初次干净发布可以新建根提交；**后续版本必须沿用该仓库历史**，不能每个版本重建根提交。

正常关闭使用同一份源码的实例及其托管器（不影响其他源码目录的服务），保留项目原暂停状态，关闭源码编辑器的自动修改。
使用原运行环境在仓库根检查，并在明确同意执行目标版本代码、下载构建依赖后升级：

```powershell
python -m project_watch.studio.updates check --repo .
python -m project_watch.studio.updates apply --repo . --home "<现有数据目录>" --stage "<全新升级目录>" --current <检查返回的完整current_commit> --target <检查返回的完整target_commit> --confirm
```

占位符必须替换。源码、数据、升级目录必须互不嵌套；升级目录含密钥备份，不放进 Git 或公开网盘。
新版源码实例持有共享运行锁，升级持有排他源码锁及数据实例锁；旧版实例没有源码锁，首次升级须人工确认已全部关闭。
工具备份并校验完整数据目录，在新环境构建前端/wheel、检查依赖，
用备份副本做迁移和无后台任务的启动验证。副本中的暂停状态、配置、未知付费回执变化会阻断升级。
隔离目录不是恶意代码安全沙箱，只能配置可信发布源；此检查不替代发布审查或代码签名。
安装依赖可能访问包仓库；不会主动调用模型、搜索和商业数据接口。

成功回执为 `source_updated`，表示源码已快进，**不表示原服务已切换或重启**。
`receipt.json` 的 `candidate.python` 是新环境解释器，`home` 是原数据目录；请用它显式启动：

```powershell
$env:PROJECT_WATCH_HOME = '<回执中的home>'
& '<回执中的candidate.python>' -m project_watch.studio --port <空闲端口> --strict-port --no-browser
```

此时按原项目状态恢复正常运行，未暂停项目可能继续正常调用。不要误用旧启动器/旧 venv 冒充已切换。
升级失败保留原环境、备份及私有日志，不自动重试或强制回退。中断回执、残留维护锁交给 Agent 检查。
恢复须先停机，用 `studio.host restore` 将备份还原到**新目录**，配合旧环境验证后再决定切换；
已经迁移的数据不能仅靠回退 Git 来恢复。实际首启/第三方账号可用性仍需使用者验收。

## Agent 接入

Studio 设置页面可生成项目级接入授权。需要现有 Codex CLI 时，可在仓库根运行
`python scripts/connect_codex.py --url http://127.0.0.1:8901 --project <project-id> --directory <private-connections-directory>`。
连接文件含凭据，目标目录必须位于源码外；这会配置本机客户端，但不主动执行付费模型/搜索任务。
接入后按项目版本和幂等契约更新，不允许 Agent 直接编辑私有 SQLite。
使用者可撤销授权，撤销后的访问应被拒绝。真实外部客户端登录与权限需另行确认。
