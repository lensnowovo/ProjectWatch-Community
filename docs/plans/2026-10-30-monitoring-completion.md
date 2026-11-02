# ProjectWatch 完整监控收尾实施计划

> 执行交接：按 writing-plans / executing-plans 工作流逐项实施；技能名称以执行环境实际可用项为准。禁止把局部通过记为完整通过。

**Goal:** 用同一精确候选证明：一个已有项目可以完成增量更新，一个新项目可以完成首轮及直接收尾，采集、证据复核、事件保存、分析和页面结果贯通；旧 unknown 不被重发。

**Architecture:** 保留现有 Python / SQLite / DSH / React 架构和请求账本。以正常用户入口、worker 和持久化结果为主线收敛缺陷；根据冻结输入及证据依赖限定阻断范围，不按任务名称粗暴放行，不引入平行调度器或第二套分析链。

**Tech Stack:** Python、pytest、SQLite、原生 DSH、Node、TypeScript、React、wheel、Windows 安装运行时。

**2026-10-31 授权补充：** 维护者已明确允许在本计划范围内持续修复接续阻断、
局部验证后继续隔离验收，解除旧的次数／额度及两轮修复停工限制，允许失败经
自查修复后重跑。下文旧窗口及额度保留为历史记录，不再要求重复申请常规复测。
已有结果、调用和消费账本不得重置；旧 unknown 不重发，新 unknown 仍停止依赖
它的调用。验收标准、输入与来源归属校验、数据隔离及正式发布确认要求保持。

---

## 1. 完成定义：所有必选项同时成立

“能跑通”不是 API 返回 202、任务变成 done、测试数变多或来源列表有数据。最终必须交付下表证据。

| 编号 | 必选结果 | 验收证据 |
|---|---|---|
| A1 | 已有项目通过普通“更新”入口完成本轮公开研究和分析；无关旧 unknown 不再全项目卡死 | 入队、claim、实际派发、冻结输入、事件、分析及页面结果可串联；旧请求逐条保持 |
| A2 | 一个未研究过的新项目走普通首轮入口，完成采集→材料处理→独立复核→事件→分析→直接收尾 | 同一根任务的阶段与子任务清单，每阶段终态及可见结果；不得手写库或调用私有发布 helper 代替产品流程 |
| A3 | A2 项目再做一次有界增量更新，复用历史且不重复插入同一事件 | 新旧事件与证据版本对照；无新事实时允许“本轮无新增”，仍要有成功更新回执 |
| A4 | 异常处理可恢复且不丢结果 | 隔离故障注入覆盖来源失败、派发前拒绝、模型 unknown、预算不足、进程正常停止与恢复；旧 unknown 重发次数=0 |
| A5 | UI 如实反映内容与运行 | 外部页签、当前判断、监控范围、今日/周报的结果/空态与库一致；只转当前活动阶段，不将任务接收当完成 |
| A6 | 交付候选与验证对象一致 | 源提交+工作树差异摘要、wheel/安装候选哈希、安装版路径、扫描结果、数据配置保持核验 |

真实验收不是全面覆盖所有公司和所有供应商。选择两个具备可公开核对资料、行业不同的固定项目；样本在执行前冻结。至少有一个专业资料丰富的样本验证论文/专利路径，另一个非临床样本验证不适用空态。不能为得到绿灯临时换样本、换模型或换目录。

以下不算通过：仅保存来源而分析未开始；内部状态称成功但页面无结果；所有内容都被拒绝后显示完成；把来源失败写成零结果；把来源条数等同新增事件；历史资料改为今天；强制确认模型提出的关联。

外部服务故障可能使真实验收不通过。可以证明故障被正确隔离，但不能据此把 A1/A2 的成功旅程豁免。无法完成时交付“未完成+具体阻断”，不能宣布本计划完成。

## 2. 起点与冻结范围

- 文档建立时源码 HEAD：`f96cf64e37f26de80603ffa59d0d0d19973818fd`；已有未提交修改必须先保存差异和文件哈希，不重置、不覆盖。
- Claude 的两批改动已整合，但仍需复核真实派发顺序、旧请求保护边界、派发前状态回滚及专利版本分组。
- 最近一次定向测试：98 passed / 7 failed；七个失败均未观察到预期采集请求，不能未经定位就称为七个独立产品缺陷。
- 最近一次续跑宿主尝试在请求发出前失败。修正宿主尚不等于研究完成；旧执行锁、START/END、回执和预算保持。
- 当前仅公开采集的独立分支是中间候选：`analysis=not_started` 必须记为 partial，不能作为 A1。
- 架构文档已明确首次分析衔接、未完成分析条件唤醒尚未完整验收。它们属于本次完成主链所必需的核查范围；只修真实入口上的接线，不重写架构。

