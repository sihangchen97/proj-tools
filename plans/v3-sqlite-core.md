# v3 Design — SQLite 为核心，YAML 模板即 schema，md 为输入通道

Supersedes `v2-templates-and-index.md` 的核心前提（v2 的 "No database" 作废，
md frontmatter 契约作废）。
v2 仍有效的部分：模板声明 kind 行为、kind 注册表由扫描模板构建、`--profile`
概念。

## 0. Decision summary

- **Source of truth = `.proj.sqlite`**。md 文件不是 source of truth，是
  **输入通道 + 渲染输出**：人/LLM 改 md → `proj sync` 读回 DB → DB 重新
  render 出规范 md。
- **模板 = YAML schema 文件**（`templates/<profile>/<kind>.yaml`）。模板只
  声明：kind、dir、编号方式、status 选项、章节名 + 每章该填什么（hint）。
  其余全部工具托管：uuid 标记行、`# N. Title` 标题命名、changelog 表格、
  章节标题。用户只填章节内容。
- **UUID 只存 DB，md 里以不可见标记行携带**（第一行 HTML comment），sync
  时与对外 id 交叉验证，不匹配 → 报错。
- **每个 kind = 两张表**：基础信息表 + 独立 Change Log 表。
- **所有 CLI 操作先查 DB**：kind 或 record 不存在 → 直接报错。
- **模板间迁移以 DB 为核心**（数据都在表里，迁移 = 表间转换）。
- 后续 MCP server = 同一引擎函数的薄封装（同 ecs-gpu 模式：CLI + MCP 双入口）。

## 1. 模板（YAML）

```yaml
# templates/default/decision.yaml
kind: decision
dir: docs/decisions
numbered: 4                # 4 = NNNN；"date" = YYYY-MM-DD；"letter" = A..Z,AA..；0 = 无编号
status-allowed:
  - Proposed
  - Accepted
  - Deprecated
  - "Superseded by DEC-{id}"
  - "Supersedes DEC-{id}"
fields:                    # kind 特有的额外字段（可选，render 成 md 顶部的托管行）
  - {name: supersedes, hint: 被本决策取代的旧决策 id}
sections:                  # 只写章节名 + 该填什么
  - {name: Context,      hint: Problem statement & constraints}
  - {name: Decision,     hint: Chosen solution & why over alternatives}
  - {name: Consequences, hint: Pros, cons, tech debt, and follow-ups}
```

- `init --profile X` 读对应 YAML → 建表 + 复制模板到项目 `templates/`
  （幂等，用户已改的不动）。
- 新增 kind = 丢一个 YAML 文件，零代码改动。
- 渲染占位符：`{id}` `{id-int}` `{uuid}` `{title}` `{date}` 等由引擎替换。

## 2. 各 kind 的 status（参考来源）

| kind | status-allowed | 来源 |
|---|---|---|
| `decision` | Proposed, Accepted, Deprecated, Superseded by DEC-{id}, Supersedes DEC-{id} | adr-tools / Nygard ADR 标准四态 + supersede 双向标记 |
| `task` | Not started, In progress, Done | 用户定义（三态）；主任务 id = 字母（见 5c） |
| `plan` | Draft, Active, Pending review, Shelved, Archived | Draft=起草；Active=实施中（task promote）；Pending review=全部 Done 待人工/LLM 审核；Shelved=搁置；Archived=冻结档案 |
| `debt` | Proposed, In progress, Repaid | v2 plan |
| `incident` | Open, Closed | rule_draft：修复后写 post-mortem → Closed |
| `experiment` | Proposed, Running, Closed | 用户：补 Proposed 阶段（先立假设再跑） |

**propose 阶段盘点**（对照 reference/rule_draft.md 后结论）：
- 已有：decision（Proposed）、debt（Proposed）、plan（Draft 即 propose）
- 本次补：experiment 加 **Proposed**（先写 Hypothesis，Accepted 式确认后才
  Running）
- 不需要：task（Not started 已是 propose 语义）、incident（发生即 Open，
  无提案阶段）

**task 三态的渲染符号**（TASKLIST.md 视图，工具托管）：
`○` Not started / `◐` In progress / `✓` Done。
status 变更**自动追加 changelog**（category=Changed，summary 含
from→to）；无状态变更时也可用 `proj log <kind> <id> "Category: 一句话"`
手动追加额外条目。

