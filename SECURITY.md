# Security policy

> [!NOTE]
> Status: early release, one maintainer, no bug bounty. Reports are handled on a best-effort basis.

**How to report a security problem in `trading-agent`, and what counts as one.**

## Report it privately

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. That opens a private advisory only the maintainer can read. Do not post details in a public issue, a pull request or a chat room first.

If the **Report a vulnerability** button is missing, the maintainer has not switched the feature on yet. Open a public issue that says only "I have a security report, please enable private reporting", with no details, and wait for a reply.

## What to include

- The command you ran and the version (`trading-agent --help` shows the commands; `pip show trading-agent` or `uv tool list` shows the version).
- The Hermes version (`hermes --version`) and your operating system.
- What you expected, what happened, and the smallest steps that reproduce it. Use fake keys such as `sk-EXAMPLE`; never send a real key.

## What counts

| In scope | Examples |
|---|---|
| A key value reaching somewhere it should not | printed output, a bundle, a receipt or state file, a log, a child process environment |
| A file written or deleted outside the new profile | `setup`, `undo`, `import` or `export` touching anything they did not create |
| A hostile bundle or catalog entry running code or sending a key somewhere without being refused or shown | `import` of a crafted `.tgz`, a crafted `config.yaml` |
| A consent bypass | a detected key used by something the user did not agree to |

Out of scope: problems in Hermes itself (report those to the [Hermes project](https://github.com/NousResearch/hermes-agent)), in a broker connector you chose to install, or in a third-party skill library. Also out of scope: anything that needs write access to your own home directory or an already compromised machine, because profiles are not a security sandbox (the agent has your filesystem access).

## What to expect

A reply within about a week, a fix or a plain written decision, and credit in the release notes if you want it. There is no service-level promise.

## Related docs

- [Keys](docs/KEYS.md): where keys live and how to remove them.
- [Moving a profile](docs/MOVING.md): what export and import check, and what they do not.
- [Publish checklist](docs/PUBLISH_CHECKLIST.md): the maintainer's open items, including this contact channel.
