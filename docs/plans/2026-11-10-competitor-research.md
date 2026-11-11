# 竞品研究的最小实现（2026-11-10）

承接 [判断页语义与呈现](2026-11-10-judgment-page-semantics.md)。那份解决"结论为什么说不清"，
这份解决"竞品为什么只有名字"。

范围：只改判断产出与呈现。不改采集口径、不加采集源、不放宽核验、不新增付费调用。
文中示例为合成数据。

## 一、结论

系统完成了"识别竞品"，跳过了"研究竞品"。两者之间缺的是一个数据结构，不是模型能力。

竞品材料已经采到、已经进分析池、已经被模型读过。它们被消耗成"支持本公司判断的弹药"，
没有成为"竞品自身的事实"。所以读者拿到的是一句"我们领先于 A、B、C"，点不开 A 的任何内容。

## 二、数据流与四个断点

| 环节 | 现状 | 代码位置 | 断点 |
|---|---|---|---|
| 采集 | 每个 peer 单独发检索，带 `target='peer:名字'` | `collection_scope.py:109-117` | 无 |
| 入库 | 只保留 `category='competitor'` | `studio_items` 无 target 字段 | 竞品身份丢失 |
| 建档 | 名字出现在正文即建 company 对象 | `structured_facts.py:247` | 只有名字，无事实 |
| 分析 | 按 category 混入材料池，`AFFECTS['competitor']=('position',)` | `analyze_job.py:106, 224` | 只当参照物 |
| 输出 | `"peers": ["对比的公司名"]` | `insight_generation.py:60` | 没有放事实的格子 |
| 呈现 | 只渲染名字串 | `frontend/src/api.ts:263` | 无对比内容 |

### 实测

分析池中竞品材料占比 9–17%：硅基流动 435/2493、博睿康 217/1419、宇树 380/2617、
北启 164/1205、映恩 159/1729。每轮有一成半材料关于竞品。

页面与系统的 peer 集合不一致：

| 项目 | 页面 peers | 已注册 peer | 依据中提到 peer |
|---|---|---|---|
| 映恩生物 | 8 | 30 | 0 / 5 |
| 博睿康 | 4 | 30 | 2 / 2 |
| 华源智因 | 5 | 12 | 4 / 5 |
| 北启生物 | 0 | 12 | 0 / 1 |

映恩的对比只存在 summary 一句"落后于第一三共/阿斯利康"，五条依据没有一条提到竞品，
这句比较结论无法核验。北启有 12 个已注册 peer，页面 0 个。

## 三、按用途组织，不按公司组织

投资人看竞争只问三件事：时点（还能领先多久）、威胁（谁在做同样的事且更快）、定价（同类值多少钱）。

按公司组织要给 30 个 peer 建档案，绝大多数常年不变，维护不动。按用途组织每轮只回答三个问题。

竞品信息的价值落在"对本项目假设的影响"上，描述竞品本身没有决策价值。

## 四、目标结构

形状要贴着现有机制：`trim()` 按 `section.list.index` 定位可丢弃条目，`ITEMS`
（`section_analysis.py:34`）声明哪些列表是"每条自带走 refs"的。扁平列表能逐条丢弃，
嵌套字典不行。所以威胁信号必须是 position 的直接列表，不能藏在 `competition.threats` 里。

```
position = {
  verdict, confidence, summary, for, against, change_if, gaps,
  peers: ["对照名单"],                                  # 保留，降级为名单
  competition_window: {months: int, basis: str},        # 0 或 1 个
  competition_signals: [ {peer, event, impact, refs} ], # 0–3 条
}
```

- `peers` 保留但不再承担对比，仅供"对照名单"展示；后续可折叠。
- `competition_signals` 加入 `ITEMS`，每条独立带 refs，可在核验时单独丢弃。
- `competition_window` 不独立丢弃：`basis` 无依据时整体移除该字段。

### 字段

| 字段 | 类型 | 内容 | 缺失处理 |
|---|---|---|---|
| `competition_window.months` | int | 先发优势剩余月数 | 缺预计时间时整字段省略 |
| `competition_window.basis` | str | ≤60 字，说明按什么算 | 无依据则整个 window 省略 |
| `competition_signals[].peer` | str | 竞品名，须在 `peers` 内 | 无此条 |
| `competition_signals[].event` | str | ≤50 字，含时间，如"2026-09 获Ⅲ类证" | 无此条 |
| `competition_signals[].impact` | str | ≤60 字，对我们意味着什么 | **无 impact 不入列表** |
| `competition_signals[].refs` | list | 来源编号 | 无 refs 不入列表 |

日期写进 `event` 的自然语言里，不单独设字段。原因见第六节。

## 五、窗口期口径

只允许两种算法，不允许给猜测的数字：

1. **有本公司节点预计时间**：`months` = 本公司预计完成目标节点 − 最近一家同类竞品完成该节点的时间。
2. **无预计时间**：不给 `months`，`basis` 写"N 家同类已进入该节点"，定性为"窗口期收窄"。

