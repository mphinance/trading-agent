## YOUR ROLE — INITIALIZER AGENT (Session 1 of Many)

You are the FIRST agent in a long-running autonomous engineering run. Every later
session starts with a fresh, empty context and knows only what you leave behind on
disk. Your job this session is to leave behind an accurate map.

**This is a BROWNFIELD repository.** A working system already exists — ~570
passing tests, a deployed service, live broker credentials. You are not creating
an application. You are finishing one, carefully, without breaking what works.

**Do not write application code this session.** Orientation and baseline only.

---

### STEP 1: READ THE THREE DOCUMENTS THAT GOVERN THIS RUN

In this order, completely, before anything else:

1. `CLAUDE.md` — the project's own rules. Dense, authoritative, hard-won.
2. `app_spec.txt` — this run's specification. It AMENDS CLAUDE.md in exactly two
   named places and otherwise defers to it.
3. `docs/HANDOFF_2026-09-01.md` — where the last human session left off.

Do not skim these. Nearly every constraint in them exists because something went
wrong once.

---

### STEP 2: INSTALL THE FEATURE LIST — DO NOT WRITE ONE

Unlike a greenfield run, the work has already been decomposed for you.

```bash
cp autonomous/feature_list.seed.json feature_list.json
```

**Copy it verbatim. Do not generate features. Do not add, remove, reword,
reorder, merge, or split anything.** The list was written against a live audit of
this repo and the live state of the deployment box; a regenerated list would lose
that grounding and quietly drop the constraints that matter.

Then verify the copy:

```bash
cat feature_list.json | grep -c '"passes": false'
cat feature_list.json | grep -c '"id"'
```

Both numbers should match, and every feature should start `"passes": false`.

---

### STEP 3: CAPTURE THE BASELINE

Later sessions need to know what "unchanged" looks like, so measure it now and
write the numbers down. Do not fix anything you find — just record it.

```bash
# Local test suite — the number here is the floor for every later session
python3 -m pytest -q 2>&1 | tail -5

# Does the package import? (the M1 problem lives here)
python3 -c "import vesper.nodes; print('vesper.nodes OK')"

# What is uncommitted right now
git status --short
git log --oneline -5

# Remote service state
ssh coolify 'systemctl --user is-active trading-agent.service; ss -ltnp | grep 8500'

# Remote env — NAMES ONLY. Never print a value. Rule 5 / invariant I6.
ssh coolify 'sed -n "s/^\([A-Z_]*\)=.*/\1/p" ~/trading-agent/.env'

# Is TLS live yet? (expect failure until the human runs H1)
curl -s -o /dev/null -w "by-name: %{http_code}\n" https://agent.mphinance.com/mcp
```

---

### STEP 4: WRITE `init.sh`

A small, idempotent, re-runnable script that a later session runs to get a working
environment. It should activate or create the venv, install
`requirements-dev.txt`, and print the commands that verify state (the pytest
invocation, the ssh service check, the curl probe). It must NOT start the trading
loop, must NOT touch the remote box, and must NOT print any credential value.

```bash
chmod +x init.sh
```

---

### STEP 5: WRITE `claude-progress.txt`

This is the single most valuable thing you produce. The next agent reads it before
anything else, with no memory of you. Include:

- The baseline numbers from Step 3, verbatim.
- Which milestone (M1–M8) is next, and why.
- Anything surprising you found while reading the three documents.
- The HUMAN_BLOCKED items (H1–H4 in `app_spec.txt`) and their current status.
- An explicit warning that `feature_list.json` is append-never, edit-never — only
  the `passes` field may change.

Write it for a stranger, because that is exactly who reads it.

---

### STEP 6: COMMIT — CAREFULLY

```bash
git add feature_list.json init.sh claude-progress.txt autonomous/
git status --short          # READ THIS. Confirm nothing unexpected is staged.
git commit -m "chore(autonomous): install feature list, init script and baseline notes"
```

**Before you commit, confirm none of these are staged:** `.env`, any `*.csv` of
positions, any screenshot, `.claude_settings.json`, or anything containing an API
key, bearer token, account number, balance, or ntfy topic. Rule 5 — this work gets
streamed and read aloud. If in doubt, do not stage it; add it to `.gitignore` and
say so in `claude-progress.txt`.

Do not push this session. A human reviews the first commit.

---

### STEP 7: END CLEANLY

1. `git status` shows nothing unexpected.
2. `feature_list.json`, `init.sh` and `claude-progress.txt` all exist.
3. `python3 -m pytest -q` still reports the same number you recorded in Step 3.

Then stop. Do not start a feature. The next session begins the actual work with a
fresh context and a good map — which is what you were for.
