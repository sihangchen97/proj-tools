# Plan: 统一 record/changelog 表 + parent 入 relations + template 去 features

> 状态：**进行中**（本仓库是 proj 工具的 dev 仓库，非 proj 项目，不在根目录 dogfood）
> 本文件是**实施规划**，给实施 agent 照做。规划人负责本文件，实施负责改代码。
> **dev 阶段，不做数据迁移、不兼容历史 `.proj.sqlite`**——直接换 schema 重建。

## 0. 现状（改动前）

每 `record_type` **两张**表（[`src/proj/core/db.py`](src/proj/core/db.py:56)）：

```
<record_type>(id PK, title, status, summary, created, <动态 fields 列...>, sections JSON)
<record_type>_changelog(id, record_id, date, category, summary)
```

- 动态列**只有一个**来源：task 的 `parent`（[`templates.py`](src/proj/core/templates.py:164) 在 `apply_task` 时把 `parent` 插进 `spec.fields` → 变列）。
- `parent` 现在是 task 表的一**列**，不是 relations。
- 全局 changelog 事件与 status 变化都写进 `record_changelog.summary` 的文本（"status Draft → Active"），`_done_date`/`first_date_to` 靠 `summary LIKE '%→ X'` 匹配。
- template 里 `features: {supersede, task}` 两个开关（[`templates.py`](src/proj/core/templates.py:156)）。

痛点：每类型一张表 × N 类型 → 表数膨胀；`parent` 用列而非关系，和 supersede/related 不对称；`features` 段和 `render` 段割裂；status 变化藏在文本里，难显示/追踪。

## 1. 需求 → 设计

| # | 需求 | 设计 |
|---|------|------|
| 1 | parent 用 relations 表记录 | `relations` 加 `kind='parent'`（**source=parent, target=child**——方向与 supersede 一致：`source supersedes target`、`source parent target`，source 都是主语/主动方；跨类型、无序、单值语义靠校验）。task 不再有 `parent` 列。 |
| 2 | template 去掉 `features:` 段 | 删 `features:`。`features.supersede` **直接删**（supersede 是纯 relation，所有类型通用，无需开关，见 §4 关系写入）；`features.task` → 顶层 `contain_task: true/false`。 |
| 3 | 加 `contain_task` 记录"表格" | `contain_task: true` 即该类型模板含 `type: task` 章节 → 渲染 `## Task List` 树 + 在父 head block 出 `> **subtask:**` 行。替代 `features.task`。 |
| 4 | 同一张 record 表 + 同一张 changelog 表 | 统一 `records` 表 + 统一 `record_changelog` 表（见 §2）。 |
| 5 | status 变化单独列存（不塞文本）+ changelog 表下加 status 图例 | `record_changelog` 加 `from_status`/`to_status` 两列：status 变化时填两列、事件备注列 `comment` **不再拼 "status X → Y"**；渲染 Change 列 = emoji 版状态变化开头 + 后跟 comment（D5 显示规则）；**changelog 表底下渲染一行 status 图例**（`emoji 状态名` 对照，复用现有 `views.status_legend`）；`first_date_to` 改查 `to_status=?` 列。非 status 事件两列为 NULL。 |
| 6 | record 表的 `summary` 列改名 `one_sentence` | `records.one_sentence`：一句话总览这条记录。列名避开与正式 section 混淆（incident/debt 等有同名 `## Summary` section）。Python/CLI 对外 API 可仍叫 `summary`（`row["one_sentence"]`、`--summary` flag、`new(summary=)` 不变），仅存储列名改。 |
| 7 | 删 `render.changelog:` 开关，改用环境变量 `PROJ_LOGS` | 环境变量 `PROJ_LOGS`（D6 已定）控制每条记录 md 里渲染的 changelog 表行数：`0` = 不显示；`-1` = 显示全部；正整数 N = 显示最新 N 条（现状 5 条，即默认值，未设变量时按 5）。6 个 template 删 `render:` 段里的 `changelog:` 行（decision 的 `render.toc: true` 保留——toc 与 changelog 无关）。 |
| 8 | head block 渲染细节：空值行隐藏 + 相对路径链接 + 文件名显示 + 多值列表；任务树保持 head 外独立可编辑 block | ① `> **done:**` 行**只在有 done 日期时显示**（未进过 done 状态 → 整行不出，不再渲染 `—`；`created` 恒有值照常显示）。② 关系链接（`supersedes`/`superseded_by`/`related`/`parent`/`subtask`）**路径改用相对记录自身文件**（现状项目根相对 `docs/tasks/T-…md`，点链接会断）：跨目录 = `../{目标dir}/{文件名}`，同目录 = 直接文件名——VS Code/GitHub 点链接可用。③ 链接**文本显示目标完整文件名**（不是 id）：`[PLN-0001-build-plan-tool.md](../plans/PLN-0001-build-plan-tool.md)`，一眼看出指向什么。④ **单值 → 一行**（`> **parent:** [文件名](路径)`）；**多值 → 列表**（key 行后跟 `> - [文件名1](路径1)`、`> - [文件名2](路径2)`，blockquote 内列表）。⑤ **任务树不进 head block**（head 围栏严格 no-hand-edit，任务树可手动勾选、回收入库要变更）：任务树保持为 head 围栏**之外的独立可编辑 block**——即现状的 `kind: task` section 渲染（`## <section>` 标题 + `task_tree_lines` 输出，位于 head + changelog 表之后），**该位置与机制不变**（不加新围栏）；手动勾选经现有 md→DB sync 回库。 |