**task 时间点显示**（工具托管行，位于 task md 顶部，与 `Status:` 同区）：
`Started: 2026-10-29` / `Done: —`。取值 = changelog 中首个
to=In progress / to=Done 条目的 date，**不单独存储**，render 时派生。

## 3. Change Log（参考 keep-a-changelog 1.1.0）

**类别（两级 changelog 共用）**：`Added` / `Changed` / `Deprecated` /
`Removed` / `Fixed` / `Security`。

**每条记录**：`{date: YYYY-MM-DD, category, summary}` — summary 是单行
一句话。

### 3a. 每 record 的 changelog

- 存储：`<kind>_changelog(record_uuid, date, category, summary)`，每个
  kind 独立一张，`record_uuid` 外键 → `<kind>.uuid`。
- 写入：CLI 显式 `proj log <kind> <id> "Category: …"` 手动追加；status
  变更**无条件自动追加**一条（category=Changed，summary 含 from→to），
  无 `--no-log` 开关——哪怕 summary 留空也会落一条记录。
- 显示：render 时把**最新 5 条**以表格放在 md 顶部（标题之后、章节之前）：

```markdown
<!-- proj:decision 3f2a1b4c-881c-... -->

# 1. Use Rust

Status: Accepted

| Date | Category | Change |
|---|---|---|
| 2026-10-29 | Changed | status Proposed → Accepted |
| 2026-10-28 | Added | created |
```

- 表格是工具托管的渲染输出（sync 时归一化，不解析回 DB）。

### 3b. 全局 CHANGELOG.md

- 不是 N 条 record，是**一个文档**：版本节 `## [1.1.2] - 2024-09-27` +
  `### Added/Changed/...` 小节 + `## [Unreleased]`，遵循 keep-a-changelog
  1.1.0 + semver。
- 存储：`changelog_entries(version, category, text, date)`。
- `CHANGELOG.md` 从该表 **render**（与 TASKLIST.md 同属"渲染视图"）。
- 命令：`proj changelog add "Added: …"`（进 Unreleased）、
  `proj changelog version v1.0.0 [--bump patch|minor|major]`
  （Unreleased 归入新版本节）、`proj changelog release-notes vX.Y.Z`。

## 4. DB schema（`.proj.sqlite`）

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);   -- schema_version 等

-- 每个 kind 两张表（init 时按 YAML 生成；sections 存 JSON）
-- 主键 = uuid（全局身份，跨 kind 迁移/引用用它）；
-- id 是可视化的文件名前缀，同样 UNIQUE（kind 内编号不重复）
CREATE TABLE decision (
  uuid TEXT PRIMARY KEY,      -- uuid4，new 时生成，不可变
  id TEXT UNIQUE,             -- "0001"，= 文件名前缀
  title TEXT,
  status TEXT,
  created TEXT,               -- YYYY-MM-DD，new 时生成，不可变
  supersedes TEXT,            -- 来自模板 fields（kind 特有）
  sections TEXT               -- JSON: {"context": "...", "decision": "..."}
);
CREATE TABLE decision_changelog (
  id INTEGER PRIMARY KEY,
  record_uuid TEXT,           -- → decision.uuid
  date TEXT,                  -- YYYY-MM-DD
  category TEXT,              -- Added/Changed/...
  summary TEXT                -- 单行一句话
);

-- 全局 changelog
CREATE TABLE changelog_entries (
  id INTEGER PRIMARY KEY,
  version TEXT,               -- "Unreleased" 或 "1.0.0"
  category TEXT,
  text TEXT,
  date TEXT
);
```

- 表名 = kind 名；`proj check` 校验表结构与模板 YAML 一致（模板改了
  sections/fields → 提示迁移）。
- 替代 v2 的 `.proj/index.json`（不再需要，DB 即索引）。
- **DB 是否进 git 由用户定**：建议直接提交 `.proj.sqlite`（核心管理数据）；
  选择不追踪时，md 里 changelog 只保留最新 5 条，更早历史不可恢复 —
  用户知悉并接受。

### 4a. `.proj.lock` + 文件吸收（adopt）

- **`.proj.lock`（项目根，跟随 commit）**：记录"用了哪些模板 + 数据库格式"：
  profile、每个 kind 的完整模板 YAML（内联）或 hash、`schema_version`、
  kind 清单。
  - `init` 看到 lock → 按 lock 内容建表/定行为（lock 是权威，即使
    `templates/` 后来被改过）；
  - 没有 lock → 从 `templates/` 构建，并写出 lock。
- **吸收（adoption）**：`init` 时对已存在于托管目录的文件做一遍吸收 +
  重新渲染（即老项目/别人 clone 后不带 DB 时的重建路径）：
  1. 带 uuid 标记的文件 → **优先收**：uuid 入 DB，交叉验证 id 与文件名前缀；
  2. 无 uuid 的文件 → 检查文件名 id 编号 + `# N. Title` 标题是否合规：
     - 编号重复 → **报错**，列出冲突文件，不自动收；
     - 编号无重复 → 收进来，**生成新 uuid 补写**进文件第一行标记
       （随后全量重新渲染）。
