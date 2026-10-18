"""Turn a finished run into a Markdown receipt and a rich table."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from receipts.gitsnap import GitDiff, Snapshot
from receipts.parsing import FoundCommand, Mismatches, Summary, UsageFigure, latest_by_label

NOT_AVAILABLE = "not available"
MAX_OUTPUT_LINES = 2000
MAX_PATCH_LINES = 3000
TOKEN_LABELS = ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "tokens", "token_usage")


@dataclass
class RunRecord:
    command: list[str]
    cwd: Path
    started: datetime
    finished: datetime
    exit_code: int
    stdout: str
    stderr: str
    repo_root: Path | None = None
    git_note: str | None = None
    before: Snapshot | None = None
    after: Snapshot | None = None
    diff: GitDiff | None = None
    commands: list[FoundCommand] = field(default_factory=list)
    usage: list[UsageFigure] = field(default_factory=list)
    summary: Summary | None = None
    mismatches: Mismatches | None = None

    @property
    def duration(self) -> float:
        return (self.finished - self.started).total_seconds()

    def total_tokens(self) -> str:
        fig = latest_by_label(self.usage).get("total_tokens")
        return f"{fig.value} ({fig.source})" if fig else NOT_AVAILABLE

    def cost(self) -> str:
        fig = latest_by_label(self.usage).get("cost")
        return f"{fig.value} ({fig.source})" if fig else NOT_AVAILABLE

    def other_tokens(self) -> list[UsageFigure]:
        latest = latest_by_label(self.usage)
        return [latest[label] for label in TOKEN_LABELS if label in latest]


def receipt_filename(started: datetime) -> str:
    """UTC timestamp with colons replaced, e.g. 2026-10-17T22-30-45Z.md."""
    return started.strftime("%Y-%m-%dT%H-%M-%SZ") + ".md"


def unique_receipt_path(directory: Path, started: datetime) -> Path:
    path = directory / receipt_filename(started)
    n = 1
    while path.exists():
        path = directory / f"{path.stem.split('.')[0]}.{n}.md"
        n += 1
    return path


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip(chr(10))}\n{ticks}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _tail(text: str, limit: int) -> tuple[str, int]:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, 0
    return "\n".join(lines[-limit:]), len(lines) - limit


def _head(text: str, limit: int) -> tuple[str, int]:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, 0
    return "\n".join(lines[:limit]), len(lines) - limit


def render_markdown(run: RunRecord, redact) -> str:
    out: list[str] = []
    add = out.append
    add(f"# Receipt {run.started.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    add("")
    add("## Run")
    add("")
    add("| Field | Value |")
    add("| --- | --- |")
    add(f"| Command | {_cell(_code(redact(shlex.join(run.command))))} |")
    add(f"| Working directory | {_cell(_code(str(run.cwd)))} |")
    add(f"| Started (UTC) | {run.started.strftime('%Y-%m-%dT%H:%M:%SZ')} |")
    add(f"| Finished (UTC) | {run.finished.strftime('%Y-%m-%dT%H:%M:%SZ')} |")
    add(f"| Duration | {run.duration:.1f}s |")
    add(f"| Exit code | {run.exit_code} |")
    add("")

    add("## Git")
    add("")
    if run.before is None or run.after is None or run.diff is None:
        add(run.git_note or "Not a git repository, so no snapshot was taken.")
        add("")
    else:
        b, a = run.before, run.after
        add("| | Before | After |")
        add("| --- | --- | --- |")
        add(f"| HEAD | {b.head or '(no commits)'} | {a.head or '(no commits)'} |")
        add(f"| Branch | {b.branch or '(detached)'} | {a.branch or '(detached)'} |")
        add(f"| Dirty paths | {len(b.status.splitlines())} | {len(a.status.splitlines())} |")
        add("")
        add("### Status before")
        add("")
        add(_fence(redact(b.status) or "(clean)"))
        add("")
        add("### Status after")
        add("")
        add(_fence(redact(a.status) or "(clean)"))
        add("")
        add("### Commits made during the run")
        add("")
        if run.diff.commits:
            for c in run.diff.commits:
                add(f"- `{c.sha[:12]}` {c.subject} ({c.author})")
        elif run.diff.head_moved:
            add(f"HEAD moved from `{b.head}` to `{a.head}` with no new commits on top.")
        else:
            add("None. HEAD did not move.")
        add("")

    add("## Changed files")
    add("")
    if run.diff is None:
        add(NOT_AVAILABLE)
    elif not run.diff.changes:
        add("None. The worktree content is the same before and after the run.")
    else:
        add("| Status | Path |")
        add("| --- | --- |")
        for ch in run.diff.changes:
            shown = f"{ch.old_path} -> {ch.path}" if ch.old_path else ch.path
            add(f"| {ch.status} | {_cell(_code(shown))} |")
    add("")
    if run.diff is not None and run.diff.changes:
        add("### Diff")
        add("")
        add(_fence(redact(run.diff.stat)))
        add("")
        patch, dropped = _head(redact(run.diff.patch), MAX_PATCH_LINES)
        add("<details><summary>Full patch</summary>")
        add("")
        add(_fence(patch, "diff"))
        if dropped:
            add("")
            add(f"({dropped} more lines not shown)")
        add("")
        add("</details>")
        add("")

    add("## Commands")
    add("")
    add(f"- invoked: {_code(redact(shlex.join(run.command)))}")
    for cmd in run.commands:
        add(f"- {cmd.source}: {_code(redact(cmd.command))}")
    add("")

    add("## Tokens and cost")
    add("")
    add(f"- Total tokens: {run.total_tokens()}")
    for fig in run.other_tokens():
        add(f"- {fig.label}: {fig.value} ({fig.source})")
    if not run.other_tokens():
        add(f"- Other token figures: {NOT_AVAILABLE}")
    add(f"- Cost: {run.cost()}")
    add("")
    if run.usage:
        add("Every figure as reported (none are added together):")
        add("")
        add("| Label | Value | Source |")
        add("| --- | --- | --- |")
        for fig in run.usage:
            add(f"| {fig.label} | {_cell(fig.value)} | {fig.source} |")
        add("")

    add("## Final summary")
    add("")
    if run.summary is not None:
        add(f"Source: {run.summary.source}.")
        add("")
        add(_fence(redact(run.summary.text) or "(empty)"))
    add("")

    add("## Mismatch flags")
    add("")
    if run.mismatches is None:
        add(NOT_AVAILABLE + " (no git snapshot)")
    else:
        m = run.mismatches
        add("### Claimed, not changed")
        add("")
        add("\n".join(f"- {_code(p)}" for p in m.claimed_not_changed) or "None.")
        add("")
        add("### Changed, not mentioned")
        add("")
        add("\n".join(f"- {_code(p)}" for p in m.changed_not_mentioned) or "None.")
    add("")

    add("## Output")
    add("")
    for name, text in (("stdout", run.stdout), ("stderr", run.stderr)):
        body, dropped = _tail(redact(text), MAX_OUTPUT_LINES)
        add(f"<details><summary>{name}</summary>")
        add("")
        if dropped:
            add(f"(first {dropped} lines not shown)")
            add("")
        add(_fence(body or "(empty)", "text"))
        add("")
        add("</details>")
        add("")
    return "\n".join(out).rstrip("\n") + "\n"


def write_receipt(run: RunRecord, directory: Path, redact) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = unique_receipt_path(directory, run.started)
    path.write_text(render_markdown(run, redact), encoding="utf-8")
    return path


def print_tables(run: RunRecord, receipt_path: Path | None, console: Console) -> None:
    summary = Table(title="Run", show_header=False, title_justify="left")
    summary.add_column("Field", style="bold")
    summary.add_column("Value", overflow="fold")
    summary.add_row("Command", escape(shlex.join(run.command)))
    summary.add_row("Directory", escape(str(run.cwd)))
    exit_style = "green" if run.exit_code == 0 else "red"
    summary.add_row("Exit code", f"[{exit_style}]{run.exit_code}[/]")
    summary.add_row("Duration", f"{run.duration:.1f}s")
    if run.diff is not None and run.diff.head_moved:
        summary.add_row("Commits", str(len(run.diff.commits)))
    summary.add_row("Commands seen", str(1 + len(run.commands)))
    summary.add_row("Receipt", escape(str(receipt_path)) if receipt_path else "[red]not written[/]")
    console.print(summary)

    files = Table(title="Changed files", title_justify="left")
    files.add_column("Status")
    files.add_column("Path", overflow="fold")
    if run.diff is None:
        files.add_row("", NOT_AVAILABLE)
    elif not run.diff.changes:
        files.add_row("", "none")
    for ch in run.diff.changes if run.diff else []:
        files.add_row(ch.status, escape(f"{ch.old_path} -> {ch.path}" if ch.old_path else ch.path))
    console.print(files)

    usage = Table(title="Tokens and cost", title_justify="left")
    usage.add_column("Figure")
    usage.add_column("Value", overflow="fold")
    usage.add_row("Total tokens", escape(run.total_tokens()))
    for fig in run.other_tokens():
        usage.add_row(fig.label, escape(f"{fig.value} ({fig.source})"))
    usage.add_row("Cost", escape(run.cost()))
    console.print(usage)

    flags = Table(title="Mismatch flags", title_justify="left")
    flags.add_column("Flag")
    flags.add_column("Path", overflow="fold")
    if run.mismatches is None:
        flags.add_row("", NOT_AVAILABLE)
    else:
        for p in run.mismatches.claimed_not_changed:
            flags.add_row("[yellow]claimed, not changed[/]", escape(p))
        for p in run.mismatches.changed_not_mentioned:
            flags.add_row("[yellow]changed, not mentioned[/]", escape(p))
        if not run.mismatches.claimed_not_changed and not run.mismatches.changed_not_mentioned:
            flags.add_row("[green]none[/]", "")
    console.print(flags)
