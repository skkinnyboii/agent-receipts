import os
import re
import subprocess
import sys
import textwrap

import pytest

import receipts
from receipts.cli import main, run

from conftest import git

AGENT = textwrap.dedent(
    """
    import json, pathlib, subprocess, sys
    pathlib.Path("src/core.py").write_text("x = 42\\n")
    pathlib.Path("notes.txt").write_text("new file\\n")
    pathlib.Path("debug.log").write_text("ignored\\n")
    print("$ git status")
    print(json.dumps({"command": "pytest -q"}))
    print("prompt_tokens: 120")
    print("Running: npm test", file=sys.stderr)
    print("## Summary")
    print("Updated src/core.py and README.md.")
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    """
)


@pytest.fixture
def agent(repo, tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    return script


def only_receipt(repo):
    receipts = list((repo / "receipts").glob("*.md"))
    assert len(receipts) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.md", receipts[0].name)
    return receipts[0].read_text()


def test_cli_run_writes_receipt(repo, agent, monkeypatch, capsys):
    monkeypatch.delenv("COST_USD", raising=False)
    (repo / "app.py").write_text("print('dirty before')\n")
    code = main(["run", "--cwd", str(repo), "--", sys.executable, str(agent)])
    assert code == 0
    text = only_receipt(repo)

    changed = text.split("## Changed files")[1].split("##")[0]
    assert "`src/core.py`" in changed
    assert "`notes.txt`" in changed
    assert "app.py" not in changed  # dirty before the run
    assert "debug.log" not in changed  # gitignored
    assert "receipts/" not in changed

    commands = text.split("## Commands")[1].split("## ")[0]
    for cmd in ("git status", "pytest -q", "npm test"):
        assert f"`{cmd}`" in commands

    assert "- prompt_tokens: 120 (stdout)" in text
    assert "- Total tokens: not available" in text
    assert "- Cost: not available" in text

    claimed = text.split("### Claimed, not changed")[1].split("###")[0]
    unmentioned = text.split("### Changed, not mentioned")[1].split("## ")[0]
    assert "`README.md`" in claimed
    assert "`notes.txt`" in unmentioned
    assert "src/core.py" not in unmentioned

    err = capsys.readouterr().err
    assert "Mismatch flags" in err and "claimed, not changed" in err


def test_failing_command_keeps_exit_code_and_writes_receipt(repo, agent):
    code = main(["run", "--cwd", str(repo), "--", sys.executable, str(agent), "3"])
    assert code == 3
    assert "| Exit code | 3 |" in only_receipt(repo)


def test_missing_command_exit_127_still_writes_receipt(repo):
    code, path = run(["definitely-not-a-real-command-xyz"], repo)
    assert code == 127
    assert path is not None and "| Exit code | 127 |" in path.read_text()


def test_commits_during_run_are_listed(repo, tmp_path):
    script = tmp_path / "commit.py"
    script.write_text(
        "import pathlib, subprocess\n"
        "pathlib.Path('feature.py').write_text('f = 1\\n')\n"
        "subprocess.run(['git', 'add', 'feature.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-q', '-m', 'add feature'], check=True)\n"
        "print('Summary: added feature.py')\n"
    )
    code, path = run([sys.executable, str(script)], repo)
    text = path.read_text()
    assert code == 0
    assert "add feature" in text.split("### Commits made during the run")[1]
    assert "`feature.py`" in text.split("## Changed files")[1]
    assert "None." in text.split("### Changed, not mentioned")[1]


def test_custom_receipts_dir_and_env_usage(repo, agent, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_TOTAL_TOKENS", "999")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_shouldneverappearinreceipts0000")
    out = tmp_path / "elsewhere"
    code = main(["run", "--cwd", str(repo), "--receipts-dir", str(out), "--", sys.executable, str(agent)])
    assert code == 0
    [receipt] = out.glob("*.md")
    text = receipt.read_text()
    assert "- Total tokens: 999 (env:OPENAI_TOTAL_TOKENS)" in text
    assert "ghp_shouldneverappear" not in text
    assert "GITHUB_TOKEN" not in text


def test_outside_git_repo(tmp_path):
    work = tmp_path / "plain"
    work.mkdir()
    code, path = run([sys.executable, "-c", "print('hi')"], work)
    assert code == 0
    text = path.read_text()
    assert "Not a git repository" in text


def test_console_script_entry_point(repo, agent):
    proc = subprocess.run(
        [sys.executable, "-m", "receipts", "run", "--cwd", str(repo), "--", sys.executable, str(agent), "2"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(receipts.__file__))},
    )
    assert proc.returncode == 2
    assert "$ git status" in proc.stdout  # command output is streamed through
    assert "Changed files" in proc.stderr
    only_receipt(repo)


def test_no_command_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code == 2