不做：新功能、商业化部署、Cloudflare、UI 全站重设计、改模型提供商、增加来源平台、无关重构、删测试、放宽引文标准、提高 32 KiB/8k 限制、清除 unknown、自动公开发布。

## 3. 阶段 P0：冻结一次可重复的验收合同

**文件：** 本文、私有 `monitoring-completion/manifest.private.json`、`baseline.private.json`。

1. 保存 HEAD、现有 diff、候选文件哈希及基线测试结果；真实实例端口与版本重新只读确认，不沿用旧截图推断。
2. 冻结 A1/A2 样本及参考事实清单、当前配置绑定、安装运行时；配置值和真实材料仅保存在私有目录。
3. 每次外部执行前冻结模型/搜索/正文/结构化来源的具体上限、原消耗、剩余量、时间窗口和停止条件；禁止“无限测试”。已有轮次预算继承，不因换 executor、任务或目录恢复额度。
4. 建立阶段矩阵：输入→调用身份→保存产物→下一阶段条件→失败处理。核对入口同时包括 bootstrap、手动 refresh、周期 monitor、直接收尾。
5. 明确真实调用授权范围。本文是开发计划，不自动执行或重开已消费的单次窗口；既有有效授权可用，过期或已消费授权必须先处理，不能悄悄绕过。

**通过门槛：** 两个样本、额度、候选身份、阶段矩阵无歧义。缺输入可以继续离线开发，但不得开始依赖该输入的真实调用。

## 4. 阶段 P1：修通入口、请求边界和专业检索

### P1.1 查清七个采集路径失败

**修改候选：** `src/project_watch/studio/independent_collection.py`、`recovery.py`、`pipeline.py`、`dsh_runtime.py`、`collection_host.py`。
**测试：** `tests/test_independent_collection.py`、`tests/test_refresh_hold_admission.py`、`tests/test_legacy_model_recovery_scope.py`、`tests/test_material_ownership.py`。

1. 取一个失败参数组合最小复现，读取 frozen plan、来源可用性、due/预算及调用日志；区分夹具没有可执行来源与产品未派发。
2. 测试预置明确可执行的合成来源，分别断言 refresh→claim→pipeline 和 monitor→实际 transport。
3. 最小修复后跑全部七个参数与负向边界。若是夹具错误，修夹具并保留“不派发不能成功”的断言。
4. 对 business/details 旧 hold 仅按可证明的输入独立性处理；同时存在相关 hold、暂停、租约失效、输入变更、旧恢复 key 或预算继承不明时继续拒绝。
5. 将“采集成功但等待依赖”的状态保留为 partial。此项过关仅证明采集入口，不记 A1 完成。

### P1.2 实际派发顺序

**文件：** `collection_host.py`、`collection_scope.py`、`collection_plan.py`；测试 `test_collection_subject_names.py`、`test_collection_plan.py`。

1. 使用一个英文名和一个中文名、剩余来源预算=1，验证英文专业索引第一次实际请求选择英文名。
2. 网页专利查询保持原确认名顺序；旧冻结计划、query identity、历史窗口保持原值。
3. 分别测试截断前/计划排序后/实际派发，不能只断言名称数组的集合。

### P1.3 派发前错误不消耗补搜机会

**文件：** `bootstrap_collection.py`；测试 `test_retrieval_queries.py`、`test_collection_continuation.py`。

1. 复现“产品一的原因为空被拒绝→产品二合法补搜”；首次无请求、无新动作占位、无新维度占位。
2. 只回滚本次新产生且确认未派发的状态；保留先前 claim、已写请求回执、unknown 与已接受的请求。
3. 测试已有账本行发生变化但行数没变的情形，不能只凭 count 判断未发送。
4. 成功空结果、失败、未执行分别断言；中文成功零结果不改成不合法，零结果不表述为不存在。

### P1.4 专利解析、版本和日期

**文件：** `google_patents.py`、`research_sources.py`、`frontend/src/api.ts`、`frontend/src/researchSources.tsx`。
**测试：** `test_retrieval_queries.py`、`test_research_sources.py`、`frontend/tests/research-sources.test.mjs`。

1. 使用实际网页布局构造合成夹具，另对已保存公开页面离线重放；本件字段不能来自引用/其他版本。
2. 同一公开号多来源、单向 Other versions、分页、缺 familyId 都不丢条目，不按标题或申请人猜同族。
3. UI 区分提供方 familyId 与 Other versions，后者显示“其他公开版本”；保留优先权、申请、公开日期及日期依据。
4. 修正 `test_collection_history.py` 已验证过时的专业来源数量预期，保留后续窗口与预算断言。

