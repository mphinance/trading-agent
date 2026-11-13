# T0 spike results (Hermes)

Run date: 2026-11-12. Hermes was exercised on two versions: the installed v0.21.7 (2026.9.24, git 0b89435e7e) first, then the latest stable v0.23.1 (2026.9.24, 3b4215785882) after `hermes update`. Every behavioral check below gave identical results on both unless noted. Throwaway profiles were built in a scratch `HERMES_HOME` from a scratch distribution (DISTRIBUTION_LAYOUT.md sections 2 to 4). No credential file was read; only bogus keys were used; no order tool was called. Paths are written relative to `<HERMES_HOME>`, which defaults to `~/.hermes`.

Legend: VERIFIED = run on the real binary. SOURCE = read from the hermes-agent checkout, not run. UNVERIFIED = not checked.

## Answers

1. **HERMES_PIN** = `hermes_requires: ">=0.23.0"`. ANSWER. How verified: syntax from `check_hermes_requires` in `hermes_cli/profile_distribution.py` (SOURCE), then run on v0.23.1 (VERIFIED): `>=0.23.0`, `>=0.23.1` and bare `0.23.0` install; `>=0.24.0` fails with "This distribution requires Hermes >=0.24.0, but you have 0.23.1."; `~=0.23` fails with "Unparseable version". Operators are `>= <= == != > <` only, a bare version means `>=`, and only major.minor.patch is compared (SOURCE: `_parse_semver` strips pre-release and build suffixes). VERIFIED by calling the real functions on v0.23.1: `_parse_semver('0.21.7+canary.20261012T072214Z-134-g0b89435e7e')` returns `(0, 21, 7)`, `_parse_semver('v0.23.1')` returns `(0, 23, 1)`, `_parse_semver('0.24.0-rc1')` returns `(0, 24, 0)`; `check_hermes_requires('>=0.23.0', <that canary string>)` raises "This distribution requires Hermes >=0.23.0, but you have 0.21.7+canary...", and `>=0.21.0` against it passes. `>=0.23.0` matches the production floor and is satisfied by 0.23.1; v0.23.0 itself was not run (UNVERIFIED, but the logic is a plain tuple compare). Latest stable at run time was v0.23.1. Impact: Task 12 writes this string into `distribution.yaml`; Task 8 must not use `~=`, `^` or ranges with commas. Wizard preflight can reuse the same rule.

2. **PROFILES_ROOT_RULE** = a profile named `<name>` lives at `<HERMES_HOME>/profiles/<name>/`, with `HERMES_HOME` defaulting to `~/.hermes`. ANSWER. How verified: install printed `Profile path: <HERMES_HOME>/profiles/aie-spike`, both with a scratch `HERMES_HOME` and with the default home (`~/.hermes/profiles/<name>`, created on demand, `hermes profile list` shows it), on both versions. Source: `get_default_hermes_root` in `hermes_constants.py`. Inside the profile `hermes -p <name>` sets the home to that directory, so `config.yaml`, `.env`, `SOUL.md`, `skills/`, `memories/` sit directly in it. A fresh install creates `distribution.yaml`, `SOUL.md`, `config.yaml`, `mcp.json` plus empty `skills/ memories/ sessions/ cron/ plans/ workspace/ logs/ home/ skins/`; no `.env` is created. Impact: Tasks 8 to 12 resolve the profile dir as `<root>/profiles/<name>` where root is `$HERMES_HOME` or the platform default; prefer parsing `Profile path:` from install output, or `hermes profile list`, as a cross-check. The install is a data-root bootstrap: with a brand new `HERMES_HOME` the first command prints "no dependency environment is committed for this install; run `hermes pm repair`" (v0.23.1) or installs an isolated runtime (v0.21.7), then works anyway; this only matters for tests with a throwaway home.

3. **MCP_LOADED_FROM** = `config.yaml` key `mcp_servers` ONLY. Profile-root `mcp.json` is NOT read. ANSWER. How verified (both versions, VERIFIED): a stdio entry in `config.yaml` `mcp_servers` shows in `hermes -p <name> mcp list` and `mcp test` connects and discovers its tool; the same entry only in profile-root `mcp.json` (valid `$schema` plus `mcpServers`, `type: stdio`) gives "No MCP servers configured" and "Server not found in config". SOURCE: the only `mcp.json` reader is the plugin loader (`hermes_cli/agent_plugins.py`) for plugin directories, not profile roots. Impact: DESIGN decision (a) is confirmed: the wizard writes servers to `config.yaml` `mcp_servers`. Controller ruling: no `mcp.json` ships (the profile-root file is inert, and `hermes profile install` does not require one; the fixture distribution used for the exact command below had none). Do not write broker entries to `mcp.json`.