- **命令**：`proj adopt [FILE…]`（无参 = 全部托管目录）执行同一套吸收逻辑；
  `init` 内部调用它。`sync` 仍严格校验（必须有 uuid），遇到无 uuid 文件
  报错并提示 `proj adopt`。

## 5. 模块分类：哪些走模板引擎，哪些单独做

### A. 模板驱动（通用引擎：YAML → 建表 → new/sync/render/status/list）

| kind | dir | 编号 | 特殊操作（引擎之上的薄封装） |
|---|---|---|---|
| `decision` | `docs/decisions/` | NNNN | supersede（双向 status + supersedes 字段）、`toc`/`graph`（mermaid）/`link` 三个渲染器，全部读 DB |
| `task` | `tasks/` | 字母 | Steps 两级 checkbox（见 5b）；id = 字母（见 5c）；`Open` 章节放待定项（见 5a）；主 TASKLIST.md 是渲染视图（见 B2）；`plan` 字段关联所属 plan |
| `plan` | `docs/plans/` | NNNN | 状态机 Draft→Active→Pending review→Shelved/Archived（见 5a）；Task List 章节是**视图章节**（按 task 分组含其待定项） |
| `debt` | `docs/tech-debt/` | NNNN | `repay` = `proj status debt N "Repaid"` |
| `incident` | `docs/incidents/` | YYYY-MM-DD | 无 |
| `experiment` | `docs/experiments/` | NNNN | `commit` 字段（fields 声明）；rule_draft 要求记录 Commit Hash / Dataset Version / Hyperparameters / Metric Deltas（对应 sections） |

### 5a. plan ↔ task 生命周期（研究结论）

用户流程：plan 里有 task list → 实施时移到主 TASKLIST → shelve 时再放回去。
设计：**task 始终是独立 record（kind=task），不跨 kind 搬家**；用
`task.plan` 字段（模板 `fields` 声明）关联所属 plan，可见性由 **plan 的
status 派生**：

```
task 显示位置规则（主 TASKLIST.md / plan 的 Task List 章节都是视图）：
- task.plan 为空                    → 永远出现在主 TASKLIST.md
- task.plan 有值 且 plan=Active     → 出现在主 TASKLIST.md（已 promote）
- task.plan 有值 且 plan ≠ Active（Draft/Pending review/Shelved/Archived）
  → 只出现在该 plan 的 Task List 章节
```

**plan 状态机**（`proj plan <verb> N`，前置校验 + 自动 changelog）：
- Draft → Active：`activate`（无前置）
- Active → Pending review：`review` — 前置：该 plan 下**所有 task 均
  Done**，否则拒绝并列出未完成项。"哪些完成了没审核" =
  `proj list plan --status "Pending review"` 一眼可见
- Pending review → Shelved：`shelve`（人工/LLM 审核通过后搁置）
- Shelved → Active：`resume`（恢复实施）
- Pending review / Shelved → Archived：`archive` — 冻结档案；`check`
  禁止再编辑（sync 报 drift）

**待定项（open items）**：task 模板有 **Open** 章节（用户所有，实施中
随时记"需要决定的因素"，一行一条）。task 不搬文件 → 待定项天然跟着
task 走；plan 的 Task List 视图章节 render 时**按 task 分组连带渲染
其待定项**，Pending review 时即为完整审核视图 — "收回 plan 时对应上
每个 task 底下的待定项"自动成立，无需搬数据。

命令（全部薄封装，无数据搬家）：
- `proj task new T --plan N`：task 建在 plan 名下（Not started）
- `proj plan activate|review|shelve|resume|archive N`：状态机迁移
  → task 在主列表/plan 章节之间自动显隐

### 5b. task 的 Steps 与嵌套 sub-task（限定两级）

Steps 章节是 Markdown checkbox 列表，**限定两级**：顶层 step + 一层
sub-task（不再深 — 更深的拆分本身就该拆成独立 task，见下）：

