# agent-receipts

Run a command, usually a coding agent, and get a receipt of what it actually did:
which files changed, which commands it says it ran, what it reported for tokens
and cost, and where its final summary disagrees with the diff.

![receipts demo](docs/demo.gif)

The demo GIF is a placeholder and is still to be recorded.

## Install

Python 3.11 or newer.

```bash
pip install git+https://github.com/skkinnyboii/agent-reciepts
```

For development:

```bash
pip install -e '.[dev]'
pytest
```

## Usage

```bash
receipts run [--cwd DIR] [--receipts-dir DIR] -- COMMAND [ARGS...]
```

Example:

```bash
receipts run -- codex exec "fix the failing test"
```

The command's stdout and stderr stream to your terminal as usual and are also
captured. `receipts` exits with the command's exit code, and a receipt is
written even when the command fails. Receipts go to `<cwd>/receipts/` by
default, named by UTC start time with colons replaced, for example
`receipts/2026-10-17T22-30-45Z.md`. The summary tables print to stderr.

## What goes in a receipt

**Git, before and after.** HEAD sha, branch, `git status --porcelain`, and the
diff between the two snapshots. Each snapshot writes the whole worktree
(tracked and untracked files, gitignored files left out) to a tree object
through a throwaway index, so your real index is never touched. Changed files
are the files whose content differs between the two trees. A file that was
already dirty before the run and was not touched again is not counted as
changed. If HEAD moves, the commits made during the run are listed. The
receipts directory itself is excluded.

**Commands.** The command you ran, plus commands found in stdout or stderr:
lines like `$ git status` or `> npm test`, lines starting with `Ran:`,
`Running:` or `Command:`, and JSON `"command"` fields.

**Tokens and cost.** Recorded only when a real figure is present; otherwise the
receipt says `not available`. Nothing is estimated or summed. From the output
it accepts `prompt_tokens`, `completion_tokens`, `total_tokens`,
`input_tokens`, `output_tokens`, `tokens`, `token usage` and `cost` (optional
`$`), written as `label: value`, `label=value`, or JSON. A total appears only
when the output or environment actually reports one. From the environment it
reads only numeric values named `TOTAL_TOKENS`, `PROMPT_TOKENS`, `COST_USD` or
`RECEIPTS_COST`, optionally with a prefix such as `OPENAI_TOTAL_TOKENS`.
Variables like `GITHUB_TOKEN` are ignored, and secret-looking values are
masked anywhere they appear in the receipt.

**Final summary.** The text after the last `Summary` or `Final summary`
heading in stdout (or stderr, if stdout has none). With no heading, the last
30 lines.

**Mismatch flags.**

- `claimed, not changed`: a path named in the summary that the diff did not touch.
- `changed, not mentioned`: a changed file the summary never names, by full
  path or by filename.

## License

MIT