**P1 门槛：** 定向集全部通过；所有失败有根因记录，不存在“先放过去”。不打包、不部署真实实例。

## 5a. 2026-10-30 局部重写决策与替换表（优先于原补丁实施方式）

**授权与结束目标：** 最新明确授权局部重写一次更新，保留数据、账本及未知请求保护；先映恩论文/专利，再新项目→分析→今日/周报，审阅通过才升级本机。不扩展成新调度框架。原 A1–A6 的真实结果门槛不变；开发继续不等于允许重开付费窗口。

| 原入口/阶段 | 新路径与复用组件 | 迁移及兼容 |
|---|---|---|
| `run_bootstrap` / `progressive_monitor.run` 各自编排 | 普通 DSH 外部任务统一进入 `update_run`，首轮/增量只选择材料发布策略 | 新任务在首次请求前冻结 owner；已开始旧任务仍按原冻结路径完成，不能热切 |
| 查询结束后直接进入材料循环 | `sources` 阶段持久保存来源检查点与输出摘要，再显式交给 `events` | 复用 `collection_host/bootstrap_collection`、已有来源回执；已知单源失败记缺口继续，未知请求不重发 |
| 材料准入及事件发布 | `events` 复用 `progressive_bootstrap`、独立复核、版本化事件 | 保存精确 publication/event versions；恢复仅复用已落盘结果，不重复写事件或伪造完成 |
| 根任务完成，再由扫描器发现分析子任务 | 同一已租约根任务的 `analysis` 阶段，固定事件/对象/证据版本后调用既有 ObjectInsight | 新 owner 不再派旧 insight 子任务；旧 outbox 仍处理旧 owner。阶段失败/缺口如实保留，不把 root done 当分析成功 |
| 日报/周报靠页面即时读取，缺少本轮交付回执 | `reports` 记录既有今日/周报投影的真实日期窗口和事件/分析依赖 | 不改新闻日期、不把历史研究挪到今天；无本期事件可为空，并记录本轮结果可见性 |

**状态合同：** 每阶段保存 pending/running/complete/partial/blocked、输入指纹、精确输出引用、下一阶段、开始/结束时间。成功输出不可因后续失败被清空。阶段入口重新核验项目身份、输入、暂停、租约、unknown 和总预算；复用现有 SQLite/checkpoint/request ledger，不另建一套状态库。输出策略使用已修复的通用角色配额，不写公司/论文/专利特例。

**恢复和承接：** 当前进程先正常落盘；保留原 START/END、失败、unknown、调用与费用。旧结果承接必须有一次显式、持久的移交记录，绑定原输入、产物、原请求集合及剩余额度；不通过新 HOME/新 root/新账户规避限额，不隐式重发未知请求。不自动迁移在途/未知依赖。旧数据结构不删除，回滚候选仍可读原事件/证据；新 owner 的处理中任务不可交给旧版本重执行。

**取舍：** 选同一 worker/lease 内阶段顺序，减少根任务与尾任务之间的交接歧义；代价是一个 job 的生命周期更长，继续沿用 heartbeat、drain 与 checkpoint 恢复。拒绝新增队列/工作流服务和一套独立发布器，也不继续添加验收专用捷径。

**固定验证：** 先阶段合同/幂等/中断恢复/旧任务不改写/双派发禁止/余额与 unknown 负例，再普通 API→原生 DSH→事件→复核分析→今日周报旅程；只重跑受影响检查。安装源码逐字节对应和安全扫描后，在原累计授权余额及截止内承接真实验收。窗口用完就明确报告未覆盖，不能擅自重开；真实门槛未满足不部署。

## 5. 阶段 P2：把后半程真正接完

**定位文件：** `progressive_bootstrap.py`、`material_ownership.py`、`material_handoff.py`、`dsh_runtime.py`、`pipeline.py`、`event_insight_work.py`；先定位再修改，不预先重写全部模块。
**测试：** `test_progressive_bootstrap.py`、`test_material_handoff_pipeline.py`、`test_affordable_publication.py`、`test_external_analysis_recovery.py`、`test_monitor_end_to_end.py`；新增 `tests/test_monitor_completion_journey.py`。

