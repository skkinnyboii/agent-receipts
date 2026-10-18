import subprocess

from receipts.gitsnap import compare, repo_root, take_snapshot, tree_files

from conftest import git


def changed(repo, before, after):
    return {c.path: c.status for c in compare(repo, before, after).changes}


def test_untracked_modified_and_deleted_files_are_changes(repo):
    before = take_snapshot(repo)
    (repo / "new.py").write_text("y = 2\n")
    (repo / "src" / "core.py").write_text("x = 2\n")
    (repo / "app.py").unlink()
    after = take_snapshot(repo)
    assert changed(repo, before, after) == {"new.py": "A", "src/core.py": "M", "app.py": "D"}


def test_ignored_files_are_not_changes(repo):
    before = take_snapshot(repo)
    (repo / "debug.log").write_text("noise\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("bin\n")
    after = take_snapshot(repo)
    assert changed(repo, before, after) == {}


def test_files_dirty_before_the_run_are_not_changes(repo):
    (repo / "app.py").write_text("print('edited before the run')\n")
    (repo / "scratch.txt").write_text("untracked before the run\n")
    before = take_snapshot(repo)
    assert "app.py" in before.status and "scratch.txt" in before.status
    (repo / "src" / "core.py").write_text("x = 3\n")
    after = take_snapshot(repo)
    assert changed(repo, before, after) == {"src/core.py": "M"}


def test_snapshot_does_not_touch_the_real_index(repo):
    (repo / "new.py").write_text("y = 2\n")
    take_snapshot(repo)
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert "?? new.py" in git(repo, "status", "--porcelain")


def test_head_branch_and_commits_made_during_the_run(repo):
    before = take_snapshot(repo)
    assert before.branch == "main"
    assert before.head == git(repo, "rev-parse", "HEAD").strip()
    (repo / "feature.py").write_text("z = 1\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-q", "-m", "add feature")
    after = take_snapshot(repo)
    diff = compare(repo, before, after)
    assert diff.head_moved
    assert [c.subject for c in diff.commits] == ["add feature"]
    assert {c.path for c in diff.changes} == {"feature.py"}
    assert "feature.py" in diff.patch


def test_excluded_receipts_dir_is_not_a_change(repo):
    before = take_snapshot(repo, ["receipts"])
    (repo / "receipts").mkdir()
    (repo / "receipts" / "r.md").write_text("receipt\n")
    after = take_snapshot(repo, ["receipts"])
    assert changed(repo, before, after) == {}
    assert "receipts" not in after.status


def test_rename_is_detected(repo):
    before = take_snapshot(repo)
    (repo / "src" / "core.py").rename(repo / "src" / "engine.py")
    after = take_snapshot(repo)
    [change] = compare(repo, before, after).changes
    assert (change.status, change.old_path, change.path) == ("R", "src/core.py", "src/engine.py")


def test_repo_without_commits(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    before = take_snapshot(root)
    assert before.head is None
    (root / "a.txt").write_text("a\n")
    after = take_snapshot(root)
    assert changed(root, before, after) == {"a.txt": "A"}
    assert tree_files(root, after.tree) == {"a.txt"}


def test_repo_root_outside_git(tmp_path):
    assert repo_root(tmp_path) is None
