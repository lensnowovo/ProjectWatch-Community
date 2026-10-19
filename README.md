# ProjectWatch

个人可自部署的 PE/VC 项目监控平台：上传内部材料，跟踪公开动态，整理公司、竞品、技术路线和行业环境，以来源和证据支持后续判断。

维护者：**qzqzqz**。当前版本为 **v0.2.0-alpha.1 开发者预览版**。
已有分析实现、提示词、规则与页面全部保留，不依赖另一个私有分析包。

[下载预览版](https://github.com/lensnowovo/ProjectWatch-Community/releases/tag/v0.2.0-alpha.1) · [版本说明](docs/release-notes-v0.2.0-alpha.1.md)

## 使用链路

配置自己的模型与搜索服务 → 输入公司名或 BP → 确认公司 → 查看内部资料、公开动态和分析 → 调整监控或通过 Agent 追问 → 持续更新与备份。

- 内部：解析 BP、经营更新和财务材料，保留原件、来源、期间和版本。
- 外部：公司动态、竞品、行业、政策，以及可用的论文、专利和注册线索。
- 分析：竞争位置、技术路线、行业环境、问题证据和内外对照；缺失信息不填造。
- 接入：自备模型/搜索配置、可选商业来源、API/MCP 与外部 Agent 授权写回。
- 运行：任务与用量回执、暂停/恢复、未知付费结果保护、备份与恢复。

完整功能不代表所有场景都已生产验收。服务覆盖、材料质量和授权范围影响结果；商业数据账号和调用费用不随代码提供。没有 Key 时可看界面，不能进行真实模型分析。

## 开始开发

推荐 Windows x64、Python 3.12、Node.js 22+。先克隆仓库，再在源码根目录运行：

```powershell
git clone https://github.com/lensnowovo/ProjectWatch-Community.git
cd ProjectWatch-Community
py -3.12 -m venv .venv-developer
.\.venv-developer\Scripts\python.exe -m pip install --upgrade "pip>=26.2"
.\.venv-developer\Scripts\python.exe -m pip install -e ".[dev,team,mcp,postgres]" build ruff
npm --prefix frontend ci
npm --prefix frontend run build
$env:PROJECT_WATCH_HOME = Join-Path $env:LOCALAPPDATA 'ProjectWatch-developer-sandbox'
.\.venv-developer\Scripts\python.exe -m project_watch.studio --port 8901 --strict-port --no-browser
```

打开 `http://127.0.0.1:8901/`。使用独立数据目录，不与已有实例共用；端口占用请换端口，不要停止不相关的服务。本机服务不能直接暴露公网。

完整步骤及前端热更新见[开发指南](docs/developer-quickstart.md)。
双击 `Start-ProjectWatch.cmd` 可使用本机启动器；首次需安装依赖，默认包含可选 OCR。
前端热更新使用 Vite，生产页面由 `npm --prefix frontend run build` 写入 Python 包。

## 验证与构建

```powershell
.\.venv-developer\Scripts\python.exe -X utf8 -m pytest tests -q
node --test tests/*.test.mjs
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
.\.venv-developer\Scripts\python.exe -m build --wheel
```

新技术包名 `projectwatch-local`，导入名 `project_watch`。
这不代表同名公共包索引登记；使用本次源码或已核验哈希的 wheel。
与旧 `greenpine-project-watch` 不能混装，升级另建环境并验证备份，原数据不随 Git 分发。
可选 OCR/MCP 和独立安装验证见开发指南；PostgreSQL 测试须专用空 `_test` 数据库。

## 文档

- [架构与目录](docs/developer-architecture.md)
- [已知边界](docs/developer-known-limitations.md)
- [参与开发](docs/contributing.md)
- [安全问题反馈](docs/security-reporting.md)

## 许可与隐私

目标为完整源码可用（source-available）：机构内部自用免费，仅对外商业服务需取得维护者授权，不宣传为 OSI 开源。
本版本的自有代码采用 [ProjectWatch 社区使用许可 1.0](LICENSE)，商业授权联系：**lensnowovo@gmail.com**。
自身基金的投委会/LP 报告，以及客户控制实例下的纯安装、培训、修复和定制劳务，不因收费而自动要求软件商业授权；
对外经营托管、API、白标或监控/分析订阅服务须事先书面授权。免费 fork 和修改版分发须保留许可与通知，
分发修改二进制时提供对应源码；仅内部使用的修改无需公开。具体范围以 LICENSE 为准。

第三方组件、字体及依赖继续适用各自许可，详见 [NOTICE](NOTICE)。[历史 Apache 文本](LICENSES/Apache-2.0.txt)
保留既有授权，不把本版本新增或改许可的部分重新授权为 Apache。已有授权不会因新仓库而追溯撤回。
本次为开发者预览，不是稳定生产版本。使用和再分发时须同时遵守第三方原有许可。

Key、真实 BP、商业返回、数据库、日志和备份必须保存在源码外。配置外部模型前确认材料披露范围，
第三方服务的使用与转售权利需自行取得，软件许可不替代数据服务合同。
