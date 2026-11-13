# Keys

> [!NOTE]
> Status: built and tested against a Hermes shim and Hermes 0.23.1 on Linux. Not yet used by outside users.

**Where this tool puts your keys, who can read them, and how to remove or replace one.**

## At a glance

| Question | Answer |
|---|---|
| Where is a key stored? | The new profile's private `.env` file, and nowhere else |
| What does `config.yaml` hold? | `${NAME}` references, never a value |
| Who can read the file? | Your user account only (mode 0600), written atomically |
| Is a key ever printed? | No. Prompts are hidden and messages name the setting, never the value |
| Is a key from my environment reused? | Only when you say yes, for one named use at a time |
| Does export or import carry keys? | Never |

## Where the file is

```text
<HERMES_HOME>/profiles/<profile>/.env
```

`HERMES_HOME` defaults to `~/.hermes`. One line per setting, `NAME=value`. Hermes resolves `${NAME}` in the profile's `config.yaml` from this file first, then from the process environment, so a name set in both uses the `.env` value.

The OS keyring is not used: core Hermes does not read provider keys from it, so a key stored there would silently never arrive.

## How a key gets in

1. You paste it at a hidden prompt. Nothing you type is shown.
2. Or the wizard found a variable of that name in your environment and asked "Copy it into the new profile's private `.env` file so X can use it?" You said yes. The default is not to copy.
3. Some values are not secrets (a mode such as `paper` or `live`, `true`, `0`, `1`). Those are asked in plain text and say so.

A profile keeps one value per name. If two parts of your setup need the same name, the wizard asks before sharing it: share the value, enter a different one, or leave that part out. If the name already exists in the profile's `.env`, it asks whether to keep or replace it; it never overwrites silently.

A key it finds as a reference only (for example a value that points elsewhere) is not copied: "Make a new key for this agent."

## What is in `config.yaml`

References such as `${ALPACA_API_KEY}`. A broker that runs as a local program gets its secrets through an explicit `env:` map written as `NAME: "${NAME}"`; Hermes does not pass other variables to it.

## Removing keys

| Goal | How |
|---|---|
| Remove one key | Open the profile's `.env`, delete that line, save. Then revoke the key at the provider; deleting a line does not revoke it |
| Replace one key | Edit the line, or re-run setup under a new profile name |
| Remove everything setup made | `trading-agent undo --profile <profile>` |

```bash
trading-agent undo --profile <profile>
```

Undo uses the setup receipt and an ownership marker inside the profile. It removes the profile it created (including its `.env`) and Hermes' command alias for it. If it cannot prove it made a path, it leaves that path alone and says so.

> [!CAUTION]
> Undo deletes the profile's memories along with its keys. Export first if you want to keep them: [MOVING](MOVING.md).

## Export and import

Neither carries a key. The export refuses a file in the profile that matches a value from your `.env` or a common key shape, and the import asks for each key by name. The scan is best effort and cannot catch every way to write a secret; read your memories before you share a bundle. The exact rules are in [MOVING](MOVING.md#step-1-export).

## Detection never reads values for display

The scan reports that a variable, file or port exists. Only names and presence are shown. Anything it reads to compare (for example, to warn that a memory holds a key) stays in memory and is never written to a message.

## Related docs

- [Setup](SETUP.md): the wizard that asks for keys.
- [Moving](MOVING.md): what export and import check, and the limits.
- [README](../README.md): what stays on your machine.