```markdown
## Steps
- [x] 搭数据管线
  - [x] 下载原始数据
  - [ ] 清洗 + 去重
- [ ] 跑 baseline
```

- 解析规则：缩进 0 的 checkbox = step；缩进 >0 的 checkbox = sub-task。
  出现第 3 层嵌套 → `check` 告警并压平为 sub-task（内容不动）。
  限两级的理由：两级覆盖 95% 场景且解析/渲染最简单；更深的结构
  信号是"该拆成独立 task 挂 plan"。
- 状态规则（勾选来源不限 — 手改 md 或 LLM 改 md 都行，sync 时检测）：
  - **Not started → In progress**：sync 检测到任意层级出现**第一个**
    `[x]` → 自动切换 + 自动 changelog。
  - **In progress → Done**：`proj task done N` 要求**两级所有 checkbox
    全部勾选**，否则拒绝并列出未勾选项；通过后 Done + 自动 changelog。
- 运行完成后的记录写在 task 模板的 **Notes** 章节（用户所有）。
- TASKLIST.md 视图：顶层 step 显示 `○/◐/✓`，sub-task 缩进显示。

### 5c. task 两级命名

- 主任务 id = **字母**（类电子表格列名）：A..Z，用完后 AA, AB…；
  = 文件名前缀（`tasks/A-data-pipeline.md`）= CLI id
  （`proj task done A`）。引擎 `next_id` 增加 letter 模式。
- 子任务 = **两位数字**（01-99），按 Steps 内文档顺序编号，视图中
  显示为 `A.01`；编号在渲染时派生，**不改用户写的 checkbox 文本**。
- task 编号池与其他 kind 独立（各目录自编号，字母与 NNNN 不冲突）。

```yaml
# templates/default/task.yaml（节选）
kind: task
dir: tasks
numbered: letter
status-allowed: [Not started, In progress, Done]
fields:
  - {name: plan, hint: 所属 plan 的 id（空 = 主列表任务）}
sections:
  - {name: Steps, hint: 两级 checkbox 列表（step + sub-task）}
  - {name: Open,  hint: 待定项（需要决定的因素），一行一条}
  - {name: Notes, hint: 运行结果 / 结论记录}
```

plan 模板的 Task List 章节声明为**视图章节**
（`sections: [{name: Task List, view: tasks}]`）：内容由引擎从 task 表
render（符号 ○/◐/✓ + 标题），sync 时该章节 md 内容忽略（DB 赢）。
引擎对视图章节零硬编码 — 任何 kind 的任何 section 都可声明 `view:`。

### B. 单独处理（非 record 形态）

1. **全局 CHANGELOG.md** — 见 §3b：`changelog_entries` 表 + render +
   独立模块（version bump / add / release-notes）。
2. **TASKLIST.md** — 渲染视图：从 `task` 表生成进度表，`proj update`/
   `proj task render` 重新生成，永不手改。
