"""`receipts run [--cwd DIR] [--receipts-dir DIR] -- COMMAND [ARGS...]`"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from rich.console import Console

from receipts import __version__
from receipts.gitsnap import GitError, compare, path_prefix, repo_root, take_snapshot, tree_files
from receipts.parsing import (
    Redactor,
    dedupe_commands,
    extract_summary,
    find_commands,
    find_mismatches,
    find_usage,
    usage_from_env,
)
from receipts.report import RunRecord, print_tables, write_receipt


def _sink_writer(stream: IO[str]):
    buffer = getattr(stream, "buffer", None)

    def write(chunk: bytes) -> None:
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:
            stream.write(chunk.decode("utf-8", errors="replace"))
            stream.flush()

    return write


def execute(command: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run `command`, streaming its output through while capturing it."""
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        msg = f"receipts: command not found: {command[0]}\n"
        sys.stderr.write(msg)
        return 127, "", msg
    except PermissionError:
        msg = f"receipts: permission denied: {command[0]}\n"
        sys.stderr.write(msg)
        return 126, "", msg

    captured: dict[str, list[bytes]] = {"stdout": [], "stderr": []}

    def pump(src: IO[bytes], name: str, write) -> None:
        for chunk in iter(lambda: src.read1(65536), b""):
            captured[name].append(chunk)
            try:
                write(chunk)
            except (OSError, ValueError):
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "stdout", _sink_writer(sys.stdout)), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "stderr", _sink_writer(sys.stderr)), daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            # The child got the same SIGINT; wait for it so a receipt is still written.
            continue
    for t in threads:
        t.join()
    if code < 0:
        code = 128 + abs(code)
    out = b"".join(captured["stdout"]).decode("utf-8", errors="replace")
    err = b"".join(captured["stderr"]).decode("utf-8", errors="replace")
    return code, out, err


def run(
    command: list[str],
    cwd: Path | str = ".",
    receipts_dir: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    console: Console | None = None,
) -> tuple[int, Path | None]:
    """Run `command` and write its receipt. Returns (exit code, receipt path)."""
    environ = os.environ if environ is None else environ
    console = console or Console(stderr=True)
    cwd = Path(cwd).resolve()
    out_dir = Path(receipts_dir).resolve() if receipts_dir else cwd / "receipts"

    root = repo_root(cwd)
    git_note = None if root else "Not a git repository, so no snapshot was taken."
    exclude: list[str] = []
    if root is not None:
        try:
            exclude = [out_dir.relative_to(root).as_posix()]
        except ValueError:
            pass

    before = None
    if root is not None:
        try:
            before = take_snapshot(root, exclude)
        except GitError as exc:
            git_note = f"Git snapshot before the run failed: {exc}"

    started = datetime.now(timezone.utc)
    exit_code, stdout, stderr = execute(command, cwd)
    finished = datetime.now(timezone.utc)

    record = RunRecord(
        command=command,
        cwd=cwd,
        started=started,
        finished=finished,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        repo_root=root,
        git_note=git_note,
    )

    if root is not None and before is not None:
        try:
            after = take_snapshot(root, exclude)
            record.before, record.after = before, after
            record.diff = compare(root, before, after)
        except GitError as exc:
            record.git_note = f"Git snapshot after the run failed: {exc}"

    record.commands = dedupe_commands(
        [*find_commands(stdout, "stdout"), *find_commands(stderr, "stderr")]
    )
    record.usage = [*find_usage(stdout, "stdout"), *find_usage(stderr, "stderr"), *usage_from_env(environ)]
    record.summary = extract_summary(stdout, stderr)
    if record.diff is not None and root is not None:
        known = tree_files(root, record.before.tree) | tree_files(root, record.after.tree)
        try:
            prefix = path_prefix(cwd)
        except GitError:
            prefix = ""
        record.mismatches = find_mismatches(record.summary.text, record.diff.changes, known, prefix)

    receipt_path: Path | None = None
    try:
        receipt_path = write_receipt(record, out_dir, Redactor(environ))
    except OSError as exc:
        console.print(f"[red]receipts: could not write receipt: {exc}[/]")
    print_tables(record, receipt_path, console)
    return exit_code, receipt_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="receipts",
        description="Run a command and write a receipt of what it actually changed.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    run_p = sub.add_parser(
        "run",
        help="run a command and write a receipt",
        usage="receipts run [--cwd DIR] [--receipts-dir DIR] -- COMMAND [ARGS...]",
    )
    run_p.add_argument("--cwd", default=".", help="directory to run the command in (default: .)")
    run_p.add_argument(
        "--receipts-dir",
        default=None,
        help="where to write receipts (default: <cwd>/receipts)",
    )
    run_p.add_argument("command", nargs="*", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    parser = _parser()
    args = parser.parse_args(argv)
    command = [*args.command, *command]
    if not command:
        parser.error("no command given; use: receipts run -- COMMAND [ARGS...]")
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        parser.error(f"--cwd is not a directory: {args.cwd}")
    exit_code, _ = run(command, cwd, args.receipts_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
