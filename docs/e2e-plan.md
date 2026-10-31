# Plan: e2e 脚本综合完善（feature 全覆盖）

> 本文件是**实施规划**，给实施 agent 照做。目标：`scripts/e2e.sh` 从"冒烟脚本"升级为**每个 feature 都有断言的回归脚本**——改任何渲染/存储/状态机逻辑，e2e 能抓住。
> 现状基线：191 行、~80 断言，单沙盒 `.testproj`，已覆盖 init/seed、hooks、decision+supersede、plan+task 生命周期、sync 自动推进、summary 斜体行、fail 动词、related、update -t/-s/-c、experiment、deprecate、plan 状态机全链、debt/incident、changelog 全链、adopt、update/check。
> 约定：断言风格沿用现有 `expect_ok <desc> <固定串> -- cmd`（输出匹配）、`expect_fail -- cmd`（拒绝）、文件内容 `grep -qF`。不引 jq/yq（保持 bash-only，NFS 安全）。

## 1. 布局：单沙盒 → 分节 + 可选分文件

**保持单文件 `scripts/e2e.sh`**（拆多文件需共享 helper + 沙盒隔离复杂化，收益低）；内部按 **10 个 feature 节**组织，每节开头打 `echo "── N. <节名>"`。helper（`ok/fail/expect_ok/expect_fail` + 新增 2 个）集中在文件头。

新增 helper：
```bash
expect_file() { # <desc> <固定串> -- <file>     文件内容匹配（grep -qF）
expect_no()   { # <desc> <固定串> -- <file>     文件**不**含（负断言，如 "no 'summary:' label"）
```
（替代现在散落的 `grep -qF … && ok || fail` 手写分支——统一走 helper，失败时同样打印文件片段。）

**沙盒纪律**（吃过并发互抢的亏）：脚本开头 `rm -rf` 后**立刻 `mkdir` 占位 + `echo $$ > $SANDBOX/.pid`**；结尾删 `.pid`。跑前若有残留 `.pid`（且进程不存在）提示用户。NFS 新文件用**命令直接打印的路径**（如 `new` 的 `Created <path>`）而非立即 glob。

## 2. 断言矩阵（按节，每行 = 一条断言）

### A. init & scaffold（扩展现有 §1）
| # | 动作 | 断言 |
|---|------|------|
| A1 | `proj init` | 输出 `Created:`；`.proj.sqlite`/`.proj.lock`/`TASKLIST.md`/`CHANGELOG.md`/`CONTRIBUTING.md` 存在（expect_file 逐个） |
| A2 | seed | `docs/decisions/DEC-0001-*.md` 存在且含 `<!-- proj:head:start` 围栏 |
| A3 | `proj --profile cv` | 输出含 `experiments`；`docs/experiments/` 目录建出；`proj experiment list` 可跑 |
| A4 | `proj --profile quality` | `docs/tech-debt/`+`docs/incidents/`+`.env.example` 建出；`proj debt/incident list` 可跑 |
| A5 | `proj --profile infra` | `docs/specs/`+`docs/dev/` 建出 |
| A6 | profile 扩展幂等 | A3–A5 后 `.proj.lock` 的 `profiles:` 含 4 项；重复 `init`（无新 profile）不重建（lock 不变）、**已有 DEC 不丢**（expect_file DEC-0001） |
| A7 | bare `proj` | 输出含 `decision`/`task`（project status 面） |

