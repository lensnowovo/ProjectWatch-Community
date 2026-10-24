# Windows 用户版（开发中，尚未发布）

目标：Windows x64 用户双击打开，不需要安装 Python、Node、Git，也不需要终端。界面继续在本机浏览器中使用；关闭浏览器不会关闭后台监控，从托盘退出才会等待任务结束并关闭程序。

## 已实现的运行方式

- `ProjectWatch.exe` 只调用同目录的内置 Python，不搜索或安装系统 Python。
- 完整产品包：Studio、已有分析、旧版兼容界面、解析、存储、监控，以及 OCR/MCP/团队/数据库依赖不裁剪。外部 Agent 的客户端、账号和第三方服务授权不随包提供。
- 首次空数据目录进入 `/setup`，选择一个模型和一个搜索服务，用户同意后保存并测试，随后进入已有的名字/BP 建项流程。
- `/setup` 不测试商业数据源，也不自动重试。与完整设置共享测试回执；切换页面不能绕过结果不明的请求。短时间内复用的回执只列出已测试的服务。
- 模型地址变更不会沿用旧地址的密钥。密钥输入框为密码框，不写入浏览器本地存储；其他配置项不因首次配置被重置。
- 数据目录为 `%LOCALAPPDATA%/ProjectWatch-Desktop`，与程序、原开发者版数据目录分离。已有资料没有自动迁移；迁移必须另行备份并验证。
- 重复启动先验证本机实例身份，再打开已有实例。退出沿用完整任务排空机制，不强杀付费请求或其他进程。
- 电脑休眠或关机期间不继续监控；本地用户版不等于云端常驻服务。
- 安装包自带 DSH 研究引擎与 Node.js，安装到程序根目录的 `runtimes/<内容标识>`，不同版本可共用未改变的运行时。全新安装默认启用 DSH；旧版升级保留此前执行方式，不因缺少设置文件就静默切换。
  设置 → 研究引擎可以关闭，重启后生效。自带运行时缺失、损坏或版本不符时程序照常启动，
  显示“不可用”与原因，自动研究暂停，不悄悄改用经典流程。数据目录中的
  `desktop-runtime.json` 仍可指定自定义 DSH 与 Node 路径，升级不会覆盖它。

## 维护者构建

维护者需要 Python 构建环境、Node 和 Windows C# 编译器；这些不是最终用户的前置条件。

1. 在隔离源码目录安装开发依赖，运行测试和前端构建。Windows 打包环境固定 `pip==26.2.2`，其 vendored distlib 必须为 `0.4.2`；构建器校验版本并随包保留许可。用 `python -m build --wheel` 构建完整 wheel。
2. 从 Python 官方下载脚本固定版本的 Windows x64 embeddable ZIP，核对 `scripts/build_windows_bundle.py` 中的 SHA-256。
3. 准备针对 CPython 3.13 / win_amd64 的离线 wheelhouse，覆盖 `desktop,ocr,mcp,team,postgres` extras；目录内只保留本次解析出的各一个版本，包含与 `--wheel` 完全相同的产品 wheel。下载发生在构建机，不在用户首次启动时发生。运行 `python -m scripts.freeze_windows_dependencies --wheelhouse <依赖目录> --product <完整wheel> --out <新的依赖锁JSON>` 固定每个 wheel 的版本、目标与 SHA-256；该锁不代表安全或许可通过。
4. 准备 DSH 运行时：在 Windows x64 上运行 `python scripts/prepare_dsh_runtime.py --out <全新目录> --approved <已审许可清单>`。
   它按 `packaging/dsh/package-lock.json` 执行 `npm ci --ignore-scripts`（**需要联网访问 npm 官方源**），
   按 `packaging/dsh/prune.json` 删除不会加载的部分，逐包核对锁文件版本，生成
   `THIRD-PARTY-NOTICES.txt`、`third-party-review.json` 与文件哈希清单。审查清单中有未处理项时打包拒绝继续；
   `--approved` 只能由维护者逐项核对后提供。`--installed <已有 npm ci 目录>` 仅用于离线复核同一锁文件的结果。
5. 从 Node.js 官方下载 `scripts/build_windows_bundle.py` 中固定版本的 win-x64 ZIP，核对其中固定的 SHA-256
   （首次使用前须按官方 `SHASUMS256.txt` 填入；为空时打包拒绝继续）。只提取 `node.exe` 与 `LICENSE`。
6. 运行 `python -m scripts.build_windows_bundle --runtime <ZIP> --wheel <完整wheel> --wheelhouse <依赖目录> --dependency-lock <依赖锁JSON> --compiler <csc.exe> --dsh <上一步目录> --node-zip <Node ZIP> --out <全新输出目录>`。构建用 `pip --isolated --no-index --require-hashes`，缺件、额外文件、重复版本或哈希变化均拒绝继续；安装后的依赖版本集合必须与锁一致。随包保存相对文件清单及哈希锁，不包含构建机路径。
7. 构建拒绝覆盖旧目录；安装后的产品内容必须逐文件等于指定 wheel，DSH 目录必须逐文件等于其清单。依赖命令保留完整入口：仅将已识别 distlib 启动器的构建机解释器路径改为相对包内 Python，脚本使用中性 shebang；原可执行前缀和 ZIP 入口不变，逐项校验并更新 RECORD。未知格式、内容不符或归属不明时拒绝构建。输出清单包含文件哈希、运行时、产品输入哈希及改写命令名，不代表发布授权。
8. 用包内 `runtime/python.exe -I -B` 运行 `scripts/installed_wheel_smoke.py --wheel <本次wheel> --output <全新回执目录>`，再运行 `tests/windows_desktop_smoke.py --home <全新合成目录>`；`--tray` 可验证真实托盘的定时安全退出。测试不能指向用户数据。

