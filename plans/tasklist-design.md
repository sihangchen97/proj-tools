# tasklist 设计：任务关系与分组（2026-10-31，最终版）

> 状态：**已实现**（2026-10-31）。验证：pytest 46 / e2e 43 / lint / check 全绿。
> 最终模型（经多轮确认）：**task 是唯一被维护的东西**，独立 md 记录；
> 关联靠一个 `parent` 字段（单值，可指向**任意** record 类型——plan/task/experiment/…）；
> 子任务 = `parent` 指向某 task 的那些 task（运行时反查）；
> plan 不需要"里面塞任务"，它名下任务 = `parent` 指向它的 task，渲染时再画出来。

## 1. 核心模型

```
task（独立 md 记录，tasks/A-xxx.md，现状不变）
├─ content：Steps / Open / Notes / Summary / status / changelog（现状）
└─ 关系字段（全在 task.yaml 的 fields:）：
   ├─ parent   单值，父记录 id —— 可指向 plan / task / experiment / 任意类型
   └─ related  多值逗号分隔，重叠/关联记录 id（无方向、不参与状态）

children：不存字段，运行时反查 "parent == 自己" 得到
```

**`plan` 字段取消**（被 parent 取代）：
- 旧"task 属于某 plan" = 新"task.parent = 该 plan 的 id"
- 旧"task 属于全局" = 新"task.parent 为空"
- 新增"task 是某 task 的子任务" = "task.parent = 该 task 的 id"

**`group`/phase 字段取消**：plan 的任务列表就是一棵以 plan 为根的树，渲染时直接画；
要分 Phase 后续再加 group，现在不做（最小方案）。

## 2. 各类型如何"拥有一个列表"

任意 record 想展示名下任务，规则统一：**渲染 `parent == 自己` 的 task + 它们的子树**。

- **plan** 的 `Task List` 章节（`view: tasks`）：画 `parent == 本 plan` 的 task（顶层组），
  每个 task 下缩进画它的子任务（`parent == 该 task`），递归。
- **全局 TASKLIST.md**：画 `parent` 为空的顶层 task + 各自子树。
- **experiment/debt/…**：暂不自动渲染任务列表（规则在，但只有 plan 和全局视图接了）；
  后续要哪个类型画，给它加一个 `view` 节用同一套渲染即可。

## 3. task.yaml 最终形态

```yaml
record_type: task
dir: tasks
numbered: letter
status-allowed:
  - {name: Not Started, emoji: 🕐}
  - {name: In Progress, emoji: 🚧}
  - {name: Done, emoji: ✅}
fields:
  - {name: parent, hint: id of the parent record (any type: plan/task/…); empty = top-level (global)}
  - {name: related, hint: overlapping/related record ids, comma-separated, no state semantics}
derived:
  - {name: Started, to-status: In Progress}
  - {name: Done, to-status: Done}
sections:
  - {name: Steps, hint: checkbox list, write order = step order; use parent for real sub-tasks}
  - {name: Open, hint: open items / decisions needed, one per line}
  - {name: Notes, hint: results & notes after running}
```

## 4. 渲染

**plan Task List 章节**：

```
## Task List

- 🕐 B — Implement parser
  - 🚧 C — Handle edge cases (parent B)
  - 🕐 D — Refactor (parent B)
- ✅ A — Design
```

顶层 = `parent == 本 plan`；子任务缩进，标 `(parent X)`。

**全局 TASKLIST.md**：`parent` 为空的 task + 子树；停泊区概念取消（不再有"挂 plan 但 plan 非 Active"）。

**`proj task show`**：头部追加 `Children: …`（反查）+ `Related: …`。

## 5. 状态判定

`Task.done`：自身 steps 全勾 **且** 所有子任务（`parent == 自己`）Done。

## 6. 不变量（check）

- `parent` 引用的记录存在（任意类型，仿 `_check_plan_ref` 扩到全 registry）
- `parent` 链无环（DFS）
- `related` 引用存在（多值逐个）
- 现有 steps ≤2 层检查保留

## 7. 迁移（现有 `plan` 字段 → `parent`）

