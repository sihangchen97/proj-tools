"""A1 (§13.2): on a network filesystem the DB uses the rollback journal
(journal_mode=DELETE), locally WAL; `proj info` reports the mode."""
import shutil

import pytest

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.engine import store as rec


@pytest.fixture
def fake_mounts(monkeypatch, tmp_path):
    # fake nfs/cifs/sshfs mounts rooted under tmp_path (a prefix of every target)
    lines = [
        f"dev {tmp_path} ext4 rw 0 0",
        f"nfs.example:/vol {tmp_path / 'vol/juneleung-solar'} nfs rw,vers=4 0 0",
        f"cifs.example:/share {tmp_path / 'mnt/share'} cifs rw 0 0",
        f"sshfs:home {tmp_path / 'mnt/ssh'} fuse.sshfs rw 0 0",
        f"tmpfs {tmp_path / 'tmp'} tmpfs rw 0 0",
    ]
    monkeypatch.setattr(dbmod, "_mounts_lines", lambda: lines)
    return tmp_path


def test_network_fs_detection(fake_mounts):
    p = fake_mounts
    assert dbmod.on_network_fs(p / "vol/juneleung-solar/proj.sqlite")
    assert dbmod.on_network_fs(p / "mnt/share/deep/a.sqlite")
    assert dbmod.on_network_fs(p / "mnt/ssh/x.sqlite")
    assert not dbmod.on_network_fs(p / "local/project.sqlite")   # ext4 root
    assert not dbmod.on_network_fs(p / "tmp/x.sqlite")           # tmpfs is fine
    # a sibling directory must not be swallowed by the mount-point prefix
    assert not dbmod.on_network_fs(p / "vol/other.sqlite")


def test_db_journal_mode_follows_filesystem(fake_mounts):
    p = fake_mounts
    (p / "vol/juneleung-solar").mkdir(parents=True)
    (p / "local").mkdir(parents=True)
    nfs_db = dbmod.Db(p / "vol/juneleung-solar/proj.sqlite")
    assert nfs_db.network_fs
    assert nfs_db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    local_db = dbmod.Db(p / "local/proj.sqlite")
    assert not local_db.network_fs
    assert local_db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # the DB is fully usable in either mode
    for d in (nfs_db, local_db):
        d.conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
        assert d.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        d.close()


def test_no_proc_mounts_means_local(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "_mounts_lines", lambda: [])
    assert not dbmod.on_network_fs(tmp_path / "x.sqlite")


def test_info_reports_journal_mode(proj, monkeypatch):
    # fake the repo's real volume as an nfs mount: the sandbox root lands on it
    real = proj.root
    nfs_root = real.parent / "vol/juneleung-solar"
    sandbox = nfs_root / "testproj"
    nfs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        dbmod, "_mounts_lines",
        lambda: [f"nfs.example:/vol {nfs_root} nfs rw 0 0",
                 f"tmpfs {nfs_root / 'vol2'} tmpfs rw 0 0"])
    shutil.move(str(real), str(sandbox))
    try:
        d = rec.info(ProjConfig(sandbox))
        assert d["journal_mode"].startswith("DELETE")
        assert "network filesystem" in rec.info_text(d)
    finally:
        shutil.move(str(sandbox), str(real))