### 需求 4 补充说明「task 本身也是 head block 里的内容」
parent 入 relations 后，**父记录的 head block 渲染 `> **subtask:**` 行**改为从 `relations` 查 `kind='parent' AND source=本记录` 得到子任务（source=父=本记录，target=子任务），不再扫 `db.all(...).parent` 列（该列已删）。即 task 的挂载关系经 relations 表达、在 head block 呈现——与 supersede/related 同机制（source 当主语）。

## 2. 目标 schema（3 张业务表 + meta）

```
records(id TEXT PK, type TEXT, title TEXT, status TEXT,
        one_sentence TEXT,                                  -- 一句话说清这条记录（≠ 正式 section；
        created TEXT, sections TEXT)                        --   incident 等有同名 "Summary" section，故列名用 one_sentence 区分）
record_changelog(id INTEGER PK, record_id TEXT, date TEXT,
                 category TEXT, comment TEXT,               -- 这条事件的备注（D5 已定名）
                 from_status TEXT, to_status TEXT)          -- status 变化专属两列（见需求 5）
relations(id INTEGER PK, source TEXT, source_type TEXT, target TEXT,
          target_type TEXT, kind TEXT, pos INTEGER)          -- kind ∈ {supersede, related, parent}
meta / changelog_entries                                    -- 不变
```

- **id 全局唯一**（现格式 `{CODE}-{n:0w}` 已带类型前缀，DEC-0001 / T-000001 / EXP-0001 天然不撞）。
- `record_changelog` 无需 `record_type` 列：`record_id` 全局唯一，type 由 `join records` 或 id 前缀推出。
- **status 变化单独存**（需求 5）：status 变化事件写 `from_status`/`to_status` 两列；**显示规则（D5）**：`| Date | Category | Change |` 表里 Change 列 = 有 status 变化时 `emoji 版状态变化` 开头（如 `✏️ Draft → 🚧 In Progress`）+ 其后跟 `comment`（有则拼空格，无则只有状态变化）；无 status 变化时 Change 列单独显示 `comment`。**表底下加一行 status 图例**：该类型全部 allowed status 的 `emoji 状态名`（复用 [`views.status_legend`](src/proj/engine/views.py:26)，现用于 `_TOC.md`，此处同样适用）。非 status 事件（created/related/supersede/one_sentence updated）两列为 NULL。
- `relations.kind='parent'`：`source`=父记录（任意类型），`target`=子任务——方向与 `kind='supersede'`（source supersedes target）一致，source 当主语；`pos` 忽略。
- **无 `fields` 列**（D2 已定删）：`records` 只有通用列；`relations` 承载 parent/related/supersede，`record_changelog` 承载事件。template 的 `fields:` 段一并删除（当前无任何额外字段）。

## 3. template 新格式

```yaml
record_type: task
code: T
dir: docs/tasks
numbered: 6
contain_task: true            # 含 Task List 表格/树 + 父 head 出 subtask 行
# 无 supersede:（纯 relation，所有类型通用，见 §4b）
# 无 render: 段（changelog 表行数走环境变量 PROJ_LOGS；toc 类型才写 render.toc）
verbs:
  start: {from: Any, to: In Progress}
  done:  {from: Any, to: Done}
  fail:  {from: Any, to: Failed}
status-allowed:
  - {name: Draft, emoji: ✏️}
  - {name: In Progress, emoji: 🚧}
  - {name: Failed, emoji: ❌}
  - {name: Done, emoji: ✅, done: true}
sections:
  - {name: Steps, hint: checkbox list}
  - {name: Open}
  - {name: Notes}
```

