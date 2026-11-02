# proj CLI 参考

> 入口：`proj`（pixi dev 环境或 `pip install .`）。
> `--root PATH`：项目根，**默认 = 当前目录**（不向上找）。项目根 = 含
> `.proj.lock` / `.proj.sqlite` 的目录（由 `proj init` 创建）。
> **未 init 的目录里只有 `proj init` 可用**；其余命令会报
> `error: no proj project in <cwd> …`。
> record_type = 模板注册的任意类型（task/plan/decision/debt/incident/experiment…）；
> **大小写不敏感**，可用类型名或其 code（`DEC`/`dec`/`Decision` 都 = decision，`T` = task）。
> **三种命令**：生命周期命令（`proj init`/`proj sync`/…）；**跨类型**命令
> `proj show ID` / `proj ls [--type T] [--status S]` / `proj open ID`（id 自带类型，
> 不用先写类型）；每个 record_type 的动态子命令 `proj <rt> new|ls|show|update|<verb>`
> （`<verb>` = 该类型模板 `verbs:` 里声明的，运行时生成，帮助从模板推导）。
> **操作者（S.4）**：每条 changelog 记录是谁改的——`PROJ_AGENT`（agent 启动时设置，
> 如 `PROJ_AGENT=claude proj …`）优先，否则取系统用户名；`proj show ID --history`
> 和 `--json` 都带这个字段。


## 生命周期（顶层命令）

```
proj init [--profile <PROFILES…>] [--zoo]
proj sync [<FILES>]
proj adopt [<FILES>]
proj render [<REF>]
proj show <ID> [--history] [--json]
proj ls [--status <STATUS>] [--type <TYPE>] [--json]
proj open <ID>
proj check
proj info [--json]
proj docs
proj bug [<TEXT>] [--cmd <CMD>] [--json] [--root <ROOT>]
```

- **DB 是唯一事实源**；md 是给人看/手写的投影。`sync` 单向 md→DB，`render` 单向 DB→md。
- 手写 md 后跑 `proj sync`（不带参数 = 全量 + 渲染全部视图）；只改 DB 后跑 `proj render`。
- 跨类型：`proj show ID` / `proj ls [--type T]` / `proj open ID`（$EDITOR 打开 md，关闭后自动
  sync）——id 自带类型（`T-…`/`DEC-…`），不用再写类型；`proj <rt> show/ls` 等价地限定到某类型。
- 记录的 H1 是 `# <ID>. <标题>`：标题文字**可手改**（编号 id 在文件名 + 首行 marker
  里体现，DB 为准）：`sync` 把新标题
  回收进 DB 并把文件重命名到新 slug（id 不变；changelog 块、`<!-- proj:head … -->`
  围栏内的 metadata 仍由工具维护）。
- `check` 检查：.proj.lock 模板在、表 schema 匹配模板、md 各 section 与 DB 一致、
  record_type 特有不变量（task：parent/related 存在、无父环）。
- 裸跑 `proj`（无子命令）：已 init → 项目状态（同 `info`）；未 init → 只有 `init` 可用。
- 新建默认打开 `$EDITOR`/`$VISUAL`；`-y` 跳过（agent/非终端下自动不打开）。
- 改状态用**模板动词** `proj <rt> <verb> ID`（等价 `record.transition(cfg, verb, id)`）；
  `from: Any` = 任意当前状态都能触发（直达目标，如 `deprecate`/`close`），
  `from: <状态>` = 必须在该状态（如 plan `activate` 只从 Draft）。带门禁的动词
  （模板 `gates:` 声明：如 plan `review` 要求名下任务树全部结束、task `done` 同理）
  由引擎统一校验。

## 所有 record 共用的命令

```
proj <rt> new [<TITLE>] [-s <SUMMARY>] [--supersede <ID…>] [--parent <PARENT>]     [-r <RELATED…>] [--commit <COMMIT>] [-y]
proj <rt> ls [--status <STATUS>]
proj <rt> show <ID> [--history] [--json]
proj <rt> update <ID> [-t <TITLE>] [-s <SUMMARY>] [-m <MESSAGE>]
```

- 每个 record_type 都有同一组通用子命令（`<rt>` = 类型名或 code，大小写不敏感）；
  类型特有的状态机动词（`task start`/`plan review`/…）在该类型自己的小节里。
- `new`：`-s`（一句话摘要）、`--supersede`（同类型、可重复）、`--parent`
  （仅 task 挂父节点）、`-r`（related，任意类型）、`--commit`（git 提交号）、
  `-y` 跳过编辑器（agent/非终端下自动不打开）。
