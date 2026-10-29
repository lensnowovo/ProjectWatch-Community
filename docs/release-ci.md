# 发布回归 CI（当前先收尾 Windows）

状态：既有 Windows 工作流已获授权并在 GitHub 执行。本文说明新的按域复用规则；规则修改仍须通过独立契约及实际远端验收，不把旧作业正在运行或本地契约通过写成新工作流已通过。Mac 作业仍未启用；CI 不发布或更新用户实例。

## 工作流边界

`.github/workflows/ci.yml` 使用 JSON 形式的 YAML，触发于普通 PR、main push 或手动运行。所有执行作业只有 `contents: read`；仅规划作业另有 `actions: read`，用于读取已有验证证据。调用它的候选工作流在 regression 调用处明确授予这两个只读权限，其他作业不提升权限。checkout 不保存 Git 凭据，不使用 `pull_request_target`、仓库密钥或写权限。

| 作业 | 覆盖 |
| --- | --- |
| 每次规划与契约（15 分钟） | 全仓 Ruff、CI 与文档契约、输入分类、成功证据核验；不能复用跳过 |
| Python（180 分钟） | 主 Python 集合及分层；明确排除已由规划/前端作业承担的整文件 |
| 前端与 Node（45 分钟） | npm ci、测试、类型、构建、根 Node 测试，以及实际读取 UI 资产的 Python 交叉契约 |
| Native DSH（60 分钟） | 当前锁对应官方 runtime、原完整 native 代表节点与合成模型网关 |
| Installed wheel（45 分钟） | 独立构建前端和唯一完整 wheel、新 venv、基本 smoke 与完整合成旅程 |
| 必过汇总（5 分钟） | 保留 `windows-2022 / Python 3.13` 检查名；所有域本次成功或有可核验的原始成功证据才通过 |
| macOS 15 arm64 / Python 3.12（预备方案，暂未启用） | 保留脚本兼容能力，恢复 Mac 工作后加入矩阵并验证；不称为 Finder/菜单栏验收 |

平台按 [GitHub runner 官方说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) 选择，并在作业开始检查实际 CPU；若标签解析为错误架构直接失败。使用固定 Node v24.22.1、仓库前端锁与 DSH 锁。CI 的 Python 声明范围依赖仍在解析，**不是最终发布依赖冻结**；Windows bundle 则使用单独的完整 wheel 清单与哈希锁。

## 未变化输入如何复用

PR 规划针对每个域的完整 Git 输入生成指纹，包含路径、文件模式、blob 以及真实执行命令、环境、OS、Python/Node 和依赖安装语义。比较的是实际检出的完整树；不以累计 PR-base diff 或未经成功验证的上一提交充当基线。删除、重命名和 base 合并结果都能改变输入。两个相同的代码树可以复用；PR 的 `github.sha` 可能是合并测试提交，不一定等于分支 head。

只查询有限数量的同 PR 历史运行及同仓库目标 main/base 分支证据。普通 main push 也能复用：除同仓 main 证据外，刚合并的 PR 必须经 GitHub 确认已合并、base 仓库/ref 正确、merge commit 精确等于本次 SHA，才能引用该 PR 的原始执行；不泛查祖先或其他未合并 PR。来源必须已结束，目标域 job 和必要步骤必须实际成功，run/job/attempt、checkout SHA/tree、远端 workflow 与重算输入指纹必须对应。其他域失败不抹掉已完成域的真实成功；取消、缺失、过期、畸形或读取失败的证据不能放行。证据保存 7 天，超过期限重新执行。该期限只表示工程输入验证的新鲜度，不承诺未锁定的外部依赖范围永远解析为同一包。

复用仅引用原始执行的 run/job/attempt；跳过的 job 不生成新的成功 proof，不建立无限继承链。汇总清楚区分 executed 与 reused。应执行却失败/取消/跳过的域，或规划契约失败，都会使必过汇总失败。

- 普通 docs、README、CHANGELOG 可免重复产品回归；文档链接、支持说明及模板契约仍每次执行。LICENSE、NOTICE、打包元数据不属于这个豁免。
- 前端/静态资源变化触发前端及安装态。实际读取 UI 的 Python 整文件也由前端作业在构建后运行；清单见 [ci_scope.py](../scripts/ci_scope.py) 的 `FRONTEND_TESTS`。主 Python 命令显式排除这些整文件，不把未执行的跨域契约算成“已复用全量”。共享的 `test_studio.py` 辅助函数仍进入其他域输入。
- 产品 Python、共享测试夹具、全局依赖及未知路径采用保守多域/全域分类。DSH 角色脚本同时影响 native、Node 和安装行为，不按文件扩展名简单当成网页资源。
- 仅调整 CI 分组、显示名、作业时间限额不会机械触发产品回归；执行命令、环境、依赖或测试选择变化会使对应证明失效。CI 逻辑自身契约始终执行。
- 没有可靠证明时执行该域。手动运行及发布候选采用完整验证；候选调用明确设置 `force_full: true`，不依赖复用结果。PR 和普通 main push 可以按域复用。

