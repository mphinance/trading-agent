# Updating

> [!NOTE]
> Status: update rules verified on Hermes 0.23.1 in a scratch home. Install from a package folder, then update, was verified on Hermes 0.23.1 with a real wheel in a scratch home. A git URL as the source was not tried: [UNVERIFIED]; to check, run `hermes profile update` on a profile whose `source:` is a repository address, in a scratch `HERMES_HOME`.

**How to update the profile's shipped files with `hermes profile update`, what it overwrites, and what it keeps.**

## At a glance

| File or folder | On `hermes profile update` |
|---|---|
| `SOUL.md` | Overwritten by the shipped one. Your edits are lost |
| `distribution.yaml` | Overwritten, with the new version |
| `config.yaml` | Kept. Your provider choice and broker entries stay |
| `.env` (keys) | Kept |
| `memories/`, `skills/` you added | Kept |

`distribution_owned` in `distribution.yaml` is `[SOUL.md, config.yaml, distribution.yaml]`. `config.yaml` is listed so Hermes copies it on install, but update still keeps your edited one; only `--force-config` replaces it.

## The recorded source

`hermes profile update` fetches the distribution from the source recorded in the profile's `distribution.yaml` (the `source:` line). When you install the tool as a package (`uv tool install`, `pipx`, a wheel), setup installs straight from the packaged folder inside that install, so the recorded source is a real folder that stays put. `uv tool upgrade trading-agent` refreshes the files in it, and `hermes profile update <profile>` then picks them up. Nothing to edit.

Two cases are different:

- **A zipped install.** Setup has to unpack the files into a temporary folder, and that folder is deleted when setup ends. Setup prints a note when this happens. After that, update fails:

  ```text
  Error: Cannot resolve distribution source: '<temporary folder>'. Expected a git URL (e.g. github.com/user/repo) or a local directory.
  ```

  Fix it once per profile: point the recorded source at the repository address (or at a folder holding the new version).

  ```bash
  grep -n "^source:" ~/.hermes/profiles/<profile>/distribution.yaml
  ```

  Edit that one line to `source: github.com/mphinance/trading-agent` in a text editor, then run `hermes profile update <profile>`. Update re-records the source it used, so you do this once. Hermes accepts a git URL such as `github.com/user/repo` there (its own error message says so); whether it can take a branch in `source:` is [UNVERIFIED], so until the maintainer makes `portable-agent` the default branch, this address resolves to the older project: use a local folder holding the new version instead, or wait for the swap. To check, run `hermes profile update` in a scratch `HERMES_HOME`.
- **A checkout.** Running from a working copy is not supported for setup (see [Setup](SETUP.md#why-not-run-it-from-a-checkout)). If you installed from a clean clone anyway, the recorded source is that clone's folder; keep it where it is, or set the source to the repository address as above.

Hermes first prints where it will update from and asks `Proceed? [y/N]`; the default is No, so pressing Enter cancels with `Update cancelled.`. Answer `y`, or pass `-y` to skip the question: `hermes profile update <profile> -y`. A wrong or imported profile name gives `Profile '<name>' is not a distribution (no distribution.yaml)`. A successful update prints:

```text
✓ Updated '<profile>' → v<version>
```

A profile made by `trading-agent import` has no `distribution.yaml` at all, so `hermes profile update` does not apply to it (see [Moving](MOVING.md#honest-limits)).

## What not to do

- Do not use `hermes profile install <source> --name <profile> --force` to update. Its effect on `config.yaml` is [UNVERIFIED] here: the spike notes say `--force` preserves user data, while an earlier scratch test reportedly reset `config.yaml` to the shipped defaults, which would wipe your provider and broker settings. To check, try it in a scratch `HERMES_HOME`. `hermes profile update` is the path that was verified.
- Do not point `hermes profile install` or `update` at a development checkout. It holds `.venv` symlinks and Hermes refuses them (see [Setup](SETUP.md#why-not-run-it-from-a-checkout)).
- Do not put hand edits you care about in `SOUL.md`; update overwrites it. Keep them in `config.yaml` personalities, or in a skill.

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
- [Docs index](README.md): all pages, in reading order.