4. **ENV_EXPANSION** = `${VAR}` is expanded in `mcp_servers` values (`env:` values, `args`, `url`, `headers`), resolved from the profile `.env` first, then the process environment. ANSWER. How verified (both versions, VERIFIED, stdio server that records what it received; bogus value `x`/`y`): `.env` only gives `x`; process env only gives `y`; both set gives the `.env` value (profile `.env` wins over the process env); neither set leaves the literal `${NAME}` in `args` and the child sees the variable as unset. Remote `url`/`headers` with an unset `${NAME}` fail closed before any request: "MCP server 'tradier': ${TRADIER_API_KEY} in url/headers is not set in this profile's .env or secret source". Teeth: a stdio server does NOT inherit arbitrary variables (a var set in `.env` or the process but not listed under `env:` was `None` in the child, `args` still expanded). Every secret a local server needs must be named in its `env:` map as `NAME: "${NAME}"`. `mcp test` and `mcp list` mask header values. SOURCE: `_interpolate_env_vars` and `_require_rendered_remote` in `tools/mcp_tool_config.py`. Keyring injection was not tested (UNVERIFIED). Impact: Task 11 writes names only (`${NAME}`) to `config.yaml` and values to the profile `.env`; Task 11 must list each secret under `env:` (stdio) or `headers:`/`url` (remote); Task 9 detectors need no `${}` handling.

5. **UPDATE_PRESERVATION** = `hermes profile update <name>` preserves `config.yaml` even when it is listed in `distribution_owned`; it overwrites `SOUL.md` and `mcp.json`; leaves `.env`, `memories/` and a user-added `skills/<dir>` alone. ANSWER. How verified (both versions, VERIFIED): distribution `distribution_owned: [SOUL.md, config.yaml, mcp.json, distribution.yaml]` (skills and cron omitted). After install, edited `config.yaml` (added `mcp_servers` and `auth`), appended to `SOUL.md`, added `skills/my-skill/SKILL.md`, a memory file and `.env`. The next distribution version changed SOUL.md and config.yaml defaults and bumped the version. After update: `config.yaml` untouched (new default ignored), `SOUL.md` replaced by the shipped one (user edit lost), `mcp.json` replaced by the shipped one (a hand-written entry was wiped), skills folder, memory file and `.env` intact, `distribution.yaml` shows the new version. Impact: Task 12 can omit `skills` and `cron` from `distribution_owned` as planned; keep `config.yaml` there if desired since it is preserved anyway (only `--force-config` overwrites it; the CLI help says so). Task 13 docs must tell users that SOUL.md edits do not survive an update. Surprise: none beyond "install of an existing profile errors out and points to `update` or `--force`".
   Also verified: `env_requires` entries with `required: false` AND `required: true` and no value set do NOT make `profile install` fail; it only prints a "copy `.env.EXAMPLE` to `.env`" hint and writes `.env.EXAMPLE` (optional entries as commented lines). Impact: FIRST_RUN section 8 item 5 is answered; setup never fails on `env_requires`.

6. **ADOPT_LOGINS_KEY** = `auth.adopt_external_logins: false` in the profile's `config.yaml` (nested `auth:` mapping, boolean, default true). ANSWER. How verified: SOURCE `adopt_external_logins_enabled()` in `agent/credential_sources.py` reads `config["auth"]["adopt_external_logins"]` via the active profile's config (default true when absent). VERIFIED on both versions: `hermes -p <name> config set auth.adopt_external_logins false` writes exactly that nested key into the profile's `config.yaml` (so the CLI covers this key and a direct YAML edit is equivalent), and with the process home pointed at the profile directory the function returns `True` before and `False` after. A full chat run was not made (no model call), so end-to-end suppression of a sibling login is UNVERIFIED beyond the switch function. Impact: Task 11 writes `auth: {adopt_external_logins: false}` into the new profile's `config.yaml` (YAML edit or `hermes -p <name> config set ...`), never into the global home.

