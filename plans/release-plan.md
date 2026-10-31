# 发布计划（2026-10-31）

> 目标：从"一团乱的工作树"到"可安装（wheel）+ CI 绿 + 文档真实"。
> **2026-10-31 拍板：不发 PyPI、版本号保留 0.1.0（仍在开发期）**——Phase 2 整体砍掉，
> 1.2 的 CHANGELOG 版本化改为"填 [Unreleased]，不打版本号"。
> 现状基线：pytest 50 全绿；工作树未提交 40 文件（v3 分层重构 + profiles 搬家）；
> 无 CI；CHANGELOG Unreleased 为空；版本 0.1.0；README 安装段还写着 "once packaged"。

原则：**先落袋、再提质量**。Phase 0/1 都不改一行 `src/` 逻辑，
只动 git 状态、CI、文档 —— 风险最低。

---

## 想法对账（2026-10-31 逐条核对代码后的结论）

> 回答"哪些想法完成了、哪些没有"。三分类：A 已完成 / B 有意未做（文档有升级路径）/ C 没开始。

### A. 已完成（e2e + 50 单测覆盖）

- SQLite core + YAML 模板即 schema + md 输入通道（v3 核心前提）
- 分层架构 core/ engine/ record_types/ + Record 基类（未提交，工作树里）
- supersede 多值双向字段 + check 环检测；plan 状态机全路径
- task 关系模型：`parent` 指向任意类型 + 运行时子树渲染（tasklist-design，已实现）
- 自动升档（sync 首个 checkbox → In progress）、task done 全勾选校验
- adopt 已存在 md、6 类漂移检测、pre-commit hook、`--root` 全局选项
- 渲染视图：TASKLIST.md / CHANGELOG.md / decision _TOC / plan Task List

### B. 有意未做（`plans/v3-implementation-notes.md` §2-5 每条都有升级路径，均一行代码级）

1. `Shelved → Archived` 迁移边（plan.py TRANSITIONS 加一行）
2. Steps 3 层嵌套自动压平（check 报错 → normalize + sync）
3. 子任务编号 A.01（views.py `_sub_task_lines` 加计数器）
4. `proj info` 的 status 分布统计（records 加 GROUP BY）
5. 多人并发 sync 的冲突保护（DB last-write-wins，设计已标风险，未做）

### C. 没开始（发布前要拍板：进 1.0 还是砍掉）

1. **MCP server**（设计 §0 承诺"CLI + MCP 双入口"，README 已提 `--root` for MCP，仓库无入口）
2. **模板间迁移命令**（设计 §0"迁移 = 表间转换"，无 CLI 命令）
3. **`--profile core|cv|infra|quality`**：仓库只有 `profiles/default/`，4 个 profile 名只在 `PROFILE_DIRS` 里区别目录，语义名不副实
4. **文档漂移**：README 第 91-95 行仍写旧 `task.plan` 模型（应改 `parent`）、第 145 行 "39 tests"（实为 50）、`docs/cli-reference.md` 未核对

### 发布决策建议

- **1.0 内容（用户 2026-10-31 拍板）= A 全部 + B1-4 + C1 MCP server + C4 修文档**
- C2（迁移命令）、B5（并发保护）、C3（profile 语义）→ Phase 3，不阻塞

## Phase 1.5：MCP server（1.0 必带，C1）

设计（同 ecs-gpu 模式：CLI + MCP 双入口，引擎函数薄封装）：

- [x] **1.5.1 模块** `src/proj/mcp.py`：`mcp` 2.x `MCPServer` stdio server（2026-10-31：
  `mcp>=2`；1.x 的 `FastMCP` 在 2.x 改名为 `MCPServer`，`from mcp.server import MCPServer`）
  - 工具集 = CLI 域函数 1:1 映射（复用 cli.py 已调用的同一批函数，不新写逻辑）：
    `proj_init` / `proj_new` / `proj_sync` / `proj_check` / `proj_list` / `proj_show` /
    `proj_status` / `proj_log` / `proj_update` / `proj_info` /
    `proj_task_done` / `proj_plan_verb`（activate|review|shelve|resume|archive）/
    `proj_decision_supersede` / `proj_changelog_add`
  - 每个工具带可选 `root: str | None`（= CLI 的 `--root`，MCP 从任意 cwd 调）
  - 返回：人类可读文本（与 CLI stdout 一致），校验失败返回完整错误信息（agent 可读）
- [x] **1.5.2 入口**：pyproject `[project.scripts] proj-mcp = "proj.mcp:main"`；
  依赖加 `mcp>=2`（同仓库 ecs-gpu 同款：官方 `mcp` 包即 stdio 事实标准，未加 fastmcp）
- [x] **1.5.3 测试** `tests/test_mcp.py`：tmp proj 沙盒里直调纯函数 `mcp.proj_*`（不走 stdio 协议），
  覆盖 init→new→show→status→plan activate→check 一条链 + 6 个错误路径
- [x] **1.5.4 README**：加 "MCP" 节（配置片段 + 工具↔CLI 映射表），CLI 表不变
- verify: `pixi run test && pixi run lint` 全绿；wheel 干净 venv 里 `proj-mcp --help` 可跑