### B. record CRUD（扩展现有 §2/§3c）
| # | 动作 | 断言 |
|---|------|------|
| B1 | `decision new` ×2 | id 递增 `DEC-0002`/`DEC-0003`；输出 `Created docs/decisions/DEC-000X-` |
| B2 | `--summary` | 文件含 `_summary 文本_` 斜体行；`expect_no "> **summary:**"`（无标签行） |
| B3 | `list` | 含新 id；`list --status Draft` 过滤（已 accept 的不在） |
| B4 | `show --history` | 输出含 `Created`/`Accepted` 两条 changelog |
| B5 | `update -t` | 输出 `title →`；**md 文件改名为新 slug**（旧名不存在 + 新名存在） |
| B6 | `update -s` | 文件斜体行换成新 summary |
| B7 | `update -c` | changelog 表多一行含新 comment 文本 |
| B8 | `update` 无参 | 拒绝（expect_fail） |
| B9 | 动词 `-c` | `accept X -c "Added: note"` 后 changelog 表含 note |
| B10 | id 裸数字歧义 | `task new "Amb" -y -r 1` 拒绝（T 与别的 6 位/1 位撞）——保留现有 |
| B11 | 未知动词 | `decision explode X` 拒绝 |

### C. relations（扩展现有 §2 supersede + §3b related/subtask）
| # | 动作 | 断言 |
|---|------|------|
| C1 | `-s` supersede | 新决策 head 含 `> **supersedes:**` + 指向旧决策的**文件名链接** |
| C2 | 反向 | 旧决策 head 含 `> **superseded_by:**` + 指回新决策 |
| C3 | `-r` 多值跨类型 | task `-r "DEC-…, PLN-…"` → task head `> **related:**` **列表**（`> - ` 两行）；decision 与 plan 各自 head 出现反向 related 行 |
| C4 | parent 正向/反向 | task `--parent PLN-…` → task head `> **parent:**` 行；plan head `> **subtask:**` 行 |
| C5 | 幽灵 parent 拒 | `task new … --parent PLN-9999` 拒绝（输出含 not found） |
| C6 | 父环检出 | 构造 T-A→T-B→T-A 后 `proj check` **FAIL**（含 环/cycle 字样），再解环恢复 OK |
| C7 | （新 schema）supersede 通用化 | `experiment new -s EXP-0001` 可用（非 decision 类型）；跨类型 `-s DEC-0001` 对 task **拒绝**（同类型校验） |
| C8 | （新 schema）被替代记录状态不变 | supersede 后旧记录 `> **status:**` 行状态**不变**（不自动 Deprecated）；手动 `deprecate` 才变 |

### D. task 生命周期 & 树（扩展现有 §3）
| # | 动作 | 断言 |
|---|------|------|
| D1 | 全链 | `new → start（→ In Progress）→ done（→ Done）`，`fail`（→ Failed）；Failed **不**进 done（`_done_date` 仍空） |
| D2 | 步骤门控 | 有未勾选步骤 `done` 拒绝；勾选后 `sync` 再 `done` 通过 |
| D3 | sync 自动推进 | 勾一个未勾一个 → `sync` 输出 `auto status Draft → In Progress`；全勾 → `sync` OK 且状态回 Draft→In Progress 后可 done |
| D4 | plan review 门禁 | 子任务未全 Done 时 `plan review` 拒绝（输出含 未完成/Done 提示）；全 Done 后通过 |
| D5 | 三层树 | plan > task > subtask：`TASKLIST.md` 含三行且缩进层级正确（`grep -nE '^[[:space:]]*[-*] ' ` 数缩进）；plan md 的 Task List section 含子树 |
| D6 | `task list --status` | 过滤后只剩目标状态行 |

### E. plan 状态机（保留现有 §4/§4b + 补非法转移）
现有已覆盖 activate/review/shelve/resume/archive/freeze 全链。补：
| # | 断言 |
|---|------|
| E1 | 非法转移全部拒绝：`activate` on Active、`archive` on Draft、`freeze` on Active、`shelve` on Draft、`review` on Active 无子任务前（保留） |