7. **INSTALL_CMD** = Linux, macOS, WSL2: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`; Windows PowerShell: `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`. ANSWER. How verified: copied from the "Quick Install" section of the hermes-agent README; the script was NOT run (SOURCE only). Termux uses a signed APT repo instead (separate guide). Afterwards `source ~/.bashrc` and run `hermes`. Upgrade command: `hermes update` (git installs; `--check` reports availability, `--plan` shows the plan, `--yes` is non-interactive, `--no-backup` skips snapshots, `--no-gateway-restart` leaves running gateways alone). Pinning and upgrading (SOURCE unless marked): `install.sh --help` lists `[--channel stable|main] [--branch NAME] [--commit SHA] [--dir PATH] [--hermes-home PATH] [--non-interactive]`; the default channel is `stable` (the latest published release), `--commit SHA` pins an exact commit, and there is no `--version` flag, so a specific release is reached through `--commit` (or by letting `stable` resolve; `hermes update --check` printed "Selected release available: stable v0.23.1"). Arguments reach a piped script as `curl -fsSL <url> | bash -s -- --channel stable`. To satisfy `>=0.23.0` on an older install the only documented route for a git install is `hermes update` (VERIFIED: v0.21.7 to v0.23.1 with `hermes update --yes --no-backup --no-gateway-restart`, exit 0; `hermes --version` prints `Install method: git`). `hermes update --plan` names other install kinds (docker, nix); the README also lists a separate MSIX/App Installer package on Windows (its updates are owned by that package) and a Termux APT repo (`pkg install hermes-agent`, per `install.sh`). For those, `hermes update` is not the upgrade path; their update procedure was NOT tested (UNVERIFIED). Task 8's doctor hint should say: "Hermes >=0.23.0 is needed; for a normal install run `hermes update`, otherwise upgrade it the way you installed it", and read the install kind from the `Install method:` line of `hermes --version` (VERIFIED line format: `Install method: git`). Impact: Task 8 doctor hint text and README prerequisites cite this command; never run it from the tool.

8. **BROKER_MCP_LOADING** = a remote broker and a local broker, each from its catalog page, wired via `config.yaml` `mcp_servers` with `${VAR}` references and bogus keys, both list as enabled and connect and discover tools; failure on a bad key is NOT visible at connect time. ANSWER. How verified (both versions, VERIFIED): remote Tradier (`url: https://mcp.tradier.com/mcp`, headers `API_KEY: "${TRADIER_API_KEY}"` and `PAPER_TRADING: "true"` per its page): `mcp list` shows enabled, `mcp test` connects (about 1 to 2 s) and discovers 21 tools including order tools, with a bogus key, so listing needs no valid credential. Local Alpaca (`command: uvx`, `args: [alpaca-mcp-server]`, `env` with `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` as `${...}`, per its page): connects (about 5 s, first run fetches the package into the uv cache) and discovers 72 tools. Clean failure observed: with the key variable absent from `.env`, Tradier fails closed with the named-variable message above, no network request sent. Bad-key rejection would only appear on an actual tool call, which was not made (no broker API call beyond the connect/list handshake). Kraken (local) was skipped because its page requires a `curl | sh` installer. Impact: Task 10/11 treat "connect OK" as "entry loads", not "credentials valid"; do not report keys as verified from `mcp test`. Tool discovery exposes order tools to the agent regardless of keys.
   **Approval question**: does an MCP tool call raise `approval.request` over `hermes` runs? ANSWER (SOURCE only, no model run): by default no. `tools/mcp_tool.py` gates a call only when the server entry sets `trust: untrusted` (then every call whose discovery-time `readOnlyHint` is not exactly true needs approval; missing `trust` means full trust and no gate). The `approval.request` event itself exists in the API-server runs bridge (`gateway/platforms/api_server.py`). Runtime behavior UNVERIFIED. No policy is built on this.

## Exact commands, output and YAML (VERIFIED on v0.23.1; the v0.21.7 run gave the same text)

All commands below were run with a throwaway `HERMES_HOME`; `<DIST>` stands for the distribution directory.

### Install call

```
hermes profile install <DIST> --name <name> -y
```

- Source argument: a local directory path (absolute, or `.` run from inside the directory; both VERIFIED) or a git URL (`github.com/user/repo`, `https://...`, `git@...`, per `hermes profile install --help`, URL form not run). `distribution.yaml` must be at its root.
- `-y/--yes` is required for non-interactive use. Without it Hermes prints the manifest preview and "Proceed with install? [y/N]"; with stdin closed that gave "Install cancelled." and exit code 0 with NO profile created. A wrapper must not treat exit 0 as success; confirm the profile exists (parse `Profile path:` or check the directory).
- Success: exit 0, stdout (leading blank line, then):

