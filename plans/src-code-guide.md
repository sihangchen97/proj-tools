# src/ 代码导读

> 2280 行、15 个文件、一个包 `proj`，按层拆成三个子包。本文按层介绍每个文件
> 在干什么、每个函数负责什么。

## 目录结构

```
src/proj/
├── cli.py            # Typer 命令树（唯一入口，只做参数解析和转发）
├── __main__.py       # python -m proj
├── scaffold.py       # proj init 脚手架
├── check.py          # proj check 不变量校验
├── hooks.py          # git pre-commit hook
├── core/             # 核心层：被所有层依赖，从不依赖别的层
│   ├── config.py         # 找项目根 + [tool.proj] 配置
│   ├── templates.py      # profiles/*.yaml → RecordTypeSpec 注册表 + emojify
│   └── db.py             # SQLite 读写 + .proj.lock 生成/读取
├── engine/           # 引擎层：md ↔ DB 双向转换 + 渲染
│   ├── record.py         # Record 基类：模板(RecordTypeSpec)派生 + 类注册表 record_for()
│   ├── records.py        # 记录引擎（最大文件，md 解析/入库/渲染/同步）
│   └── views.py          # 派生视图：TASKLIST.md / CHANGELOG.md / 表格图例
└── record_types/            # 领域层：各 record_type 的 Record 子类（业务规则）
    ├── task.py           # class Task：checkbox 升档、done 校验、任务停泊
    ├── plan.py           # class Plan：状态机 + Task List 视图
    ├── decision.py       # class Decision：supersede / _TOC / graph
    └── changelog.py      # 全局 changelog（非 Record，独立函数）
```

依赖方向（只许向下）：`cli/scaffold/check → record_types → engine → core`。

**模板 → 记录 的派生关系（2026-10 重构）**：`load_registry()` 读出
`{record_type: RecordTypeSpec}`（模板）；`record_for(record_type, registry)` 把模板绑到对应的
`Record` 子类上，得到一个"知道自己 schema"的记录对象。引擎（records/views/
check）遇到 record_type 专属行为时，调用 `record_for(spec).<钩子>()`，不再写
`if record_type == "task"` 分支。钩子有：`validate_transition`（状态机前置）、
`on_ingest`（md 入库副作用，task 自动升档）、`visible_in_main`（TASKLIST 可见性）、
`render_view`（plan 的 Task List 章节）、`check_rows`（`proj check` 专属不变量）。
只有 yaml、没有子类的 record_type 走基类 `Record`——**新 record_type 仍 = 丢一个 yaml 文件**。

