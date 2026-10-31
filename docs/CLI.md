# proj CLI 参考

> 入口：`proj`（pixi dev 环境或 `pip install .`）。
> `--root PATH`：项目根，**默认 = 当前目录**（不向上找）。项目根 = 含
> `.proj.lock` / `.proj.sqlite` 的目录（由 `proj init` 创建）。
> **未 init 的目录里只有 `proj init` 可用**；其余命令会报
> `error: no proj project in <cwd> …`。
> record_type = 模板注册的任意类型（task/plan/decision/debt/incident/experiment…）；
> **大小写不敏感**，可用类型名或其 code（`DEC`/`dec`/`Decision` 都 = decision，`T` = task）。
> **两种命令**：生命周期命令（`proj init`/`proj sync`/…）+ 每个 record_type 的动态子命令
> `proj <rt> new|list|show|<verb>`（`<verb>` = 该类型模板 `verbs:` 里声明的，运行时生成）。


## 生命周期（顶层命令）

```
proj init [--profile <PROFILES…>]
proj sync [<FILES>]
proj adopt [<FILES>]
proj update
proj render [--record-type <RECORD_TYPE>] [--id <ID>]
proj check
proj info [--json]
proj docs
```

- **DB 是唯一事实源**；md 是给人看/手写的投影。`sync` 单向 md→DB，`render` 单向 DB→md。
- 手写 md 后跑 `proj sync`（或 `proj update`）；只改 DB 后跑 `proj render`。
- 记录的 `# <ID>. <标题>` 行**可手改**：`sync` 把新标题回收进 DB 并把文件重命名到新
  slug（id 不变；changelog 块、`<!-- proj:head → -->` 围栏内的 metadata 仍由工具维护）。
- `check` 检查：.proj.lock 模板在、表 schema 匹配模板、md 各 section 与 DB 一致、
  record_type 特有不变量（task：parent/related 存在、无父环、Steps ≤2 层）。
- 裸跑 `proj`（无子命令）：已 init → 项目状态（同 `info`）；未 init → 只有 `init` 可用。
- 新建默认打开 `$EDITOR`/`$VISUAL`；`-y` 跳过。
- 改状态用**模板动词** `proj <rt> <verb> ID`（等价 `record.transition(cfg, verb, id)`）；
  `from: Any` = 任意当前状态都能触发（直达目标，如 `deprecate`/`close`），
  `from: <状态>` = 必须在该状态（如 plan `activate` 只从 Draft）。带门禁的动词
  （如 plan `review` 要求名下任务全 Done、task `done` 要求 checkbox 全勾）由引擎校验。

## task

```
proj task new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj task list [--status <STATUS>]
proj task show <ID> [--history]
proj task update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj task render [id <ID>]
proj task start <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj task done <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj task fail <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

- 状态：`Draft ✏️ → In Progress 🚧 → Done ✅ / Failed ❌`（new 默认 Draft）。
- `task done` 门禁：自己的 Steps 全勾上（子任务独立，父任务不等子任务）。
- `task fail`：标记无法完成/放弃（Any → Failed）；Failed 不是 done，不会计入
  done 日期，plan `review` 门禁照样阻塞。
- `--parent` 可指向 plan 或另一个 task（子任务）；子任务建出来后出现在父的 Task List 里（缩进）。
- 主区 `TASKLIST.md` 放**树根**（无 parent 的 task，+ 所有声明 `type: task` 章节的
  record_type 如 plan/experiment）；挂了 parent 的任务缩进渲染在父节点下。
- 第一次勾 checkbox 后 `sync` 自动升 In Progress（on_ingest 钩子）。
- 本地子步骤用 Steps 的 2 层 checkbox（轻量、不占 id）；要独立状态/进列表的子任务
  用 `--parent` 建真 task。

## debt

```
proj debt new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj debt list [--status <STATUS>]
proj debt show <ID> [--history]
proj debt update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj debt render [id <ID>]
proj debt draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj debt open <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj debt progress <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj debt repay <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj debt waive <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

## decision

```
proj decision new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj decision list [--status <STATUS>]
proj decision show <ID> [--history]
proj decision update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj decision render [id <ID>]
proj decision draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj decision propose <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj decision accept <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj decision reject <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj decision deprecate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

- 状态：`Proposed 🌱 → Accepted ✅ → Deprecated 📛`。
- `-s`：被替代的决策 id（所有类型通用；写时校验**同类型**，纯 relations 边，
  不自动改被替代记录的状态——要废弃时手动 `proj decision deprecate`）。
- 不变量：supersede 链无环。

## experiment

```
proj experiment new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj experiment list [--status <STATUS>]
proj experiment show <ID> [--history]
proj experiment update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj experiment render [id <ID>]
proj experiment draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj experiment propose <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj experiment run <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj experiment close <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj experiment abandon <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

## incident

```
proj incident new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj incident list [--status <STATUS>]
proj incident show <ID> [--history]
proj incident update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj incident render [id <ID>]
proj incident draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj incident open <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj incident mitigate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj incident close <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj incident reopen <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

## plan

```
proj plan new [<TITLE>] [--supersede -s <SUPERSEDE…>] [--parent <PARENT>] [--related -r <RELATED…>] [--commit <COMMIT>] [--summary <SUMMARY>] [--yes -y]
proj plan list [--status <STATUS>]
proj plan show <ID> [--history]
proj plan update <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan render [id <ID>]
proj plan activate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan resume <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan review <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan shelve <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan archive <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
proj plan freeze <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--comment -c <COMMENT>]
```

```
Draft → Active → Pending Review → Shelved
          ↑____________ resume ______|
          Active → Pending Review → Archived
```
- 任务通过 `--parent <plan-id>` 挂到 plan 下（不是 plan 里塞任务）。
- 每个 plan 的 md 有渲染的 `## Task List` 章节（以 plan 为根的任务树）。
- `review` 门禁：直接子任务全 Done。

## changelog

```
proj changelog add <ENTRY> [--version <VERSION>]
proj changelog version [<NAME>] [--bump <BUMP>]
proj changelog release-notes <VERSION>
proj changelog list
```

- category / unreleased 名 / 文件名全在 `profiles/changelog.yaml`（每项目一份）。
- `version [NAME]` 或 `--bump major|minor|patch`（基于最新版本号）。
- `release-notes V` 打印某版本的段落（贴 PR / tag body）。

## hooks

```
proj hooks install
```

装 `.git/hooks/pre-commit`：提交前跑 `proj check`，不过则拒。

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