- `show --history`：该记录的完整 changelog（每条 = 状态 + 可选 message）；`--json` 机器可读。
- `update`：`-t` 改标题（文件随之重命名为新 slug，id 不变）、`-s` 改一句话摘要、
  `-m` 追加一条 changelog（`Category: text`，跟 git 的 `-m` 习惯一致）。
- `ls` 列该类型的记录（一行：id、状态、标题、parent）；`list` 是隐藏别名。
- `render [REF|TYPE]…` 只 DB→md（不带 md 输入，全局一条）：省略参数 = 全部记录 +
  全部视图，否则只渲染指定的记录或类型。
- `task move ID (--parent X | --detach)`：改挂已有任务（两个选项必须且只能给一个）；
  挂在已关闭记录下、挂到自己或自己的后代上都会被拒绝。

## task

```
proj task move <ID> [--parent <PARENT>] [--detach]
proj task start <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj task done <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj task fail <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

- 状态：`Draft ✏️ → In Progress 🚧 → Done ✅ / Failed ❌`（new 默认 Draft）；
  Done 和 Failed 都是**终止状态**（任务结束，不再阻塞父任务门禁）。
- `task done` 门禁：整棵后代任务树都处于终止状态（Done/Failed）**且** Acceptance
  Criteria 清单（`items:` 节，用 `proj item check/set` 管理）全部已处理才放行——
  父任务先于子任务结束、或仍有未处理的 criterion 都会被拒绝。
- `task fail`：标记无法完成/放弃（Any → Failed）；Failed 不是 done，不计入 done
  日期，但同样满足父任务的门禁。
- `--parent` 可指向 plan/experiment 等记录或另一个 task（子任务，任意深度）；
  无 `--parent` 的任务属于虚拟分组 **Unparented**。已关闭的记录
  （处于其 `gates` 门禁状态的 plan/task）不能再挂新任务，`task move` 同样拒绝；
  已终止状态的任务想再开时，其祖先处于门禁状态会被拒绝（防重开）。
- 任意挂有任务的记录，其 md 在 head 围栏下渲染一个 `<details open>` 的 **Tasks**
  块，**完全由 DB 生成**（不再保留手写的任务行）：每个任务一行 checkbox+超链接
  （id-slug 文件名，相对路径），按**任务树深度缩进**展开整棵后代树（不封顶，任意
  深度，完成的任务也保留以体现进度）；有 summary 的任务在其后以斜体跟一行摘要；
  带清单的任务行末附 `n/m`（已处理/总数）。改挂用 `proj task move`。
- 全局 `WIP.md`（取代旧 TASKLIST.md）：正在做/进行中的记录自动登记——记录
  进入其类型首个 `wip` 状态（如 task In Progress、plan Active）即入列，退出
  （如任务 Done/Failed）即移除；也可 `proj wip add/return <ID…>` 手动登记/移出。
  每条含记录的状态、标题与清单项（直接在这里勾清单 = 改记录，`proj sync` 写回）；
  无 parent 的任务归在 `## Unparented` 一节。

## debt

```
proj debt draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj debt open <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj debt progress <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj debt repay <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj debt waive <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

## decision

```
proj decision draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj decision propose <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj decision accept <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj decision reject <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj decision deprecate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

- 状态：`Proposed 🌱 → Accepted ✅ → Deprecated 📛`。
- `--supersede`：被替代的决策 id（所有类型通用，同类型、可重复；写时校验
  **同类型**，纯 relations 边，不自动改被替代记录的状态——要废弃时手动
  `proj decision deprecate`）。
- 不变量：supersede 链无环。

## experiment

```
proj experiment draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj experiment propose <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj experiment run <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj experiment close <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj experiment abandon <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

## incident

```
proj incident draft <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj incident open <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj incident mitigate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj incident close <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj incident reopen <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

## plan

```
proj plan activate <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj plan resume <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj plan review <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj plan shelve <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj plan archive <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
proj plan freeze <ID> [--title -t <TITLE>] [--summary -s <SUMMARY>] [--message -m <MESSAGE>]
```

```
Draft → Active → Pending Review → Shelved
          ↑____________ resume ______|
          Active → Pending Review → Archived
```
- 任务通过 `--parent <plan-id>` 挂到 plan 下（不是 plan 里塞任务）；Draft 的
  plan 名下的任务不进全局 WIP，`activate` 后自动登记。