有一个严格限定的旧流程桥接：只接受已审核的旧 workflow blob、固定 run/job/attempt 和实际 checkout SHA/tree。旧单作业终态后，只有该域完整必要步骤均成功，才能作为该域的原始步骤证据；后续阶段失败不让未执行的域通过。旧全量 Python 可证明明确分组后的原测试子集，不能证明新命令或新环境。任一核验缺失就执行对应域，不自动迁移其他历史格式。

Actions 固定为只读核实过的提交，不依赖浮动标签：checkout v6.1.0、setup-python v6.3.0、setup-node v6.5.0、upload-artifact v6.0.0。对应来源为各官方 `actions/*` 仓库；更新前复核变化与 runner 兼容性。

## 隔离与证据

- `scripts/run_offline_tests.py` 只接受源码外全新输出目录，移除继承的 ProjectWatch/供应商/云凭据和 GitHub token，设置专用数据与应用配置目录。结果写入新目录中的 `pytest.xml`。
- Python 审计钩子拒绝非 loopback 连接和 DNS；本机合成网关允许访问。这不是针对任意恶意子进程的 OS 沙箱，不可用它在有真实凭据的主机运行不可信 PR。
- 默认清除 PostgreSQL、DSH 等外部集成变量。只通过显式 `--dsh-runtime` 接入已核验目标、锁、文件与许可状态的 DSH，并运行脚本化模型网关；不使用真实模型/搜索/商业来源。
- GitHub runner 无真实服务配置；依赖安装会联网下载包，测试阶段不授权供应商调用。官方 DSH 子进程协议测试不是实际模型质量测试。
- `scripts/installed_wheel_smoke.py` 拒绝源码/editable 加载、已有输出目录和与 wheel 不符的内容；在全新安装环境检查完整产品文件、关键原生依赖导入、合成 PDF、页面与静态资源、合成项目暂停状态重开。基本 smoke 不启动 worker；`--full-journey` 继续运行已有安装态的合成主体确认、建档、分析、兼容界面和材料摄取验证，保留原有覆盖，不用新短测试替代。
- 工作流只上传列明的 JUnit/安装回执、公开输入规划和最小域 proof，保存 7 天；proof 仅含 SHA/tree/domain/hash 与原始执行身份。不上传数据目录、设置、数据库、BP、原始提供商回执或整个临时目录。
- GitHub token 只注入规划的证据查询步骤，不进入测试步骤。API 下载重定向只允许受限 HTTPS 存储域，跳转后不带 Authorization；小 proof ZIP 仅在内存读取唯一条目，不解压执行。查询次数、响应大小、时间均有上限；不可读则不复用。
- 执行作业的摘要始终尝试上传；原始 proof 仅在该域全部检查成功后生成。失败不设 `continue-on-error`，既有集成条件跳过仍保留在 JUnit，不因域拆分新增 Windows 门禁豁免。

本地使用示例（先激活独立测试环境）：

```sh
python -I -X utf8 scripts/run_offline_tests.py --output /absolute/new/offline-run
python -I -X utf8 scripts/run_offline_tests.py --output /absolute/new/dsh-run --dsh-runtime /absolute/prepared-dsh tests/test_studio_dsh_runtime.py tests/test_studio_dsh_monitor.py
```

安装态 smoke 必须由另一个新虚拟环境从明确 wheel 安装后执行，并传该 wheel 绝对路径；不能拿源码测试环境里的包代替。

## 不覆盖的发布门槛

每次合并或公开同步仍须检查实际作业、executed/reused 来源、跳过项及权限。原生 DSH、PostgreSQL 等环境不足造成的跳过须按支持矩阵解释，不能因为总结果绿色就认为全部真实业务路径通过。

桌面安装器、Finder/Gatekeeper/菜单栏、真实使用者首次配置、完整 A01–A11、48 小时观察、安全许可实物审查与最终双平台 RC 冻结仍分别执行。当前工作流没有正式发布步骤；受保护的候选构建流程与精确资产清单待依赖冻结后补齐，发布必须再获维护者确认。