- 列改名：DB `task.plan` → `task.parent`（ALTER TABLE … RENAME COLUMN，SQLite 3.25+）
- 存量数据：`plan='1'` 的 task 自动变 `parent='1'`（指向 plan 0001），语义等价，无需改值
- 渲染/查询逻辑：`r['plan']` → `r['parent']`，"属于 plan" 判定 = `parent == plan.id`

## 8. CLI 增量

```
proj new task TITLE [--parent X] [--related X,Y]     # --parent 替代 --plan
proj task show ID          # 追加 Children/Related
```

## 9. 代码改动清单

| 文件 | 改动 |
|---|---|
| `profiles/default/task.yaml` | §3 |
| `src/proj/record_types/task.py` | done gate 加子任务；`_check_parent_ref`（全类型）；check_rows 加 parent/related 存在 + 环检测；`visible_in_main` 改 `parent` 空判定 |
| `src/proj/record_types/plan.py` | render_view 画任务树（顶层 + 递归子任务缩进） |
| `src/proj/engine/views.py` | 全局 TASKLIST：parent 空 = 顶层 + 子树；删停泊区；`r['plan']`→`r['parent']` |
| `src/proj/core/db.py` | task 表 `plan` 列 → `parent`（建表 + 迁移 RENAME） |
| `src/proj/cli.py` | `--plan` → `--parent` |
| `tests/test_task_plan.py` | 改写：parent 关联、子任务 done gate、环检测、related、树渲染 |

## 10. 确认点（全部已确认）

- [x] `plan` 字段取消、统一成 `parent`（单值、任意类型）
- [x] 子任务 = parent 指向 task，children 运行时反查（按 id 序 = 创建顺序）
- [x] plan 列表 = 以 plan 为根的任务树，渲染时画
- [x] 保留 `related`（重叠，无状态）
- [x] 现有 `plan` 列迁移成 `parent`（ALTER RENAME，存量值语义等价）

## 11. 实现备忘（已落地的细节）

- 迁移触发点：`db.ensure_kind`（`proj init` 对每个 record_type 调一次）；
  **不放 `open_db`**——那会破坏"schema 失配应被 check 报出"的不变量
  （`test_check_detects_schema_mismatch` 依赖它）
- 旧库迁移顺序：先 `RENAME COLUMN plan TO parent`（仅当 plan 存在且 parent
  不存在），再按模板 `ADD COLUMN` 缺失字段（related 等）；幂等可重复跑
- `Children:` 管理行渲染在 task md 头部（反查 parent 指向自己的 task，
  不被 parse 回读，和 Status/Summary 同级）
- `proj show task` 的 fields 回显天然覆盖 parent/related，未加专门行
- plan review 门禁只看**直接子任务**（parent == plan）；孙任务经父任务的
  done gate 间接传递
- 文件名：`profiles/default/task.yaml`（task 是唯一维护对象，命名与其他
  record_type 一致；tasklist 只指渲染出来的列表）

## 12. md 头部格式 → front matter（2026-10-31 二次决定）

record md 头部从"html marker 行 + `Status:` 管理行"改为 **YAML front matter**：

    ---
    <!-- proj:task 02a5… -->        <- uuid marker 保留为注释（文件头内）
    status: 🕐 Not Started          <- emoji + 名（status_display）
    （summary 不在框内，见下）
    parent: 1                       <- 模板声明的字段（非空才显示）
    subtask: C                      <- 子任务 id 列表（反查 parent 指向自己的 task）
    started: —                      <- derived 行常显（空 = —）
    done: —
    ---

- 键小写；`subtask`（不叫 children）；uuid **不**做成显式键，是注释行
- `Summary: <一句话>` 写在**灰框下方**（`# 标题` 下、changelog 表上），用户可手写（sync 时
  md wins）；不放进 front matter，顺带消除 incident `## Summary` 章节重名
- 渲染/解析**不走 PyYAML**（PyYAML 扫描器对 4 字节 emoji 报 ScannerError），
  手写 `key: value` 行（值仅在含冒号/引号时 json 加引号）
- legacy 格式（marker 第 1 行 + `Status:` 行）仍被 parse 兼容；任何重渲染的
  文件都会写成 front matter 形态
- 契约测试：`tests/test_init.py::test_front_matter_shape`
