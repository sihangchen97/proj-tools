#!/usr/bin/env python3
"""e2e: one full story through every proj feature (PLAN §6).

A CV team builds and ships "PCB defect detection model v1" — selection, plan,
tasks, gates, incident, debt, experiment, review, release, housekeeping.
Each chapter asserts real behavior inside the story (including "the gate
refuses" counter-examples).

Modes:
  in-process  (default):  `pixi run e2e` — CliRunner in one Python process, ~5 s
  subprocess        :    `pixi run e2e-full` (PROJ_E2E_SUBPROCESS=1) — every
                         command starts the real `proj` entry point, ~30 s

The sandbox `.testproj/` is rebuilt each run and IS the demo project at the end.
Edge cases (bad refs, ambiguity, error texts) live in pytest, not here.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# F6.2: the incremental-render verification is ON by default for e2e — every
# write command's `render_affected` re-renders everything in memory and
# compares with the disk (subprocess mode inherits os.environ into each `proj`
# run; in-process mode reads it directly). Set PROJ_RENDER_VERIFY=0 to opt out.
os.environ.setdefault("PROJ_RENDER_VERIFY", "1")

REPO = Path(__file__).resolve().parent.parent
TMPDIR_TESTPROJ = REPO / ".testproj"
# N4: the repo lives on NFS — the whole story runs in a LOCAL temp dir (tmpfs)
# and is copied back to .testproj/ at the end (pass OR fail). ~60s -> ~5s.
# PROJ_E2E_SANDBOX=<dir>: run directly there, no temp dir, no copy-back.
# PROJ_E2E_KEEP_TMP=1: keep the temp dir (its path is printed).
SANDBOX: Path = (Path(os.environ["PROJ_E2E_SANDBOX"]).resolve()
                 if os.environ.get("PROJ_E2E_SANDBOX") else None)
_TMP_SANDBOX: Path | None = None
_KEEP_TMP = os.environ.get("PROJ_E2E_KEEP_TMP") == "1"


def setup_sandbox() -> None:
    global SANDBOX, _TMP_SANDBOX
    if SANDBOX is not None:
        return
    _TMP_SANDBOX = Path(tempfile.mkdtemp(prefix="proj-e2e-"))
    SANDBOX = _TMP_SANDBOX


def _guard(dst: Path, label: str) -> None:
    """e2e only ever manages <repo>/.testproj and its own temp dir (user
    requirement) — refuse to delete/copy anything else."""
    assert not dst.is_symlink(), f"{label} must not be a symlink: {dst}"
    assert dst.resolve() == (REPO / label).resolve(), \
        f"{label} must be {REPO / label}: got {dst}"


def teardown() -> None:
    """Close DB connections + WAL-checkpoint (no leftover -wal/-shm), then copy
    the sandbox back to .testproj/ (pass OR fail) and remove the temp dir."""
    global SANDBOX, _TMP_SANDBOX
    if _TMP_SANDBOX is None:
        return  # PROJ_E2E_SANDBOX: ran in place, nothing to copy back
    import gc
    gc.collect()  # drop any command connection still referenced in-process
    if (SANDBOX / ".proj" / "cache.sqlite").is_file():  # S.10
        con = sqlite3.connect(SANDBOX / ".proj" / "cache.sqlite", timeout=5.0)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
    gc.collect()
    _guard(TMPDIR_TESTPROJ, ".testproj")
    if TMPDIR_TESTPROJ.exists():
        shutil.rmtree(TMPDIR_TESTPROJ)
    shutil.copytree(SANDBOX, TMPDIR_TESTPROJ)
    if _KEEP_TMP:
        print(f"\ne2e: temp dir kept (PROJ_E2E_KEEP_TMP=1): {SANDBOX}")
    else:
        # the temp dir must be the one this run created (inside the system tmp)
        assert (not SANDBOX.is_symlink()
                and SANDBOX.resolve().parent == Path(tempfile.gettempdir()).resolve()), \
            f"refusing to remove a dir I did not create: {SANDBOX}"
        shutil.rmtree(SANDBOX)
SUBPROCESS = os.environ.get("PROJ_E2E_SUBPROCESS") == "1"
RECORDS_DIR = {
    "task": "docs/tasks", "decision": "docs/decisions", "plan": "docs/plans",
    "debt": "docs/debts", "incident": "docs/incidents",
    "experiment": "docs/experiments",
}

# CLI commands that are install/dev-only (not exercised by the story)
CLI_EXEMPT: set[str] = {"init"}  # `init --zoo` flag variant only
# record_types that carry no verbs of their own (changelog is a static group)
NO_VERB_TYPES: set[str] = set()


@dataclass
class Report:
    checks: int = 0
    failed: int = 0
    commands: set[str] = field(default_factory=set)
    mcp_tools: set[str] = field(default_factory=set)
    t0: float = field(default_factory=time.time)

    def ok(self, desc: str) -> None:
        self.checks += 1
        print(f"  \u2713 {desc}")

    def fail(self, desc: str, out: str = "") -> None:
        self.failed += 1
        self.checks += 1
        print(f"  FAIL {desc}")
        if out:
            for line in out.splitlines()[:30]:
                print(f"        {line}")

    def cmd(self, path: str) -> None:
        self.commands.add(path)

    def tool(self, name: str) -> None:
        self.mcp_tools.add(name)

    def chapter(self, title: str) -> None:
        _emit("chapter", title)
        print(f"\n\u2500\u2500 {title} \u2500\u2500")


REP = Report()

# A12 (PLAN §18.3): record every step as it runs so `emit_sh` can render the
# reference `scripts/e2e.sh` (the same scenario, `proj` CLI commands only).
EMIT: list = []           # (kind, payload); kind in cmd ok/fail chapter/section/edit/move/mcp
_EMIT_ON = True           # --no-emit turns this off

def _emit(kind: str, payload) -> None:
    if _EMIT_ON:
        EMIT.append((kind, payload))

# ---- framework -----------------------------------------------------------------


def _id_of(out: str, prefix: str = "") -> str:
    """The `XXX-NNNNNN` id in a `new` result line."""
    for line in out.splitlines():
        m = re.search(r"\b([A-Z]{1,4}-\d{4,6})\b", line)
        if m:
            return m.group(1)
    raise AssertionError(f"no id in output:\n{out}")


def _recorded_path(args: list[str]) -> str:
    """CLI command path for coverage: `task move`, `item group add`, …"""
    parts = [a for a in args if not a.startswith("-")]
    return " ".join(parts[:3]) if parts else "(root)"


def run(*args: str, expect: int = 0, _desc: str | None = None,
        _kind: str = "ok") -> tuple[int, str]:
    """Run a proj command (CliRunner in-process or a real subprocess).
    A12: recorded for e2e.sh (`_kind` "ok"/"fail" pick the sh form, `_desc` the
    per-step comment; "skip" records nothing — the sandbox init is emitted by
    emit_sh's header instead)."""
    path = _recorded_path(list(args))
    REP.cmd(path)
    if _kind != "skip":
        _emit(_kind, (list(args), _desc))
    if SUBPROCESS:
        env = dict(os.environ)
        env["PROJ_HOME"] = str(SANDBOX)
        proj = str(REPO / ".pixi" / "envs" / "dev" / "bin" / "proj")
        p = subprocess.run([proj, *args], cwd=SANDBOX, env=env,
                           capture_output=True, text=True)
        out = p.stdout + p.stderr
        rc = p.returncode
    else:
        from typer.testing import CliRunner

        from proj.cli import app

        old = os.getcwd()
        os.chdir(SANDBOX)
        try:
            r = CliRunner().invoke(app, list(args), catch_exceptions=False)
            out, rc = r.output, r.exit_code
        finally:
            os.chdir(old)
    if rc != expect:
        raise AssertionError(f"{args!r} expected rc={expect}, got {rc}:\n{out}")
    return rc, out


def expect_ok(desc: str, needle: str, *args: str) -> str:
    try:
        rc, out = run(*args, expect=0, _desc=desc, _kind="ok")
    except AssertionError as e:
        REP.fail(desc, str(e))
        return ""
    if needle in out:
        REP.ok(desc)
        return out
    REP.fail(desc, f"missing {needle!r} in:\n{out}")
    return ""


def expect_fail(desc: str, *args: str) -> None:
    try:
        run(*args, expect=0, _desc=desc, _kind="fail")
        REP.fail(f"{desc} (unexpectedly succeeded)", " ".join(args))
    except AssertionError:
        REP.ok(f"{desc} (rejected)")


def read(rel: str) -> str:
    return (SANDBOX / rel).read_text(encoding="utf-8")


def glob1(pattern: str) -> str:
    hits = sorted(SANDBOX.glob(pattern))
    assert hits, f"no file for {pattern!r}"
    return str(hits[0].relative_to(SANDBOX))


def md_of(id_: str) -> str:
    """The md file path for a record id (the profile's dir, from the registry)."""
    from proj.engine import store as rec

    reg = rec.load_registry(None)
    code = id_.split("-")[0]
    name = next((n for n, s in reg.items() if s.code == code), code)
    spec = reg[name]
    return glob1(f"{spec.dir}/{id_}-*.md")


def write_sections(rel: str, sections: dict[str, str]) -> None:
    """Fill `## Section` bodies in a record md (the human/LLM input channel),
    then sync them back into the DB."""
    p = SANDBOX / rel
    t = p.read_text(encoding="utf-8")
    for name, body in sections.items():
        m = re.search(rf"^## {re.escape(name)}[ \t]*\n", t, re.M)
        assert m, f"no '## {name}' in {rel}"
        start = m.end()
        nxt = re.search(r"\n## |\n<!-- proj:", t[start:])
        stop = start + (nxt.start() if nxt else len(t[start:]))
        t = t[: start] + body.strip() + "\n\n" + t[stop:].lstrip("\n")
        _emit("section", (rel, name, body.strip()))
    p.write_text(t, encoding="utf-8")
    run("sync")


def edit_md(rel: str, old: str, new: str, desc: str | None = None) -> None:
    """Hand-edit a record md: replace `old` with `new` (one line each). The next
    write command auto-ingests it (A1) — no explicit sync here (PLAN §18.3: the
    reference sh shows the direct edit + the follow-up `proj` command)."""
    p = SANDBOX / rel
    t = p.read_text(encoding="utf-8")
    assert old in t, f"{old!r} not in {rel}"
    p.write_text(t.replace(old, new, 1), encoding="utf-8")
    _emit("edit", (rel, old, new, desc))


def move_md_line(rel: str, frag: str, target: str, desc: str | None = None) -> None:
    """Hand-move one md line (the one starting with `frag`) to just below the
    `target` line (a `### ` group heading)."""
    p = SANDBOX / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    line = next((ln for ln in lines if ln.startswith(frag)), None)
    assert line, f"no line starting {frag!r} in {rel}"
    assert target in lines, f"{target!r} not in {rel}"
    lines.remove(line)
    lines.insert(lines.index(target) + 1, line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _emit("move", (rel, frag, target, desc))


def mcp_call(tool: str, *args: str, **kw) -> str:
    """Call an MCP tool in-process (chapter 12 becomes a real stdio session
    in phase 4); the call is recorded for MCP coverage."""
    from proj import mcp

    fn = getattr(mcp, tool)
    kw.pop("root", None)
    out = str(fn(root=str(SANDBOX), *args, **kw))
    REP.tool(tool)
    _emit("mcp", (tool, args, kw))
    return out


def reset_sandbox() -> None:
    # the temp dir already exists (mkdtemp created it); the explicit-sandbox
    # path may hold a previous run — wipe it (guarded: it must be .testproj)
    if SANDBOX.exists() and any(SANDBOX.iterdir()):
        if SANDBOX is not None and SANDBOX.resolve() == TMPDIR_TESTPROJ.resolve():
            _guard(SANDBOX, ".testproj")
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=SANDBOX, check=True)
    old = os.getcwd()
    os.chdir(SANDBOX)
    try:
        run("init", _kind="skip")
        run("hooks", "install", _kind="skip")
        REP.cmd("hooks install")
    finally:
        os.chdir(old)


# ---- story: PCB defect detection model v1 ---------------------------------------

D = "DEC-0002"          # YOLOv8 baseline
D2 = "DEC-0003"        # abandoned: RetinaNet
P = "PLN-0001"         # defect-detection v1 rollout
T_DS = "T-000001"      # dataset
T_PIPE = "T-000002"    # training pipeline
T_EVAL = "T-000003"    # eval script (moved under T_PIPE)
T_ONNX = "T-000004"    # ONNX export
T_CUDA = "T-000005"    # standalone: upgrade training-node CUDA driver
T_QC = "T-000006"      # label QC, under T_DS
T_TRT = "T-000007"     # TensorRT quantization — fails, Failed = terminal
I = "INC-0001"         # train/val overlap incident
DEB = "DEB-0001"       # hardcoded anchor sizes
DEB2 = "DEB-0002"      # fixed augmentation seed — waived
E = "EXP-0001"         # mosaic augmentation on small-defect recall
D3 = "DEC-0004"        # distill to a smaller model (supersedes D)


def ch0() -> None:
    REP.chapter("第 0 章 开局：新建 PCB 缺陷检测项目仓库")
    out = read("CONTRIBUTING.md")
    assert out.strip(), "CONTRIBUTING.md missing"
    REP.ok("scaffold 生成 CONTRIBUTING.md")
    out = mcp_call("proj_info")
    assert "decision" in out, out
    REP.ok("proj_info 项目状态（MCP）")
    # A11: the scaffold-seeded decision starts with Context/Decision/Consequences
    # only — fill the rest so it is not an empty shell
    write_sections(md_of("DEC-0001"), {
        "Decision Drivers": (
            "- 多个 agent 并发写同一项目不冲突"
            "\n- 变更可进 git 审计（md 投影）"
            "\n- 零外部服务依赖"
        ),
        "Considered Options": (
            "### SQLite + md 投影（已选）\n"
            "Pros: 单文件原子写、md 即人工/LLM 输入通道、编号由 DB 保证\n"
            "### 纯 markdown + 约定\n"
            "Cons: 无并发写保护，编号靠扫描，门禁没有单一事实来源"
        ),
    })
    REP.ok("种子决策 DEC-0001 补 Drivers/Options 正文")


def ch1() -> None:
    REP.chapter("第 1 章 选型：基线检测器选型")
    out = expect_ok("decision YOLOv8 新建", f"{D}",
                    "decision", "new", "基线检测器选 YOLOv8", "-y")
    write_sections(md_of(D), {
        "Context": "现有产线质检是人工目检，漏检率 ~5%。需要一个可部署到边缘端的"
                   "缺陷检测基线，作为后续迭代与蒸馏的对照。",
        "Decision": "以 YOLOv8n 为基线检测器：8 类缺陷（划痕/缺件/焊接不良/…），"
                    "输入 640×640，ONNX 导出后部署。",
    })
    # the abandoned alternative, for completeness of the state machine
    expect_ok("decision RetinaNet（已放弃）新建", f"{D2}",
              "decision", "new", "备选：RetinaNet", "-y")
    expect_ok("RetinaNet draft", "→ Draft", "decision", "draft", D2)
    expect_ok("RetinaNet reject", "→ Rejected", "decision", "reject", D2)
    # Confirmation checklist — the acceptance bar for the baseline
    expect_ok("Confirmation: mAP 不低于现有方案", "#001",
              "item", "add", D, "mAP@0.5 不低于现有方案")
    expect_ok("Confirmation: 边缘端延迟", "#002",
              "item", "add", D, "边缘端延迟 < 30 ms")
    expect_ok("YOLOv8 propose", "→ Proposed", "decision", "propose", D)
    expect_ok("YOLOv8 accept", "→ Accepted", "decision", "accept", D)
    write_sections(md_of(D), {
        "Consequences": "训练显存需求低（单卡 A10 即可）；ONNX 链路成熟；"
                        "小缺陷召回率需在实验阶段单独验证。",
        "Decision Drivers": (
            "- mAP@0.5 ≥ 0.85（对照现有目检漏检率 ~5%）"
            "\n- 单帧延迟 < 30 ms（边缘端 A10）"
            "\n- 团队熟悉训练与 ONNX 链路"
        ),
        "Considered Options": (
            "### YOLOv8n（已选）\n"
            "Pros: 显存需求低、ONNX 链路成熟、训练快\n"
            "Cons: 小目标召回待验证\n"
            "### RetinaNet（见 " + D2 + "，已 Rejected）\n"
            "Pros: dense 回归对小目标略优\n"
            "Cons: 显存高、部署链路要重跑、团队无经验"
        ),
    })
    # A11: the rejected alternative also gets a full body — a rejection
    # without context is exactly what this project should not ship
    write_sections(md_of(D2), {
        "Context": "YOLOv8n 的小缺陷（<32px）召回偏低，评估是否引入 dense 回归检测器补足。",
        "Decision Drivers": "- 边缘端部署成本\n- 与现有 ONNX 链路兼容\n- 团队经验",
        "Considered Options": (
            "### RetinaNet\n"
            "Pros: dense 回归对小目标略优\n"
            "Cons: 显存需求高、ONNX 链路要重跑、团队无生产经验"
        ),
        "Decision": "不引入 RetinaNet；小目标问题先用数据增强解决（见 EXP-0001）。",
        "Consequences": "小缺陷召回押注 mosaic 实验；若仍不达标再重新评估检测器。",
    })


def ch2() -> None:
    REP.chapter("第 2 章 立项：缺陷检测 v1 上线计划")
    expect_ok("plan 新建（带 summary）", f"{P}",
              "plan", "new", "缺陷检测 v1 上线", "-y",
              "-s", "在产线边缘端跑 YOLOv8n，达到验收指标后全量上线")
    write_sections(md_of(P), {
        "Goal": "缺陷检测 v1 在边缘端稳定运行：mAP@0.5 ≥ 0.85、单帧延迟 < 30 ms、"
                "漏检率 < 2%。",
        "Scope": "8 类缺陷；不含产线整改与复检流程（后续版本）。",
        "Risks & Dependencies": "依赖标注数据量（≥3 万张）与 CUDA 驱动升级（T-000005）。",
    })
    # K2: the plan no longer has a Milestones section (and cannot parent tasks);
    # it tracks work through its Work checklist (ch3 references the tasks there)
    for text in ("mAP@0.5 ≥ 0.85", "单帧延迟 < 30 ms", "漏检率 < 2%"):
        expect_ok(f"Success Criterion: {text}", "#", "item", "add", P, text)
    expect_fail("activate 前 review 被拒（from: Active）", "plan", "review", P)
    expect_ok("plan activate", "→ Active", "plan", "activate", P)


def ch3() -> None:
    REP.chapter("第 3 章 拆任务")
    # K2: parent is task→task only (no plan parent); the plan tracks these via
    # its Work checklist, referenced with `item add P … --task T-…` (added below)
    T_DS = _id_of(expect_ok("数据集任务", "Created",
                            "task", "new", "构建数据集（采集/标注/划分）", "-y"))
    # A11: task bodies (Goal/Steps; Open/Notes only appear when there is a
    # blocker or a remark — see the ch9 EMPTY_OK exceptions)
    write_sections(md_of(T_DS), {
        "Goal": "产出 3 万张、8 类缺陷、可直接喂给 YOLOv8n 训练的数据集，含按板号哈希的划分（P 的 M1）。",
        "Steps": (
            "- 产线相机采集原图并按文件哈希去重（INC-0001 之后）"
            "\n- YOLO 格式标注 8 类缺陷 + 抽检"
            "\n- 按板号哈希划分 train/val/test = 7/2/1"
        ),
    })
    T_PIPE = _id_of(expect_ok("训练管线任务", "Created",
                              "task", "new", "训练管线（数据增强/训练/早停）", "-y"))
    write_sections(md_of(T_PIPE), {
        "Goal": "用数据集把 YOLOv8n 训到 val mAP ≥ 0.85，产出可部署 checkpoint（P 的 M2）。",
        "Steps": (
            "- 数据增强：mosaic p=0.5（E 的结论）"
            "\n- 训练 100 epoch、早停 patience=8"
            "\n- 保存 checkpoint 与训练日志"
        ),
    })
    T_EVAL = _id_of(expect_ok("评估脚本任务", "Created",
                              "task", "new", "评估脚本（mAP/延迟/漏检）", "-y"))
    write_sections(md_of(T_EVAL), {
        "Goal": "在 test 集评出 mAP@0.5、p99 延迟、漏检率，作为 P 是否达标的判定依据。",
        "Steps": "- 在 test 集评 mAP@0.5\n- 边缘端实测 p99 延迟\n- 抽检漏检率",
    })
    T_ONNX = _id_of(expect_ok("ONNX 导出任务", "Created",
                              "task", "new", "导出 ONNX 并部署边缘端", "-y"))
    write_sections(md_of(T_ONNX), {
        "Goal": "checkpoint 导出 ONNX 并部署边缘端：模型 ≤ 15 MB、单帧延迟 < 30 ms（P 的 M3）。",
        "Steps": "- 导出 ONNX（opset 12）\n- 边缘端实测延迟与内存\n- 部署脚本入仓",
    })
    # standalone task — no parent (a task may stand alone)
    T_CUDA = _id_of(expect_ok("独立任务：升级训练机 CUDA 驱动", "Created",
                              "task", "new", "升级训练机 CUDA 驱动", "-y"))
    write_sections(md_of(T_CUDA), {
        "Goal": "训练机 CUDA 驱动升到与 PyTorch 2.x 轮子兼容的版本（T_PIPE 的前置，独立任务无父任务）。",
        "Steps": "- 确认目标驱动版本\n- 停机窗口升级\n- 重启后 nvidia-smi 验证",
    })
    # sub-task under the dataset task
    T_QC = _id_of(expect_ok("子任务：标注质检", "Created",
                            "task", "new", "标注质检", "-y", "--parent", T_DS))
    write_sections(md_of(T_QC), {
        "Goal": "标注质检：抽检 ≥ 10% 的板、一致率 ≥ 95%，不达标的批退回重标（T_DS 的子任务）。",
        "Steps": "- 按板号随机抽 10%\n- 双人复核一致率\n- 不达标批退回重标",
    })
    # related (cross-type, unordered): eval script ↔ baseline decision
    T_REL = _id_of(expect_ok("相关任务：产线抽检对照", "Created",
                             "task", "new", "产线抽检对照", "-y", "-r", D))
    write_sections(md_of(T_REL), {
        "Goal": "产线抽检盲测对照：量化模型相对人眼目检的漏检率（与 D 相关，cross-type 边）。",
        "Steps": "- 抽一批板盲测\n- 人眼 vs 模型双评\n- 汇总漏检率对照",
    })
    # move: the eval script re-homes under the training pipeline
    expect_ok("评估脚本改挂到训练管线", f"parent → {T_PIPE}",
              "task", "move", T_EVAL, "--parent", T_PIPE)
    # K2: the plan tracks its work through the Work checklist (item → task
    # reference); a terminal task closes the item automatically (P3)
    for label, tid in (("构建数据集", T_DS), ("训练管线", T_PIPE),
                       ("导出 ONNX 并部署边缘端", T_ONNX)):
        expect_ok(f"Work 清单引用 {label}", "→", "item", "add", P,
                  label, "--task", tid)
    expect_ok("plan 的 Work 清单列出 3 条 item", "PLN", "item", "ls", P)
    pmd = read(md_of(P))
    for tid in (T_DS, T_PIPE, T_ONNX):
        assert f"→ {tid}" in pmd, f"Work checklist missing → {tid}"
    REP.ok("plan 的 Work 清单渲染进 md（3 个 → T- 引用）")
    # rename: -t renames the md file (id stable); the new title mixes CJK + ASCII,
    # so the slug (and therefore the filename) changes (A10: CJK kept in the slug)
    expect_ok("-t 改名（id 不变）", "title →",
              "task", "update", T_ONNX, "-t", "ONNX 导出 + TensorRT 部署 (edge)")
    assert md_of(T_ONNX).endswith("onnx-导出-tensorrt-部署-edge.md"), md_of(T_ONNX)
    REP.ok("改名后 md 文件重命名到新 slug（id 稳定）")
    # WIP.md replaces TASKLIST.md (W1): it is always rendered (empty shows
    # the placeholder). The WIP membership story (wip add / auto-enter / sync
    # write-back) is exercised in its own e2e chapter once the `proj wip`
    # commands land (PLAN §5).
    run("sync")
    assert "WIP" in read("WIP.md"), read("WIP.md")
    REP.ok("WIP.md 已渲染（取代 TASKLIST.md）")
    # the dataset task shows its sub-task tree (checkbox + hyperlink, indented)
    md = read(md_of(T_DS))
    assert f"[{T_QC}-" in md and "<summary>Tasks</summary>" in md
    REP.ok("父任务 md 渲染子任务树（checkbox + 超链接）")
    # K2: the plan has NO Tasks block (it cannot parent tasks); it references its
    # work through the Work checklist (item → task). The dataset TASK shows its
    # own task subtree in a Tasks block. The plan's summary trails in the head.
    pmd = read(md_of(P))
    assert "<summary>Tasks</summary>" not in pmd  # no Tasks block on a plan
    assert f"## Work" in pmd and f"→ {T_DS}" in pmd  # Work checklist refs the task
    assert "_在产线边缘端跑 YOLOv8n，达到验收指标后全量上线_" in pmd
    REP.ok("plan 无 Tasks 块；Work 清单引用任务；summary 斜体在 head 围栏")
    # cross-type ls + cross-type show (id carries the type)
    out = expect_ok("跨类型 ls（--type task）", T_DS, "ls", "--type", "task")
    expect_ok("跨类型 show（id 自带类型）", "status:", "show", T_QC)



def ch4() -> None:
    REP.chapter("第 4 章 做任务：数据集清单（分组 A·采集 / B·标注 / C·划分）")
    expect_ok("分组 A 采集", "### A", "item", "group", "add", T_DS, "采集")
    a1 = expect_ok("采集：3 万张 PCB 原图", "#A01",
                   "item", "add", T_DS, "3 万张 PCB 原图（含缺陷）", "--group", "A")
    expect_ok("分组 B 标注", "### B", "item", "group", "add", T_DS, "标注")
    expect_ok("标注：8 类缺陷", "#B01",
              "item", "add", T_DS, "标注 8 类缺陷（YOLO 格式）", "--group", "B")
    expect_ok("标注：质检抽检", "#B02",
              "item", "add", T_DS, "标注质检抽检 ≥ 10%", "--group", "B")
    expect_ok("分组 C 划分", "### C", "item", "group", "add", T_DS, "划分")
    expect_ok("划分：train/val/test = 7/2/1", "#C01",
              "item", "add", T_DS, "train/val/test = 7/2/1（按板号分层）", "--group", "C")
    # edit / mv (cross-group renumbers) / ls / set / rm
    expect_ok("item edit 加备注", "产线相机实拍", "item", "edit", f"{T_DS}#A01",
              "--note", "产线相机实拍")
    # cross-group move: A01 takes a FRESH number in group C (#C02), A01 is void
    expect_ok("item mv 跨组换号（A01 → C02，提示新编号）", "note: item #A01 → #C02",
              "item", "mv", f"{T_DS}#A01", "--group", "C")
    expect_ok("item ls 分组过滤（C 组两行）", "#C02", "item", "ls", T_DS, "--group", "C")
    expect_fail("换号后旧编号作废（A01 已无效）", "item", "check", f"{T_DS}#A01")
    expect_ok("item rm（编号永不复用，重列清单）", "#C01", "item", "rm", f"{T_DS}#C02")
    expect_fail("rm 后编号不复用（C02 已删）", "item", "check", f"{T_DS}#C02")
    # the first closed item promotes Draft → In Progress (the task profile's
    # promote); the command reports it as a note above the list (R5)
    expect_ok("首次勾选 promote Draft → In Progress（命令输出提示）",
              f"note: {T_DS} promoted Draft → In Progress", "item", "check", f"{T_DS}#B01")
    assert "`🚧 In Progress`" in read(md_of(T_DS))
    # hand-edit the record md (the LLM's primary input channel) → sync it back
    edit_md(md_of(T_DS), "- [ ] #B02 标注质检抽检 ≥ 10%",
            "- [x] #B02 标注质检抽检 ≥ 10%（抽 3 板，一致率 96%）",
            "勾选 B02 并加备注（抽 3 板）")
    expect_ok("手改记录 md 后 sync（md 为准）", "OK", "sync")
    assert "（抽 3 板，一致率 96%）" in read(md_of(T_DS))
    REP.ok("手改的 item 行（含备注）被 sync 回收进 DB")
    # hand-move a line to ANOTHER group in the md: B02 goes under "A · 采集",
    # which is followed by groups B and C (R1: this used to block every write)
    move_md_line(md_of(T_DS), "- [x] #B02 ", "### A · 采集",
                 "把 B02 挪到 A · 采集 组")
    expect_ok("在 md 里跨组移动后 sync（B02 → A02，换号）", "#B02 → #A02", "sync")
    assert "- [x] #A02 ✅ 标注质检抽检" in read(md_of(T_DS))
    expect_ok("跨组移动后的 item 仍在清单里", f"{T_DS}#A02", "item", "ls", T_DS)
    # hand-edit an open item's TEXT, then operate straight away: A1 auto-ingests
    # the edit first (the next write command is the sync)
    edit_md(md_of(T_DS), "- [ ] #C01 train/val/test = 7/2/1（按板号分层）",
            "- [ ] #C01 train/val/test = 8/1/1（按板号哈希重划）",
            "改 C01 的文本（直接操作，A1 自动 ingest）")
    expect_ok("手改文本后直接操作（A1 自动 ingest）", "- [x]", "item", "check", f"{T_DS}#C01")
    assert "（按板号哈希重划）" in read(md_of(T_DS))
    REP.ok("手改的 item 文本被自动回收进 DB，无需先 sync")


def ch5() -> None:
    REP.chapter("第 5 章 门禁：评估未完成时关不掉训练任务")
    # T_PIPE has an open acceptance item → done is refused (gates.items)
    expect_ok("训练管线验收项（暂留）", "#001",
              "item", "add", T_PIPE, "训练收敛：val mAP 平台期 ≥ 5 epoch")
    expect_fail("done 被拒：仍有未处理 item", "task", "done", T_PIPE)
    # T_EVAL (child, not terminal) → the parent cannot go Done either
    expect_fail("父任务被拒：子任务 T_EVAL 未结束", "task", "done", T_PIPE)
    # the TensorRT quantization spike fails — Failed is terminal and unblocks gates
    T_TRT = _id_of(expect_ok("TensorRT 量化任务", "Created",
                             "task", "new", "TensorRT 量化（INT8）", "-y", "--parent", T_PIPE))
    write_sections(md_of(T_TRT), {
        "Goal": "INT8 量化压边缘端延迟（spike）：精度掉 ≤ 5pt 才保留，否则放弃（T_PIPE 的子任务）。",
        "Steps": "- 校准集生成\n- INT8 量化\n- 对照 mAP 与延迟",
    })
    write_sections(md_of(T_TRT), {
        "Acceptance Criteria": "",  # section exists; items are added via CLI
    }) if "## Acceptance Criteria" in read(md_of(T_TRT)) else None
    expect_ok("TensorRT 量化失败（Failed = 终止，满足父门禁）", "→ Failed",
              "task", "fail", T_TRT, "-m", "Changed: INT8 量化精度掉 6 点，放弃")
    # close the tree: child first, then the parent (its items closed by now).
    # S.9 前置: T_EVAL / T_QC have no Acceptance Criteria, so nothing promotes
    # them to In Progress — start them explicitly before done (done can only
    # leave In Progress).
    run("task", "start", T_EVAL)
    expect_ok("评估脚本完成", "→ Done", "task", "done", T_EVAL,
              "-m", "Changed: mAP 0.87 / p99 38ms（延迟超标，见 INC-0001）")
    run("item", "check", f"{T_PIPE}#001")
    expect_ok("子任务都终止 → 训练管线 done（门禁放行）", "→ Done",
              "task", "done", T_PIPE)
    # the dataset task: its sub-task (label QC) first, then the dataset itself
    run("task", "start", T_QC)
    expect_ok("标注质检完成", "→ Done", "task", "done", T_QC)
    for ref in (f"{T_DS}#B01", f"{T_DS}#C01"):
        if "- [ ] " in read(md_of(T_DS)):
            run("item", "check", ref)
    expect_ok("数据集任务完成（items 全关闭）", "→ Done", "task", "done", T_DS)
    # the ONNX export task: open, then done
    run("task", "start", T_ONNX)
    expect_ok("ONNX 导出任务完成", "→ Done", "task", "done", T_ONNX,
              "-m", "Changed: 导出 12MB 模型，边缘端跑通（延迟未达标）")
    # the standalone task (no parent) starts too: entering In Progress (task
    # wip.enter) auto-registers it in WIP.md — the demo ends with a task entry,
    # not just the plan; one acceptance item so the entry carries a checklist
    expect_ok("独立验收项（driver 版本）", "#001",
              "item", "add", T_CUDA, "驱动版本 ≥ 525（支持 CUDA 12）")
    expect_ok("独立任务开工（自动登记进 WIP）", "→ In Progress",
              "task", "start", T_CUDA)


def ch6() -> None:
    REP.chapter("第 6 章 出事故：训练/验证集图片重复，指标虚高")
    I = _id_of(expect_ok("incident 新建", "Created",
                         "incident", "new", "训练集与验证集图片重复", "-y"))
    write_sections(md_of(I), {
        "Summary": "评估 mAP 0.91 异常偏高，排查发现 1,240 张图在 train 与 val 同时出现。",
        "Impact": "v0.3 评估结果作废；已基于虚高指标做的对比结论需要重跑。",
        "Root cause": "划分脚本按文件创建时间分层，同一次拍摄的文件时间戳相同，"
                      "导致同一块板的两张图被分到不同集合。",
    })
    expect_ok("incident open（进入 Open）", "→ Open", "incident", "open", I)
    expect_ok("Action Item：按文件哈希去重（owner+due，已过期）", "· owner: sara",
              "item", "add", I, "按文件哈希去重", "--owner", "sara", "--due", "2026-10-01")
    expect_ok("Action Item：按板号重新划分", "· owner: lin",
              "item", "add", I, "按板号重新划分 train/val", "--owner", "lin", "--due", "2026-10-05")
    expect_ok("item ls --overdue 列出过期项", f"{I}#001", "item", "ls", "--overdue")
    expect_ok("incident mitigate（止血）", "→ Mitigated", "incident", "mitigate", I,
              "-m", "Changed: 去重完成，指标回到 0.87 区间")
    expect_fail("close 被拒：还有未处理的 Action Item（gates.items）",
                "incident", "close", I)
    run("item", "check", f"{I}#001")
    run("item", "check", f"{I}#002")
    write_sections(md_of(I), {
        "Timeline": "- 2026-10-01 发现指标异常\n- 2026-10-02 定位重复图片\n- 2026-10-03 去重 + 重划",
        "Actions taken": (
            "- 按文件哈希去重 1,240 张（sara，2026-10-03）"
            "\n- 按板号哈希重划 train/val（lin），mAP 回到 0.87 区间"
        ),
        "Lessons learned": (
            "按创建时间去重不可靠：同一次拍摄的文件时间戳相同；"
            "去重必须按内容哈希，划分必须按板号哈希。"
        ),
        "Prevention rule": "划分脚本必须按板号哈希分层，CI 加 train/val 重叠率检查（须为 0）。",
    })
    expect_ok("Action Items 全处理 → close", "→ Closed", "incident", "close", I)


def ch7() -> None:
    REP.chapter("第 7 章 还债：赶进度留下的两处硬编码")
    DEB = _id_of(expect_ok("debt：anchor 尺寸硬编码", "Created",
                           "debt", "new", "anchor 尺寸硬编码在配置文件", "-y"))
    write_sections(md_of(DEB), {
        "Location": "configs/yolov8.yaml:anchor_sizes",
        "Reason": "赶 v1 进度，没来得及按数据集统计 anchor，先用默认值。",
        "Impact": "8 类 anchor 用默认尺寸，小缺陷 mAP 约低 1pt（E 的对照臂受影响）。",
        "Trigger for removal": "v1.1 按缺陷尺寸重算 anchor 时。",
    })
    expect_ok("debt open", "→ Open", "debt", "open", DEB)
    expect_ok("Repaid 需要门禁：先留一个未完成步骤", "#001",
              "item", "add", DEB, "参数化 anchor 配置（读数据集统计）")
    expect_fail("repay 被拒：Repayment Plan 未全部处理（gates.items）",
                "debt", "repay", DEB)
    DEB2 = _id_of(expect_ok("debt：固定数据增强随机种子", "Created",
                            "debt", "new", "数据增强随机种子固定为 42", "-y"))
    write_sections(md_of(DEB2), {
        "Location": "train/configs/aug.py:seed = 42",
        "Reason": "v1 期间让 mosaic 实验可复现（E 需要同种子对照）。",
        "Impact": "增强分布固定在同一份随机抽取，无法多抽几次看方差。",
        "Trigger for removal": "v2 放开随机种子时。",
    })
    run("debt", "open", DEB2)
    run("debt", "progress", DEB2)
    expect_ok("Repayment Plan 步骤", "#001", "item", "add", DEB2, "恢复随机种子")
    expect_ok("dropped 步骤（带备注）", "🚫", "item", "set", f"{DEB2}#001", "dropped",
              "--note", "v1 阶段保持复现性，v2 再放开")
    expect_ok("Waived 不需要门禁", "→ Waived", "debt", "waive", DEB2,
              "-m", "Deprecated: 复现性优先，留到 v2 一并处理")
    # now repay DEB (its step done)
    run("debt", "progress", DEB)
    run("item", "check", f"{DEB}#001")
    expect_ok("步骤全处理 → Repaid", "→ Repaid", "debt", "repay", DEB,
              "-m", "Fixed: anchor 按数据集 3×3 聚类重算")


def ch8() -> None:
    REP.chapter("第 8 章 做实验：mosaic 增强对小缺陷召回率的影响")
    E = _id_of(expect_ok("experiment 新建（带 commit 字段）", "Created",
                         "experiment", "new", "mosaic 增强与小缺陷召回率", "-y",
                         "--commit", "a1b2c3d"))
    write_sections(md_of(E), {
        "Hypothesis": "小缺陷（< 32px）是主要漏检源；mosaic 把小目标放到大图边角，"
                      "能提升小缺陷召回率而不显著损失整体 mAP。",
        "Setup": "同一数据集（去重后），两臂：baseline（无 mosaic）vs +mosaic(p=0.5)；"
                 "各 100 epoch，早停 patience=8。",
    })
    expect_ok("experiment propose", "→ Proposed", "experiment", "propose", E)
    expect_ok("experiment run", "→ Running", "experiment", "run", E)
    assert "a1b2c3d" in read(md_of(E))
    REP.ok("commit 字段渲染进 experiment md")
    expect_ok("criterion：小缺陷召回 +5%", "#001",
              "item", "add", E, "小缺陷（<32px）召回率提升 ≥ 5%")
    expect_ok("criterion：整体 mAP 不掉", "#002",
              "item", "add", E, "整体 mAP@0.5 下降 < 0.5pt")
    write_sections(md_of(E), {
        "Results": "mosaic 臂：小缺陷召回 41.2% → 46.8%（+5.6pt）；整体 mAP 0.871 → 0.868（-0.3pt）。",
        "Analysis": "提升来自 < 64px 档；< 32px 档仅 +2.1pt，mosaic 对极小目标作用有限。",
    })
    expect_ok("criterion set done（带证据）", "✅",
              "item", "set", f"{E}#001", "done", "--note", "小目标 +5.6pt（41.2→46.8）")
    expect_ok("criterion set dropped（证据不足）", "🚫",
              "item", "set", f"{E}#002", "dropped", "--note", "-0.3pt 在噪声区间内")
    expect_ok("第 3 条 criterion（仍 open）", "#003",
              "item", "add", E, "训练时间不超过 baseline +10%")
    expect_fail("close 被拒：criterion 未全部 evaluated（gates.items）",
                "experiment", "close", E)
    run("item", "set", f"{E}#003", "done", "--note", "+4%")
    write_sections(md_of(E), {
        "Conclusion": "mosaic 保留在训练管线（小目标收益）；< 32px 召回的进一步改进"
                      "转交 T_ONNX 边缘端方案（更大输入分辨率）评估。",
    })
    expect_ok("criterion 全 evaluated → close", "→ Closed", "experiment", "close", E,
              "-m", "Changed: 结论定稿，mosaic 进入管线")
    # a second, doomed experiment: 4-bit quantization — abandoned (any → Abandoned)
    E2 = _id_of(expect_ok("第二实验：4-bit 量化", "Created",
                          "experiment", "new", "4-bit 量化压缩", "-y"))
    write_sections(md_of(E2), {
        "Hypothesis": "4-bit 量化可再砍一半边缘端内存，精度损失可接受（< 3pt）。",
        "Setup": "同一 checkpoint，FP16 vs 4-bit 两臂，test 集对照 mAP 与边缘端内存。",
        "Results": "4-bit：mAP 0.871 → 0.78（-9pt）；内存 12MB → 6MB。",
    })
    run("experiment", "propose", E2)
    run("experiment", "run", E2)
    expect_ok("abandon（放弃，Abandoned 终止）", "→ Abandoned",
              "experiment", "abandon", E2, "-m", "Deprecated: 4-bit 精度掉 9 点，不可用")


# ---- ch9: 演示项目收尾 — 没有空的 ## 正文节（A11, §18.2）-----------------------


def ch9() -> None:
    REP.chapter("第 9 章 收尾：演示项目没有空的 ## 正文节")
    # A11: sections the story has deliberately not reached yet stay empty —
    # listed by NAME (each exists on exactly one record type): the task's
    # Open/Notes only carry a blocker or a remark, the plan's Review only at
    # M3 (plan still Active). These are the PLAN §18.2 "explicit exceptions".
    SECTION_EMPTY_OK = {"Open", "Notes", "Review"}
    # abandoned 实验没有结论可写（原因走 -m，不写 Conclusion 节）
    PAIR_EMPTY_OK = {("EXP-0002", "Analysis"), ("EXP-0002", "Conclusion")}
    # items/渲染拥有的节（由 DB 生成，不是"正文"，不在本断言范围内）
    ITEMS_OR_RENDER = {"Confirmation", "Success Criteria", "Acceptance Criteria",
                       "Repayment Plan", "Action Items"}
    from proj.engine import store as rec
    reg = rec.load_registry(None)

    def body_of(t: str, name: str) -> str:
        # the body of the `## name` section, up to the next `##` or EOF
        out: list[str] = []
        on = False
        for line in t.splitlines():
            if line.startswith("## "):
                on = line[3:].strip() == name
                continue
            if on:
                out.append(line)
        return "\n".join(out).strip()

    empty: list[str] = []
    n_files = 0
    for name, spec in reg.items():
        if name == "changelog":
            continue  # 渲染视图（CHANGELOG.md），不是记录文件
        text_secs = [sec.name for sec in spec.sections if sec.kind == "text"]
        for p in sorted(SANDBOX.glob(f"{spec.dir}/*.md")):
            n_files += 1
            t = p.read_text(encoding="utf-8")
            m = re.search(r"<!-- proj:\S+ ([A-Z]+-\d+) -->", t)
            rid = m.group(1) if m else p.name
            for sec_name in text_secs:
                if sec_name in ITEMS_OR_RENDER:
                    continue
                if not body_of(t, sec_name) and sec_name not in SECTION_EMPTY_OK \
                    and (rid, sec_name) not in PAIR_EMPTY_OK:
                    empty.append(f"{rid} {sec_name}")
    assert not empty, f"空正文节（应补正文或列入 EMPTY_OK）: {empty}"
    REP.ok(f"演示项目 {n_files} 条记录文件：无空 ## 正文节"
           f"（例外 {sorted(SECTION_EMPTY_OK)}，items/渲染节除外）")
    # WIP.md's final state: the plan (still Active) AND the standalone task
    # (In Progress) both carry an entry; the task's checklist is tickable there
    wip_text = read("WIP.md")
    assert f"## {P}." in wip_text and f"## {T_CUDA}." in wip_text, wip_text
    REP.ok("WIP.md：plan + 独立任务两条目（任务条目带清单，可直接勾）")


# ---- ch12: the agent takes over through a REAL MCP stdio session (S.8) -------
# §S.8: an earlier version of this chapter called `proj.mcp` functions
# in-process, bypassing the tool dispatch (§21.2's bug slipped through that
# hole). Now the chapter spawns the `proj-mcp` entry point and drives it with
# the `mcp` client SDK over stdio — exactly what a client (Claude, ...) does.
# The session runs on a SECOND, throwaway project (not the story sandbox):
# e2e-full's equivalence check compares the generated e2e.sh (MCP steps are
# comments there, CLI only) against the story sandbox's final `proj ls
# --json` / `item ls --json` — so the MCP session must not leave a different
# final state on that project.

MCP_AGENT = "agent-mcp"        # S.4: every changelog row records this actor


def _mcp_text(res) -> str:
    """An MCP tool result -> its text parts joined (empty for none)."""
    return "\n".join(getattr(c, "text", "") for c in res.content
                     if getattr(c, "text", None) is not None)


def ch12() -> None:
    REP.chapter("第 12 章 agent 接手：真实 MCP stdio 会话（proj-mcp 子进程 + 客户端 SDK）")
    import asyncio

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # a one-off project (its own temp dir): the stdio session's writes stay
    # here, so the story sandbox's final state (compared by e2e-full) is untouched
    home = Path(tempfile.mkdtemp(prefix="proj-e2e-mcp-"))
    env = dict(os.environ)
    env["PROJ_HOME"] = str(home)
    env["PROJ_AGENT"] = MCP_AGENT
    subprocess.run([str(REPO / ".pixi" / "envs" / "dev" / "bin" / "proj"),
                    "init", "--profile", "default"],
                   cwd=home, env=env, check=True, capture_output=True)
    try:
        params = StdioServerParameters(
            command=str(REPO / ".pixi" / "envs" / "dev" / "bin" / "proj-mcp"),
            env=env)

        def ok(desc, needle, tool, out):
            if needle not in out:
                REP.fail(desc, f"({tool})\n{out}")
            else:
                REP.tool(tool)
                REP.ok(desc)

        async def call(s, tool, **kw):
            # one call through the live stdio session (the real tool dispatch —
            # the hole S.21.2's bug slipped through); recorded for the sh's comments
            res = await s.call_tool(tool, kw)
            _emit("mcp", (tool, (), kw))  # sh: MCP steps stay comments (CLI-only sh)
            return _mcp_text(res)

        async def drive() -> None:
            async with stdio_client(params) as (r, w):  # noqa: SIM117 (r, w feed the session)
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    names = {t.name for t in (await s.list_tools()).tools}
                    assert {"proj_info", "proj_wip_ls", "proj_item_update",
                            "proj_verb", "proj_new", "proj_item_add"} <= names, names
                    REP.ok(f"list_tools：{len(names)} 个工具，含本章用到的 6 个")
                    ok("proj_info 项目状态（stdio 会话）", "managed by proj", "proj_info",
                       await call(s, "proj_info"))
                    ok("proj_wip_ls 空 WIP（stdio 会话）", "no records", "proj_wip_ls",
                       await call(s, "proj_wip_ls"))
                    out = await call(s, "proj_new", record_type="task", title="MCP 接手任务")
                    ok("proj_new 建任务（stdio 会话，工具分发）", "Created", "proj_new", out)
                    tid = _id_of(out)
                    ok("proj_item_add 加验收项（stdio 会话）", "#001", "proj_item_add",
                       await call(s, "proj_item_add", ref=tid, text="由 MCP 会话接手"))
                    out = await call(s, "proj_item_update", item=f"{tid}#001",
                                     state="done", note="备注：stdio 会话完成")
                    ok("proj_item_update 勾选 + 备注（stdio 会话）", "备注",
                       "proj_item_update", out)
                    ok("proj_verb(done) 任务完成（stdio 会话）", "→ Done", "proj_verb",
                       await call(s, "proj_verb", id=tid, verb="done"))
                    # S.4: the session's own writes carry the PROJ_AGENT actor
                    out = await call(s, "proj_show", id=tid, history=True)
                    ok("proj_show --history（stdio 会话）", "created:", "proj_show", out)
                    assert f"· {MCP_AGENT}" in out, out
                    REP.ok(f"changelog 记录操作者（· {MCP_AGENT}，PROJ_AGENT 生效）")
        asyncio.run(drive())
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---- A12: render the reference scripts/e2e.sh (PLAN §18.3) ----------------------

def _sh_line(cmd: list) -> str:
    return " ".join(shlex.quote(a) for a in cmd)


# the set_section python, as it must appear in the generated sh (a <<'PY' heredoc)
_SET_SECTION_PY = r"""
import sys, re
f, name = sys.argv[1], sys.argv[2]
body = sys.stdin.read().strip()
t = open(f, encoding='utf-8').read()
m = re.search('^## ' + re.escape(name) + r'[ \t]*\n', t, re.M)
start = m.end()
nxt = re.search('\n## |\n<!-- proj:', t[start:])
stop = start + (nxt.start() if nxt else len(t[start:]))
open(f, 'w', encoding='utf-8').write(t[:start] + body + '\n\n' + t[stop:].lstrip('\n'))
"""


def emit_sh(steps: list, out: Path) -> Path:
    """Render the recorded steps as a human-readable scripts/e2e.sh (PLAN §18.3):
    the `set_section` helper + the sandbox bootstrap, then per-chapter sections,
    `shlex`-quoted `proj` commands, expected-fail commands that cannot abort the
    script, `set_section` heredocs for the md section edits, python one-liners for
    the line edits / moves, and a comment per MCP call (the sh runs CLI only).
    Ids and item numbers are literal (the story is deterministic)."""
    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# scripts/e2e.sh — GENERATED by scripts/e2e.py (A12). Do not hand-edit:",
        "# re-run `pixi run e2e` to refresh it. 同一个场景，只用 `proj` CLI 命令复现（给用户当参考）。",
        "# 运行: bash scripts/e2e.sh [目录]  （默认 mktemp -d，结束保留并打印路径）；需要 `proj` 在 PATH 上。",
        "set -u",
        'dir="${1:-$(mktemp -d)}"',
        'cd "$dir" || exit 1',
        "",
        "# 把一个 ## 节的内容替换成 stdin 的内容:  set_section <file> <节名>",
        "set_section() {",
        "  python3 - \"$1\" \"$2\" <<'PY'",
        *_SET_SECTION_PY.splitlines(),
        "PY",
        "}",
        "",
        "git init -q",
        "proj init",
        "proj hooks install",
    ]
    for kind, payload in steps:
        if kind == "chapter":
            lines.append("")
            lines.append("# ── " + payload + " ──")
        elif kind in ("ok", "fail"):
            args, desc = payload
            cmd = _sh_line(args)
            note = ("  # " + desc) if desc else ""
            if kind == "ok":
                lines.append("proj " + cmd + note)
            else:
                lines.append('proj ' + cmd + ' && echo "意外成功" || echo "（按预期被拒绝）"' + note)
        elif kind == "section":
            rel, name, body = payload
            if not body:
                continue  # an empty section (T_TRT): nothing to fill
            lines.append("# 编辑 " + rel + " 的 ## " + name + " 节，再 proj sync 回收")
            lines.append("cat <<'EOF' | set_section " + _sh_line([rel, name]))
            lines.append(body)
            lines.append("EOF")
            lines.append("proj sync")
        elif kind == "edit":
            rel, old, new, desc = payload
            lines.append("# 手改 " + rel + "： " + (desc or old))
            py = ("import sys; f, o, n = sys.argv[1:4]; "
                  't = open(f, encoding="utf-8").read(); assert o in t; '
                  'open(f, "w", encoding="utf-8").write(t.replace(o, n, 1))')
            lines.append("python3 -c '" + py + "' " + _sh_line([rel, old, new]))
        elif kind == "move":
            rel, frag, target, desc = payload
            lines.append("# 在 " + rel + " 里把行 " + frag.strip() + " 挪到 " + target
                         + "（" + (desc or "换组") + "）")
            py = ("import sys; f, fr, tg = sys.argv[1:4]; "
                  'L = open(f, encoding="utf-8").read().splitlines(); '
                  'ln = next(x for x in L if x.startswith(fr)); L.remove(ln); '
                  'L.insert(L.index(tg) + 1, ln); '
                  'open(f, "w", encoding="utf-8").write(chr(10).join(L) + chr(10))')
            lines.append("python3 -c '" + py + "' " + _sh_line([rel, frag, target]))
        elif kind == "mcp":
            tool, args, kw = payload
            extra = " ".join(k + "=" + _sh_line([str(v)]) for k, v in kw.items())
            lines.append("# (MCP) " + tool + " " + " ".join(args) + " " + extra
                         + " — 参考脚本只跑 CLI；MCP 调用见 e2e.py")
    lines.append("")
    lines.append('echo "场景完成，项目在: $dir"')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _sh_equivalence() -> int:
    """PLAN §18.3 等价性检查（只在 e2e-full 里跑，约 100 命令 × 0.25 s）：在临时目录
    跑生成的 scripts/e2e.sh，比较它和 e2e.py 两次产出的 `proj ls --json` /
    `proj item ls --json` 是否一致（基准 = 本次 e2e.py 跑完的沙盒 SANDBOX，teardown 之前）。"""
    sh = REPO / "scripts" / "e2e.sh"
    if not sh.is_file():
        print("e2e-full: 跳过等价性检查（scripts/e2e.sh 不存在）")
        return 0
    env = dict(os.environ)
    env["PATH"] = str(REPO / ".pixi" / "envs" / "dev" / "bin") + os.pathsep + env.get("PATH", "")
    proj = str(REPO / ".pixi" / "envs" / "dev" / "bin" / "proj")
    print("\ne2e-full: 等价性检查 — 在临时目录跑 scripts/e2e.sh，对比 JSON …")
    shdir = Path(tempfile.mkdtemp(prefix="proj-e2e-sh-"))
    try:
        p = subprocess.run(["bash", str(sh), str(shdir)], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            print(f"FAIL: scripts/e2e.sh 退出码 {p.returncode}")
            for line in p.stdout.splitlines()[-20:]:
                print(f"    {line}")
            return 1
        for label, args in (("proj ls --json", ["ls", "--json"]),
                            ("proj item ls --json", ["item", "ls", "--json"])):
            got = subprocess.run([proj, *args], cwd=shdir, env=env,
                                 capture_output=True, text=True).stdout.strip()
            want = subprocess.run([proj, *args], cwd=SANDBOX, env=env,
                                  capture_output=True, text=True).stdout.strip()
            if got == want:
                print(f"  ✓ {label} 一致")
            else:
                print(f"  FAIL {label} 不一致：")
                print(f"    sh: {got[:400]}")
                print(f"    py: {want[:400]}")
                return 1
        return 0
    finally:
        shutil.rmtree(shdir, ignore_errors=True)


def main() -> int:
    global _EMIT_ON
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sh", default=str(REPO / "scripts" / "e2e.sh"),
                    metavar="PATH", help="参考 e2e.sh 写到哪（默认 scripts/e2e.sh）")
    ap.add_argument("--no-emit", action="store_true", help="不刷新 scripts/e2e.sh")
    args = ap.parse_args()
    if args.no_emit:
        _EMIT_ON = False
    emit_path = Path(args.emit_sh)
    t0 = time.time()
    setup_sandbox()
    where = f"{SANDBOX}（本地临时目录，结束拷回 .testproj/）" if _TMP_SANDBOX else f"{SANDBOX}（PROJ_E2E_SANDBOX，就地）"
    print(f"e2e: PCB 缺陷检测模型 v1  ({'子进程模式' if SUBPROCESS else '进程内模式'}，沙盒 {where})")
    try:
        reset_sandbox()
        for ch in (ch0, ch1, ch2, ch3, ch4, ch5, ch6, ch7, ch8, ch9, ch12):
            ch()
        rc = _summary()
        rc = rc or _render_verify_sweep()
        # A12: refresh the reference e2e.sh after every run (pass OR fail)
        if _EMIT_ON:
            emit_sh(EMIT, emit_path)
            print(f"e2e: 参考脚本已更新 {emit_path}")
        # PLAN §18.3: equivalence (sh vs e2e.py) only in the slow subprocess run
        if SUBPROCESS:
            rc = rc or _sh_equivalence()
        return rc
    finally:
        teardown()


def _render_verify_sweep() -> int:
    """F6.3: after the whole story, the on-disk project must equal a full
    in-memory render (incremental == full, end-to-end)."""
    if os.environ.get("PROJ_RENDER_VERIFY") in ("0", "off", "false"):
        return 0
    from proj.core.config import ProjConfig
    from proj.render_verify import verify

    diffs = verify(ProjConfig(root=Path(SANDBOX)))
    if diffs:
        print(f"\ne2e: RENDER VERIFY FAILED — {len(diffs)} file(s) out of sync:")
        for rel, kind, detail in diffs[:10]:
            print(f"  {rel}: {kind} — {detail}")
        return 1
    REP.checks += 1
    print("✓ 渲染校验：磁盘与全量渲染逐文件一致（F6 增量 = 全量）")
    return 0


def _summary() -> int:
    dt = time.time() - REP.t0
    print(f"\ne2e: {REP.checks} 项检查，{REP.failed} 项失败（{dt:.1f}s）")
    return 1 if REP.failed else 0


if __name__ == "__main__":
    sys.exit(main())