- 每个 plan 的 md 有渲染的 **Tasks** 块（名下整棵任务树，扁平列出）。
- `review`/`shelve`/`archive` 门禁：整棵后代任务树都终止（Done/Failed）才放行；
  `archive` 另要求 Success Criteria 清单（`proj item check/set`）全部已处理
  （`shelve` 暂停不设此门禁）。

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

## item

```
proj item ls [<REF>] [--group <GROUP>] [--open] [--type <TYPE>] [--owner <OWNER>] [--overdue] [--json]
proj item add <REF> <TEXT> [--group <GROUP>] [--after <AFTER>] [--state <STATE>] [--note <NOTE>] [--owner <OWNER>] [--due <DUE>]
proj item set <ITEM> <STATE> [--note <NOTE>] [--commit <COMMIT>]
proj item check <ITEM> [--note <NOTE>] [--commit <COMMIT>]
proj item uncheck <ITEM>
proj item edit <ITEM> [--text <TEXT>] [--note <NOTE>] [--owner <OWNER>] [--due <DUE>]
proj item mv <ITEM> [--group <GROUP>] [--ungroup] [--after <AFTER>] [--first]
proj item rm <ITEM>
proj item group
```

- 结构化清单（模板 `items:` 声明的某个 `##` 节）：task 的 Acceptance Criteria、
plan 的 Success Criteria、debt 的 Repayment Plan、decision 的 Confirmation…
编号**全局三位**（无分组 `#001`；分组内 `#A01`，组字母 A–Z 由工具分配、永不复用），
解析任意宽度。引用格式 `ID#n`（`T-000001#A01`）；没有 `items:` 的类型用这些
命令会报 `has no items`。
- `add REF TEXT`：自动编号、追加到其组末尾（`--group L` 指定组、`--after ITEM`
指定位置、`--state` 起始状态、`--note`/`--owner`/`--due`，后三者需该类型启用对应
字段）；返回该记录的最新清单。`ls [REF]`：`ID#n  state  text  [owner]  [due]`
（省 REF = 全部记录；`--group`/`--open`/`--owner`/`--overdue` 过滤，`--type`
限定类型）。
- `set ID#n STATE`：改状态（如 met / missed / done / skipped）；`check ID#n` =
标记为已处理（该清单第一个 closed 状态）；`uncheck ID#n` = 重开（回到第一个未处理
状态）；`edit ID#n --text/--note/--owner/--due`（空串清除该字段）。
- `mv ID#n`：`--group L`/`--ungroup`/`--after ITEM`/`--first` 四选一。
**跨组移动换新编号**（旧编号作废，永不复用）；组内移动只换顺序。
`rm ID#n` 删除（编号永不复用）。
- 分组是清单里的 `### L · 名称` 标题（空组也保留）：`item group add REF 名称`
分配下一个字母；`group rename` 改名称（字母不变）；`group rm REF L`
（有成员须 `--ungroup-items`/`--delete-items`）；`group mv REF L --after B/--first`。
- 操作前 md 必须与 DB 一致，否则报 `unsynced edits`（先 `proj sync`）。处于
`gates.items` 门禁状态的记录（如 Archived plan）拒绝重开/新增未处理项。
- 进度：任务带清单时，其行末在 Tasks 块与 `WIP.md` 显示 `n/m`（已处理/总数）。

## wip

```
proj wip add <IDS>
proj wip return <IDS>
proj wip ls
```

- 全局 `WIP.md`（取代旧 TASKLIST.md）：正在做/进行中的记录自动登记——记录进入
  其类型首个 `wip` 状态（如 task In Progress、plan Active）即入列，退出（如任务
  Done/Failed）即移除。也可 `proj wip add <ID…>` / `return <ID…>` 手动登记/移出
  （`ls` 列当前 WIP）。
- 每条含状态 + 标题，清单项直接列在条目下（含分组）：在 WIP.md 里直接勾清单 =
  改记录，下次 `proj sync` 写回 DB（记录 md 也改了就冲突，提示二选一）。

## 常用工作流

```
proj init
proj plan new "Ship v1" -y && proj plan activate 1
proj task new "Parse config" --parent 1 -y
proj task new "Edge cases" --parent B -y      # B = 上面的任务 id（子任务）
proj task start A -y
proj task done B && proj task done A          # 子任务先结束，父任务门禁才放行
proj plan review 1   # 门禁通过（任务树全终止）→ Pending Review
proj changelog add "Added: v1 发布"
proj changelog version --bump major
```