- 删 `features:` 段 + 删顶层 `supersede:`（D1）。`RecordTypeSpec`：`contain_task`（读 `contain_task`，替换 `apply_task`）；**删除 `apply_supersede`/`apply_changelog`/`apply_task`**（changelog 表行数改由 env 变量 `PROJ_LOGS` 控制，`render.toc` 保留）。
- **删除** [`templates.py`](src/proj/core/templates.py:164) 里"apply_task → 注入 parent 字段"那段（parent 不再是列）；`fields:` 段与 `RecordTypeSpec.fields` 一并删除（D2）。

## 4. 决策点（已拍板；实施按此执行）

| # | 决策 | 建议默认 |
|---|------|---------|
| D1 | 顶层 `supersede:` 开关 | **删掉**（你确认）。supersede 是纯 relation，所有类型通用，无需开关。删 `RecordTypeSpec.apply_supersede` + `record.py` 里那道 `apply_supersede` 门槛 + template 的 `supersede:` 行；`-s`/`supersede` 变所有类型通用（写时校验同类型）。关系写入见 §4b。 |
| D2 | `records` 表是否保留 `fields TEXT` JSON 列 | **删掉**（你问来意 → 本意存额外字段，但唯一动态列 `parent` 已退 relations、当前所有 template 无额外字段 → 永远 `{}` 空壳）。删列 + 删 template `fields:` 段；将来某 profile 要额外字段时 dev 阶段加回（重建即可）。 |
| D3 | `record_changelog` 是否加 `record_type TEXT` 冗余列 | **不加**（你确认）。record_id 全局唯一，join `records` 或 id 前缀推出 type。 |
| D4 | parent 关系约束 | **不加 DB 约束，但强制"一棵树"**（你确认）：① 无环——`check_rows` 父环校验（已有）；② 一子一父——一个 task 只带单值 `--parent`（new 参数天然单值，不会挂两个父）。应用层保证，不加 DB CHECK/UNIQUE。 |
| D5 | status 变化事件的备注列 | **列名 `comment`**（你确认）。status 变化时 `from_status`/`to_status` 两列表达变化，`comment` 可空可写（如 "review passed with notes"）；**显示**：有 status 变化 → Change 列 = `emoji 版状态变化`（如 `✏️ Draft → 🚧 In Progress`）开头，后跟 `comment`；无 status 变化 → Change 列单独显示 `comment`。非 status 事件（created 等）两列为 NULL。 |
| D6 | changelog 表行数的环境变量名 | **`PROJ_LOGS`**（你确认，保留建议名）：`0`=不显示，`-1`=全部，未设/空=默认 5，正整数 N=最新 N 条。注意：`render.toc` 开关保留（decision 用），只有 `render.changelog` 被 env 变量取代。 |

| D7 | supersede 通用化后，被替代记录的状态副作用 | **不自动置 Deprecated**（你确认）。写入 supersede 边**不改被替代记录的状态**；`> **superseded_by:**` 行保留（从 relations 反向查，纯渲染，与状态无关）。`deprecate` 动词仍可用，需要时手动 `proj decision deprecate`。 |

> **不做数据迁移、不兼容历史**（dev 阶段）：直接 `SCHEMA_VERSION` 置 2，`.proj.sqlite` 重建即可，无 migrate 分支。

## 4b. 关系写入接口（supersede/related/parent 怎么进 relations）

三种关系全部落 `relations` 表（`kind` 列区分），写入走**现有的 CLI/MCP 面**，不新造命令（lazy 原则）：

| kind | CLI 面 | MCP 面 | 约束（应用层校验，见 D4） |
|------|--------|--------|--------------------------|
| `parent` | `proj task new "T" --parent <id>`（仅 task） | `proj_new(record_type="task", parent=...)` | 目标必须存在（跨任意类型）；单值（一个 task 只有一个父）；父环拒（`check_rows`） |
| `related` | `proj <rt> new "T" -r <id1,id2>` | `proj_new(..., related="...")` | 目标存在（跨任意类型）；无序；不得自关联 |
| `supersede` | `proj <rt> new "T" -s <old-id>`（**所有类型**，D1 去掉 decision-only 门槛） | `proj_new(..., supersede="...")` | 目标存在；**同类型**（写入时校验 `source_type == target_type`，替代原来的 apply_supersede 开关）；有序（pos=输入序）；链无环 |