数据流一句话：**profiles/*.yaml 定义 schema → SQLite 存数据 → md 文件是人类读写界面
（渲染出来/解析回去）→ 渲染视图（TASKLIST.md 等）是只读派生物**。

## 核心层

### `core/config.py`（63 行）— 项目定位
- `ProjConfig`：dataclass，持有项目根目录 + `[tool.proj]` 配置段。
- `find_root()`：从当前目录向上找 `.proj.lock` / `.proj.sqlite` 定项目根。
- `load_config()`：组合 find_root + 读 pyproject.toml 的 `[tool.proj]`。

### `core/templates.py`（145 行）— 模板解析
- `TemplateField / TemplateSection / TemplateDerived`：yaml 里的字段/章节/派生日期。
- `RecordTypeSpec`：一个 record_type 的完整定义（record_type 名、目录、编号方式、status 列表+emoji、字段、章节）。
  - `emojify(status)`：文字→纯 emoji（slug+小写比较，支持 `{id}` 模式）；表格显示用。
  - `status_display(status)`：emoji+文字；非表格显示用。
- `parse_template()`：yaml 文本 → RecordTypeSpec。
- `load_registry()`：扫 `profiles/<profile>/*.yaml` → `{record_type: RecordTypeSpec}`；
  项目目录优先，回退打包内置（`bundled_dir()`）。

### `core/db.py`（250 行）— SQLite 存储
- `Db` 类：每种 record_type 两张表（`<record_type>` 主表 + `<record_type>_changelog`），外加全局
  `changelog_entries`。提供 `all/get/insert/set/add_log`、编号生成（`next_id`，
  支持 4 位数字/字母表 A..ZZ/YYYY-MM-DD）、`first_date_to`（派生日期）。
- `template_matches_table()`：表结构与模板是否一致（`proj check` 用）。
- `write_lock / read_lock / specs_from_lock`：`.proj.lock` 记录"DB 是用哪版模板建的"，
  让没有 DB 的新 clone 能重建同一 schema。
- `parse_summary()`：changelog 条目 `Changed: xxx` → (类别, 文本)。

## 引擎层

### `engine/record.py` — Record 基类（模板 → 记录的派生点）
- `Record`：构造时绑定 `RecordTypeSpec`（模板）；通用操作 `new / list / show / transition`
  （`transition` 读类属性 `verbs` 状态机表）。
- 钩子（子类覆写，引擎在对应位置调用）：`validate_transition`、`on_ingest`、
  `visible_in_main`、`render_view`、`check_rows`。
- `@register` + `record_class(record_type)`：record_type → 类 注册表（惰性导入 `proj.record_types`
  触发注册）；`record_for(record_type, registry)`：模板 → 记录对象。

### `engine/records.py`（最大的文件）— md ↔ DB 引擎
- 打开/定位：`open_db`、`spec_and_db`、`by_id`、`file_path`（id+标题 slug → 文件名）。
- **DB → md**：`render_record`（单条记录全文：标记头、Status、派生日期、changelog 小表、
  各章节；view 章节委托 `record_for(spec).render_view`）、`render_all`。
- **md → DB**：`parse_record`、`md_status`、`normalize`、`_ingest_md`
  （单文件入库，副作用委托 `record_for(spec).on_ingest`）。
- 公共命令：`new_record`（建记录+建 md）、`status_set`（校验 status 合法后改）、
  `add_log`、`sync`（md 改了→入 DB→重渲染）、`adopt`（手写 md 收编进 proj）、
  `md_drift`（md 与 DB 不一致的文件清单）。
- checkbox 工具：`checkboxes`（两级缩进解析）、`has_checked`、`all_checked`。
- `info / info_text`：`proj info` 的数据与排版。
- 注意：records ↔ record 有循环依赖，record 的 import 写在函数内（lazy）。

### `engine/views.py` — 渲染视图（派生文件，禁止手改）
- `status_legend(spec)`：表格下方一行图例 `* 🌱 Proposed · ✅ Accepted …`。
- `_visible_tasks`：委托 `record_for(spec).visible_in_main`（task 的实现：
  只显示无 plan 或 plan 为 Active 的任务，其余"停泊"在 plan 里）。
- `_sub_task_lines`：plan Task List 章节的勾选框两级渲染（被 Plan.render_view 用）。
- `render_tasklist_md`：根目录 `TASKLIST.md`。
- `render_changelog_md / read_versions / bump / release`：全局 `CHANGELOG.md`
  （Keep a Changelog 格式）与版本号 bump。

## 领域层（各 record_type 的 Record 子类）

每个文件 = 一个 `@register` 子类 + 模块级委托函数（保持 cli/测试的稳定调用形态，
如 `taskmod.done(cfg, id)` → `Task(spec).done(...)`）。

### `record_types/task.py` — `class Task(Record)`
- `on_ingest`：首个 checkbox 勾选自动 Not Started→In Progress。
- `visible_in_main`：plan 非 Active 的任务从主 TASKLIST 停泊到 plan 里。
- `check_rows`：Steps 超两级、plan 引用悬空。
- `done`：In Progress→Done，要求所有 checkbox 勾完；`new` 的 `--plan` 引用必须存在。

### `record_types/plan.py` — `class Plan(Record)`
- `verbs`（= `TRANSITIONS`）：Draft→Active→Pending Review→Shelved|Archived
  （Shelved 可 resume）；`transition` 由基类驱动。
- `validate_transition`：review 前要求该 plan 下所有任务 Done（且非空）。
- `render_view("tasks")`：plan 记录里的 Task List 章节（勾选框两级 + open items）。

### `record_types/decision.py` — `class Decision(Record)`
- `new`：`-s` 指定 supersede 目标（双向写 supersedes/superseded_by 字段）。
- `toc`：`docs/decisions/_TOC.md`（表格 Status 列用 `emojify` + 下方单行图例）。
- `graph`：mermaid 依赖图；`check_rows`：supersede 完整性（悬空/双向/环）。
- `link`：往 Consequences 追加 "See also"。

### `record_types/changelog.py`（不继承 Record）
- `add / version_bump / release_notes / show_versions`：全局 changelog 的入口，
  渲染逻辑在 views.py。

## 外围

### `scaffold.py`（185 行）— `proj init`
建治理文件（CONTRIBUTING 等，内嵌模板字符串）、建目录、建表（lock 优先）、
seed 决策 0001、收编已存在的手写文件。profiles/ 不拷进项目（回退内置）。

### `check.py`（92 行）— `proj check`
汇总不变量：DB 存在且表结构匹配模板/lock、lock 里的 record_type 都在 profiles、
每个记录文件 id/uuid 与 DB 一致、plan 引用不悬空、task 不超过两级 checkbox。

### `hooks.py`（包根）
往 `.git/hooks/pre-commit` 装一个跑 `proj check` 的 shell 脚本。
安装时把 proj 二进制的**绝对路径**（优先本项目的 pixi dev env）写进 hook——
裸 `proj` 在 PATH 上可能撞名到无关程序（本机就踩过：`/usr/pkg/bin/proj` 是
另一个软件）。非 proj 的已有 hook 拒绝覆盖。

### `cli.py`（487 行）— Typer 命令树
所有命令都是"解析参数 → 调领域层 → 打印结果"，无业务逻辑。
子命令组：`decision`（new/list/toc/graph/link）、`task`（new/done/list）、
`plan`（new/activate/resume/review/shelve/archive/list）、`changelog`（add/version/
notes/list）、通用（init/new/sync/adopt/update/render/check/info/list/show/status/log）、
`hooks install`。

### `__init__.py` / `__main__.py`
版本号 + `python -m proj` 入口。

## 已拆分的结构说明（2026-10-30）

按上面三层拆成 `core/`、`engine/`、`record_types/` 三个子包，文件本身未改逻辑，
只挪位置 + 改 import。`cli/scaffold/check/hooks` 留在包根（它们是入口/外围，
不属于任何一层）。

后续再拆的信号：
- 单个文件持续 >600 行（目前最大 `engine/records.py` 468，真超了优先把
  views 的 tasklist/changelog 渲染拆出去）；
- 出现第二个"非 record_type 的业务模块"（如 web 服务）→ 再加 `proj/serve/`。

**可选整理**（未做）：`views.py` 里 changelog 相关
（`render_changelog_md/read_versions/bump/release`，约 80 行）和
`record_types/changelog.py` 是一对，挪过去会更内聚。纯搬家不改行为，默认不动。
