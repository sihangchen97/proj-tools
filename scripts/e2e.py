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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

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
    if (SANDBOX / ".proj.sqlite").is_file():
        con = sqlite3.connect(SANDBOX / ".proj.sqlite", timeout=5.0)
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
        print(f"\n\u2500\u2500 {title} \u2500\u2500")


REP = Report()

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


def run(*args: str, expect: int = 0) -> tuple[int, str]:
    """Run a proj command (CliRunner in-process or a real subprocess)."""
    path = _recorded_path(list(args))
    REP.cmd(path)
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
        rc, out = run(*args, expect=0)
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
        run(*args, expect=0)
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
    p.write_text(t, encoding="utf-8")
    run("sync")


def mcp_call(tool: str, *args: str, **kw) -> str:
    """Call an MCP tool in-process (chapter 12 becomes a real stdio session
    in phase 4); the call is recorded for MCP coverage."""
    from proj import mcp

    fn = getattr(mcp, tool)
    kw.pop("root", None)
    out = str(fn(root=str(SANDBOX), *args, **kw))
    REP.tool(tool)
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
        run("init")
        run("hooks", "install")
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
    for text in ("mAP@0.5 ≥ 0.85", "单帧延迟 < 30 ms", "漏检率 < 2%"):
        expect_ok(f"Success Criterion: {text}", "#", "item", "add", P, text)
    expect_fail("activate 前 review 被拒（from: Active）", "plan", "review", P)
    expect_ok("plan activate", "→ Active", "plan", "activate", P)


