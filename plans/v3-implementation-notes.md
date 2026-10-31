# v3 实现笔记 — 与 `v3-sqlite-core.md` 设计的偏差（2026-10-29）

v3 已按 `v3-sqlite-core.md` 实现（39 tests, lint 绿）。以下偏差全部是**有意简化**，
按 ponytail 原则最小可用；升级路径已注明。

## 1. DB 位置与 git 策略（2026-10-29 定稿）

- DB = **`.proj.sqlite`**（项目根单个文件；2026-10-29 由 `.proj/sqlite`
  改名，`.proj/` 文件夹取消），**默认提交**（设计 §4 建议；clone 即用，
  md 之外的 changelog 全量历史不丢）。
- hooks 装进 `.git/hooks/pre-commit`（不需要 core.hooksPath）；decision toc
  写到 `docs/decisions/TOC.md`。
- 仓库根 `.gitignore` 的 `*.sqlite` 兜底会匹配 `.proj.sqlite`，已加
  `!.proj.sqlite` 例外保证 DB 默认提交。
- 风险：DB 进 git 后，多人同时 sync 会 last-write-wins 互相覆盖——
  协作前需并发保护。
- 升级：无。

## 2. `archive` 的来源状态

- 设计 §5a：`Pending review / Shelved → Archived`。
- 实现（2026-10-31 补齐）：`Pending Review → Archived`（archive）**与**
  `Shelved → Archived`（freeze，新 verb）；`Shelved → Active`（resume）仍在。

## 3. Steps 第 3 层嵌套：报错而非压平

- 设计 §5b："出现第 3 层嵌套 → check 告警并压平为 sub-task"。
- 实现：`proj check` 直接报错（不自动改内容）。
- 理由：自动压平改用户内容风险大；报错让人/LLM 决定（压平还是拆独立 task）。
- 升级：`check.py` 里把该条改成 normalize 后 `proj sync` 一次。

## 4. 子任务编号 A.01 未实现

- 设计 §5c："子任务两位数字，视图显示 A.01"。
- 实现：子任务原样缩进显示，无编号。
- 理由：编号是视图装饰，价值低；需要时 render 时按顺序派生即可（不改 DB）。
- 升级：`views.py:_sub_task_lines` 里加计数器。

## 5. `proj info` 输出比设计 §8a 简化

- 设计：含每个 kind 的 status 分布、Unreleased 条目数、latest version。
- 实现（2026-10-31 补齐）：kind + record 数 + dir + 编号方式 + **每 kind 的
  status 分布** + changelog 条目数/latest version + ops cheatsheet（`--json` 同）。
- 仍未做：Unreleased 条目单列（现在只报 total entries + versions）。

## 6. 其它小决策

- `new` 后自动打开 `$EDITOR`（`-y` 跳过）：设计未写，实现补上，降低手敲频率。
- `--root PATH` 全局选项：设计为 MCP 预留，CLI 一并加上（`cli.py:_ROOT`）。
- 字段名加 SQL 引号：`experiment.commit` 撞 SQLite 保留字，`db.py:_q()` 统一引号。
- 内置模板移到仓库根 `templates/`（用户要求），`bundled_dir()` 优先仓库根、
  回退包内。wheel 已配 `force-include = { "templates" = "proj/templates" }`
  并在干净 venv 验证（init/supersede/check 全流程过）。
- plan id 比较统一归一化（`"1"` vs `"0001"`）：`views.py:_norm_id` /
  `plan.py:transition`，用户传 `--plan 1` 或 `--plan 0001` 都成立。
- changelog category 白名单：`Added/Changed/Deprecated/Removed/Fixed/Security`，
  未知 category 报错（防拼写污染）；小写自动规范化（`added:` → `Added`）。
- 变更操作（new/status_set）后自动重渲染视图：TASKLIST/plan 章节即时一致，
  不等下一次 `proj sync`。
- status 命名（2026-10-29 用户拍板）：`Proposed` → **`Pending`**
  （decision/debt/experiment）；emoji 方案 = 模板 `status-allowed` 条目可带
  `{name, emoji}`（可选），"进行中" 状态统一 🚧（task/debt In progress、
  experiment Running、plan Active）；emoji 只出现在视图（_TOC/list），
  md 的 `Status:` 行保持纯文字；emoji 随 lock 持久化。
- supersede 从 status 改为**多值字段**（`supersedes`/`superseded_by`，
  逗号分隔）：一个决策可取代多个，一个旧的也可被多个取代（追加不覆盖）；
  存储用规范化 id（`-s 1` → `"0001"`）；decision 只剩
  Pending/Accepted/Deprecated 三态；`proj check` 校验悬挂引用、
  双向一致性（A supersedes B ⇒ B.superseded_by 含 A）、环（DFS）。
- init **不拷** `templates/` 进项目：registry 回退 bundled；项目想覆盖
  kind 自建 `templates/<profile>/<kind>.yaml`（load_registry 项目目录优先）。
- 渲染视图文件约定：托管目录里 `_` 前缀 = 视图（如 `docs/decisions/_TOC.md`），
  check/sync/adopt/seed 全部跳过 `_` 前缀文件。

## 7. 已验证（.testproj 冒烟，scripts/e2e.sh + 单测）

init（含 seed 决策 0001 + 幂等）→ supersede 双向状态 → plan/task 关联 →
sync 自动 In progress → task done 校验 → 状态机全路径（含非法迁移拒绝）→
TASKLIST 显隐（park/promote）→ plan Task List 视图章节 → changelog
add/version/release-notes → adopt（uuid 生成 + id 前缀剥离）→
check（6 类漂移检测）→ 全部错误路径友好报错。
