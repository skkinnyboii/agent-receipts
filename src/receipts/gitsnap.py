"""Git snapshots taken before and after a run, and the diff between them.

A snapshot records HEAD, the branch, `git status --porcelain`, and a tree
object holding the full worktree (tracked and untracked files, gitignored
files left out). The tree is written through a throwaway index, so the
user's real index and worktree are never touched. Diffing the two trees
gives exactly the content that changed during the run: files that were
already dirty before the command and were left alone do not show up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(cwd: Path) -> Path | None:
    """Return the worktree root containing `cwd`, or None outside a repo."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def path_prefix(cwd: Path) -> str:
    """Path of `cwd` relative to the repo root, with a trailing slash ('' at root)."""
    return _git(["rev-parse", "--show-prefix"], cwd).strip()


@dataclass
class Snapshot:
    head: str | None
    branch: str | None
    status: str
    tree: str


@dataclass
class FileChange:
    status: str  # A, M, D, R, T, C
    path: str
    old_path: str | None = None


@dataclass
class Commit:
    sha: str
    author: str
    subject: str


@dataclass
class GitDiff:
    changes: list[FileChange] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    head_moved: bool = False
    stat: str = ""
    patch: str = ""

    def touched_paths(self) -> set[str]:
        paths: set[str] = set()
        for change in self.changes:
            paths.add(change.path)
            if change.old_path:
                paths.add(change.old_path)
        return paths


def _pathspecs(exclude: list[str]) -> list[str]:
    return [":/"] + [f":(top,exclude){p}" for p in exclude]


def take_snapshot(root: Path, exclude: list[str] | None = None) -> Snapshot:
    """Snapshot the repository at `root`, leaving out the `exclude` paths (root-relative)."""
    exclude = exclude or []
    head = _git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], root, check=False).strip() or None
    branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], root, check=False).strip() or None
    status = _git(
        ["status", "--porcelain", "--untracked-files=all", "--", *_pathspecs(exclude)], root
    )

    index = Path(_git(["rev-parse", "--git-path", "index"], root).strip())
    if not index.is_absolute():
        index = root / index
    with tempfile.TemporaryDirectory(prefix="receipts-") as tmp:
        tmp_index = Path(tmp) / "index"
        if index.exists():
            # Starting from the real index reuses its stat cache, so unchanged
            # files are not rehashed. The real index is never written.
            shutil.copyfile(index, tmp_index)
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
        _git(["add", "-A", "--", *_pathspecs(exclude)], root, env=env)
        if exclude:
            _git(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *exclude], root, env=env)
        tree = _git(["write-tree"], root, env=env).strip()
    return Snapshot(head=head, branch=branch, status=status, tree=tree)


def _parse_name_status(raw: str) -> list[FileChange]:
    parts = raw.split("\0")
    changes: list[FileChange] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        code = status[0]
        if code in ("R", "C"):
            changes.append(FileChange(status=code, old_path=parts[i + 1], path=parts[i + 2]))
            i += 3
        else:
            changes.append(FileChange(status=code, path=parts[i + 1]))
            i += 2
    return changes


def tree_files(root: Path, tree: str) -> set[str]:
    out = _git(["ls-tree", "-r", "-z", "--name-only", tree], root)
    return {p for p in out.split("\0") if p}


def compare(root: Path, before: Snapshot, after: Snapshot) -> GitDiff:
    diff = GitDiff()
    raw = _git(["diff-tree", "-r", "-z", "-M", "--name-status", before.tree, after.tree], root)
    diff.changes = _parse_name_status(raw)
    diff.stat = _git(
        ["diff", "--no-ext-diff", "--no-color", "-M", "--stat=200", before.tree, after.tree], root
    )
    diff.patch = _git(
        ["diff", "--no-ext-diff", "--no-color", "-M", before.tree, after.tree], root
    )

    if after.head and after.head != before.head:
        diff.head_moved = True
        rev_range = f"{before.head}..{after.head}" if before.head else after.head
        log = _git(["log", "--format=%H%x1f%an%x1f%s", rev_range], root, check=False)
        for line in log.splitlines():
            sha, author, subject = (line.split("\x1f") + ["", ""])[:3]
            diff.commits.append(Commit(sha=sha, author=author, subject=subject))
    elif before.head != after.head:
        diff.head_moved = True
    return diff