### F. render & views（新节，现有只零星碰过 TASKLIST/CHANGELOG）
| # | 动作 | 断言 |
|---|------|------|
| F1 | `render`（全部） | 输出列出的写文件数 ≥ 记录数；改 DB 后 md 更新 |
| F2 | `render decision DEC-0001` 单条 | 只重写该文件（输出只含 DEC-0001 路径） |
| F3 | `render task <id>` | 只重写该 task 文件 |
| F4 | `_TOC.md`（decision toc） | `docs/decisions/_TOC.md` 存在、含所有 DEC id + 状态 emoji + 图例行（`* ✏️ Draft · …`） |
| F5 | TASKLIST.md 再生成 | 删掉 TASKLIST.md 后 `update` 重建且内容同 F5-前 |
| F6 | CHANGELOG.md | `## [Unreleased]` + `## [v0.1.0] - 日期` 两节；release 后 unreleased 清空 |
| F7 | （新 schema）`PROJ_LOGS` 三态 | 默认：changelog 表 ≤5 行；`PROJ_LOGS=0` 渲染：文件**无** `| Date | Category |` 表；`PROJ_LOGS=-1`：全量（>5 行场景）；`PROJ_LOGS=2`：恰 2 行 |
| F8 | （新 schema）status 变化显示 | status 变化行 = `emoji from → emoji to` 开头 + 可选 comment；非 status 行（如 created）= 纯 comment/文本 |
| F9 | （新 schema）表下 status 图例 | changelog 表最后一行下有 `* ✏️ Draft · …` 图例行 |
| F10 | （新 schema）done 行空不显示 | 新记录 head **无** `> **done:**` 行；done 后出现且 = 日期 |
| F11 | （新 schema）链接文本=完整文件名 + 相对路径 | head 关系链接文本 = 目标 `*.md` 文件名；跨目录（task→plan）路径以 `../plans/` 开头 |

### G. sync / adopt / drift（扩展现有 §7）
| # | 动作 | 断言 |
|---|------|------|
| G1 | adopt 手写 debt（现有） | `OK`；`debt list` 含 DEB-0002 |
| G2 | adopt 缺 section 拒 | 删 `## Reason` 的手写 debt → adopt 输出 FAIL/缺 section 提示（expect_fail） |
| G3 | adopt 未知类型拒 | `docs/unknowns/X-0001.md` → 拒绝（not a known record_type） |
| G4 | drift 警告 | 手动改 md 某 section 后跑 **db-only** 命令（`render`）→ 输出含 drift/unsynced 警告，不静默覆盖；`sync` 后警告消失 |
| G5 | sync 单文件参数 | 只改一个 md → `sync <file>` 只 ingest 该文件（其它记录 changelog 无新增） |

### H. changelog 全局（保留现有 §6 + 补 bump 级别）
现有已覆盖 add/version --bump minor/release-notes/list/CHANGELOG.md。补：
| # | 断言 |
|---|------|
| H1 | `--bump major`/`--bump patch` 各走一遍（v0.2.0 / v0.2.1），CHANGELOG.md 对应节出现 |
| H2 | 未知类别拒 | `changelog add "Bogus: x"` 拒绝（unknown changelog category） |

### I. hooks / docs / info / misc（扩展现有 §hooks）
| # | 动作 | 断言 |
|---|------|------|
| I1 | hooks install + pre-commit 跑（现有） | 保留 |
| I2 | `docs` | 重新生成 `docs/CLI.md`/`docs/MCP.md`；改动代码后再跑 diff 非空（可选，弱断言：两文件存在且非空即可） |
| I3 | `info --json` | 输出可被 `python3 -c 'import json,sys; json.load(sys.stdin)'` 校验（python 是 pixi 环境必有的，允许） |
| I4 | `--version` | 输出含 `proj ` |

### J. MCP 面（不重复，指回 pytest）
MCP 工具与 CLI 等价（`proj_info/proj_list/proj_show/proj_status/…`），已由 `tests/test_mcp.py` 覆盖——**e2e 不重复**，在脚本文头注释里说明"MCP 面见 pytest"。

## 3. 实施顺序（**过程只改功能、不跑测试；最后统一跑一次测**）

> 纪律（你定的）：步骤 1–7 **只改代码/断言、不跑 e2e/pytest**（避免 NFS 上反复起沙盒、反复等渲染）；全部改完后**步骤 8 一次性统一测**（`pixi run test` + `pixi run e2e`），红了再回查。