第二种是常态。宁可只给"3 家已进入注册临床"，也不要编一个"约 12 个月"。

`basis` 必须能被引文支持，否则该字段被核验丢弃，页面回到无数量的状态。

`months` 是整数，`review_input` 只收字符串，评审看不到它，所以程序侧必须自己兜底：
`basis` 里至少要出现两个日期型 token（两端各一），否则 `months` 丢弃、只留 `basis`。
"N 家同类已进入注册临床"这类句子没有日期，配 12 个月就是编的。

**竞品名必须有出处。** `peer` 只在 `peers` 名单里出现是不够的——`peers` 也是模型产出，
不构成证据；否则会出现"幻影科技 · 某型号获证 → 引文却是山海医疗获证"。判定条件是
`peer` 出现在这一条信号自己引用的材料里，`peers` 只用于展示。

**一个编造的引文码只赔上那一条。** 原先 `_refs()` 遇到不存在的编号就抛错，整栏
`rejected`；现在进评审前先清洗：不存在的码从条目里去掉，条目没有码就整条删除，
其余照常评审。

## 六、实现约束（已实测）

**评审预算。** 博睿康 `position` 现有 12 个评审字段，正好占满 `MAX_FIELDS=12` 的一个请求。
新增 `competition_window.basis` 1 个 + 3 条信号 × 3 个 = 10 个字段，共 22 个 → 2 个请求。
`MAX_BATCHES=24` 余量充足。`months` 是 int，`review_input` 只收字符串，不计入评审。

**引文校验。** `judgment_review.quoted_in` 做精确引文匹配。若把日期拆成独立字段
（`"2026-09"`），原文常写作"2026年9月"，会因格式差异被丢弃。写进 `event` 的自然语言里可避开。

**核验覆盖。** `review_input` 递归遍历 `analysis`，新增嵌套字段无需在白名单登记，
但每个非空字符串都会被要求逐条核验——所以字段宁少，`event` 与 `impact` 合并会降低可核验性，
分开则增加请求量。当前取分开，代价是 1 个额外请求。

**降级顺序。** 单条信号 refs 不支持 → 丢弃该条；全部信号丢弃且 window 无依据 →
`position` 回到现状（只有名字）。不因此把整栏置为 `undetermined`。

## 七、按阶段差异化

| 阶段 | 竞争价值 | 投入 |
|---|---|---|
| 天使 / A 轮前 | 低，问题是"能不能做出来" | 只给 window 一句 |
| B / C 轮 | 高，比速度与资源 | window + 3 条信号 |
| 并购 / 退出 | 最高，要可比交易与接盘方 | 加定价锚（后续） |

早期项目不做精细竞品对比。若材料显示阶段为早期，`competition_signals` 允许为空。

## 八、不做

- 不做全量竞品档案（30 个 peer 建 30 份档，成本线性涨，内容常年不变）。
- 不做排名与打分。`competitive_comparison.py` 的立场正确：口径不同不能排名，
  混口径的对比表比没有更危险。
- 不做竞品新闻流。400 条竞品材料已在池内，再加流只增噪音。
- 不复活 `competitive_comparison` 的 HTTP 通路。它的输入需要审过的
  `CompetitiveMetric`（period/unit/basis/source_locator），当前无人填充，接上去也是空表。
- 不补 `other` 分类。先验证契约能否用现有材料产出 3 条信号，再决定是否回头改分类。

## 九、验收（离线）

1. 重放博睿康 position：217 条竞品材料下 `competition_signals` 产出 0–3 条，每条有 refs，
   且 `peer` 出现在该条自己的引文里（仅在 `peers` 内不算）。
2. 合成输入：`impact` 为空 → 该条被丢弃，其余保留，`position` 不整栏失败。
3. 合成输入：`window.basis` 无引文支持 → window 整字段移除，页面无数值；
   `position` 的 verdict、summary、信号均保留，不整栏失败。
4. 评审预算：`position` 字段数 ≤ 24，请求数 ≤ 2，未触碰 `MAX_BATCHES`。
5. 北启这类 `peers` 为空的记录：`competition_signals` 允许为空，页面不显示空区块。

## 十、`peers` 缺失已确认

北启生物 `raw` 中没有 `peers` 键（`raw.keys() = [verdict, confidence, summary, for]`），
`normalize` 以 `or []` 兜底，不报错、不提示。所以 12 个已注册 peer 一个也没进页面。

结论：契约把 `peers` 当作可选，缺失被静默容忍。这比"随机丢空"更值得先修——
在加 `competition_window` 之前，先决定 `peers` 是必填，还是由 `peer_names.peers_for()`
的注册名单兜底（页面需标明来源）。否则新字段同样可能被模型整段省略而无任何提示。

同一问题会影响 `competition_signals`：它天然允许为空（早期项目），因此无法用"缺失即告警"
判断模型是否偷懒。建议在记录里保留一个 `competition_status`（`produced` / `not_applicable` /
`omitted`），区分"确实没有"与"没输出"。
