from receipts.gitsnap import FileChange
from receipts.parsing import (
    Redactor,
    extract_summary,
    find_commands,
    find_mismatches,
    find_usage,
    latest_by_label,
    usage_from_env,
)


# ------------------------------------------------------------------ commands

def test_commands_from_prompt_lines_and_labels():
    out = "\n".join(
        [
            "$ git status",
            "> npm test",
            "Ran: pytest -q",
            "Running: `ruff check .`",
            "Command: make build",
            "nothing to see here",
        ]
    )
    assert [c.command for c in find_commands(out, "stdout")] == [
        "git status",
        "npm test",
        "pytest -q",
        "ruff check .",
        "make build",
    ]


def test_commands_from_json_fields():
    out = "\n".join(
        [
            '{"type": "exec", "command": "ls -la"}',
            '{"tool": {"command": ["git", "commit", "-m", "two words"]}}',
            '  "command": "echo \\"quoted\\"",',
        ]
    )
    assert [c.command for c in find_commands(out, "stderr")] == [
        "ls -la",
        "git commit -m 'two words'",
        'echo "quoted"',
    ]


# ---------------------------------------------------------------- tokens/cost

def test_usage_label_formats():
    out = "\n".join(
        [
            "prompt_tokens: 1,200",
            "completion_tokens=300",
            "Token usage: 1500",
            "cost: $0.0123",
            '{"usage": {"input_tokens": 10, "output_tokens": 5}, "cost": "$0.02"}',
        ]
    )
    figs = {(f.label, f.value) for f in find_usage(out, "stdout")}
    assert figs == {
        ("prompt_tokens", "1,200"),
        ("completion_tokens", "300"),
        ("token_usage", "1500"),
        ("cost", "$0.0123"),
        ("input_tokens", "10"),
        ("output_tokens", "5"),
        ("cost", "$0.02"),
    }


def test_total_is_never_computed_from_parts():
    figs = find_usage("prompt_tokens: 100\ncompletion_tokens: 50\n", "stdout")
    assert "total_tokens" not in latest_by_label(figs)


def test_total_recorded_when_present_and_latest_wins_without_summing():
    figs = find_usage("total_tokens: 100\ntotal_tokens: 250\n", "stdout")
    assert latest_by_label(figs)["total_tokens"].value == "250"


def test_lookalike_labels_are_ignored():
    out = "max_tokens: 4096\nMax tokens: 4096\ncost_center: 12\nmaxTokens=9\n"
    assert find_usage(out, "stdout") == []


def test_no_usage_in_plain_output():
    assert find_usage("all done, 3 files edited\n", "stdout") == []


def test_env_usage_reads_counts_and_costs_only():
    env = {
        "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "OPENAI_TOTAL_TOKENS": "4321",
        "PROMPT_TOKENS": "1000",
        "COST_USD": "0.75",
        "RECEIPTS_COST": "$1.10",
        "ANTHROPIC_API_KEY": "123456789",
        "TOTAL_TOKENS_BUDGET": "99999",
        "MY_COST_USD": "not-a-number",
    }
    figs = {(f.label, f.value, f.source) for f in usage_from_env(env)}
    assert figs == {
        ("total_tokens", "4321", "env:OPENAI_TOTAL_TOKENS"),
        ("prompt_tokens", "1000", "env:PROMPT_TOKENS"),
        ("cost", "0.75", "env:COST_USD"),
        ("cost", "$1.10", "env:RECEIPTS_COST"),
    }


# ------------------------------------------------------------------- summary

def test_summary_after_last_heading():
    out = "Summary\nold\n\n## Final summary\nEdited `app.py`.\nAll tests pass.\n"
    s = extract_summary(out, "")
    assert s.text == "Edited `app.py`.\nAll tests pass."
    assert s.source.startswith("stdout")


def test_inline_summary_heading():
    s = extract_summary("work\n**Summary:** changed src/core.py\n", "")
    assert s.text == "changed src/core.py"


def test_summary_falls_back_to_stderr_heading():
    s = extract_summary("no heading here\n", "Summary:\nupdated app.py\n")
    assert s.text == "updated app.py"
    assert s.source.startswith("stderr")


def test_summary_without_heading_uses_last_30_lines():
    out = "\n".join(f"line {i}" for i in range(50))
    s = extract_summary(out, "")
    assert s.text.splitlines() == [f"line {i}" for i in range(20, 50)]


def test_word_summary_in_prose_is_not_a_heading():
    s = extract_summary("the summary of this run is below\nok\n", "")
    assert "no Summary heading" in s.source


# ------------------------------------------------------------------ mismatch

KNOWN = {"app.py", "src/core.py", "README.md", "docs/guide.md"}


def test_claimed_not_changed():
    summary = "I updated src/core.py and README.md, and added tests/test_core.py."
    changes = [FileChange("M", "src/core.py")]
    m = find_mismatches(summary, changes, KNOWN)
    assert m.claimed_not_changed == ["README.md", "tests/test_core.py"]
    assert m.changed_not_mentioned == []


def test_changed_not_mentioned_by_path_or_filename():
    summary = "Touched core.py only."
    changes = [FileChange("M", "src/core.py"), FileChange("A", "src/extra.py")]
    m = find_mismatches(summary, changes, KNOWN)
    assert m.changed_not_mentioned == ["src/extra.py"]
    assert m.claimed_not_changed == []


def test_filename_match_needs_word_boundary():
    m = find_mismatches("Edited data.py", [FileChange("M", "a.py")], KNOWN | {"a.py"})
    assert m.changed_not_mentioned == ["a.py"]


def test_prose_is_not_a_path_claim():
    summary = "Read/write handling, e.g. retries, see https://example.com/x.py and v1.2.3."
    m = find_mismatches(summary, [], KNOWN)
    assert m.claimed_not_changed == []


def test_directory_claim_matches_files_under_it():
    m = find_mismatches("Reworked docs/", [FileChange("M", "docs/guide.md")], KNOWN)
    assert m.claimed_not_changed == []
    assert m.changed_not_mentioned == ["docs/guide.md"]


def test_paths_relative_to_cwd_prefix():
    m = find_mismatches("Changed core.py and ./core.py", [FileChange("M", "src/core.py")], KNOWN, "src/")
    assert m.claimed_not_changed == []
    assert m.changed_not_mentioned == []


# ----------------------------------------------------------------- redaction

def test_redactor_masks_secret_env_values_and_patterns():
    redact = Redactor({"MY_API_KEY": "supersecretvalue123", "PROMPT_TOKENS": "12345678"})
    text = "key=supersecretvalue123 gh=ghp_abcdefghijklmnopqrstuvwxyz0123 n=12345678"
    out = redact(text)
    assert "supersecretvalue123" not in out
    assert "ghp_" not in out
    assert "12345678" in out
