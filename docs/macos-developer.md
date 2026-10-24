# Mac 开发者版

完整监控、内部材料解析、现有分析、Agent 接口及页面共用同一份源码。不是功能精简版，也不是双击即装的 `.app`/`.dmg`。
这批提供安装/启动入口；**macOS 实机验收仍待完成**，Windows 上的离线检查不能替代。

## 1. 准备环境

- macOS：建议 13 或更新版本，原生 Apple Silicon（arm64）或 Intel（x86_64）。两种架构分别验收，不把一种的结果泛化到另一种。
- [Python 3.12 或 3.13](https://www.python.org/downloads/macos/)：选择官方 universal2 安装器，或自己已信任的原生安装。不要用系统 Python，也不要混用 Rosetta。
- [Node.js 22+](https://nodejs.org/en/download)：与 Python 使用同一架构。Git 仅克隆/源码更新需要；下载源码 ZIP 也可安装。
- 不需要 Windows、WSL、Docker、PostgreSQL 服务器或 Apple 开发者证书。开发版需要打开终端。

请获取**包含 `scripts/macos_developer.py` 的这一批源码**。已发布的 `v0.2.0-alpha.1` 不会自动包含尚未发布的改动；维护者确认发布前，用配套私有候选源码包，不要用旧标签按新步骤执行。
把源码解压到自己的开发目录；不要覆盖旧 checkout，不要把数据库或 Key 放入源码。

## 2. 首次安装

终端进入源码根目录（路径有空格时加引号），例如 `cd "$HOME/Developer/ProjectWatch"`。
以下以 Python 3.12 为例；已装 3.13 则把命令里的 `python3.12` 换为 `python3.13`。

```bash
python3.12 -I scripts/macos_developer.py doctor
python3.12 -I scripts/macos_developer.py install
```

`doctor` 只查环境，不下载、不建项目。`install` 会联网安装依赖并构建前端，不启动监控。
完整环境包含 dev、OCR、MCP、team 和 PostgreSQL 客户端；所有分析源码均保留，不安装 Windows 托盘运行器。
只写当前源码内的新 `.venv-macos`、前端构建产物及正常包缓存；不改全局 Python/Node，不调用 `sudo`，不自动换源。

安装失败不标记就绪，保留失败环境供检查。修好网络/依赖后可换新目录重试：

```bash
python3.12 -I scripts/macos_developer.py install --venv .venv-macos-r2
python3.12 -I scripts/macos_developer.py run --venv .venv-macos-r2
```

安装/前端构建前请先正常关闭使用这份源码的实例。源码位置改变后，venv 不可直接搬用；在新 checkout 重新安装。
Git checkout 的安装入口会取得现有源码维护锁，运行中的新版实例会阻止安装；ZIP 无 Git 源码锁，必须人工确认已停机。
不要复制 Windows `.venv` 或 `Scripts/python.exe` 到 Mac。

## 3. 启动与首次配置

默认环境安装完成后：

```bash
chmod u+x Start-ProjectWatch.command
./Start-ProjectWatch.command
```

也可在 Finder 双击该 `.command`；它会打开终端并启动本机网页，而不是 Mac 原生窗口。
如果 Finder/系统阻止执行，先核对来源并使用上面的终端命令；不要关闭 Gatekeeper、关闭杀毒或执行全盘去隔离命令。
默认地址 `http://127.0.0.1:8901/`。被占用时使用 `./Start-ProjectWatch.command run --port 8902`，不会抢占进程。
运行时不自动安装/更新依赖。启动器保留系统代理设置，不修改 VPN。

默认数据目录为 `~/Library/Application Support/ProjectWatch-Developer/<源码路径标识>`；每份 checkout 独立。
终端会显示确切位置。不会扫描、导入或替换 Windows/其他实例数据。
自选数据目录可用 `--home "/绝对路径/私有数据"` 或 `PROJECT_WATCH_HOME`；必须在源码外，显式参数优先。

首次在网页配置自己的一个模型和一个搜索服务，测试连接后输入公司名、确认公司，或上传获准处理的 BP。
模型/搜索/商业账号不随代码提供。测试连接及正常更新可能计费；上传材料可能发送到所配置模型，按自己的授权使用。
启动旧数据时沿用项目暂停状态；未暂停项目可能继续监控。结果不明的付费请求不自动重发。

关闭浏览器不停止监控。回终端 **Ctrl+C**，等待退出，不强杀；Mac 休眠/关机时不保证监控持续在线。
本批不安装登录自启、LaunchAgent 或常驻后台服务。

## 4. 前端热更新、测试与构建

后端运行后，在另一终端（同一源码目录）执行：

```bash
PW_API_TARGET=http://127.0.0.1:8901 npm --prefix frontend run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

访问 `http://127.0.0.1:5174/`；React/CSS 热更新，后端改动需要正常退出再重启开发实例。
后端端口改变时同步改 `PW_API_TARGET`。不要暴露开发服务器到公网。

```bash
.venv-macos/bin/python -m pytest tests -q
node --test tests/*.test.mjs
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
.venv-macos/bin/python -m build --wheel
```

使用 `.venv-macos-r2` 时替换路径。PostgreSQL 测试须专用空 `_test` 数据库；跳过不等于验收通过。
OCR 依赖和部分模型随上游包安装；额外语言/模型可能另行下载。安装成功不代表所有扫描 PDF 都可识别。

## 5. 备份、更新与卸载

正常停止自己的实例后，用自己的路径替换以下占位符：

```bash
.venv-macos/bin/python -m project_watch.studio.host backup --home "/原数据目录" --dest "/全新备份目录"
.venv-macos/bin/python -m project_watch.studio.host verify --backup "/备份目录"
.venv-macos/bin/python -m project_watch.studio.host restore --backup "/备份目录" --dest "/全新恢复目录"
```

备份含凭据，不发群、不入 Git。跨 Windows/Mac 的真实数据迁移尚待验收，先在新目录恢复验证，不直接拿唯一原件试。
不要复制正在使用的 SQLite。恢复后 Agent 授权可能需要重新生成。

沿用[源码安全更新](developer-quickstart.md#软件更新源码安装)：只有干净、无分叉且可快进才自动升级，有本地定制交给 Agent。
设置更新源时使用 `export PROJECT_WATCH_UPDATE_SOURCE=https://github.com/lensnowovo/ProjectWatch-Community`。
当前公开 alpha 标签不在稳定版自动通道；不要把“暂无稳定版本”误解为最新预览已安装。
更新成功后用回执 `candidate.python` 启动新环境，不能继续用旧 `.command`/旧 venv 冒充已切换。

```bash
PROJECT_WATCH_HOME="/原数据目录" "/回执中的candidate.python" -m project_watch.studio --port 8901 --strict-port
```

新环境验证通过前保留旧环境和备份；源码回退不等于数据回退。
移除开发版时先退出进程，再通过 Finder 移走自己的源码和 venv；数据与备份位于源码外，不随移除源码消失。
不提供自动清空数据命令。

## 6. Mac 实机验收清单

请分别记录芯片、macOS/Python/Node 版本，不提供 Key 或真实材料：

- [ ] 新目录 doctor/install 成功，中文/空格路径可用。
- [ ] 首次配置一个模型+一个搜索，名字建项目看到结果；调用须自行明确同意。
- [ ] 合成 BP 可解析；扫描 PDF/OCR 单独测试，记录下载及失败情况。
- [ ] 暂停项目 → Ctrl+C → 重开：原件、配置及暂停状态保留。
- [ ] 同数据双开/端口冲突拒绝启动，不影响原实例。
- [ ] 本地定制时更新提示 Agent，不覆盖修改。
- [ ] 备份校验并恢复到新目录成功；旧数据不变。
- [ ] Finder 入口、浏览器打开、热更新、睡眠恢复行为分别记录。

仅离线单测通过不等于 Mac 实机通过；完整验证后再由维护者决定是否发布。