1. **helper 抽取 + 分节重排**：现有 ~80 断言原样归位到 A/B/C/D/E/F/G/H/I 节（断言不删不改，只搬家 + 手写 grep 分支换成 `expect_file/expect_no`）。（不跑）
   - 唯一例外（便宜保险）：bash 语法用 `bash -n scripts/e2e.sh` 过一遍（不执行、不建沙盒、秒回）——语法错了最后统一测会连锁假红、难定位。
2. **A 节 profile 扩展**（A3–A7）+ 沙盒 pid 护栏。（不跑）
3. **B 节 CRUD 补齐**（B3/B4/B5 文件改名/B8/B10）。（不跑）
4. **C 节 relations**（C2 反向/C3 列表/C5 幽灵/C6 父环/C7-C8 新 schema）。C7/C8 依赖统一 schema 落地——**若实施时新 schema 未落地，这两条先注释 `# (post-schema)` 占位**，落地后启用。（不跑）
5. **D/E 节 task 树 + 状态机非法转移**（D5 三层缩进/E1）。（不跑）
6. **F 节 render/views**（F1–F6 立即可做；F7–F11 新 schema 依赖，同样 `# (post-schema)` 占位）。（不跑）
7. **G/H/I 节**（G2/G3/G4 drift、H1/H2、I3/I4）。drift（G4）若现有 `md_drift` 警告面不全，**先补代码再写断言**（drift 警告是 `render` 静默覆盖的数据保护，属该补）。（不跑）
8. **统一测（唯一一次）**：`pixi run test` + `pixi run e2e` 双跑；红了按节定位回查（输出分节 + 断言 desc 串可直接 grep 到矩阵行）；修完重跑**整条** e2e（不增量单跑）。文件头注释补"MCP 面见 pytest"+ 节索引。→ 验收全绿。

## 4. 验收标准

- `pixi run e2e` 全绿，断言总数 **≥ 130**（现 ~80；矩阵 A–I 合计 ~60 条新增/细化）。
- 矩阵里每条 `#` 断言在脚本里**有一行对应**（grep 得到 desc 串）；post-schema 占位条数 ≤ 6（C7/C8/F7–F11），统一 schema 落地后全启用再跑一遍全绿。
- 故意破坏验证（抽查 3 类）：① 把 `_head_block` 的 done 行改成恒显示 → F10 红；② 把 supersede 反向行删掉 → C2 红；③ 把 plan review 门禁删掉 → D4 红。修回后全绿（确认断言真在抓行为，不是恒真）。
- 脚本单跑时长 < 60s（NFS 环境 < 3min），输出分节清晰（`── A. init & scaffold` …）。

## 5. 风险 / 注意

- **断言串必须用稳定文案**（`→ Done`、`Created docs/…`），别断言随实现措辞易变的输出（如 auto-promote 提示若改文案 e2e 会假红）——改 CLI 文案时要同步改 e2e 断言串（本条写进 CONTRIBUTING/脚本头注释）。
- 文件内容断言用**固定串 `grep -qF`**，不用正则（`→`、emoji、中文都安全）。
- 三层树缩进断言（D5）别硬编码空格数——用"子行缩进 > 父行缩进"的相对比较（`awk` 取 `match(/^[[:space:]]*/)` 长度比较），避免渲染改缩进风格就假红。
- drift 断言（G4）依赖 `md_drift` 面（[`store.py:293`](src/proj/engine/store.py:293)）——先读清哪个命令会消费它（预期 `render`），断言对齐实际命令。
- 父环（C6）构造方式：`task new B --parent A` 后把 A 的 parent 改成 B 目前**没有 CLI 路径**（`update` 不支持改 parent）——若实施时确认无路径，C6 改为**直接改 `.proj.sqlite`**（sqlite3 或 python 一行）构造脏数据再 `check`，断言 check 抓出环；或给 `task update -p`（需你点头才加命令，否则走脏数据路线）。