- **去门槛**：[`record.py`](src/proj/engine/record.py) `new` 里 `if not self.spec.apply_supersede and supersede: raise` 那道门槛删除（D1）——supersede 对任何类型都可用，只留同类型校验。
- **supersede 无状态副作用**（D7 已定）：写入 supersede 边**不改被替代记录的状态**（不自动置 Deprecated）；`> **superseded_by:**` 行保留（relations 反向查，纯渲染）。被替代记录要废弃时手动 `proj decision deprecate`（该动词保留在 decision.yaml）。
- **head block 渲染**（读 relations，与写入对称，source 当主语）：`> **supersedes:**` = `kind=supersede AND source=本记录`→target、`> **superseded_by:**` = `kind=supersede AND target=本记录`→source、`> **related:**`（kind=related）、`> **subtask:**` = `kind=parent AND source=本记录`→target（本记录是父）、task 的 `> **parent:**` 行 = `kind=parent AND target=本记录`→source（本记录是子，新增）——全部经 `relations` 查，不再读列。显示格式（需求 8）：单值 `> **key:** [目标完整文件名](相对路径)`；多值 key 行后跟 `> - [文件名](路径)` 列表。
- **删除**：`RELATION_FIELDS={"parent"}`（parent 不再是列，[`store.py`](src/proj/engine/store.py)）。

## 5. 分步实施（**过程只改功能、不跑测试；最后统一跑一次测**，按序）

> 纪律（你定的）：步骤 1–8 **只改代码、不跑 pytest/e2e**（避免反复起沙盒、反复等渲染）；每步末尾的"→ 验证：pytest（…）"是**该步应满足的行为标准**（供最后统一测时按清单核对），不是"跑完这一步立即验证"。全部改完后**步骤 9 一次性统一测**（`pixi run test` + `pixi run e2e`），红了再回查。

