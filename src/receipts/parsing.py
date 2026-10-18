"""Read what a run reported about itself: commands, token and cost figures, its summary.

Everything here only records figures that literally appear in the output or
environment. Nothing is estimated, totalled or filled in.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from receipts.gitsnap import FileChange

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _try_json(line: str) -> object | None:
    stripped = line.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _walk(obj: object) -> Iterator[tuple[str, object]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key), value
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


# --------------------------------------------------------------------------- commands

COMMAND_LINE_RES = [
    re.compile(r"^\s*\$\s+(?P<cmd>\S.*?)\s*$"),
    re.compile(r"^\s*>\s+(?P<cmd>\S.*?)\s*$"),
    re.compile(r"^\s*(?:Ran|Running|Command):\s*(?P<cmd>\S.*?)\s*$", re.IGNORECASE),
]
JSON_COMMAND_RE = re.compile(r'"command"\s*:\s*("(?:[^"\\]|\\.)*")')


@dataclass(frozen=True)
class FoundCommand:
    source: str
    command: str


def _clean_command(cmd: str) -> str:
    cmd = cmd.strip()
    if len(cmd) >= 2 and cmd[0] == cmd[-1] == "`":
        cmd = cmd.strip("`").strip()
    return cmd


def _json_command(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return shlex.join(value)
    return None


def find_commands(text: str, source: str) -> list[FoundCommand]:
    found: list[FoundCommand] = []
    for line in strip_ansi(text).splitlines():
        parsed = _try_json(line)
        if parsed is not None:
            for key, value in _walk(parsed):
                if key == "command" and (cmd := _json_command(value)):
                    found.append(FoundCommand(source, cmd.strip()))
            continue
        for match in JSON_COMMAND_RE.finditer(line):
            try:
                cmd = json.loads(match.group(1))
            except ValueError:
                continue
            if cmd.strip():
                found.append(FoundCommand(source, cmd.strip()))
        for regex in COMMAND_LINE_RES:
            if m := regex.match(line):
                cmd = _clean_command(m.group("cmd"))
                if cmd:
                    found.append(FoundCommand(source, cmd))
                break
    return found


def dedupe_commands(commands: Iterable[FoundCommand]) -> list[FoundCommand]:
    seen: set[str] = set()
    out: list[FoundCommand] = []
    for cmd in commands:
        if cmd.command not in seen:
            seen.add(cmd.command)
            out.append(cmd)
    return out


# ------------------------------------------------------------------ tokens and cost

USAGE_LABELS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "tokens",
    "token_usage",
    "cost",
)
_LABEL_PATTERN = (
    r"prompt[_ ]tokens|completion[_ ]tokens|total[_ ]tokens|input[_ ]tokens"
    r"|output[_ ]tokens|token[_ ]usage|tokens|cost"
)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
USAGE_TEXT_RE = re.compile(
    rf'(?<![\w$.-])(?<!max )"?(?P<label>{_LABEL_PATTERN})"?\s*[:=]\s*"?'
    rf"(?P<value>\$\s?{_NUMBER}|{_NUMBER})(?![\d.]\d|\d)",
    re.IGNORECASE,
)
_VALUE_STR_RE = re.compile(rf"^\s*(\$\s?{_NUMBER}|{_NUMBER})\s*$")

ENV_USAGE_RE = re.compile(
    r"^(?:[A-Z0-9]+_)*(?P<name>TOTAL_TOKENS|PROMPT_TOKENS|COST_USD|RECEIPTS_COST)$", re.IGNORECASE
)
ENV_VALUE_RE = re.compile(r"^\$?\d+(?:\.\d+)?$")
ENV_LABELS = {
    "TOTAL_TOKENS": "total_tokens",
    "PROMPT_TOKENS": "prompt_tokens",
    "COST_USD": "cost",
    "RECEIPTS_COST": "cost",
}


@dataclass(frozen=True)
class UsageFigure:
    label: str  # one of USAGE_LABELS
    value: str  # exactly as reported, e.g. "1,234" or "$0.42"
    source: str  # "stdout", "stderr" or "env:NAME"


def _normalize_label(label: str) -> str:
    return re.sub(r"[\s-]+", "_", label.strip().lower())


def _json_value(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and (m := _VALUE_STR_RE.match(value)):
        return m.group(1)
    return None


def find_usage(text: str, source: str) -> list[UsageFigure]:
    found: list[UsageFigure] = []
    for line in strip_ansi(text).splitlines():
        parsed = _try_json(line)
        if parsed is not None:
            for key, value in _walk(parsed):
                label = _normalize_label(key)
                if label in USAGE_LABELS and (raw := _json_value(value)) is not None:
                    found.append(UsageFigure(label, raw, source))
            continue
        for m in USAGE_TEXT_RE.finditer(line):
            found.append(UsageFigure(_normalize_label(m.group("label")), m.group("value"), source))
    return found


def usage_from_env(environ: Mapping[str, str] | None = None) -> list[UsageFigure]:
    environ = os.environ if environ is None else environ
    found: list[UsageFigure] = []
    for name in sorted(environ):
        m = ENV_USAGE_RE.match(name)
        if not m:
            continue
        value = environ[name].strip()
        if ENV_VALUE_RE.match(value):
            found.append(UsageFigure(ENV_LABELS[m.group("name").upper()], value, f"env:{name}"))
    return found


def latest_by_label(figures: Iterable[UsageFigure]) -> dict[str, UsageFigure]:
    """The last reported figure for each label. Figures are never added together."""
    latest: dict[str, UsageFigure] = {}
    for fig in figures:
        latest[fig.label] = fig
    return latest


# ------------------------------------------------------------------------ summary

_DECOR = r"[\s#=*_~-]*"
SUMMARY_HEADING_RE = re.compile(rf"^{_DECOR}(?:final\s+)?summary[\s#=*_~:-]*$", re.IGNORECASE)
SUMMARY_INLINE_RE = re.compile(
    rf"^{_DECOR}(?:final\s+)?summary\s*[*_]*\s*:\s*[*_]*\s*(?P<rest>\S.*)$", re.IGNORECASE
)
FALLBACK_LINES = 30


@dataclass
class Summary:
    text: str
    source: str  # human readable description of where it came from


def _after_last_heading(text: str) -> str | None:
    lines = strip_ansi(text).splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if SUMMARY_HEADING_RE.match(line):
            return "\n".join(lines[i + 1 :]).strip("\n")
        if m := SUMMARY_INLINE_RE.match(line):
            return "\n".join([m.group("rest"), *lines[i + 1 :]]).strip("\n")
    return None


def extract_summary(stdout: str, stderr: str) -> Summary:
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        body = _after_last_heading(text)
        if body is not None:
            return Summary(body, f"{name}, after the last Summary heading")
    name, text = ("stdout", stdout) if stdout.strip() else ("stderr", stderr)
    lines = strip_ansi(text).rstrip("\n").splitlines()[-FALLBACK_LINES:]
    return Summary("\n".join(lines), f"{name}, last {FALLBACK_LINES} lines (no Summary heading)")


# ----------------------------------------------------------------------- mismatch

FILE_EXTENSIONS = {
    "c", "cc", "cfg", "conf", "cpp", "cs", "css", "csv", "dart", "env", "gif", "go", "gradle",
    "h", "hpp", "html", "ini", "ipynb", "java", "jpg", "js", "json", "jsx", "kt", "lock",
    "lua", "md", "mjs", "cjs", "php", "png", "proto", "py", "pyi", "rb", "rs", "rst", "scss",
    "sh", "sql", "svg", "swift", "toml", "ts", "tsx", "txt", "vue", "xml", "yaml", "yml", "zsh",
}
_TOKEN_SPLIT_RE = re.compile(r"[\s`'\"()\[\]{}<>,;|]+")
_EDGE_PUNCT = ".,;:!?*"
_BARE_FILE_RE = re.compile(r"^[\w][\w.-]*\.(?P<ext>[A-Za-z][A-Za-z0-9]{0,9})$")


def _strip_token(token: str) -> str:
    token = token.strip(_EDGE_PUNCT)
    if token.startswith("./"):
        token = token[2:]
    return token


def claimed_paths(summary: str, known_paths: set[str]) -> list[str]:
    """Paths named in the summary.

    A token counts as a path when it contains a slash and either has a file
    extension, ends with '/', or exists in the repo; or when it is a bare
    filename that exists in the repo or has a common source-file extension.
    """
    known_dirs = {p.rsplit("/", 1)[0] for p in known_paths if "/" in p}
    known_dirs |= {"/".join(d.split("/")[:i]) for d in known_dirs for i in range(1, d.count("/") + 1)}
    known_names = {p.rsplit("/", 1)[-1] for p in known_paths}
    claims: list[str] = []
    for raw in _TOKEN_SPLIT_RE.split(summary):
        if not raw or "@" in raw or "://" in raw or raw.startswith("www."):
            continue
        token = _strip_token(raw)
        if not token or token in claims:
            continue
        if "/" in token:
            if token.startswith("/") or "//" in token:
                continue
            last = token.rstrip("/").rsplit("/", 1)[-1]
            looks_like_file = bool(_BARE_FILE_RE.match(last))
            if (
                token.endswith("/")
                or looks_like_file
                or token in known_paths
                or token.rstrip("/") in known_dirs
            ):
                claims.append(token)
        elif m := _BARE_FILE_RE.match(token):
            if token in known_names or m.group("ext").lower() in FILE_EXTENSIONS:
                claims.append(token)
    return claims


def _claim_touched(claim: str, touched: set[str], prefix: str) -> bool:
    candidates = {claim, prefix + claim}
    if claim.endswith("/"):
        return any(t.startswith(c) for t in touched for c in candidates)
    if "/" in claim:
        return any(
            t in candidates or t.endswith("/" + claim) or any(t.startswith(c + "/") for c in candidates)
            for t in touched
        )
    return any(t.rsplit("/", 1)[-1] == claim or t.startswith(prefix + claim + "/") or t.startswith(claim + "/") for t in touched)


def _mentions(summary: str, name: str) -> bool:
    pattern = rf"(?<![\w.-]){re.escape(name)}(?![\w-]|\.\w)"
    return re.search(pattern, summary) is not None


def _names_for(path: str, prefix: str) -> set[str]:
    names = {path, path.rsplit("/", 1)[-1]}
    if prefix and path.startswith(prefix):
        names.add(path[len(prefix) :])
    return names


@dataclass
class Mismatches:
    claimed_not_changed: list[str]
    changed_not_mentioned: list[str]


def find_mismatches(
    summary: str,
    changes: list[FileChange],
    known_paths: set[str],
    prefix: str = "",
) -> Mismatches:
    touched: set[str] = set()
    for change in changes:
        touched.add(change.path)
        if change.old_path:
            touched.add(change.old_path)

    claimed_not_changed = [
        claim
        for claim in claimed_paths(summary, known_paths | touched)
        if not _claim_touched(claim, touched, prefix)
    ]

    clean = strip_ansi(summary)
    changed_not_mentioned = []
    for change in changes:
        names = _names_for(change.path, prefix)
        if change.old_path:
            names |= _names_for(change.old_path, prefix)
        if not any(_mentions(clean, n) for n in names):
            changed_not_mentioned.append(change.path)
    return Mismatches(claimed_not_changed, changed_not_mentioned)


# ---------------------------------------------------------------------- redaction

SECRET_ENV_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|AUTH(?!OR)|SESSION|COOKIE", re.IGNORECASE
)
SECRET_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/-]{12,}=*"),
]
REDACTED = "[redacted]"


class Redactor:
    """Masks secret-looking values before anything is written to a receipt."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        environ = os.environ if environ is None else environ
        values = {
            value
            for name, value in environ.items()
            if SECRET_ENV_NAME_RE.search(name)
            and len(value) >= 8
            and not ENV_VALUE_RE.match(value.strip())
        }
        self._values = sorted(values, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, REDACTED)
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text