## 发布前仍需完成

- 用户级安装器、开始菜单/桌面快捷方式、版本目录和卸载保留数据已实现，仍须完成发行验收。
- 旧数据导入、升级备份与回退的图形化路径；不让用户为了升级再次回到终端。
- 签名与 SmartScreen 验证。当前本地启动器未签名，不能宣称没有系统安全提示。
- 内置运行时、全部依赖与模型文件的安全/许可清单。新增 pystray 的 LGPLv3 及其源码提供、替换权利和说明须纳入分发审查；当前包含它的 COPYING/COPYING.LGPL 不代表审查已完成。
- 本地构建、模拟服务测试不能替代干净 Windows 的安装和真实服务验收。
- 正式候选需独立版本号、全新干净构建、校验值；发布前取得用户确认。

2026-10-19 实物复核：当前候选已经包含默认中文 OCR 的三份模型；之前仅按下载机制推断“未预置”并不准确。合成图片在禁止 Python 网络连接时识别通过。其他模型/字体与模型权利仍需分别验证，见 [签名与分发审查](windows-signing-distribution-review.md) 和 [干净电脑验收步骤](windows-clean-pc-acceptance.md)。

## Windows 安装器与维护

安装器使用 NSIS 3.13（zlib 压缩），仅安装给当前用户，不请求管理员权限，不写 PATH、不注册开机自启。
目标为 Windows x64，当前本机实测 Windows 11 build 26200，独立用户验收待完成；
Windows 10 的具体版本不能仅凭运行时最低要求宣称整包已经验证，见
[支持矩阵](release-support-matrix.md)。NSIS 上游 [版本与校验清单](https://nsis-dev.github.io/release-data/versions.json)
中的 ZIP SHA-256 为 `ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8`。
下载后核对再解压使用，安装器随附上游 COPYING；不把编译器安装进用户系统。

```powershell
python -m scripts.build_windows_installer --bundle <已校验的全新bundle> --nsis <makensis.exe> --out <全新输出目录>
```

生成本地 `ProjectWatch-Setup.exe` 和 `installer-build.json`，不会发布或上传。编译器输入先冻结，
防止另一位开发者在压缩期间修改脚本形成混合版本；bundle 必须只包含清单列出的文件，不可带测试缓存。
`--test-root <全新隔离路径>` 会把程序、数据、快捷方式和 HKCU 登记全部编译为独立测试目标。
测试包不能作为用户安装包发布。运行 `tests/windows_installer_smoke.py --help` 查看实际安装演练。

| 项目 | Windows 用户版位置/行为 |
|---|---|
| 程序 | `%LOCALAPPDATA%/Programs/ProjectWatch/versions/<内容标识>` |
| 启动入口 | 程序根目录 `ProjectWatch.exe`，读取原子切换的安装记录 |
| 资料与配置 | `%LOCALAPPDATA%/ProjectWatch-Desktop`，不自动打开旧开发者版数据目录 |
| 升级备份 | `%LOCALAPPDATA%/ProjectWatch-Desktop-backups/<唯一标识>`，包含配置，须私密保管 |
| 快捷方式 | 当前用户开始菜单；桌面快捷方式可在安装时取消 |
| 卸载 | 默认保留程序目录，只移除快捷方式与安装登记；可取消勾选以按清单删除程序文件。资料、配置、原件、备份始终保留 |

升级须从托盘退出。安装器不强杀进程，持有与桌面/数据服务兼容的锁后才备份，
SQLite 使用备份 API，包括已提交的 WAL 内容。新版本逐文件校验通过后才原子切换入口；旧版本保留。
即使旧快捷方式与升级发生竞争，桌面程序获取锁后仍会核对活动版本，拒绝启动已被替换的版本。
自定义启动器或不匹配的安装记录要求 Agent/维护者处理，不覆盖。不同启动器源码代际的迁移也须另行处理。

低版本安装器拒绝直接降级。回退应把对应的已校验备份恢复到新目录，再用匹配版本验证，
不能只运行旧程序打开迁移后的数据。保留旧版本不等于已经提供一键回滚界面。
卸载页默认勾选“保留程序目录（包括本地定制）”，保留时程序文件与安装状态不变，便于恢复或重装；不会继续保留系统中的安装登记和快捷方式。取消勾选后按清单直接删除程序和共用运行时，**修改过但仍在清单中的文件也会删除**，清单外文件保留。静默卸载同样默认保留程序；只有显式 `/REMOVEFILES` 才删除清单文件。
安装前先检查目录和运行锁，再解压；已存在的 `.payload` 不会直接删除，请交给 Agent 检查。升级执行器默认值记录在原子安装状态中，不改写用户的 `desktop-runtime.json`。显式选择和自定义路径始终优先。
安装、卸载和备份不调用模型/搜索，也不重发任何结果不明的付费请求。
