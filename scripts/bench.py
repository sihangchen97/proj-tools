#!/usr/bin/env python3
"""Performance baseline (PLAN §26.1) — `pixi run bench`.

Builds projects with 50 / 300 / 1000 task records (bulk DB insert, NOT the
CLI) in a local temp dir and, if BENCH_NFS_DIR is set, in an NFS directory;
times real `proj` CLI invocations and measures md reads / DB connections of a
write command. Outputs one table per location. Targets: a write command ≤ 0.5 s
(1000 records, local), ≤ 1 s (NFS).

Run a quick self-check of the counters with `--selftest`.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
N_RECORDS = [50, 300, 1000]
# median of RUNS real CLI invocations
RUNS = 3

# wrapper: count md reads + sqlite connections of one real proj CLI run
_WRAPPER = r'''
import os
import sqlite3
import sys
from pathlib import Path

md = 0
db = 0
_orig_read = Path.read_text
def _counted(self, *a, **k):
    global md
    if str(self).endswith(".md"):
        md += 1
    return _orig_read(self, *a, **k)
Path.read_text = _counted
_orig_connect = sqlite3.connect
def _db(*a, **k):
    global db
    db += 1
    return _orig_connect(*a, **k)
sqlite3.connect = _db
from proj.cli import main
code = 0
try:
    main()
except SystemExit as e:
    code = e.code or 0
print(f"md_reads={md} db_conns={db}")
sys.exit(code)
'''

# bulk record creation: one transaction, no CLI, no render
_BULK = r'''
import sys
from pathlib import Path
from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.engine import store as rec

root = Path(sys.argv[1])
n = int(sys.argv[2])
cfg = ProjConfig(root=root)
spec = rec.load_registry(cfg.root)["task"]
db = dbmod.Db(cfg.db_path)
have = db.conn.execute(
    "SELECT COUNT(*) FROM records WHERE id LIKE 'T-%'").fetchone()[0]
start = 1 if not have else max(
    int(r["id"][2:]) for r in db.conn.execute(
        "SELECT id FROM records WHERE id LIKE 'T-%'")) + 1
with db:
    for i in range(n):
        db.insert(spec, f"T-{start + i:06d}", f"Bench task {start + i}",
                  spec.status_allowed[0], dbmod.utc_now())
db.close()
'''


def _py(script: str, *args: str, env: dict[str, str] | None = None) -> str:
    env = dict(env if env is not None else os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env.setdefault("PROJ_AGENT", "bench")
    p = subprocess.run([sys.executable, "-c", script, *args],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise SystemExit(f"helper failed:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def build_project(root: Path, n: int) -> None:
    # proj init takes no --root: run it from the target directory itself
    p = subprocess.run(["proj", "init", "--profile", "default"],
                       capture_output=True, text=True, cwd=str(root))
    if p.returncode != 0:
        raise SystemExit(f"proj init failed:\n{p.stdout}\n{p.stderr}")
    _py(_BULK, str(root), str(n))


def time_cmd(root: Path, *args: str) -> float:
    env = dict(os.environ)
    env["PROJ_HOME"] = str(root)
    ts = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        p = subprocess.run(["proj", *args], capture_output=True,
                           text=True, env=env)
        ts.append(time.perf_counter() - t0)
        if p.returncode != 0:
            raise SystemExit(f"proj {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
    return statistics.median(ts)


def _cmd_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PROJ_HOME"] = str(root)
    return env


def _ls_json(root: Path) -> list[dict]:
    p = subprocess.run(["proj", "ls", "--json"], env=_cmd_env(root),
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout)


def write_command(root: Path) -> tuple[float, int, int]:
    """One write command: wall time + md reads + db connections."""
    env = _cmd_env(root)
    tid = _ls_json(root)[0]["id"]
    subprocess.run(["proj", "item", "add", tid, "bench item"],
                   env=env, check=True, capture_output=True)
    t0 = time.perf_counter()
    out = _py(_WRAPPER, "item", "check", f"{tid}#001", env=env)
    t = time.perf_counter() - t0
    line = next(l for l in out.splitlines() if l.startswith("md_reads="))
    md, dbn = (int(x) for x in line.split())
    return t, md, dbn


def bench_location(label: str, root: Path) -> None:
    print(f"--- {label} ({root})")
    print(f"{'records':>8}  {'command':<12} {'median ms':>10}   notes")
    for n in N_RECORDS:
        p = root / f"p{n}"
        if p.exists():
            shutil.rmtree(p)
        p.mkdir()
        build_project(p, n)
        tid = _ls_json(p)[0]["id"]
        for name, args in [("ls", ["ls"]), ("show", ["show", tid]),
                           ("task new", ["task", "new", f"Bench cmd {n}", "-y"]),
                           ("sync", ["sync"]), ("check", ["check"])]:
            t = time_cmd(p, *args) * 1000
            print(f"{n:>8}  {name:<12} {t:>10.0f}")
        t, md, dbn = write_command(p)
        print(f"{n:>8}  {'item check':<12} {t * 1000:>10.0f}"
              f"   write: md_reads={md} db_conns={dbn}")


def selftest() -> None:
    """The counters must see a write command's md reads and one+ db connections."""
    d = Path(tempfile.mkdtemp(prefix="bench-selftest-"))
    try:
        build_project(d, 1)
        _, md, dbn = write_command(d)
        assert md >= 1 and dbn >= 1, f"counters broken: md={md} db={dbn}"
        print(f"selftest OK (item check: md_reads={md}, db_conns={dbn})")
    finally:
        shutil.rmtree(d)


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    try:
        bench_location("local", tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    nfs = os.environ.get("BENCH_NFS_DIR")
    if nfs:
        bench_location("nfs", Path(nfs))
    else:
        print("--- nfs (skipped: set BENCH_NFS_DIR=/vol/… to include)")


if __name__ == "__main__":
    main()
