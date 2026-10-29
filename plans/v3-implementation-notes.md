# v3 实现笔记 — 与 `v3-sqlite-core.md` 设计的偏差（2026-10-29）

v3 已按 `v3-sqlite-core.md` 实现（39 tests, lint 绿）。以下偏差全部是**有意简化**，
按 ponytail 原则最小可用；升级路径已注明。

## 1. DB 位置与 git 策略

- 设计：`.proj.sqlite`（根目录），建议提交。
- 实现：`.proj/sqlite`，且 `.proj/.gitignore` 默认**不提交**（`.proj/` 只提交
  lock/toc/githooks）。
- 理由：DB 不提交 → clone 后 `proj init` 走 adopt 重建（该路径已实现并测试）；
  提交则 DB 与 md 可能漂移。不提交更安全。
- 后果：md 的 changelog 表格只有最新 5 条，更早历史在未提交的 DB 里，
  换机器后丢失（设计 §4 已知）。
- 升级：想提交 DB 时删掉 `.proj/.gitignore` 里的 `sqlite` 一行即可。

## 2. `archive` 的来源状态

- 设计 §5a：`Pending review / Shelved → Archived`。
- 实现：只允许 `Pending review → Archived`（`Shelved` 走 `resume` 回 Active）。
- 理由：Shelved 语义是"搁置待恢复"，从 Shelved 直接冻结需二次确认，v1 不做。
- 升级：`plan.py:TRANSITIONS` 里加 `("Shelved", "Archived")` 一行，或改 verb 前置校验。

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
- 实现：kind + record 数 + dir + 编号方式 + ops cheatsheet（`--json` 同）。
- 理由：核心信息（有什么、怎么改）已够 MCP/人用；分布统计一行 sqlite 查询的事，
  需要时再加。
- 升级：`records.py:info()` 加 `GROUP BY status`。

## 6. 其它小决策

- `new` 后自动打开 `$EDITOR`（`-y` 跳过）：设计未写，实现补上，降低手敲频率。
- `--root PATH` 全局选项：设计为 MCP 预留，CLI 一并加上（`cli.py:_ROOT`）。
- 字段名加 SQL 引号：`experiment.commit` 撞 SQLite 保留字，`db.py:_q()` 统一引号。
- plan id 比较统一归一化（`"1"` vs `"0001"`）：`views.py:_norm_id` /
  `plan.py:transition`，用户传 `--plan 1` 或 `--plan 0001` 都成立。
- changelog category 白名单：`Added/Changed/Deprecated/Removed/Fixed/Security`，
  未知 category 报错（防拼写污染）；小写自动规范化（`added:` → `Added`）。
- 变更操作（new/status_set）后自动重渲染视图：TASKLIST/plan 章节即时一致，
  不等下一次 `proj sync`。

## 7. 已验证（testproj 冒烟 + 39 单测）

init（含 seed 决策 0001 + 幂等）→ supersede 双向状态 → plan/task 关联 →
sync 自动 In progress → task done 校验 → 状态机全路径（含非法迁移拒绝）→
TASKLIST 显隐（park/promote）→ plan Task List 视图章节 → changelog
add/version/release-notes → adopt（uuid 生成 + id 前缀剥离）→
check（6 类漂移检测）→ 全部错误路径友好报错。
