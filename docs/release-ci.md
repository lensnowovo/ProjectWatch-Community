# 发布回归 CI（当前先收尾 Windows）

状态：工作流与本地验证入口已准备，**尚未推送或在 GitHub 执行**。2026-10-23 维护者要求先收尾 Windows，Mac 暂缓；未启用 Mac 作业。绿色本地契约测试不能冒充远程作业通过。

## 工作流边界

`.github/workflows/ci.yml` 使用 JSON 形式的 YAML，触发于普通 PR、main push 或手动运行。它只有 `contents: read`，checkout 不保存 Git 凭据，不使用 `pull_request_target`、仓库密钥或发布权限，也不推送、打 tag、创建 Release 或部署桌面实例。

| 作业 | 覆盖 |
| --- | --- |
| Windows 2022 x64 / Python 3.13 | 前端、Node、全量 Python、分层与 lint、官方 DSH 合成协议、完整 wheel 独立安装 smoke |
| macOS 15 arm64 / Python 3.12（预备方案，暂未启用） | 保留脚本兼容能力，恢复 Mac 工作后加入矩阵并验证；不称为 Finder/菜单栏验收 |

平台按 [GitHub runner 官方说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) 选择，并在作业开始检查实际 CPU；若标签解析为错误架构直接失败。使用固定 Node v24.22.1、仓库前端锁与 DSH 锁。CI 的 Python 声明范围依赖仍在解析，**不是最终发布依赖冻结**；Windows bundle 则使用单独的完整 wheel 清单与哈希锁。

Actions 固定为只读核实过的提交，不依赖浮动标签：checkout v6.1.0、setup-python v6.3.0、setup-node v6.5.0、upload-artifact v6.0.0。对应来源为各官方 `actions/*` 仓库；更新前复核变化与 runner 兼容性。

## 隔离与证据

- `scripts/run_offline_tests.py` 只接受源码外全新输出目录，移除继承的 ProjectWatch/供应商/云凭据和 GitHub token，设置专用数据与应用配置目录。结果写入新目录中的 `pytest.xml`。
- Python 审计钩子拒绝非 loopback 连接和 DNS；本机合成网关允许访问。这不是针对任意恶意子进程的 OS 沙箱，不可用它在有真实凭据的主机运行不可信 PR。
- 默认清除 PostgreSQL、DSH 等外部集成变量。只通过显式 `--dsh-runtime` 接入已核验目标、锁、文件与许可状态的 DSH，并运行脚本化模型网关；不使用真实模型/搜索/商业来源。
- GitHub runner 无真实服务配置；依赖安装会联网下载包，测试阶段不授权供应商调用。官方 DSH 子进程协议测试不是实际模型质量测试。
- `scripts/installed_wheel_smoke.py` 拒绝源码/editable 加载、已有输出目录和与 wheel 不符的内容；在全新安装环境检查完整产品文件、关键原生依赖导入、合成 PDF、页面与静态资源、合成项目暂停状态重开。基本 smoke 不启动 worker；`--full-journey` 继续运行已有安装态的合成主体确认、建档、分析、兼容界面和材料摄取验证，保留原有覆盖，不用新短测试替代。
- 工作流仅上传四份合成测试摘要，保存 7 天；不上传数据目录、设置、数据库、BP、原始提供商回执或整个 runner 临时目录。失败不设 `continue-on-error`，跳过原因保留在 JUnit。

本地使用示例（先激活独立测试环境）：

```sh
python -I -X utf8 scripts/run_offline_tests.py --output /absolute/new/offline-run
python -I -X utf8 scripts/run_offline_tests.py --output /absolute/new/dsh-run --dsh-runtime /absolute/prepared-dsh tests/test_studio_dsh_runtime.py tests/test_studio_dsh_monitor.py
```

安装态 smoke 必须由另一个新虚拟环境从明确 wheel 安装后执行，并传该 wheel 绝对路径；不能拿源码测试环境里的包代替。

## 不覆盖的发布门槛

CI 没有被授权远程启用。合并或公开同步后仍需检查实际作业、跳过项及权限。原生 DSH、PostgreSQL 等环境不足造成的跳过必须按支持矩阵补验，不能因为总结果绿色就认为核心路径通过。

桌面安装器、Finder/Gatekeeper/菜单栏、真实使用者首次配置、完整 A01–A11、48 小时观察、安全许可实物审查与最终双平台 RC 冻结仍分别执行。当前工作流没有正式发布步骤；受保护的候选构建流程与精确资产清单待依赖冻结后补齐，发布必须再获维护者确认。