1. **relations 加 `kind='parent'`**：`db.py` 加 `children_of(parent_id)`（`kind='parent' AND source=parent`→target，返回子任务列表）、`parent_of(child_id)`（`kind='parent' AND target=child`→source，返回父；无则 None）。方向与 supersede 一致（source=父/主动方）。直接切，无双写过渡。（行为标准：pytest parent 查询）
2. **template 去 `features` + 顶层 `supersede:`，加 `contain_task`，删 `fields:`**：`templates.py` 改 `RecordTypeSpec` 字段（删 `apply_supersede`/`apply_task`/`fields`，加 `contain_task`）+ 解析；6 个 yaml 同步改（去 features、去 fields、去 `render.changelog`）；`write_lock`/`specs_from_lock` 去掉 `features`/`fields`、加 `contain_task` 顶层键。→ 验证：pytest（registry 加载）。
3. **统一 `records` 表（含 `one_sentence` 列）**：`db.py` 用单表 `records`（`type` 列过滤替代每类型表）；列名 `summary`→`one_sentence`；`ensure_kind` → 建单表；`insert/get/all/set/delete/max_int_id` 全改 `WHERE type=?`；`next_id` 按 type 过滤；`check.template_matches_table` 改查单表；`store.py`/`record.py`/`cli.py` 里读 `row["summary"]` 处改 `row["one_sentence"]`（对外 `--summary`/`new(summary=)` API 不变）。→ 验证：pytest（增删改查 + id 递增 + one_sentence roundtrip）。
4. **统一 `record_changelog` 表 + status 列 + `comment` + status 图例**：`db.add_log/logs/first_date_to` 改用单表；`add_log` 签名加 `from_status=None, to_status=None`，事件备注列改名 `comment`（status_set 的 status 变化调用点填两列、comment 可空）；`first_date_to` 改查 `to_status=?` 列；`_done_date` 改按 `to_status IN status_done`；渲染 `| Date | Category | Change |` 表时 Change 列 = **有 status 变化 ? f"{emoji(from_status)} {from_status} → {emoji(to_status)} {to_status}" + (f" {comment}" if comment) : comment**（D5 显示规则），表底下接 `views.status_legend(spec)` 一行；删每类型 `_changelog` 建表。→ 验证：pytest（changelog roundtrip + done 日期 + D5 显示规则两种形态 + 图例行在）。
5. **parent 从列迁到 relations**：`Task.new` 写 `add_relation(parent_id, task_type, new_id, 'parent')`（source=父）；`store.render_record` 的 subtask 行（`children_of`）+ task 的 `> **parent:**` 行（`parent_of`）+ `views.task_tree_lines`（`children_of` 建树）+ `record_types/plan.py` review 门禁（`children_of` 取直接子任务）+ `cli._dyn_list` 的 `(parent X)`（`parent_of`）全部改读 `relations`；删 `parent` 列与 `RELATION_FIELDS={"parent"}`。→ 验证：pytest（task/plan 树渲染 + review 门禁 + 幽灵/环）。
6. **supersede 通用化（D1 去门槛 + D7 去副作用）**：删 `record.py` 的 `apply_supersede` 门槛，supersede 对任何类型可用（加同类型校验：`source_type != target_type` 拒）；**不**再自动置 Deprecated（D7），只写 relations 边 + changelog 一条；被替代记录的 `> **superseded_by:**` 行照常渲染。→ 验证：pytest（非 decision 类型也能 `-s`；跨类型拒；同类型链无环；被替代记录状态不变 + superseded_by 行在）。
7. **changelog 表行数改 env 变量**：`store.render_record` 的 `db.logs(..., limit=5)` 改读 `PROJ_LOGS`（0→不渲染表；-1→不限；默认 5；N→N 条）；`RecordTypeSpec.apply_changelog` 删除，6 个 yaml 删 `render.changelog:` 行（decision 保留 `render.toc:`）；`write_lock`/`specs_from_lock` 同步去 `render.changelog`。→ 验证：pytest（三种取值）+ e2e（默认 5 条不变）。
8. **head block 细节（需求 8）**：`_head_block` 的 `done` 行改为**有值才渲染**（未 done → 整行不出）；`_link_value` 重写为：**文本 = 目标完整文件名**（dangling id 落回纯文本 id）、**路径 = 相对记录自身文件**（同目录 → 直接文件名，跨目录 → `../{目标dir}/{文件名}`，`dir` 从 registry 的 spec 取）；head 渲染处：key 的值**单个 → 一行** `> **key:** [文件名](路径)`，**多个 → key 行 + 列表**（`> - [文件名1](路径1)` 逐条，blockquote 内列表）。**任务树位置与机制不变**（⑤）：仍是 head 围栏之外的独立可编辑 `kind: task` section（`## <section>` + `task_tree_lines`），手动勾选经现有 sync 回库。→ 验证：pytest（无 done 的行缺失 + 单值一行 + 多值列表（supersede 两个目标）+ task→plan 跨目录链接文本为完整文件名 + 任务树仍在 head 之后的 section 里）+ e2e（既有 head 断言适配新格式）。
9. **收尾 + 统一测（唯一一次跑测试）**：`mcp.py` 里 parent/one_sentence/supersede 通用/PROJ_LOGS 相关描述、`docs.py`/CLI 帮助文案、lock 格式；`SCHEMA_VERSION`→2（无 migrate 分支）——这些都只改代码不跑；全部改完后**统一跑** `pixi run test` + `pixi run e2e`，红了按第 5 节各步的"行为标准"清单回查定位，修完重跑整条。

## 6. 影响面清单（文件级）

