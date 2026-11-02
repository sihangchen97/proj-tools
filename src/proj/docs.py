"""Reference docs as code: docs/CLI.md and docs/MCP.md are generated from this
module (`proj docs`) so they can never drift from the CLI/MCP code.
Run `proj docs` (from this repo or any project) to regenerate them."""
from __future__ import annotations

import click
from typer.main import get_group

from proj import cli as cli_mod
from proj.core import fsio
from proj.core.db import LOCK_FILE
from proj.engine import store as rec
from proj.mcp import PROJ_HOME_ENV, mcp

CLI_REF_PATH = "docs/CLI.md"
MCP_REF_PATH = "docs/MCP.md"

# ---- CLI ---------------------------------------------------------------------------------

_CLI_INTRO = f"""\
# proj CLI 参考

> 入口：`proj`（pixi dev 环境或 `pip install .`）。
> `--root PATH`：项目根，**默认 = 当前目录**（不向上找）。项目根 = 含
> `{LOCK_FILE}` / `.proj.sqlite` 的目录（由 `proj init` 创建）。
> **未 init 的目录里只有 `proj init` 可用**；其余命令会报
> `error: no proj project in <cwd> …`。
> record_type = 模板注册的任意类型（task/plan/decision/debt/incident/experiment…）；
> **大小写不敏感**，可用类型名或其 code（`DEC`/`dec`/`Decision` 都 = decision，`T` = task）。
> **三种命令**：生命周期命令（`proj init`/`proj sync`/…）；**跨类型**命令
> `proj show ID` / `proj ls [--type T] [--status S]` / `proj open ID`（id 自带类型，
> 不用先写类型）；每个 record_type 的动态子命令 `proj <rt> new|ls|show|update|<verb>`
> （`<verb>` = 该类型模板 `verbs:` 里声明的，运行时生成，帮助从模板推导）。
"""


def _param_token(p: click.Parameter) -> str:
    """`<NAME>` (positional) / `--opt <NAME>` / `[…optional…]` from the real param."""
    flags = " ".join(f for f in p.opts if f.startswith("--"))
    short = next((f for f in p.opts if f.startswith("-") and len(f) == 2), "")
    positional = not any(f.startswith("-") for f in p.opts)
    if positional:
        name = p.name.upper().rstrip("_")
        arg = f"<{name}…>" if getattr(p, "multiple", False) else f"<{name}>"
        return arg if p.required else f"[{arg}]"
    opt = " ".join(x for x in (flags, short) if x)
    if not getattr(p, "is_flag", False):
        name = p.name.upper().rstrip("_")  # `id_` param → `<ID>` (no Python-name leak)
        arg = f"<{name}…>" if getattr(p, "multiple", False) else f"<{name}>"
        val = f"{opt} {arg}".strip()
        return val if p.required else f"[{val}]"
    return opt if p.required else f"[{opt}]"


def _cmd_tokens(cmd: click.Command) -> list[str]:
    return [_param_token(p) for p in cmd.params if p.name != "help"]


def _cmd_line(cmd: click.Command, prefix: str = "proj") -> str:
    """`<prefix> <name> <REQ>… [opt]…` from the real click command (names + params)."""
    return " ".join([f"{prefix} {cmd.name}"] + _cmd_tokens(cmd))


def _cli_sections() -> list[tuple[str, str]]:
    """(heading, code-block) for every CLI command, in typer definition order.

    Lifecycle commands are static (the top-level group). Record_types are dynamic
    (built on demand by `_DynamicGroup.get_command`), so their sub-groups are
    generated here from the bundled registry; changelog/hooks are static sub-groups."""
    app = cli_mod.app
    group_names = {g.name for g in app.registered_groups}
    click_group = get_group(app)
    top = [c for c in click_group.commands.values() if c.name not in group_names]
    sections: list[tuple[str, str]] = []
    if top:
        sections.append(("生命周期（顶层命令）",
                         "\n".join(_cmd_line(c) for c in top)))
    sections.append(("所有 record 共用的命令", _COMMON_CMD_BLOCK))
    # dynamic record_type groups: the type-specific verbs only (new/ls/show/update are the
    # shared commands shown once above — do not repeat them here)
    for rtype in rec.load_registry(None):
        grp = cli_mod._build_record_group(rtype)
        lines = [_cmd_line(c, prefix=f"proj {rtype}")
                 for c in grp.commands.values()
                 if not c.hidden and c.name not in _COMMON_CMD_NAMES]
        if lines:
            sections.append((rtype, "\n".join(lines)))
    # static sub-groups (changelog / hooks are not record_types)
    for g in app.registered_groups:
        cmd = click_group.commands[g.name]
        sections.append((g.name, "\n".join(_cmd_line(c, prefix=f"proj {g.name}")
                                           for c in cmd.commands.values())))
    return sections


