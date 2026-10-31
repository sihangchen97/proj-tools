# 发布计划（终态，2026-10-31 收口）

> 目标（最终）：**可安装（wheel / `uv tool install`）+ 文档真实 + 全测试绿**。
> 关键拍板（2026-10-31，全部已落 commit）：
> - **不发 PyPI**，版本号 **0.1.0**（开发期）→ 分发 = `uv tool install`（wheel 或 `-e .`）
> - **包名 `proj` → `proj-tools`**（PyPI 上 `proj` 被占 + 系统 libproj 占 PATH，双撞名）；
>   导入名仍 `import proj`，CLI 入口仍 `proj` / `proj-mcp`
> - **不配 GitHub CI**（workflow 存 `tobedelete/github-scaffold/`，留作将来启用）
> - **本仓库不是 proj 项目**：根目录 dogfood 状态（`.proj.sqlite`/`TASKLIST.md`/实验记录/
>   脚手架 CHANGELOG+CONTRIBUTING+.github）全清入 `tobedelete/`；实验一律走 `.testproj` 沙盒
>
> 基线：pytest 53 / e2e 52 / lint 全绿；wheel 干净 venv 冒烟通过；`proj --version` 已加。

原则：**先落袋、再提质量**；Phase 0/1 不动 `src/` 逻辑。

---

## 想法对账（逐条核对代码后）

### A. 已完成（e2e + 单测覆盖）

- SQLite core + YAML 模板即 schema + md 输入通道（v3 核心前提）
- 分层架构 `core/` `engine/` `record_types/` + Record 基类
- supersede 多值双向字段 + check 环检测；plan 状态机全路径（含新增 `freeze`）
- task 关系模型：`parent` 指向任意类型 + 运行时子树渲染
- 自动升档、task done 全勾选校验、adopt、6 类漂移检测、pre-commit hook、`--root` 全局
- 渲染视图：TASKLIST.md / CHANGELOG.md / decision _TOC / plan Task List

### B. 有意未做 / 已处理

1. `Shelved → Archived` 迁移边 — **已做**（新增 `freeze` verb，plan.yaml + cli + 测试 + e2e）
2. Steps 3 层嵌套自动压平 — **跳过**（自动压平会改用户内容，维持"check 报错不自动改"）
3. 子任务编号 A.01 — **跳过**（纯视图装饰，价值低）
4. `proj info` 的 status 分布 — **已实现**（`store.py:info` 的 `by_status`，此前实现笔记过时）
5. 多人并发 sync 冲突保护 — **未做**（DB last-write-wins，设计已标风险；单人/开发期可接受）

### C. 没开始 → 现状

1. MCP server — **已完成**（`src/proj/mcp.py`，14 tools 1:1 映射 CLI，`proj-mcp` 入口，测试覆盖）
2. 模板间迁移命令 — **未做**（设计 §0 提过"迁移=表间转换"，无 CLI；留 Phase 3）
3. `--profile core|cv|infra|quality` 语义 — **名不副实**（仓库只有 `profiles/default/`，
   4 个名字只在 `PROFILE_DIRS` 里区别建哪些目录；留 Phase 3 再定）
4. 文档漂移 — **已修**（README `task.plan`→`parent`、测试段补 e2e、设计文档引用改"本地 plans/"）

---

## 已完成（全部落 commit）

- **Phase 0 落袋**：删 5 空壳 + review checklist → `tobedelete/`；大 diff 拆 commit
  （`e26e5c6` 分层重构 / `ced9034` docs+e2e / `52434bd` freeze verb）
- **MCP（1.5）**：`9a646dd` + `94e41f7`(PROJ_HOME) + `cc15aa9`(error 文案)
- **清 dogfood（0.3 反转）**：`87976a9` 删根目录 proj 状态 + 摘误装 pre-commit hook
  + 修 `.roo/rules/testing.md`（"本仓库不是 proj 项目"）
- **撤 CI（1.1 砍）**：`8e60d6f` workflow + issue/PR 模板存 `tobedelete/github-scaffold/`
- **改名（撞名修复）**：`f1bd471` 包名 `proj`→`proj-tools`（CLI 不变）+ `--version` 查分布名
  + pixi 本地依赖同步
- **版本 + CHANGELOG（1.2）**：保留 0.1.0，`[Unreleased]` 填好，`proj --version` 已加
- **README 发布化（1.3）**：安装段如实写 wheel / `uv tool install`，MCP 节（配置片段 + 工具表）
- **打包预检（1.4）**：`hatch build` → 干净 venv `pip install dist/proj-*.whl`，
  `proj --version/--help/init` + `proj-mcp` 入口 + profiles 进 wheel，全过
- **Phase 2 发布**：~~PyPI / tag / release~~ —— 砍掉，留作将来清单（见 git history）
- **init-demo（新增）**：`cb7c0c4` `scripts/init-demo.sh` 在 `.testproj` 沙盒起一个可浏览的项目

## 真实剩余（发布后还债，均不阻塞）

- [ ] 3.1 `.proj.lock` 旧键 `kinds:` 回退（`specs_from_lock` 一行 fallback）—— **0.1.0 无老用户，暂不做**
- [ ] 3.2 `record_types/__init__.py` 两处 `import *`（changelog/plan）收紧为显式导入
- [ ] 3.3 CLI 统一：task/decision 命令走 `record_for(kind)`（plan 已走类，task/decision 走模块级委托）
- [ ] 3.5 给 5 个 record_type 各留一条真实示例（**本仓库不适用**——改用 `.testproj` 沙盒演示，即 init-demo.sh）
- [ ] 3.6 模板间迁移命令（C2）
- [ ] 3.7 `--profile` 语义落地（C3：要么补齐 cv/infra/quality 差异，要么砍成仅 core）
- [ ] 3.8 并发 sync 冲突保护（B5）

## 已接受的风险

- **CLI 名 `proj` 与系统 libproj 撞 PATH**：`/usr/pkg/bin/proj`（libproj 9.8.1，GDAL 依赖，动不得）。
  装了本工具的机器 `where proj` 第一个是 `~/.local/bin/proj`（我们的）靠 PATH 顺序遮蔽；
  **未装 uv tool 的机器敲 `proj` 仍会踩到 libproj**。`uv tool install -e .` 需 `--force`（同名占位）。
  用户拍板：CLI 名不变。
- **无 CI**：验证全靠本地 `pixi run test && pixi run e2e`；仓库无 remote。
- **MCP 只经 tmp 沙盒测试**，未挂真实 MCP 客户端跑过 stdio 会话。

## 分发方式（当前）

```bash
cd proj-tools
uv tool install -e . --force        # 开发：editable，改代码即时生效
# 或
hatch build                         # 出 wheel
uv tool install dist/proj-*.whl     # 或 pip install dist/proj-*.whl
proj --version && proj-mcp --help   # 两个入口
```
