# Updating

> [!NOTE]
> Status: update rules verified on Hermes 0.23.1 in a scratch home. The one-time source fix below was verified with a local folder as the source, not a git URL.

**How to update the profile's shipped files with `hermes profile update`, what it overwrites, and what it keeps.**

## At a glance

| File or folder | On `hermes profile update` |
|---|---|
| `SOUL.md` | Overwritten by the shipped one. Your edits are lost |
| `distribution.yaml` | Overwritten, with the new version |
| `config.yaml` | Kept. Your provider choice and broker entries stay |
| `.env` (keys) | Kept |
| `memories/`, `skills/` you added, `sessions/` | Kept |

`distribution_owned` in `distribution.yaml` is `[SOUL.md, config.yaml, distribution.yaml]`. `config.yaml` is listed so Hermes copies it on install, but update still keeps your edited one; only `--force-config` replaces it.

## The one catch: the recorded source

`hermes profile update` fetches the distribution from the source recorded in the profile's `distribution.yaml` (the `source:` line). Setup installs from a temporary copy of the packaged files, and that copy is deleted when setup ends. After that, update fails:

```text
Error: Cannot resolve distribution source: '<temporary folder>'. Expected a git URL (e.g. github.com/user/repo) or a local directory.
```

Fix it once per profile: point the recorded source at the repository address (or at a folder holding the new version), then update.

```bash
grep -n "^source:" ~/.hermes/profiles/<profile>/distribution.yaml
```

Edit that one line to `source: <repo-url>` in a text editor, then:

```bash
hermes profile update <profile>
```

```text
✓ Updated '<profile>' → v<version>
```

Update re-records the source it used, so you do this once. A profile made by `trading-agent import` has no `distribution.yaml` at all, so `hermes profile update` does not apply to it (see [MOVING](MOVING.md#honest-limits)).

## What not to do

- Do not use `hermes profile install <source> --name <profile> --force` to update. On Hermes 0.23.1 it kept `.env` and `memories/` but reset `config.yaml` to the shipped defaults in a scratch test, which would wipe your provider and broker settings.
- Do not point `hermes profile install` or `update` at a development checkout. It holds `.venv` symlinks and Hermes refuses them (see [SETUP](SETUP.md#why-not-run-it-from-a-checkout)).
- Do not put hand edits you care about in `SOUL.md`. Keep them in `config.yaml` personalities, or in a skill.

## Updating the tool

```bash
uv tool upgrade trading-agent
```

This updates the command only. The profile files in an existing profile change only when you run `hermes profile update`.

## Updating Hermes

Hermes >= 0.23.0 is required. `hermes update` upgrades a normal install; the tool never runs it for you.

## Related docs

- [Setup](SETUP.md): first install.
- [Moving](MOVING.md): why an imported profile cannot be updated this way.
- [T0 spike results](spike/T0-results.md): the verified update behavior.
- [README](../README.md): front door.
