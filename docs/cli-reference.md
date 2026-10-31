# proj CLI 参考（2026-10-31）

> 入口：`proj`（pixi dev 环境或 `pip install .`）。
> 全局选项：`--root PATH`（默认从当前目录向上发现项目根）。
> 除 `init` 外所有命令都要求在一个已 init 的项目里（找到 `.proj.lock` / `.proj.sqlite`）。
> 记录类命令里的 record_type = 模板注册的任意类型（task/plan/decision/debt/incident/experiment…）。

## 总览

| 组 | 命令 | 干什么 |
|---|---|---|
| 生命周期 | `init` / `sync` / `adopt` / `update` / `render` / `check` | 建项目、md↔DB 往返、校验 |
| 记录（通用） | `new` / `list` / `show` / `status` / `log` | 对任意 record_type 的 CRUD |
| task | `task new` / `done` / `list` | 任务（含子任务门禁） |
| plan | `plan new` / `activate` / `review` / `shelve` / `resume` / `archive` / `freeze` / `list` | 计划状态机 |
| decision | `decision new` / `list` / `toc` / `graph` / `link` | ADR 风格决策 |
| changelog | `changelog add` / `version` / `release-notes` / `list` | CHANGELOG.md |
| hooks | `hooks install` | pre-commit 装 `proj check` |

## 生命周期

```
proj init [--profile X]…            # 建项目：拷模板、建表（.proj.lock 锁定模板版本）、
                                    # 种子文件（0001 决策）、adopt 已有 md
proj sync [FILE…]                   # md → DB（校验+规范化）→ 重渲染全部 md
proj adopt [FILE…]                  # 吸收已有 md：有 uuid marker 直接收，没有则按
                                    # record_type 规则校验 id 并生成 uuid
proj update                         # sync 全部 + 重渲染所有视图（TASKLIST.md /
                                    # CHANGELOG.md / 各 view 章节）
proj render [TYPE] [ID]             # DB → md（不读 md）；无参 = 全部记录 + 视图
proj check                          # 校验不变量：DB↔lock↔模板↔md；失败 exit 1
```

- **DB 是唯一事实源**；md 是给人看/手写的投影。`sync` 单向 md→DB，`render` 单向 DB→md。
- 手写 md 后跑 `proj sync`（或 `proj update`）；只改 DB 后跑 `proj render`。
- `check` 检查：lock 模板在、表 schema 匹配模板、md 各 section 与 DB 一致、
  record_type 特有不变量（task：parent/related 存在、无父环、Steps ≤2 层）。

## 记录（通用）

```
proj new TYPE TITLE [-s N] [--parent N] [--field k=v]… [--summary S] [-y]
                                    # 建新记录：DB 插入 + 渲染 md（-y 不开编辑器）
                                    #   -s        decision 专用：supersede 的 id（可重复/逗号）
                                    #   --parent  task 专用：父记录 id（任意类型：plan/task/…）
                                    #   --field   模板声明的字段：name=value（可重复）
                                    #   --summary 一句话摘要（结果/进展）
proj list TYPE [--status S]         # 列出某 record_type 的记录（DB）
proj show TYPE ID [--history]       # 看单条记录（+ --history 显示完整 changelog）
proj status TYPE ID NEW_STATUS      # 改状态（对照模板允许集校验）
proj log TYPE ID "Category: 摘要"   # 追加一条 changelog（纯文本 = 默认 category）
```

- 新建默认打开 `$EDITOR`/`$VISUAL`；`-y` 跳过。
- 状态机类型（plan）的状态变更建议走专门动词（`proj plan activate 1`），
  因为动词带**门禁**（如 review 要求名下任务全 Done）；`proj status` 是裸改。

## task（任务）

```
proj task new TITLE [--parent N] [--summary S] [-y]
                        # 建任务；--parent 可指向 plan 或另一个 task（子任务）
                        # 子任务建出来后出现在父的 Task List 里（缩进）
proj task done ID       # → Done：要求 ① 自己的 Steps 全勾上 ② 所有子任务已 Done
proj task list [--status S]
```

- 状态：`Not Started 🕐 → In Progress 🚧 → Done ✅`。
- 第一次勾 checkbox 后 `sync` 自动升 In Progress（on_ingest 钩子）。
- 主区 `TASKLIST.md` 只放**顶层任务**（无 parent）；挂了 parent 的任务渲染在
  父记录的 `Task List` 章节（plan 是 `## Task List`，task 的子任务直接缩进）。
- 本地子步骤用 Steps 的 2 层 checkbox（轻量、不占 id）；要独立状态/进列表的
  子任务用 `--parent` 建真 task。

## plan（计划，状态机）

```
proj plan new TITLE [--summary S] [-y]
proj plan activate  ID      # Draft → Active
proj plan review    ID      # Active → Pending Review（门禁：直接子任务全 Done）
proj plan shelve    ID      # Pending Review → Shelved
proj plan resume    ID      # Shelved → Active
proj plan archive   ID      # Pending Review → Archived
proj plan freeze    ID      # Shelved → Archived
proj plan list [--status S]
```

```
Draft → Active → Pending Review → Shelved
          ↑____________ resume ______|
          Active → Pending Review → Archived
```

- 任务通过 `--parent <plan-id>` 挂到 plan 下（不是 plan 里塞任务）。
- 每个 plan 的 md 有渲染的 `## Task List` 章节（以 plan 为根的任务树）。

## decision（ADR 风格）

```
proj decision new TITLE [-s N]… [--summary S] [-y]
                        # -s：被替代的决策 id（写入 supersedes 字段，
                        # 同时把旧决策置 Deprecated + superseded_by）
proj decision list [--status S]
proj decision toc       # 重生成 docs/decisions/_TOC.md（一行图例+列表）
proj decision graph [OUT]   # mermaid 图（supersede 关系），默认 stdout
proj decision link TARGET LINKS…   # 往某决策的 Consequences 加 See-also 交叉引用
```

- 状态：`Proposed 🌱 → Accepted ✅ → Deprecated 📛`（被 supersede 时自动置 Deprecated）。
- 不变量：supersede 链无环。

## changelog（CHANGELOG.md）

```
proj changelog add "Added: 一句话" [--version V]
                                    # 默认进 unreleased 占位（配置在 profiles/changelog.yaml）
proj changelog version [NAME] [--bump major|minor|patch]
                                    # 发版：把 unreleased 挪到具体版本
                                    #   NAME 或 --bump（基于最新版本号）
proj changelog release-notes V      # 打印某版本的段落（贴 PR / tag）
proj changelog list                 # 已发版本列表（新→旧）
```

- category / unreleased 名 / 文件名全在 `profiles/changelog.yaml`（每项目一份）。

## hooks

```
proj hooks install    # 装 .git/hooks/pre-commit（提交前跑 proj check，不过则拒）
```

## 常用工作流

```
proj init
proj plan new "Ship v1" -y && proj plan activate 1
proj task new "Parse config" --parent 1 -y
proj task new "Edge cases" --parent B -y      # B = 上面的任务 id（子任务）
# …编辑 tasks/*.md 勾 checkbox…
proj sync            # md→DB，自动升 In Progress
proj task done B && proj task done A
proj plan review 1   # 门禁通过 → Pending Review
proj changelog add "Added: v1 发布"
proj changelog version --bump major
```