1. 创建贯穿普通 API→实际 worker→DSH/工具边界→SQLite→页面 API 的合成旅程；只替换外部传输，不 mock 内部阶段成功，不手工置 done。
2. 同一旅程先跑新项目首轮，再跑增量；断言来源→候选→复核准入→事件→分析→直接收尾的精确因果链。
3. 为旧项目添加无关 business/details unknown。证明新分析仅依赖明确冻结、已完成的证据，不读取旧未确认输出、不改变旧请求身份。若不能证明独立，维持阻断并明确缺失依赖；不能把 public-only 分支当终点。
4. 对首次分析未触发、必要输入就绪后未唤醒、已保存结果未投影等实际断点做最小接线；同一事件只触发一次，周期任务不作为本轮无限等待对象。
5. 预算在进入材料阶段前必须覆盖该有界批次的复核及必要收尾。沿用现有限额，缩小本批工作集合、保留待处理材料，不丢断言或强行把未分析记成功。
6. 注入一次来源超时：保存失败，其他独立来源继续。注入模型 unknown：该请求不重发；已保存结果可见，依赖步骤明确暂停；独立步骤仅在输入证明成立时继续。
7. 测试正常退出/恢复后复用已结算结果、不重复付费请求、不重复事件；旧消费过的单次执行宿主不重新开锁。
8. 对完整请求体、输出限制、原生运行时、HOME 锁在任何真实调用前做安装环境一致性预检，且预检新增外部调用=0。

**P2 门槛：** 正常完整旅程通过；失败旅程诚实 partial/blocked 且不丢结果；相关输入不能绕保护。原生 DSH 对应项实际执行，不用 skip 代替验证。若后半程仍未通过，禁止进入“最终部署”。

## 6. 阶段 P3：一次集成候选与安装版验证

1. P1/P2 通过后冻结候选，按受影响依赖补齐回归；运行规定的 Python/Node、lint、TypeScript、wheel 和 fresh-install smoke。
2. 首次集中集成验证失败只回到本计划内根因，不新增优化项目。任何产品修复改变候选哈希后，重跑受影响检查和旅程；不引用旧哈希的安装结果。
3. 使用与用户安装版相同的 Python/DSH/依赖，安装到新隔离目录；完成原生 DSH、真实 worker 和页面访问的合成旅程。
4. 核对源码→wheel→安装文件对应、精确制品安全扫描。依赖未变化则复用已验证运行时，不反复制作全部依赖包。
5. 测试总数、通过/失败/跳过/未覆盖分别入私有回执。影响 A1–A6 的 skip 必须补跑；无关可选后端的 skip 可说明范围。

**P3 门槛：** 精确安装候选合成完整旅程通过、安全扫描通过、无未解释的相关失败。

## 7. 阶段 P4：固定真实验收，不能用合成测试替代

1. 在权限受控的私有隔离目录使用精确安装版；真实实例保持原状。旧项目副本需安全一致快照，禁用调度器，不克隆旧请求作为新请求重发。
2. 核对有效执行窗口、未消费状态、同项目请求身份与合并预算；锁/副本已存在先读取结果，不另起一次。
3. 执行 A1 原项目增量更新；完成后读取实际论文/专利/公司动态、事件和分析，不仅检查任务状态。
4. 执行 A2 新项目首轮及其直接收尾，随后执行 A3 一次增量。每次阶段边界保存小型脱敏进度回执。
5. 逐项核对冻结的公开参考事实：是否找到了、是否关联正确、日期是否正确、依据能否打开、是否混淆不同产品的审批/临床阶段。存在明确已知资料却整类始终为零，需要解释检索路径，不能直接标“不适用”。
6. A1/A2 至少各有一条证据合格事件和一份可读、有依据的分析；有资料的专业路径须实际命中。A3 可无新增。日报/周报不能因无本期事件伪造新闻，需验证合理空态及本轮研究可见性。
7. 输出耗时、各类调用/unknown、来源正文比例、候选/准入/拒绝数、事件/分析数及页面结果；中间记录不算最终事件。

真实检查发生新 unknown 时立即停止依赖它的新增调用，保存现场，不自动重跑、不换目录避开。修复后的再次执行也必须满足原授权和请求保护；计划不赋予重发 unknown 的权限。

**P4 门槛：** A1–A5 全部有真实证据；供应商失败或合法预算不足导致未完成就记未完成，不能降低门槛。

## 8. 阶段 P5：一次本机升级与最终交接