| 文件 | 改动 |
|------|------|
| [`src/proj/core/db.py`](src/proj/core/db.py) | 核心：单 `records`（`summary`→`one_sentence` 列）+ 单 `record_changelog`（加 `from_status`/`to_status`）表；relations 加 `parent_of`/`children_of`；`ensure_kind`/`insert`/`get`/`all`/`set`/`delete`/`max_int_id`/`add_log`/`logs`/`first_date_to` 全改；`write_lock`/`specs_from_lock` 去 features 加顶层键；`SCHEMA_VERSION`→2（无 migrate）。 |
| [`src/proj/core/templates.py`](src/proj/core/templates.py) | `RecordTypeSpec`：删 `apply_supersede`/`apply_task`/`fields`，加 `contain_task`；删 parent 注入列段；删 `load_registry` 里的 fields 解析。 |
| [`profiles/*.yaml`](profiles/task.yaml) ×6 | 删 `features:` 段、顶层 `supersede:` 行、`fields:` 段、`render.changelog:` 行；加顶层 `contain_task:`（decision 保留 `render.toc:`）。 |
| [`src/proj/engine/store.py`](src/proj/engine/store.py) | subtask 行改读 relations（head 内单值/多值链接，**任务树不进 head**——仍是 head 外可编辑 section）；删 `RELATION_FIELDS={"parent"}`；`row["summary"]`→`row["one_sentence"]`；status_set 填 from/to_status + `comment`、按 D5 规则渲染 Change 列（emoji 状态变化开头 + comment）；`_head_block` done 行有值才渲染；`_link_value` 文本=完整文件名、路径=相对记录自身文件、单值一行/多值列表（需求 8）。 |
| [`src/proj/engine/views.py`](src/proj/engine/views.py) | `task_tree_lines` 用 `children_of` 建树；`render_tasklist_md` 用 `contain_task` + `children_of`；`status_legend(spec)` 现供 changelog 表底下的图例行复用（签名不变）。 |
| [`src/proj/record_types/task.py`](src/proj/record_types/task.py) | `new` 写 parent 关系；`check_rows` 幽灵 parent/父环改查 relations。 |
| [`src/proj/record_types/plan.py`](src/proj/record_types/plan.py) | review 门禁 `children_of` 取直接子任务。 |
| [`src/proj/engine/record.py`](src/proj/engine/record.py) | `set_status`/`add_log` 走 from/to_status；`new` 的 supersede 分支**删 `apply_supersede` 门槛**（所有类型可用，加同类型校验），**不**置 Deprecated（D7）；`graph()` 不再以开关为条件（supersede 边有则渲染）。 |
| [`src/proj/cli.py`](src/proj/cli.py) | `_dyn_list` 的 `(parent X)` 改读 relations；`row["one_sentence"]`。 |
| [`src/proj/check.py`](src/proj/check.py) | `template_matches_table` 改单表校验（`one_sentence` 列）。 |
| [`src/proj/mcp.py`](src/proj/mcp.py) | parent / one_sentence 描述微调。 |
| [`tests/*`](tests/test_task_plan.py) | task/plan 树 + 门禁断言适配（parent 走 relations）；one_sentence / status 列断言。 |
| [`scripts/e2e.sh`](scripts/e2e.sh) | 既有断言应仍过；可加 one_sentence + status 两列的显示断言。 |

## 7. 验收标准

- `pixi run test` 全绿（含：单表增删改查、id 按 type 递增、`one_sentence` roundtrip、changelog 单表 roundtrip、status 变化落 from/to_status 两列（comment 不再拼文本）+ D5 显示规则（有/无 status 变化两种形态）+ 表下 status 图例行、done 日期、task 树/父 head subtask 行、plan review 门禁、幽灵 parent 拒、父环检出、supersede 任意类型可用 + 跨类型拒 + 同类型链无环 + 被替代记录状态不变且 superseded_by 行在（D7）、`PROJ_LOGS` 三态（0/-1/默认）渲染）。
- `pixi run e2e` 全绿（`.testproj` 沙盒全生命周期）。
- 无残留每类型表；`.proj.sqlite` 仅 `records`/`record_changelog`/`relations`/`changelog_entries`/`meta`。
- 渲染产物 md 与现状一致，**除 head block 三处预期变化**（需求 8）：无 done 日期时 `> **done:**` 行**不出现**（原来显示 `—`）；关系链接文本改**目标完整文件名**（原来是 id）、路径改**相对记录自身文件**（原来项目根相对），点链接可用；单值一行 / 多值列表（原来多值逗号拼接一行）。任务树**位置与机制不变**（head 围栏之外的独立可编辑 section，手动勾选经 sync 回库）。其余（`## Task List`、TASKLIST.md 树、changelog 表的 emoji 状态变化 + comment 显示、表下 status 图例）——只改存储，不改呈现。

## 8. 风险

- 统一表后 `SELECT * FROM records WHERE type=?` 比每类型表多一列过滤，规模下可忽略（项目级数据量）。
- `one_sentence` 改名牵动 store/record/cli 多处 `row["summary"]` 读取点，需全 grep 无遗漏（对外 API 不变，仅内部列名）。
- status 从文本改两列后，`first_date_to`/`_done_date` 的语义要等价（done 日期 = 最早进入任一 done 状态），改完用现有 `test_done_sets_derived_dates` 覆盖。