3. **静态脚手架** — CONTRIBUTING.md、.github/*、DATASET.md、BENCHMARK.md、
   .env.example：init 时复制一次，之后用户所有。无 DB、无生命周期、无
   模板引擎参与。
4. **基础设施** — `config.py`（root 发现）、`db.py`（新：连接/建表/迁移）、
   `hooks.py`、`cli.py`、`check.py`（md↔DB 一致性 + 契约校验）。

## 6. md 文件格式（无 frontmatter，uuid 隐藏标记）

md 文件**没有 YAML frontmatter**。结构（全部由 render 生成）：

```markdown
<!-- proj:decision 3f2a1b4c-881c-... -->

# 1. Use Rust

Status: Accepted

| Date | Category | Change |
|---|---|---|
| 2026-10-29 | Changed | status Proposed → Accepted |
| 2026-10-28 | Added | created |

## Context

…用户填的内容…

## Decision

…
```

- **第一行 = 不可见标记**：HTML comment，携带 kind + uuid。UUID 只存
  DB；render 时写进第一行，仅为后续读取提供载体。
- **交叉验证**：sync/check 提取该标记 → 用 uuid 查 DB → 记录的 id 必须
  等于文件名前缀（0001…），uuid 不在 DB 或 id 不匹配 → 报错，不入库。
  新文件一律由模板生成（new），所以该验证总是成立的前提。
- title 从 `# N. Title` 标题解析（仅用于漂移检测，DB 赢）。
- `Status:` 行、changelog 表格、kind 特有字段行（`supersedes`/`plan`/
  `Started`/`Done` 等）= 工具托管的渲染输出，sync 不解析回 DB。
- 用户只编辑 `##` 章节里的内容。

**章节内容入库前的归一化**（只做格式，不动内容）：
- 去掉每节头尾的空白行；
- 连续 3 个及以上空行压缩为 1 个（代码围栏内不动）；
- 表格、代码块、列表等具体文字内容**一律不改**。

## 7. 数据流（md 优先，方案 B）

```
proj new → 写 DB + render md（打开 $EDITOR 填章节）
人/LLM 改 md 章节内容
proj sync [FILE…] → 两阶段：
  1) 入：读全部 md → 提取 uuid 标记 → 交叉验证 → sections 归一化 → 写 DB
  2) 出：DB 全量重新 render 所有 md（格式统一规整）
proj render [kind [ID]] → 只 DB → md（不读 md）
所有查询命令（list/show/toc/graph/release-notes）→ 只读 DB
```

**sync 的归属规则**（冲突时谁赢）：
- 章节内容（sections）→ **md 赢**（md 是输入通道）
- id / uuid / created / title → **DB 赢**（工具生成，不可变）
- status / 额外 fields → **DB 赢**（CLI 管理）
- changelog 表 → **DB 赢**（append-only，md 表格不解析回）

## 8. CLI surface（v3）

```
proj init [--profile X]              # 建表(lock 优先) + 脚手架 + 复制模板 + adopt
proj adopt [FILE…]                   # 吸收既有 md（uuid 优先，无 uuid 校验编号后补）
proj info                            # 本项目管理了什么（见 8a）
proj new <kind> TITLE [-s N] [-y]    # DB insert + render md
proj sync [FILE…]                    # md → DB → md（无参 = 全部托管目录）
proj render [kind [ID]]              # DB → md
proj list <kind> [--status S]        # 查 DB
proj show <kind> ID [--history]      # 查 DB（--history = changelog 全量）
proj status <kind> ID STATUS         # 校验 allowed set + 自动 changelog 条目
proj log <kind> ID "Category: 一句话" # 追加 changelog 条目
proj check                           # md↔DB 一致、契约、表结构 vs 模板
proj decision toc|graph|link         # ADR 渲染器（读 DB）
proj task new|done|list [--plan N]   # task 薄封装（id = 字母）
proj plan new|activate|review|shelve|resume|archive|list
proj list plan --status "Pending review"   # 快速找"完成未审核"
proj changelog version|add|release-notes
proj update                          # = sync 全量 + 重新渲染所有视图
proj hooks install                   # 不变
```

v2 的 `submit`/`update` 被 `sync` 取代；`debt repay`/`experiment close`/
`incident close` 退化为 `proj status`。

### 8a. `proj info` — 目录内容盘点（供人和 MCP/LLM 用）

一条命令回答"这个目录里有什么在 proj 管理、要变更该用什么"。全部读 DB：

```
$ proj info
Project: proj-tools (profile: default, cv)
Managed by proj (.proj.sqlite):
  decision    12 records  @ docs/decisions/        [NNNN]
              statuses: Accepted 8, Proposed 3, Superseded 1
  task        5 records   @ tasks/                 [NNNN]
              statuses: In progress 2, Not started 3
              main TASKLIST: 5 entries (rendered view)
  plan        3 records   @ docs/plans/            [NNNN]
  experiment  2 records   @ docs/experiments/      [NNNN]
Changelog: CHANGELOG.md @ changelog_entries (14 entries, latest v0.3.0)
Views (rendered, never hand-edit): TASKLIST.md, CHANGELOG.md, plan Task List sections
Ops cheatsheet:
  new:      proj new <kind> TITLE [--plan N]
  change:   proj status <kind> ID STATUS | proj log <kind> ID "Category: …"
  md edited: proj sync | proj update
  query:    proj list <kind> | proj show <kind> ID [--history]
```

- 输出为纯文本（人读）；`proj info --json` 给 MCP/LLM 结构化消费
  （同 ecs-gpu 的 `ecs_list_servers` 模式）。
- MCP 侧对应一个 `proj_info` 工具，返回同样的 JSON。

## 9. 已定决策（原开放问题）

- ~~`--no-log` 开关~~ → **不要**。status 变更无条件自动记 changelog。
- **MCP server**（后续）：工具 = CLI 动词的薄封装，config 走
   `config.toml`（不提交）+ `config.example.toml`（提交），同 ecs-gpu。