1. P4 通过后检查候选未变化；通过产品安全退出功能排空任务，不强杀，不要求用户反复手动退出。
2. 备份数据库、文件与配置并校验；精确候选增量升级，保留旧版本与回滚路径。
3. 校验数据、API 配置、暂停状态、旧 unknown 与备份一致；按原配置启动。
4. 只读核对身份、页面资源、项目结果及后台状态。正常调度遵守原暂停状态，不代用户新增手动研究或重试旧请求。
5. 保存 `final-handoff.private.md`：精确版本/路径/哈希、A1–A6证据、耗时、结果、失败/跳过/未覆盖、剩余非阻断问题和回滚方法。

正式推送、合并、tag、资产上传、Release 是独立发布决策，不是本计划完成条件，不自动执行。

## 9. 快速验证命令与执行顺序

执行者从私有 manifest 取得 `$TestPython`、`$NativeDshRuntime`、`$EvidenceRoot`；不得把密钥或用户配置放进 shell 输出。以下在仓库根目录运行。每次 output 必须是新的私有目录，保留旧失败回执。

```powershell
# 第一层：先单独跑当前失败测试，预期看到能解释的失败
& $TestPython -I -B -X utf8 scripts/run_offline_tests.py --output "$EvidenceRoot/entry-before" tests/test_independent_collection.py

# 第二层：入口与检索定向集，预期 0 failed
& $TestPython -I -B -X utf8 scripts/run_offline_tests.py --output "$EvidenceRoot/entry-after" tests/test_independent_collection.py tests/test_refresh_hold_admission.py tests/test_collection_subject_names.py tests/test_collection_plan.py tests/test_retrieval_queries.py tests/test_research_sources.py tests/test_collection_history.py tests/test_legacy_model_recovery_scope.py tests/test_material_ownership.py

# 第三层：完整路径与原生运行时；新增旅程文件实现后执行
& $TestPython -I -B -X utf8 scripts/run_offline_tests.py --output "$EvidenceRoot/journey" --dsh-runtime $NativeDshRuntime tests/test_monitor_completion_journey.py tests/test_progressive_bootstrap.py tests/test_material_handoff_pipeline.py tests/test_affordable_publication.py tests/test_external_analysis_recovery.py tests/test_monitor_end_to_end.py tests/test_bibliography_once.py

& $TestPython -m ruff check src/project_watch/studio tests
git diff --check
npm --prefix frontend test
npm --prefix frontend run build
node --test tests/*.test.mjs

# 源码冻结后集中跑一次 Python 全集；保存真实 skip 原因
& $TestPython -I -B -X utf8 scripts/run_offline_tests.py --output "$EvidenceRoot/final-python" --dsh-runtime $NativeDshRuntime tests
& $TestPython -m build --wheel
```

安装和扫描使用仓库既有 `scripts/installed_wheel_smoke.py`、`scripts/probe_installed_collection.py`、`scripts/build_windows_bundle.py` / 增量流程及已审私有部署步骤；先核对当前 CLI 参数，不能复制旧候选路径直接执行。安装版导入必须来自新安装位置，不能被源码 PYTHONPATH 掩盖。

每个小项按“失败复现→最小修复→定向通过→记录差异”推进。只在阶段边界提交精确文件；不得 `git add .` 吸入他人修改或私有资料。全量回归和安装候选在主链局部通过后集中进行，不每修一行就重新打全包。

## 10. 收敛、时间与停止规则

- 本计划不是新的无限目标：只处理阻断 A1–A6 的缺陷。其他问题记录为后续，不插队。
- **最多两轮集中修复复测**：第一轮定位并修复已列缺陷、建立完整旅程；第二轮仅处理第一轮集成或真实验收暴露的直接阻断。同一根因不通过时不通过换样本或放宽断言躲避。
- 第二轮后仍未满足完成条件，停止追加产品补丁，保留当前最好候选，交付复现、剩余根因与范围内方案；标为未完成。新的架构改造或第三轮需作为独立决策，不能默认为本计划延续。
- 暂估主动开发 6–10 小时，集中回归/安装 2–4 小时，真实旅程另计约 2–4 小时；合计约 1–2 个工作日。此为排期预算，不是通过承诺；P1结束后用实际测试耗时校准一次，不滚动无限顺延。
- 单个源码缺陷定位超过60分钟仍没有最小复现，先记录证据缺口并调整定位方法，不开始大重构。每个真实轮次按冻结窗口执行；达到截止只读汇报，不强杀正在进行的付费请求，不自动追加调用。
- 阶段汇报固定写：已通过哪条用户路径、仍卡在哪一步、是否产生真实调用、下一步解除什么阻断。不得用大量通过测试数掩盖主链仍失败。

**最终决策：A1–A6 全通过才标“监控收尾完成”；否则明确写“未完成”，保留已有结果和运行安全边界。**
