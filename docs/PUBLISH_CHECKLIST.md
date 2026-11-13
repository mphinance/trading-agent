# Publish checklist

> [!NOTE]
> Status: open. Nothing here is done until a box is ticked by the maintainer. The repository is intended to be public.

**What must be true before this repository is made public, with the exact commands that check it.**

## At a glance

- 13 open items need a human decision (lineage, license, images, contact, name, risk wording).
- 5 checks are run and read: local paths, dashes, secret shapes, tests, and a command check.
- Nothing here is run by automation. Tick a box only after you looked.

## In this doc

- [Open decisions](#open-decisions)
- [Checks to run](#checks-to-run)
- [Final steps](#final-steps)

## Open decisions

Tick a box only after the maintainer decided it.

### Licensing and attribution

- [ ] **License confirmed by the maintainer.** MIT is provisional (`LICENSE`, `distribution.yaml`, `pyproject.toml`). Confirm or change in all three.
- [ ] **alpha-skills lineage resolved.** `THIRD_PARTY.md` says the README credits "TraderMonty" and several skills name it directly; the upstream, its license and which skills are copies are unverified. Until resolved, alpha-skills stays `listed: false` and is never offered in the wizard.
- [ ] **`THIRD_PARTY.md` complete.** Every library the wizard can point at has an entry with source, license and attribution; the lineage entry above is added.
- [ ] **GitHub license detector.** GitHub's detector shows no license for alpha-skills, momentum-mcp and awesome-broker-mcp although LICENSE files exist locally. Check each repository's license file is committed and recognised.
- [ ] **Image provenance checked.** About 3.3 MB of bundled images in the skills packs (for example in `sector-analyst` and `breadth-chart-analyst`) have no recorded origin. Nothing is shipped here, but any listed library that carries them needs an answer.
- [ ] **Promo-text note.** Free-tier output from momentum-mcp may carry vendor promotional text (exact output unverified). Confirm what it prints, or keep the note in `THIRD_PARTY.md` and in the library's wizard entry.
- [ ] **EDGAR client origin.** momentum-mcp's EDGAR client says it was ported from another repository; the origin license is unverified.

### Repository and contact

- [ ] **`SECURITY.md` contact channel.** Add a real reporting address or private advisory link; none is in the repository yet.
- [ ] **Name and repository settings.** Confirm the project name, the repository address used in the install commands, description, topics, default branch, branch protection and issue settings.

### Product scope

- [ ] **Risk and terms wording reviewed.** The README, SETUP and KEYS pages carry plain warnings: no trading rules or risk controls, a broker connector can place live orders, not financial advice, the profile `.env` is readable by the agent, bundles are unencrypted. Confirm the wording, and decide whether broker and AI service names in the catalog and wizard need a trademark or terms-of-service review. Detection of signed-in CLIs is detect-only by design; no vendor terms were checked, so that remains [UNVERIFIED].
- [ ] **More brokers curated.** Only Alpaca and Tradier are installable. Decide whether to curate more overlay entries before launch or ship as is.
- [ ] **CLI-powered brain follow-up.** A found signed-in AI CLI cannot power the Hermes brain in this version. Decide whether to document it as a known limit or schedule the follow-up.
- [ ] **Outside-user pass.** Someone who has not seen the project follows [SETUP](SETUP.md) on a clean machine and reports where it broke. Until then the README status line stays "not yet used by outside users".

## Checks to run

Run these from the repository root. The output is the evidence; do not tick a box on a guess.

### Local paths

```bash
git grep -n "/home/" -- . ':!docs/PUBLISH_CHECKLIST.md'
```

Expected hits only: a documentation URL path in the catalog (`https://docs.kraken.com/home/mcp`) and fake paths in test fixtures. Any real home folder, hostname or address is a failure.

Private addresses, and your own machine names (add them to the pattern yourself; they are not listed here because this page is public):

```bash
git grep -nE '\b(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]+\.[0-9]+' -- . ':!tests' ':!uv.lock'
```

Expected output: nothing.

- [ ] Local-path and private-address checks read and clean.

### Dashes

The pattern uses byte escapes so this page contains no dash character itself. It matches the em dash (UTF-8 bytes E2 80 94) and the en dash (E2 80 93).

```bash
git grep -nP '\xE2\x80[\x93\x94]'
```

```text
(no output)
```

The same check with Python, also written with escapes only:

```bash
python3 -c "import subprocess; r=subprocess.run(['git','ls-files'],capture_output=True,text=True).stdout.split(); bad=[f for f in r if any(c in open(f,errors='ignore').read() for c in (chr(0x2014),chr(0x2013)))]; print(bad)"
```

```text
[]
```

- [ ] Dash check clean (both forms print nothing or an empty list).

### Secret shapes

```bash
git grep -nE '(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[bp]-[0-9A-Za-z]|-{5}BEGIN [A-Z ]*PRIVATE KEY)' -- . ':!tests'
```

Expected output: nothing. The `tests/` folder is excluded because it holds deliberately fake key shapes that the export checks must catch. Review that folder once by eye: every match must be an obviously fake value.

- [ ] Secret-shape check clean outside `tests/`; `tests/` matches reviewed as fake.
- [ ] No `.env`, `auth.json` or key file is tracked: `git ls-files | grep -E '(^|/)(\.env|auth\.json|.*\.pem|.*\.key)$'` prints nothing.

### Tests

```bash
uv run pytest -q
```

- [ ] Test suite passes.

### Command check

- [ ] Every command printed in the docs exists: run `trading-agent <command> --help` for `setup`, `undo`, `scan`, `export`, `import` and `catalog build`, and compare the flags with the docs.

## Final steps

- [ ] After the swap makes `portable-agent` the default branch, drop the `@portable-agent` suffix from every install command (`git grep -n "@portable-agent"`), change the `source:` note in UPDATING, and re-run `tests/test_docs.py`.
- [ ] README status line still matches reality (built, tested against the shim and Hermes 0.23.1, not yet used by outside users).
- [ ] The swap in [SWAP.md](SWAP.md) is done only after every box above is ticked, and only by the maintainer.

## Related docs

- [Swap procedure](SWAP.md): replacing the old repository and folder.
- [Third-party notices](../THIRD_PARTY.md): lineage and licenses.
- [Docs index](README.md): all pages.