# Hand-written group notes, keyed by heading — the code blocks are the source of truth.
# the record_type-specific commands are the type verbs; these are the shared ones
_COMMON_CMD_NAMES = {"new", "ls", "list", "show", "update"}

_COMMON_CMD_BLOCK = """\
proj <rt> new [<TITLE>] [-s <SUMMARY>] [--supersede <ID…>] [--parent <PARENT>] \
    [-r <RELATED…>] [--commit <COMMIT>] [-y]
proj <rt> ls [--status <STATUS>]
proj <rt> show <ID> [--history] [--json]
proj <rt> update <ID> [-t <TITLE>] [-s <SUMMARY>] [-m <MESSAGE>]"""

_CLI_NOTES: dict[str, str] = {
    "生命周期（顶层命令）":
f"""- **DB 是唯一事实源**；md 是给人看/手写的投影。`sync` 单向 md→DB，`render` 单向 DB→md。
- 手写 md 后跑 `proj sync`（不带参数 = 全量 + 渲染全部视图）；只改 DB 后跑 `proj render`。
- 跨类型：`proj show ID` / `proj ls [--type T]` / `proj open ID`（$EDITOR 打开 md，关闭后自动
  sync）——id 自带类型（`T-…`/`DEC-…`），不用再写类型；`proj <rt> show/ls` 等价地限定到某类型。
- 记录的 H1 是 `# <ID>. <标题>`：标题文字**可手改**（编号 id 在文件名 + 首行 marker
  里体现，DB 为准）：`sync` 把新标题
  回收进 DB 并把文件重命名到新 slug（id 不变；changelog 块、`<!-- proj:head … -->`
  围栏内的 metadata 仍由工具维护）。
- `check` 检查：{LOCK_FILE} 模板在、表 schema 匹配模板、md 各 section 与 DB 一致、
  record_type 特有不变量（task：parent/related 存在、无父环）。
- 裸跑 `proj`（无子命令）：已 init → 项目状态（同 `info`）；未 init → 只有 `init` 可用。
- 新建默认打开 `$EDITOR`/`$VISUAL`；`-y` 跳过（agent/非终端下自动不打开）。
- 改状态用**模板动词** `proj <rt> <verb> ID`（等价 `record.transition(cfg, verb, id)`）；
  `from: Any` = 任意当前状态都能触发（直达目标，如 `deprecate`/`close`），
  `from: <状态>` = 必须在该状态（如 plan `activate` 只从 Draft）。带门禁的动词
  （模板 `gates:` 声明：如 plan `review` 要求名下任务树全部结束、task `done` 同理）
  由引擎统一校验。""",
    "所有 record 共用的命令":
"""- 每个 record_type 都有同一组通用子命令（`<rt>` = 类型名或 code，大小写不敏感）；
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
  挂在已关闭记录下、挂到自己或自己的后代上都会被拒绝。""",
    "decision":
"""- 状态：`Proposed 🌱 → Accepted ✅ → Deprecated 📛`。
- `--supersede`：被替代的决策 id（所有类型通用，同类型、可重复；写时校验
  **同类型**，纯 relations 边，不自动改被替代记录的状态——要废弃时手动
  `proj decision deprecate`）。
- 不变量：supersede 链无环。""",
    "task":
"""- 状态：`Draft ✏️ → In Progress 🚧 → Done ✅ / Failed ❌`（new 默认 Draft）；
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
  无 parent 的任务归在 `## Unparented` 一节。""",
    "plan":
"""\
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
  （`shelve` 暂停不设此门禁）。""",
    "item":
    """- 结构化清单（模板 `items:` 声明的某个 `##` 节）：task 的 Acceptance Criteria、
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
- 进度：任务带清单时，其行末在 Tasks 块与 `WIP.md` 显示 `n/m`（已处理/总数）。""",
    "changelog":
    """- category / unreleased 名 / 文件名全在 `profiles/changelog.yaml`（每项目一份）。
- `version [NAME]` 或 `--bump major|minor|patch`（基于最新版本号）。
- `release-notes V` 打印某版本的段落（贴 PR / tag body）。""",
    "wip":
"""- 全局 `WIP.md`（取代旧 TASKLIST.md）：正在做/进行中的记录自动登记——记录进入
  其类型首个 `wip` 状态（如 task In Progress、plan Active）即入列，退出（如任务
  Done/Failed）即移除。也可 `proj wip add <ID…>` / `return <ID…>` 手动登记/移出
  （`ls` 列当前 WIP）。
- 每条含状态 + 标题，清单项直接列在条目下（含分组）：在 WIP.md 里直接勾清单 =
  改记录，下次 `proj sync` 写回 DB（记录 md 也改了就冲突，提示二选一）。""",
    "hooks": "装 `.git/hooks/pre-commit`：提交前跑 `proj check`，不过则拒。",
}

