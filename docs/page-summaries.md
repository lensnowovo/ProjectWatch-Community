# 页面摘要

当前判断以段落摘要开场，投资假设不再在该页显示；既有假设数据与后台任务不删除。
摘要下方直接展示公司本体、竞争位置、技术路线、产业链、行业环境五个分项页签，保留原有切换和图表。外部信息各页和宏观子页共用摘要字体、间距与点击展开的依据区域。

摘要读取当前页面的独立发布产物或本机所有者已审阅的整理稿，保留各自日期，较新的版本优先。
不再从其它分析页、通用简报、前三个标题或材料数量拼接摘要。打开页面只读取已保存内容，
不触发模型、搜索或重试。没有该页摘要时保留下方资料，显示摘要暂缺。

所有者整理稿是数据目录中的 `editorial-summaries.json`，不进入源码或安装包。
格式为 `version: 1`、`projects: {project_id: {generated_at, basis, pages}}`。
`pages` 按页面键保存 `{text, refs: [{label, url, date}]}`；`basis` 来自
`studio.editorial_summaries.fingerprint`。来源或已保存简报变化时显示“有新资料”，
保留原整理时间，不自动覆写所有者确认的文字。该导入格式没有公开写接口。

所有者整理稿不会被自动重写。新代码以独立 `page_summary` 任务读取已保存公开证据，按页生成、
复核并发布；不收集新来源，也不阻塞已经完成的报告或收录结果。复用不可变证据、任务请求记录和
版本表，不增设另一套研究数据库。无效引文、无事实支撑或错误页签内容不能发布。
旧摘要在新稿失败时保留原日期；证据撤回后不再展示。样式精简不删除否定、条件或财务期间。

临床、论文、专利只把已审核的对应来源纳入专业页分析；单次上下文分批处理，不能截掉尾部材料。
下方图表仍明确注明当前已加载范围。临床登记的公司参与按申办方/合作方字段对应，不能从检索命中推定。

专利展示的主文献身份必须满足 Google Patents 文献 URL 与结构化公开号一致，且具有主文献申请人字段；公司申请采用已保存公司名的精确匹配。历史检索摘录中的引用表、公司名称、日期和公开号不参与公司归属及地域/种类统计。原始标题与摘录仍保留在点击展开的来源详情中，不修改历史数据库或重新调用模型。

## 内容要求

| 页面 | 摘要应回答的问题 |
|---|---|
| 当前判断 | 公司处于什么阶段，近期最关键的变化是什么，机会和主要制约是什么 |
| 项目总览 | 产品、客户、收入模式和发展阶段 |
| 公司进展 | 具体事件、时间、业务变化、已披露里程碑 |
| 临床 | 产品、适应症、申办方、设计/阶段、疗效和安全结果、公司参与关系 |
| 论文 | 团队、技术方法、研究问题、结果与基线、对公司路线的意义 |
| 专利与注册 | 主文献申请人、发明或产品、保护内容、地域和法律状态；注册另述 |
| 竞品 | 具体可比产品、客户需求、指标、商业化进度与位置变化 |
| 产业链 | 具体供应商/客户/伙伴、事件及供给/交付/收入/议价传导 |
| 行业 | 细分市场需求、采购、价格、产能或融资的实际变化 |
| 政策 | 文件、机关、状态、日期、地域、适用对象及作用机制 |
| 技术 | 主流和替代路线、具体指标、验证环境、公司路线关系 |
| 内部经营 | 经营期间、订单、交付、收入、回款、现金与原因 |
| 产品与业务 | 研发和产品里程碑、验证结果、延期、资源条件 |
| 数据 | 期间、口径、单位、主要可比数字变化 |
| 财务 | 增长质量、毛利、现金转化、资金压力及对应数字 |
| 风险 | 具体矛盾、影响事项与关键待确认问题 |
| 材料/监控范围 | 操作和状态，不强行生成公司判断 |

内部摘要使用独立 internal_page_summary 任务，只读取当前项目与模型绑定允许的材料，并在调用及发布前复核权限。
正文为两至三句话；参数、编号和详细来源置于 details，点击查看依据时展示，不使用悬浮展开。离线测试不等于真实模型内容验收。旧协议的未完成请求不能切换协议重新发送。


## Provider output and page isolation

Official GLM-5.3-Flash structured calls use JSON-object output and low reasoning effort; thinking remains enabled. Wire options are bound to provider/model and frozen for an existing job or collection-batch continuation, so an upgrade cannot resend an old unknown request under a new payload hash. Existing usage and request receipts are retained.

An incomplete JSON response is a typed output failure, never an empty finding or a reconstructed result. Page-local model output/unknown failures are checkpointed; other independent pages continue through full evidence review and can publish. A shared draft failure retains prior drafts for review and never resends that failed request. The result remains partial with page failures recorded. Account/backoff, consent, permission, snapshot and lease failures are not swallowed by page isolation. No application call or token quota is added.
