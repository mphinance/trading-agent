# Moving your agent to a new computer

> [!NOTE]
> Status: built in v1 (`trading-agent export` and `trading-agent import`). Linux first.

**How to carry an agent's memories, persona, settings and skills to another computer in one file, without carrying any key.**

## At a glance

| Step | Where | Command |
|---|---|---|
| 1. Export | old computer | `trading-agent export --profile <profile> -o <profile>.tgz` |
| 2. Copy the one file | your choice | USB stick, your own cloud folder, `scp` |
| 3. Install | new computer | Hermes, then this tool (see the [README](../README.md)) |
| 4. Import | new computer | `trading-agent import <profile>.tgz` |
| 5. Re-enter keys | new computer | the import asks for each key by name; skip any and add it later |

> [!CAUTION]
> A bundle is **not encrypted**. It holds your memories and persona in plain text. Treat it like a private document: do not post it, and delete stray copies when you are done.

## What a bundle holds

| Included | Not included |
|---|---|
| `memories/` (what the agent remembers) | Keys and secret settings (`.env`) |
| `SOUL.md` (the agent's persona) | Sign-ins and OAuth tokens (`auth.json`) |
| `config.yaml` (settings, MCP server entries) | Chat history (`sessions/`), `logs/`, `workspace/`, `cron/`, `plans/` |
| `skills/` you added or changed | Stock skills Hermes seeded and you never changed, with `skills/.bundled_manifest` and `skills/.hub/` (Hermes adds them again on the new computer) |
| `bundle.json`: a short description with the **names** of the keys to re-enter | The link to the distribution it was installed from (`distribution.yaml`) |
| | The tool's ownership marker (the importer makes a new one) |

`config.yaml` can contain web addresses (a provider's base URL, an MCP server URL) and the **names** of settings such as `${ALPACA_API_KEY}`. Values belong in the profile's private `.env` file, which never goes in a bundle, and the export enforces that: it refuses a `config.yaml` that writes a value out in full where a key belongs (see below).

## Step 1: export

```sh
trading-agent export --profile <profile> -o <profile>.tgz
```

- `--profile` can be left out when setup made only one profile; with several, the tool asks which one.
- The output file must not exist yet. The tool never overwrites; it writes to a temporary file and moves it into place, readable only by you (mode 0600).
- Afterwards it prints `This contains: ...` and `It does not contain: keys, sign-ins.`, plus the key names you will re-enter.

The rule has two levels. Every message names the file or the setting, never the value.

**Refused, nothing written.** Each file is read as it is on your disk, and compared with what Hermes' own export made of it. Hermes rewrites text files (`.md`, `.txt`, `.yaml`, `.json` and similar) wherever something looks like a key, so **any difference between a file and Hermes' copy of it refuses the export**: a real key would otherwise go out in part (its first 6 and last 4 characters). A placeholder shaped like a key (`password: example123`) trips this too; edit it. `config.yaml` is the one exception: it passes its own stricter checks on the disk copy (below), and that disk copy is what goes into the bundle.

| Because | Example | What to do |
|---|---|---|
| A file is named like a credential | `.env.local`, `auth.json`, `*.pem`, `*.key`, `token*` anywhere in the profile | Move the file out of the profile |
| A file holds a value from the profile's `.env` | a memory that quotes your API key | Edit that file |
| A file holds a high-confidence key shape, or a secret written after its name | `sk-...` (also `sk-or-...`), `Bearer ...`, `ghp_...`, `github_pat_...`, `xoxb-...`, `AKIA...`, `AIza...`, `glpat-...`, a PEM private key, a JWT, `user:password@` in a URL, a long value in a key-named URL parameter; `db password: Kq8...`, `the token is ...`, `api_key = ...` (a value of 8 or more characters with letters and digits) | Edit that file |
| `config.yaml` writes a value out in full where a key belongs | `api_key: hunter2pass`; a number, date or binary value under a key-like name (singular or plural: `tokens`, `passwords`, `api_keys`, also `signature`, `bearer`); any value under a key-like parent such as `credentials:`, `auth:`, `secrets:`, `password:`; any literal inside an `env:`, `headers:`, `default_headers:`, `extra_headers:`, `docker_env:` or other `*_env` / `*_headers` map. Hermes' own non-secret settings with such names (`voice.record_key`, `desktop.password_store`, `auth.codex_login_flow`, `proxy.credential_source`, `vault.key_storage` and the like) are fine. | Move the value to the profile's `.env` file and write `${NAME}` in its place |

**Named warning, you decide (the answer defaults to No):** a plain long hex or base64 run, or a token-like web address value, in one of your own files. This is usually a hash or an example. The export lists each file and why, then asks `Export them as they are? [y/N]`.

Values shorter than 8 characters (`true`, `paper`) are settings, not keys, and are not compared with your `.env`. In `config.yaml`, `true`, `false`, empty values, `${NAME}` and `Bearer ${NAME}` are fine; so are key-like names whose value is a name, address, kind or count (`key_env`, `token_url`, `auth_type`, `token_limit`), also under a key-like parent.

### A readable folder instead

```sh
trading-agent export --profile <profile> --files <folder>
```

This writes the same snapshot as plain files (`memories/`, `SOUL.md`, `config.yaml`, `skills/`, `bundle.json`) into a new or empty folder, so you can read it, copy it, or keep it in your own version control. Use the one-file bundle to move; `import` reads bundle files only.

## Step 2: copy the one file

Any way you like. The file is usually small (memories and skills are text). Prefer a channel only you can read.

## Step 3: install on the new computer

Install Hermes and this tool as the [README](../README.md) describes. You do not need to run `trading-agent setup` first.

## Step 4: import

```sh
trading-agent import <profile>.tgz
trading-agent import <profile>.tgz --name <new-name>
```

What happens, in order:

1. The whole file is checked before anything is created (see [Safety checks](#safety-checks-on-import)). A bad file gives one plain line and nothing is written.
2. You see what the bundle contains, and what happens to its `config.yaml`:
   - **Refused outright** (the whole import stops): settings that run a program every time Hermes starts or weaken command approvals: `secrets.command.enabled`, `skills.inline_shell`, a wildcard or glob in `command_allowlist`, `delegation.subagent_auto_approve`, `plugins.host.launcher`, `terminal.shell_init_files`, `security.redact_secrets` set to anything but true, `hooks_auto_accept` set to anything but a plain false, and `approvals.mode` set to anything but `manual` or `smart` (Hermes reads bare `y`, `n`, `on`, `off` as booleans, so those count too).
   - **Brought in:** only an allowlist of settings: the model and provider choice (`model`, `providers`, `fallback`, `fallback_providers`), `auth.adopt_external_logins`, `mcp_servers`, display and UI, memory (not `memory.provider` or `memory.write_approval`), context, compression and streaming knobs, personalities and prompt text, timezone, privacy, goals and loop limits, vision, a few agent knobs (`agent.max_turns` and similar) and `skills.template_vars`.
   - **Dropped and listed:** every other setting, for example `terminal`, `browser`, `gateway`, `cron`, `plugins`, `security`, `delegation`, `secrets`, `vault`, `lsp`, `desktop`, `hooks`, `quick_commands`, `approvals`, `command_allowlist`, `prefill_messages_file`, and any setting this tool does not know. The tool says `These settings were not imported: <key paths>` before you confirm and again after the import, and writes a trimmed `config.yaml` that does not hold them. Add any of them yourself with `hermes config set` if you trust them.
   - **Shown before you confirm:** each MCP server with its command and arguments or its address, every address the kept settings use (`model.base_url`, provider addresses), and each `${NAME}` key that would be sent to one of those addresses. These can run programs on your computer or send your keys somewhere. The checks are best effort: import only a bundle you made or trust.
3. If the name is taken, by a Hermes profile or by a command of that name on this computer, you pick another name or go back. An existing profile is never overwritten.
4. You confirm. The answer defaults to No.
5. Keys: one hidden prompt per key name. Paste it, or press Enter to skip. Keys found in your environment are **not** offered for reuse during import; type them or add them later.
6. The name is checked once more, then Hermes creates the profile and the tool records a setup receipt.

If anything fails, or you press Ctrl-C, after the profile was created, it is removed again (with Hermes' command alias), so the name is free for another try. Going back or answering No ends the import with nothing created and is not an error (exit code 0).

## Step 5: finish later, or undo

The import lists the keys you skipped under `Left to finish later:`. Add each one to the profile's private `.env` file (`NAME=value`, one per line) when you have it.

To remove an imported profile completely:

```sh
trading-agent undo --profile <profile>
```

## Safety checks on import

A bundle from anyone else is treated as hostile input. Before anything is written:

| Check | Limit |
|---|---|
| Bundle file size (compressed) | 50 MB |
| `bundle.json` size | 64 KB |
| Profile archive inside the bundle (compressed) | 50 MB |
| Profile archive once expanded | 200 MB |
| One file in the profile archive | 50 MB |
| Entries in the profile archive | 5000 |
| Compression ratio (past the first 1 MB) | 100 to 1 |

- The bundle must hold exactly `bundle.json` and `profile.tar.gz`, both plain files.
- Every entry must be a plain file or folder: no links, devices or pipes; no absolute paths, `..`, backslashes or control characters; no duplicate names (also ignoring case).
- The profile archive may hold only `SOUL.md`, `config.yaml`, `memories/` and `skills/`. Anything else, such as `.env`, `auth.json`, chat history, scheduled jobs or Hermes' own `skills/.bundled_manifest` and `skills/.hub/`, refuses the bundle.
- `config.yaml` must hold no value written out in full where a key belongs (the same rule as export), may not carry any setting that refuses a bundle (step 2), and is trimmed to the allowlist; every setting left out is listed.
- `bundle.json` is checked field by field. An unknown `format` is refused. Key names must look like `NAME_WITH_CAPS` and may not be names that change how programs load, connect or ask for input (`PATH`, `HERMES_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `NODE_OPTIONS`, `EDITOR`, `PAGER`, `SSH_ASKPASS`, proxies and similar).

## Honest limits

- **Not encrypted.** Anyone with the file can read your memories and persona.
- **No keys move.** You re-enter them on the new computer, by design.
- **Not tied to the distribution.** An imported profile does not carry `distribution.yaml` (it records a local path), so `hermes profile update` does not apply to it.
- **Chat history stays behind.** Sessions, logs, the workspace folder and scheduled jobs are not included.
- **A command alias appears.** `hermes profile import` also creates a `~/.local/bin/<profile>` shortcut; `undo` removes it. The import refuses a name that already exists as a command, so nothing of yours is replaced.
- **The key scan finds typical shapes and exact copies, not everything.** It checks each file on your disk for: your `.env` values (8 characters or more) copied exactly, the key formats listed above, and secrets written after their name; and it refuses any text file Hermes' export would change. It does not catch a key that was retyped in lower case, split across lines, cut short, encoded (base64 of a key looks like any hash and only gets a warning), or in a format neither this tool nor Hermes knows. Read your memories before you share a bundle.
- **Hermes edits text on export, and that blocks the export.** `hermes profile export` itself rewrites secret-looking text inside text files (first 6 and last 4 characters, or `***`, or `[REDACTED PRIVATE KEY]`). Any such change refuses the export, including for harmless lookalikes such as `SECRET=your-webhook-secret-here`; edit the file, then export again. `config.yaml` is checked on its own terms and ships unchanged.
- **Some false alarms.** A memory that quotes a long hash gets a named warning and a `[y/N]` question.

## Related docs

- [README](../README.md): install the tool and run setup.
- [T0 spike results](spike/T0-results.md): verified Hermes behavior the tool relies on.