_CLI_WORKFLOW = """
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
"""


def cli_reference() -> str:
    out = [_CLI_INTRO, ""]
    for heading, block in _cli_sections():
        out += [f"## {heading}", "", "```", block, "```", ""]
        if heading in _CLI_NOTES:
            out += [_CLI_NOTES[heading], ""]
    out.append(_CLI_WORKFLOW.strip())
    return "\n".join(out) + "\n"


# ---- MCP ---------------------------------------------------------------------------------

_MCP_INTRO = f"""\
# proj MCP 参考

> 入口：`proj-mcp`（stdio MCP server，`mcp` 2.x `MCPServer`）。
> 每个 tool 对应一个 CLI 动词，参数同名（`--x` → `x`），并多一个 `root`：
> 绝对项目路径，= CLI 的 `--root`。优先级：tool `root` > `${PROJ_HOME_ENV}` > 当前目录。
> 从项目目录启动 client（如 Claude Code 在仓库里）不需要任何配置；从全局配置启动
> （client cwd ≠ 项目）就给每个 tool 传 `root`，或设 `${PROJ_HOME_ENV}`。
> 校验失败不抛错，而是返回 `error: …` 文本（agent 可读）。
> proj 行为不符合文档 / 出现未预期报错时，用 `proj_bug(text, command=…)` 记录
> 现象和命令（写入项目的 `.proj.bugs`，不入库、不进 git）。
"""


def mcp_reference() -> str:
    out = [_MCP_INTRO, "## Tools", ""]
    for tool in mcp._tool_manager.list_tools():
        out += [f"### `{tool.name}`", ""]
        desc = (tool.description or "").strip()
        if desc:
            out += [desc, ""]
        props = (tool.parameters or {}).get("properties", {})
        params = [n for n, p in props.items() if p.get("description")]
        if params:
            out += ["| 参数 | 说明 |", "|---|---|"]
            for n in params:
                d = (props[n].get("description") or "").replace("\n", " ")
                out.append(f"| `{n}` | {d} |")
            out.append("")
    return "\n".join(out) + "\n"


def write(root: str | None = None) -> list[str]:
    """Regenerate both reference files (in this repo, or anywhere with a --root)."""
    from proj.core.config import load_config
    cfg = load_config(root, strict=False)
    written = []
    for rel, text in ((CLI_REF_PATH, cli_reference()), (MCP_REF_PATH, mcp_reference())):
        p = cfg.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fsio.write_atomic(p, text)
        written.append(rel)
    return written