def ch3() -> None:
    REP.chapter("第 3 章 拆任务")
    T_DS = _id_of(expect_ok("数据集任务", "Created",
                            "task", "new", "构建数据集（采集/标注/划分）", "-y", "--parent", P))
    T_PIPE = _id_of(expect_ok("训练管线任务", "Created",
                              "task", "new", "训练管线（数据增强/训练/早停）", "-y", "--parent", P))
    T_EVAL = _id_of(expect_ok("评估脚本任务", "Created",
                              "task", "new", "评估脚本（mAP/延迟/漏检）", "-y"))
    T_ONNX = _id_of(expect_ok("ONNX 导出任务", "Created",
                              "task", "new", "导出 ONNX 并部署边缘端", "-y", "--parent", P))
    # standalone task — no parent (a task may stand alone)
    T_CUDA = _id_of(expect_ok("独立任务：升级训练机 CUDA 驱动", "Created",
                              "task", "new", "升级训练机 CUDA 驱动", "-y"))
    # sub-task under the dataset task
    T_QC = _id_of(expect_ok("子任务：标注质检", "Created",
                            "task", "new", "标注质检", "-y", "--parent", T_DS))
    # related (cross-type, unordered): eval script ↔ baseline decision
    T_REL = _id_of(expect_ok("相关任务：产线抽检对照", "Created",
                             "task", "new", "产线抽检对照", "-y", "-r", D))
    # move: the eval script re-homes under the training pipeline
    expect_ok("评估脚本改挂到训练管线", f"parent → {T_PIPE}",
              "task", "move", T_EVAL, "--parent", T_PIPE)
    # rename: -t renames the md file (id stable); the new title carries distinct
    # ASCII so the slug (and therefore the filename) actually changes
    expect_ok("-t 改名（id 不变）", "title →",
              "task", "update", T_ONNX, "-t", "ONNX 导出 + TensorRT 部署 (edge)")
    assert md_of(T_ONNX).endswith("onnx-tensorrt-edge.md"), md_of(T_ONNX)
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
    # the plan's Tasks block lists the tree from the DB (sub-task indented);
    # the plan's own summary trails as an italic line in the head fence
    pmd = read(md_of(P))
    assert f"[{T_DS}-" in pmd
    assert "  - [ ]" in pmd, "sub-task line not indented under its parent"
    assert "_在产线边缘端跑 YOLOv8n，达到验收指标后全量上线_" in pmd
    REP.ok("plan Tasks 块：DB 生成 + 子任务缩进；summary 斜体在 head 围栏")
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
    p = SANDBOX / md_of(T_DS)
    t = p.read_text(encoding="utf-8")
    t = t.replace("- [ ] #B02 标注质检抽检 ≥ 10%",
                  "- [x] #B02 标注质检抽检 ≥ 10%（抽 3 板，一致率 96%）")
    p.write_text(t, encoding="utf-8")
    expect_ok("手改记录 md 后 sync（md 为准）", "OK", "sync")
    assert "（抽 3 板，一致率 96%）" in read(md_of(T_DS))
    REP.ok("手改的 item 行（含备注）被 sync 回收进 DB")
    # hand-move a line to ANOTHER group in the md: B02 goes under "A · 采集",
    # which is followed by groups B and C (R1: this used to block every write)
    lines = p.read_text(encoding="utf-8").splitlines()
    b02 = next(ln for ln in lines if ln.startswith("- [x] #B02 "))
    lines.remove(b02)
    lines.insert(lines.index("### A · 采集") + 1, b02)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    expect_ok("在 md 里跨组移动后 sync（B02 → A02，换号）", "#B02 → #A02", "sync")
    assert "- [x] #A02 标注质检抽检" in read(md_of(T_DS))
    expect_ok("跨组移动后的 item 仍在清单里", f"{T_DS}#A02", "item", "ls", T_DS)
    # hand-edit an open item's TEXT, then operate straight away: A1 auto-ingests
    # the edit first (the next write command is the sync)
    p.write_text(p.read_text(encoding="utf-8").replace(
        "- [ ] #C01 train/val/test = 7/2/1（按板号分层）",
        "- [ ] #C01 train/val/test = 8/1/1（按板号哈希重划）"), encoding="utf-8")
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
        "Acceptance Criteria": "",  # section exists; items are added via CLI
    }) if "## Acceptance Criteria" in read(md_of(T_TRT)) else None
    expect_ok("TensorRT 量化失败（Failed = 终止，满足父门禁）", "→ Failed",
              "task", "fail", T_TRT, "-m", "Changed: INT8 量化精度掉 6 点，放弃")
    # close the tree: child first, then the parent (its items closed by now)
    expect_ok("评估脚本完成", "→ Done", "task", "done", T_EVAL,
              "-m", "Changed: mAP 0.87 / p99 38ms（延迟超标，见 INC-0001）")
    run("item", "check", f"{T_PIPE}#001")
    expect_ok("子任务都终止 → 训练管线 done（门禁放行）", "→ Done",
              "task", "done", T_PIPE)
    # the dataset task: its sub-task (label QC) first, then the dataset itself
    expect_ok("标注质检完成", "→ Done", "task", "done", T_QC)
    for ref in (f"{T_DS}#B01", f"{T_DS}#C01"):
        if "- [ ] " in read(md_of(T_DS)):
            run("item", "check", ref)
    expect_ok("数据集任务完成（items 全关闭）", "→ Done", "task", "done", T_DS)
    # the ONNX export task: open, then done
    run("task", "start", T_ONNX)
    expect_ok("ONNX 导出任务完成", "→ Done", "task", "done", T_ONNX,
              "-m", "Changed: 导出 12MB 模型，边缘端跑通（延迟未达标）")


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
        "Trigger for removal": "v1.1 按缺陷尺寸重算 anchor 时。",
    })
    expect_ok("debt open", "→ Open", "debt", "open", DEB)
    expect_ok("Repaid 需要门禁：先留一个未完成步骤", "#001",
              "item", "add", DEB, "参数化 anchor 配置（读数据集统计）")
    expect_fail("repay 被拒：Repayment Plan 未全部处理（gates.items）",
                "debt", "repay", DEB)
    DEB2 = _id_of(expect_ok("debt：固定数据增强随机种子", "Created",
                            "debt", "new", "数据增强随机种子固定为 42", "-y"))
    run("debt", "open", DEB2)
    run("debt", "progress", DEB2)
    expect_ok("Repayment Plan 步骤", "#001", "item", "add", DEB2, "恢复随机种子")
    expect_ok("skipped 步骤（带备注）", "⏭", "item", "set", f"{DEB2}#001", "skipped",
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
    expect_ok("criterion set met（带证据）", "✅",
              "item", "set", f"{E}#001", "met", "--note", "小目标 +5.6pt（41.2→46.8）")
    expect_ok("criterion set inconclusive（证据不足）", "❔",
              "item", "set", f"{E}#002", "inconclusive", "--note", "-0.3pt 在噪声区间内")
    expect_ok("第 3 条 criterion（仍 pending）", "#003",
              "item", "add", E, "训练时间不超过 baseline +10%")
    expect_fail("close 被拒：criterion 未全部 evaluated（gates.items）",
                "experiment", "close", E)
    run("item", "set", f"{E}#003", "met", "--note", "+4%")
    write_sections(md_of(E), {
        "Conclusion": "mosaic 保留在训练管线（小目标收益）；< 32px 召回的进一步改进"
                      "转交 T_ONNX 边缘端方案（更大输入分辨率）评估。",
    })
    expect_ok("criterion 全 evaluated → close", "→ Closed", "experiment", "close", E,
              "-m", "Changed: 结论定稿，mosaic 进入管线")
    # a second, doomed experiment: 4-bit quantization — abandoned (any → Abandoned)
    E2 = _id_of(expect_ok("第二实验：4-bit 量化", "Created",
                          "experiment", "new", "4-bit 量化压缩", "-y"))
    run("experiment", "propose", E2)
    run("experiment", "run", E2)
    expect_ok("abandon（放弃，Abandoned 终止）", "→ Abandoned",
              "experiment", "abandon", E2, "-m", "Deprecated: 4-bit 精度掉 9 点，不可用")


def main() -> int:
    t0 = time.time()
    setup_sandbox()
    where = f"{SANDBOX}（本地临时目录，结束拷回 .testproj/）" if _TMP_SANDBOX else f"{SANDBOX}（PROJ_E2E_SANDBOX，就地）"
    print(f"e2e: PCB 缺陷检测模型 v1  ({'子进程模式' if SUBPROCESS else '进程内模式'}，沙盒 {where})")
    try:
        reset_sandbox()
        for ch in (ch0, ch1, ch2, ch3, ch4, ch5, ch6, ch7, ch8):
            ch()
        return _summary()
    finally:
        teardown()


def _summary() -> int:
    dt = time.time() - REP.t0
    print(f"\ne2e: {REP.checks} 项检查，{REP.failed} 项失败（{dt:.1f}s）")
    return 1 if REP.failed else 0


if __name__ == "__main__":
    sys.exit(main())
