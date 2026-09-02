"""
Bash security hook — trading-agent override
===========================================

Drop-in replacement for the quickstart's `security.py`.

The upstream file is kept alongside this one as `security_upstream.py`; its
command-parsing helpers are generic and are reused verbatim. What changes here is
the policy, because this run is not a greenfield Node app:

- The allowlist is Python + git + ssh, not npm + node.
- `ssh` and `scp` are permitted, because the run is allowed to deploy — but only
  to a named host, and the remote command is inspected rather than trusted.
- A handful of project-specific prohibitions are enforced mechanically rather than
  by asking the agent nicely: no credential literals in a command, no reading a
  `.env` in a way that dumps values, no force-push, no `git clean`, no `sudo`.

The last two matter more than they look. There is uncommitted, unpushed in-flight
work in this repo (`vesper/agents/`, the swarm and synthesis nodes), so a stray
`git clean -fdx` destroys real work, and a force-push destroys the record of it.
"""

import os
import re
import shlex

# Upstream's three original validators are reused unchanged. Its *parser* is not:
# `split_command_segments` splits on ";" with a regex that ignores quoting, so a
# perfectly ordinary `python3 -c "import x; print(1)"` splits mid-string, fails to
# lex, and is blocked as unparseable. The lexer below is quote-aware.
from security_upstream import (  # noqa: F401
    validate_pkill_command,
    validate_chmod_command,
    validate_init_script,
)

# Tokens that end one command and begin another.
_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
# Tokens that introduce a redirect target rather than a command.
_REDIRECTS = {">", ">>", "<", "<<", "<<<", "2>", "2>>", "&>"}
_SHELL_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "select", "time", "!", "{", "}",
}


def _lex(command_string: str) -> list[str]:
    """Tokenize like a shell would, respecting quotes."""
    lexer = shlex.shlex(command_string, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def split_command_segments(command_string: str) -> list[str]:
    """Split into per-command segments on quote-aware separators."""
    segments, current = [], []
    for tok in _lex(command_string):
        if tok in _SEPARATORS:
            if current:
                segments.append(shlex.join(current))
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(shlex.join(current))
    return segments or ([command_string] if command_string.strip() else [])


def extract_commands(command_string: str) -> list[str]:
    """Every command name a shell would actually execute."""
    tokens = _lex(command_string)
    if not tokens and command_string.strip():
        return []  # unparseable -> caller fails closed

    commands, expect_command, skip_next = [], True, False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _SEPARATORS:
            expect_command = True
            continue
        if tok in _REDIRECTS:
            skip_next = True
            continue
        if not expect_command:
            continue
        if tok in _SHELL_KEYWORDS:
            continue
        if tok.startswith("-"):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            continue  # VAR=value prefix
        commands.append(os.path.basename(tok))
        expect_command = False
    return commands


def get_command_for_validation(cmd: str, segments: list[str]) -> str:
    """The segment in which `cmd` is the command being run."""
    for segment in segments:
        found = extract_commands(segment)
        if found and found[0] == cmd:
            return segment
    for segment in segments:
        if cmd in extract_commands(segment):
            return segment
    return ""


ALLOWED_COMMANDS = {
    # File inspection
    "ls", "cat", "head", "tail", "wc", "grep", "find", "sed", "awk",
    "diff", "sort", "uniq", "cut", "tr", "du", "df", "file", "stat",
    # File operations (the SDK's Write/Edit tools handle most of this)
    "cp", "mv", "mkdir", "touch", "chmod",
    # Directory
    "pwd", "cd",
    # Python toolchain
    "python", "python3", "pip", "pip3", "pytest", "ruff", "black", "mypy",
    # Version control
    "git",
    # Remote deployment — validated below, host-restricted
    "ssh", "scp", "curl",
    # Process inspection
    "ps", "lsof", "sleep", "pkill", "free", "uptime",
    # Trivia
    "echo", "date", "which", "true", "env",
    # Script execution
    "init.sh",
}

COMMANDS_NEEDING_EXTRA_VALIDATION = {
    "pkill", "chmod", "init.sh",          # upstream
    "ssh", "scp",                          # remote blast radius
    "git",                                 # history destruction
    "cat", "head", "tail", "grep",         # credential disclosure
    "python", "python3",                   # -c is an arbitrary-execution hole
    "pip", "pip3",                         # supply chain
    "env",                                 # dumps the whole environment
}

# The one host this run is allowed to reach.
ALLOWED_SSH_HOSTS = {"coolify"}

# Units this run is allowed to control. Everything else on that box is
# somebody's production.
ALLOWED_REMOTE_UNIT_PREFIXES = ("trading-agent", "vesper-", "vesper.")

# Literal credential shapes. If one of these appears in a command string it is
# being pasted rather than read from the environment, and this transcript is
# read aloud on stream.
CREDENTIAL_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "an Anthropic key or OAuth token"),
    (re.compile(r"\btd_live_[A-Za-z0-9_\-]{8,}"), "a TraderDaddy live key"),
    (re.compile(r"\bsmk_[A-Za-z0-9]{16,}"), "a supermcp key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "a Slack token"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"), "a Telegram bot token"),
    (re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{20,}"), "a bearer token"),
]

