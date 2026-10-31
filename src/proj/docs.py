"""Reference docs as code: docs/CLI.md and docs/MCP.md are generated from this
module (`proj docs`) so they can never drift from the CLI/MCP code.
Run `proj docs` (from this repo or any project) to regenerate them."""
from __future__ import annotations

import click
from typer.main import get_group

from proj import cli as cli_mod
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
> **两种命令**：生命周期命令（`proj init`/`proj sync`/…）+ 每个 record_type 的动态子命令
> `proj <rt> new|list|show|<verb>`（`<verb>` = 该类型模板 `verbs:` 里声明的，运行时生成）。
"""


def _param_token(p: click.Parameter) -> str:
    flags = " ".join(f for f in p.opts if f.startswith("--"))
    short = next((f for f in p.opts if len(f) == 2), "")
    opt = " ".join(x for x in (flags, short) if x)
    if not getattr(p, "is_flag", False):
        name = p.name.upper()
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
    # dynamic record_type groups: new / list / show + that type's template verbs
    for rtype in rec.load_registry(None):
        grp = cli_mod._build_record_group(rtype)
        sections.append((rtype, "\n".join(_cmd_line(c, prefix=f"proj {rtype}")
                                           for c in grp.commands.values())))
    # static sub-groups (changelog / hooks are not record_types)
    for g in app.registered_groups:
        cmd = click_group.commands[g.name]
        sections.append((g.name, "\n".join(_cmd_line(c, prefix=f"proj {g.name}")
                                           for c in cmd.commands.values())))
    return sections


# Hand-written group notes, keyed by heading — the code blocks are the source of truth.
_CLI_NOTES: dict[str, str] = {
    "生命周期（顶层命令）":
f"""- **DB 是唯一事实源**；md 是给人看/手写的投影。`sync` 单向 md→DB，`render` 单向 DB→md。
- 手写 md 后跑 `proj sync`（或 `proj update`）；只改 DB 后跑 `proj render`。
- `check` 检查：{LOCK_FILE} 模板在、表 schema 匹配模板、md 各 section 与 DB 一致、
  record_type 特有不变量（task：parent/related 存在、无父环、Steps ≤2 层）。
- 裸跑 `proj`（无子命令）：已 init → 项目状态（同 `info`）；未 init → 只有 `init` 可用。
- 新建默认打开 `$EDITOR`/`$VISUAL`；`-y` 跳过。
- 改状态用**模板动词** `proj <rt> <verb> ID`（等价 `record.transition(cfg, verb, id)`）；
  `from: Any` = 任意当前状态都能触发（直达目标，如 `deprecate`/`close`），
  `from: <状态>` = 必须在该状态（如 plan `activate` 只从 Draft）。带门禁的动词
  （如 plan `review` 要求名下任务全 Done、task `done` 要求 checkbox 全勾）由引擎校验。""",
    "decision":
"""- 状态：`Proposed 🌱 → Accepted ✅ → Deprecated 📛`（被 supersede 时自动置 Deprecated）。
- `-s`：被替代的决策 id（写入 supersedes 字段，同时把旧决策置 Deprecated + superseded_by）。
- 不变量：supersede 链无环。""",
    "task":
"""- 状态：`Draft ✏️ → In Progress 🚧 → Done ✅ / Failed ❌`（new 默认 Draft）。
- `task done` 门禁：自己的 Steps 全勾上（子任务独立，父任务不等子任务）。
- `task fail`：标记无法完成/放弃（Any → Failed）；Failed 不是 done，不会计入
  done 日期，plan `review` 门禁照样阻塞。
- `--parent` 可指向 plan 或另一个 task（子任务）；子任务建出来后出现在父的 Task List 里（缩进）。
- 主区 `TASKLIST.md` 放**树根**（无 parent 的 task，+ 所有声明 `type: task` 章节的
  record_type 如 plan/experiment）；挂了 parent 的任务缩进渲染在父节点下。
- 第一次勾 checkbox 后 `sync` 自动升 In Progress（on_ingest 钩子）。
- 本地子步骤用 Steps 的 2 层 checkbox（轻量、不占 id）；要独立状态/进列表的子任务
  用 `--parent` 建真 task。""",
    "plan":
"""\
```
Draft → Active → Pending Review → Shelved
          ↑____________ resume ______|
          Active → Pending Review → Archived
```
- 任务通过 `--parent <plan-id>` 挂到 plan 下（不是 plan 里塞任务）。
- 每个 plan 的 md 有渲染的 `## Task List` 章节（以 plan 为根的任务树）。
- `review` 门禁：直接子任务全 Done。""",
    "changelog":
"""- category / unreleased 名 / 文件名全在 `profiles/changelog.yaml`（每项目一份）。
- `version [NAME]` 或 `--bump major|minor|patch`（基于最新版本号）。
- `release-notes V` 打印某版本的段落（贴 PR / tag body）。""",
    "hooks": "装 `.git/hooks/pre-commit`：提交前跑 `proj check`，不过则拒。",
}

_CLI_WORKFLOW = """
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
        p.write_text(text, encoding="utf-8")
        written.append(rel)
    return written
