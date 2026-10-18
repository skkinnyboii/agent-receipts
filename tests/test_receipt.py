from datetime import datetime, timedelta, timezone
from pathlib import Path

from receipts.gitsnap import FileChange, GitDiff, Snapshot
from receipts.parsing import Mismatches, Redactor, Summary, UsageFigure
from receipts.report import RunRecord, receipt_filename, render_markdown, write_receipt

START = datetime(2026, 10, 17, 22, 30, 45, tzinfo=timezone.utc)


def record(**overrides) -> RunRecord:
    base = dict(
        command=["agent", "--fix"],
        cwd=Path("/work"),
        started=START,
        finished=START + timedelta(seconds=3),
        exit_code=0,
        stdout="done\n",
        stderr="",
        before=Snapshot("a" * 40, "main", "", "t1"),
        after=Snapshot("a" * 40, "main", "?? new.py\n", "t2"),
        diff=GitDiff(changes=[FileChange("A", "new.py")], stat=" new.py | 1 +\n", patch="+x\n"),
        summary=Summary("added new.py", "stdout, after the last Summary heading"),
        mismatches=Mismatches([], []),
    )
    base.update(overrides)
    return RunRecord(**base)


def test_filename_is_utc_timestamp_without_colons():
    assert receipt_filename(START) == "2026-10-17T22-30-45Z.md"


def test_write_receipt_creates_dir_and_avoids_overwriting(tmp_path):
    out = tmp_path / "receipts"
    first = write_receipt(record(), out, Redactor({}))
    second = write_receipt(record(), out, Redactor({}))
    assert first.name == "2026-10-17T22-30-45Z.md"
    assert second != first and second.exists()
    assert ":" not in first.name and ":" not in second.name


def test_receipt_sections(tmp_path):
    text = write_receipt(record(), tmp_path, Redactor({})).read_text()
    for heading in (
        "## Run",
        "## Git",
        "## Changed files",
        "## Commands",
        "## Tokens and cost",
        "## Final summary",
        "## Mismatch flags",
        "## Output",
    ):
        assert heading in text
    assert "| Exit code | 0 |" in text
    assert "`new.py`" in text
    assert "- invoked: `agent --fix`" in text


def test_missing_tokens_and_cost_say_not_available():
    text = render_markdown(record(usage=[]), Redactor({}))
    assert "- Total tokens: not available" in text
    assert "- Cost: not available" in text
    assert "- Other token figures: not available" in text


def test_partial_usage_does_not_invent_a_total():
    usage = [UsageFigure("prompt_tokens", "100", "stdout"), UsageFigure("completion_tokens", "50", "stdout")]
    text = render_markdown(record(usage=usage), Redactor({}))
    assert "- Total tokens: not available" in text
    assert "- prompt_tokens: 100 (stdout)" in text
    assert "150" not in text


def test_reported_total_and_cost_appear():
    usage = [UsageFigure("total_tokens", "2,000", "stdout"), UsageFigure("cost", "$0.40", "env:COST_USD")]
    text = render_markdown(record(usage=usage), Redactor({}))
    assert "- Total tokens: 2,000 (stdout)" in text
    assert "- Cost: $0.40 (env:COST_USD)" in text


def test_mismatch_flags_rendered():
    m = Mismatches(["README.md"], ["new.py"])
    text = render_markdown(record(mismatches=m), Redactor({}))
    claimed = text.split("### Claimed, not changed")[1].split("###")[0]
    unmentioned = text.split("### Changed, not mentioned")[1].split("##")[0]
    assert "`README.md`" in claimed
    assert "`new.py`" in unmentioned


def test_secrets_are_not_written():
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    rec = record(stdout=f"using key {secret}\n", command=["agent", f"--key={secret}"])
    text = render_markdown(rec, Redactor({}))
    assert secret not in text


def test_output_with_backticks_keeps_fences_intact():
    rec = record(stdout="```python\nprint(1)\n```\n")
    text = render_markdown(rec, Redactor({}))
    assert "````text" in text


def test_no_git():
    rec = record(before=None, after=None, diff=None, mismatches=None, git_note="Not a git repository, so no snapshot was taken.")
    text = render_markdown(rec, Redactor({}))
    assert "Not a git repository" in text
    assert "not available (no git snapshot)" in text