## 执行顺序（最终）

0 落袋 → B1-4 小修 → 1.5 MCP → C4 文档修正 → 1 质量门（CI/版本/CHANGELOG/README/wheel 预检）→ 2 发布

---

## Phase 0：把乱账落袋（不改代码）

- [ ] **0.1 决定 5 个空壳记录的归属**（checklist 遗留项）
  - `tasks/A-t.md`、`docs/decisions/0002..0004`、`docs/tech-debt/0001` 全是测试 adopted 的空壳
  - 建议：**全删**（`mv` 到 `tobedelete/`，按仓库规则不用 rm）；保留 `docs/decisions/0001`（真 ADR）
  - verify: `proj check` 仍绿；`TASKLIST.md` 重渲染后无 A-t
- [ ] **0.2 清理 session 副产品**
  - `CODE-REVIEW-CHECKLIST.md`：本 session 一次性 review 用 → `mv` 到 `tobedelete/`
    （遗留问题清单里还活着的 3 条挪进 Phase 3）
  - `plans/tasklist-design.md`、`plans/src-code-guide.md`：**保留**（已实现功能的设计文档，README 可链接）
  - verify: `git status` 不再有一性产物
- [ ] **0.3 决定本仓库 dogfooding 文件的提交策略**
  - 保留并提交：`.proj.lock`、`.proj.sqlite`、`TASKLIST.md`、`docs/decisions/`（功能自证，README 承诺 DB 默认提交）
  - 不提交：无（`.gitignore` 已用 `!.proj.sqlite` 锚定）
  - verify: `git status --short` 中 `??` 项清零（除 Phase 1 新增的 CI 文件）
- [ ] **0.4 逻辑切分提交**（一个大 diff 拆 4 个 commit，可 review）
  1. `profiles: move templates/ → profiles/, kind → record_type`（6 个 yaml 改名）
  2. `refactor: layered layout core/ engine/ record_types/ + Record base class`（RM 们 + 新增 `__init__.py`×3 + `engine/record.py`）
  3. `tests: update for layered layout + adopt flow`
  4. `docs+meta: README, CHANGELOG, CONTRIBUTING, .github, scripts/e2e, plans`
  - verify: 每步 `git show --stat` 干净；全量提交后 `pixi run test && pixi run e2e && pixi run lint` 全绿

## Phase 1：发布质量门（~半天）

- [ ] **1.1 GitHub Actions CI**：`.github/workflows/ci.yml`
  - 3 job：lint（ruff）/ test（pytest）/ e2e（bash scripts/e2e.sh），matrix：ubuntu + macos-arm64
  - 用 `pixi install`（`pixi-distribution/setup-pixi`）复用 pyproject 的 pixi workspace，CI 与本地同源
  - verify: push 后 CI 全绿；本地 `bash scripts/e2e.sh` 与 CI 同款命令
- [x] **1.2 版本号决策 + CHANGELOG**（2026-10-31 拍板：**保留 0.1.0，不发 PyPI**）
  - `proj --version` 已加（cli.py `@app.callback(invoke_without_command=True)` + eager callback）
  - 待办：填 CHANGELOG `[Unreleased]`（v3 SQLite core、YAML 模板即 schema、分层架构、
    profiles 搬家、pre-commit hook、--root 全局选项、MCP server）——不打版本号
- [ ] **1.3 README 发布化**（不发 PyPI，改为"真实可跑"）
  - 删 "once packaged" 措辞；安装段如实写 wheel 安装路径（`hatch build` + `pip install dist/*.whl`）
  - 设计文档链接补上 `plans/tasklist-design.md`（task 关系模型）
  - verify: 按 README 逐条跑一遍 quick start（在 .testproj 里），全部真实可跑
- [ ] **1.4 打包预检**（不等发布）
  - `hatch build` → 解开 wheel 确认 `proj/profiles/*.yaml` 都在（pyproject 已配 force-include，验证即可）
  - 干净 venv：`pip install dist/proj-*.whl && proj --help && proj init` 在临时目录跑通
  - verify: 干净环境不依赖 pixi/仓库路径可用

## Phase 2：~~发布~~（2026-10-31 砍掉：不发 PyPI，版本号保留 0.1.0）

- ~~2.1-2.4 PyPI / tag / release~~ —— 留作将来真要发布时的清单（保留在上面 git history）

## Phase 3：发布后还债（不阻塞发布）

- [ ] 3.1 `.proj.lock` 旧键 `kinds:` 向后兼容（`specs_from_lock` 一行 fallback）—— 只有已有人用过旧 lock 才做
- [ ] 3.2 `record_types/__init__.py` 星号导入收紧
- [ ] 3.3 CLI 统一：task/decision 命令走 `record_for(kind)` 类调用（现 plan 已走类，task/decision 还走模块级委托）
- [x] 3.4 MCP server（2026-10-31 提前进 1.0：`src/proj/mcp.py` + `proj-mcp` 入口 + 测试 + README）
- [ ] 3.5 空壳删除后给 5 个 record_type 各留一条**真实**示例记录（dogfood 自证 + README 截图素材）