# Shell constructs the upstream parser does not decompose, so it cannot vet what
# is inside them.
SUBSHELL_PATTERNS = [
    ("$(", "command substitution"),
    ("`", "backtick substitution"),
    ("<(", "process substitution"),
    (">(", "process substitution"),
]

# Things that must never appear inside a remote command.
REMOTE_FORBIDDEN = [
    (re.compile(r"\bsudo\b"), "sudo is not available to this run — mark the feature blocked instead"),
    (re.compile(r"\bdoas\b"), "privilege escalation"),
    (re.compile(r"\bdocker\b"), "docker touches other people's containers on that box"),
    (re.compile(r"\b(shutdown|reboot|poweroff|init\s+0)\b"), "host power control"),
    (re.compile(r"\bmkfs\b"), "filesystem creation"),
    (re.compile(r"\bdd\s+if="), "raw disk write"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/(\s|$)"), "recursive delete of /"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
]


def _is_env_file(token: str) -> bool:
    """True for a dotenv path, but not for a committed example file."""
    base = os.path.basename(token.rstrip("'\""))
    if base.startswith(".env.example") or base.endswith(".example"):
        return False
    return base == ".env" or base.startswith(".env.")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


# ssh/scp flags that consume the following token as their value.
_SSH_VALUE_FLAGS = {"-o", "-p", "-P", "-i", "-F", "-l", "-J", "-b", "-c", "-m", "-S", "-w"}


def _ssh_hosts(tokens: list[str]) -> list[str]:
    """Every host an ssh/scp invocation would contact."""
    hosts, skip_next = [], False
    is_scp = os.path.basename(tokens[0]) == "scp" if tokens else False

    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _SSH_VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue

        if is_scp:
            # scp targets are host:path; bare tokens are local paths.
            if ":" not in tok:
                continue
            candidate = tok.split(":", 1)[0]
        else:
            candidate = tok

        if "@" in candidate:
            candidate = candidate.split("@", 1)[1]
        if candidate:
            hosts.append(candidate)
            if not is_scp:
                break  # for ssh, everything after the host is the remote command
    return hosts


def validate_ssh_command(command_string: str) -> tuple[bool, str]:
    """ssh/scp may reach exactly one host, and the remote command is inspected."""
    tokens = _tokens(command_string)
    hosts = _ssh_hosts(tokens)

    if not hosts:
        return False, "Could not identify an ssh/scp target host; refusing."
    for host in hosts:
        if host not in ALLOWED_SSH_HOSTS:
            return (
                False,
                f"ssh/scp target '{host}' is not permitted. This run may only reach: "
                f"{', '.join(sorted(ALLOWED_SSH_HOSTS))}.",
            )

    for pattern, why in REMOTE_FORBIDDEN:
        if pattern.search(command_string):
            return False, f"Remote command rejected ({why})."

    if "systemctl" in command_string:
        if "--user" not in command_string:
            return (
                False,
                "System-wide systemctl is not permitted — this run owns only "
                "systemd *user* units. Add --user.",
            )
        for unit in re.findall(r"[\w.\-@]+\.service", command_string):
            if not unit.startswith(ALLOWED_REMOTE_UNIT_PREFIXES):
                return (
                    False,
                    f"Unit '{unit}' is not owned by this run. Permitted prefixes: "
                    f"{', '.join(ALLOWED_REMOTE_UNIT_PREFIXES)}.",
                )

    return True, ""


def validate_git_command(command_string: str) -> tuple[bool, str]:
    """Block the git operations that destroy unrecoverable local work."""
    lowered = command_string.lower()

    if re.search(r"\bpush\b", lowered) and re.search(r"(--force(?!-with-lease)|\s-f\b|\s\+refs/)", lowered):
        return (
            False,
            "Force-push is not permitted. If history genuinely needs rewriting, "
            "leave it for a human.",
        )
    if re.search(r"\bclean\b", lowered):
        return (
            False,
            "git clean is not permitted — this repo has uncommitted, unpushed "
            "in-flight work (vesper/agents/, swarm_node.py, synthesis_node.py) "
            "that it would destroy.",
        )
    if re.search(r"\bfilter-(branch|repo)\b", lowered):
        return False, "History rewriting is not permitted in an unattended run."
    if re.search(r"\bconfig\b.*--global", lowered):
        return False, "Global git config changes affect the whole machine; refusing."
    return True, ""


def validate_secret_read(cmd: str, command_string: str) -> tuple[bool, str]:
    """Keep dotenv values out of the transcript. Rule 5 / invariant I6."""
    tokens = _tokens(command_string)
    targets = [t for t in tokens[1:] if not t.startswith("-")]
    if not any(_is_env_file(t) for t in targets):
        return True, ""

    if cmd == "grep":
        if any(f in tokens for f in ("-c", "-q", "-l", "-L")) or any(
            t.startswith("-") and set(t[1:]) & {"c", "q", "l"} for t in tokens if len(t) > 1
        ):
            return True, ""
        return (
            False,
            "grep on a .env would print secret values. Use `grep -c '^KEY=' .env` "
            "to test presence, or list names with "
            "`sed -n 's/^\\([A-Z_]*\\)=.*/\\1/p' .env`.",
        )

    return (
        False,
        f"`{cmd}` on a .env file would dump credentials into a transcript that is "
        "read aloud on stream. List names instead: "
        "`sed -n 's/^\\([A-Z_]*\\)=.*/\\1/p' .env`.",
    )


def validate_python_command(command_string: str) -> tuple[bool, str]:
    """`python -c` is arbitrary execution; permit it, but not as an escape hatch."""
    if not re.search(r"\s-c\b", command_string):
        return True, ""
    forbidden = ("os.system", "subprocess", "__import__", "eval(", "exec(", "pty.", "socket.")
    for needle in forbidden:
        if needle in command_string:
            return (
                False,
                f"`python -c` containing `{needle}` would bypass this allowlist. "
                "Run the command directly so it can be validated.",
            )
    return True, ""


def validate_pip_command(command_string: str) -> tuple[bool, str]:
    if "--index-url" in command_string or "--extra-index-url" in command_string:
        return False, "Alternate package indexes are not permitted (supply chain)."
    return True, ""


def validate_env_command(command_string: str) -> tuple[bool, str]:
    tokens = _tokens(command_string)
    if len(tokens) == 1:
        return False, "Bare `env` dumps every credential in the environment; refusing."
    return True, ""


async def bash_security_hook(input_data, tool_use_id=None, context=None):
    """
    Pre-tool-use hook validating bash commands against the policy above.

    Returns an empty dict to allow, or {"decision": "block", "reason": ...}.
    """
    if input_data.get("tool_name") != "Bash":
        return {}

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        return {}

    # 1. Never let a credential literal into the transcript, whatever the command.
    for pattern, what in CREDENTIAL_PATTERNS:
        if pattern.search(command):
            return {
                "decision": "block",
                "reason": (
                    f"This command contains what looks like {what}. Read it from the "
                    "environment or a 0600 file instead of pasting the literal — "
                    "this transcript is read aloud on stream (rule 5)."
                ),
            }

    # 2. Refuse constructs the parser cannot see inside.
    for marker, what in SUBSHELL_PATTERNS:
        if marker in command:
            return {
                "decision": "block",
                "reason": (
                    f"{what.capitalize()} ({marker}) hides commands from validation. "
                    "Run the steps separately, and prefer single quotes."
                ),
            }

    commands = extract_commands(command)
    if not commands:
        return {
            "decision": "block",
            "reason": f"Could not parse command for security validation: {command}",
        }

    segments = split_command_segments(command)

    for cmd in commands:
        if cmd not in ALLOWED_COMMANDS:
            return {
                "decision": "block",
                "reason": (
                    f"Command '{cmd}' is not in the allowed commands list. "
                    "If the task genuinely needs it, mark the feature blocked and "
                    "explain why in claude-progress.txt rather than working around it."
                ),
            }

        if cmd not in COMMANDS_NEEDING_EXTRA_VALIDATION:
            continue

        cmd_segment = get_command_for_validation(cmd, segments) or command

        if cmd == "pkill":
            ok, reason = validate_pkill_command(cmd_segment)
        elif cmd == "chmod":
            ok, reason = validate_chmod_command(cmd_segment)
        elif cmd == "init.sh":
            ok, reason = validate_init_script(cmd_segment)
        elif cmd in ("ssh", "scp"):
            ok, reason = validate_ssh_command(cmd_segment)
        elif cmd == "git":
            ok, reason = validate_git_command(cmd_segment)
        elif cmd in ("cat", "head", "tail", "grep"):
            ok, reason = validate_secret_read(cmd, cmd_segment)
        elif cmd in ("python", "python3"):
            ok, reason = validate_python_command(cmd_segment)
        elif cmd in ("pip", "pip3"):
            ok, reason = validate_pip_command(cmd_segment)
        elif cmd == "env":
            ok, reason = validate_env_command(cmd_segment)
        else:
            ok, reason = True, ""

        if not ok:
            return {"decision": "block", "reason": reason}

    return {}