```
Distribution: <manifest name> v<version>
  <description>
  Requires: Hermes <hermes_requires>
  Source:   <source as given>
  Target:   <HERMES_HOME>/profiles/<name>

✓ Installed '<name>' v<version>
  Profile path: <HERMES_HOME>/profiles/<name>

  Use with:      hermes -p <name> chat
```

  The marker before "Installed" is U+2713. Parse with the regex `^\s*Profile path:\s*(.+)$`. When `env_requires` is non-empty an extra "Next: copy .env.EXAMPLE to .env" block follows.
- Failures print a single line starting `Error:` on STDOUT (stderr was empty) and exit 1: existing profile ("Error: Profile '<name>' already exists at <path>. Use `hermes profile update` to upgrade in place, or pass --force to overwrite."), reserved name ("Error: Cannot install a distribution as 'default' ..."), too-new Hermes ("Error: This distribution requires Hermes >=9.0.0, but you have 0.23.1."). Other reserved names (`hermes`, `test`, `tmp`, `root`, `sudo`) were not run (UNVERIFIED, those come from the design doc).
- First command against a brand new `HERMES_HOME` also prints dependency-preparation lines before the preview ("Preparing the isolated Hermes runtime...", "Installing Python dependencies"); they are noise, not errors.
- Update: `hermes profile update <name> -y` prints `✓ Updated '<name>' → v<version>` (U+2192 arrow) and exits 0. Check: `hermes profile list` shows `<name>  ...  <name>@<version>` in the Distribution column.

### `config.yaml` shapes accepted by Hermes

One stdio and one remote entry, plus the adopt switch, exactly as written to the profile `config.yaml` and loaded (no `enabled` key was needed; `mcp list` showed both as "enabled"):

```yaml
model: {}
auth:
  adopt_external_logins: false
mcp_servers:
  localdemo:
    command: python3
    args: [<path to server script>]
    env:
      DEMO_TOKEN: "${DEMO_TOKEN}"
  remotedemo:
    url: https://mcp.tradier.com/mcp
    headers:
      API_KEY: "${TRADIER_API_KEY}"
      PAPER_TRADING: "true"
```

- A stdio entry carries `command`, `args` and an `env:` map that names EVERY secret as `NAME: "${NAME}"` (children do not inherit unlisted variables). A remote entry carries `url` and `headers`; transport is chosen by the presence of `url` versus `command`. Values for `${...}` go in the profile `.env` (`NAME=value` lines).
- `hermes -p <name> config set auth.adopt_external_logins false` prints `✓ Set auth.adopt_external_logins = False in <profile>/config.yaml` and exits 0; it creates the nested `auth:` mapping shown. Direct YAML edit is equivalent.
- Check calls: `hermes -p <name> mcp list` (exit 0) prints a table with columns `Name  Transport  Tools  Status` and a row per server such as `localdemo  python3 <path>...  all  ✓ enabled`; with none it prints "No MCP servers configured." `hermes -p <name> mcp test <server>` prints `Transport: stdio → python3` or `Transport: HTTP → <url>`, header values masked as `***`, then `✓ Connected (<ms>ms)` and `✓ Tools discovered: <n>` followed by one line per tool, exit 0; a failed connect prints `✗ Connection failed (...)` ; an unknown name prints "✗ Server '<n>' not found in config." with an "Available:" list and exits 3. The exit code of a failed connect was not captured (UNVERIFIED); parse the `✓ Connected` line rather than relying on it.

## Cleanup

All `aie-spike*` profiles and both scratch data roots were deleted, as was the scratch distribution, scripts and logs in the temp directory. A deleted default-home profile leaves a tombstone directory under `<HERMES_HOME>/profiles/`; that was removed too. The uv cache entry for the Alpaca server was cleaned. Hermes itself was upgraded v0.21.7 to v0.23.1 with `hermes update --yes --no-backup --no-gateway-restart`; a running gateway was deliberately not restarted.

## Gotchas for later tasks

- `hermes profile install` refuses an existing profile (use `update`, or `--force`, which preserves user data).
- `mcp_servers` appended to `config.yaml` must be a top-level key; appending text under a trailing `auth:` block silently nests it wrongly (the profile then lists no servers). Use a YAML library to merge, never a text append.
- Unset `${NAME}` in a stdio `args` stays literal; in remote `url`/`headers` it hard-fails with a message that names the variable.

## Related docs

- [Setup](../SETUP.md): the user-facing install flow built on these facts.
- [Updating](../UPDATING.md): the update rules in plain language.
- [Docs index](../README.md): all pages.
