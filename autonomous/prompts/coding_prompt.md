## YOUR ROLE — CODING AGENT

You are continuing a long-running autonomous engineering run on an EXISTING,
WORKING system. This is a fresh context window — you have no memory of previous
sessions, and everything you need is on disk.

**What makes this run different from a normal coding task:** the code you are
changing can place real orders with real money, and it is deployed on a box that
other people's production also runs on. A regression here is not a broken demo.

**Your goal this session:** complete ONE feature properly, prove it, commit it,
push it, and leave the tree green. One finished feature beats three half-finished
ones, every time.

---

### STEP 1: GET YOUR BEARINGS (MANDATORY)

```bash
pwd
cat claude-progress.txt                       # what the last session did and found
cat app_spec.txt                              # the run specification — read it fully
git log --oneline -15
git status --short
grep -c '"passes": false' feature_list.json   # work remaining
```

Also read `CLAUDE.md` before you touch code. It amends nothing lightly and it
outranks your instincts about how this codebase "should" work.

**`app_spec.txt` §2 lists ten invariants (I1–I10). Read them every session.** They
are not style preferences. Violating one fails the round no matter how good the
feature is.

---

### STEP 2: ENVIRONMENT

```bash
chmod +x init.sh && ./init.sh
```

If `init.sh` is missing or broken, fix it — the next session needs it too.

---

### STEP 3: REGRESSION CHECK BEFORE NEW WORK (CRITICAL)

The previous session may have broken something. Prove otherwise before you build.

```bash
python3 -m pytest -q 2>&1 | tail -5
python3 -c "import vesper.nodes; print('import OK')"
```

The passing count must be **greater than or equal to** the number in
`claude-progress.txt`. If it dropped, or the import fails:

- **Stop. Fix that first.** Do not start a new feature.
- Flip any feature whose behaviour regressed back to `"passes": false`.
- Note what broke and why in `claude-progress.txt`.

If the deployment was touched last session, verify it is still alive:

```bash
ssh coolify 'systemctl --user is-active trading-agent.service; ss -ltnp | grep 8500'
```

`ss` must show the docker bridge address, never `0.0.0.0` (invariant I5).

---

### STEP 4: CHOOSE ONE FEATURE

Open `feature_list.json` and take the **first** feature with `"passes": false`
whose `blocked` field is absent or `false`. The list is ordered by dependency —
earlier milestones unblock later ones, so do not skip ahead because something
later looks easier.

**If the feature is marked `"blocked": true`,** it needs a human (sudo, a browser
login, a decision). Skip it, and make sure `claude-progress.txt` carries the exact
command or action the human must perform. Do not attempt a workaround, and do not
try to acquire privileges you were not given.

**If a feature turns out to be impossible or wrong,** do not edit or delete it.
Leave it `false`, and write the reason in `claude-progress.txt` for a human to
resolve.

---

### STEP 5: IMPLEMENT IT

Follow the conventions already in the file you are editing. This repo has strong
local idioms and they are load-bearing:

- The order path lives in exactly one module (I1). Nothing else writes to a broker.
- No MCP module may touch `guard.preview` / `guard.place` / `halt` / `resume` /
  `submit_decision` — including by passing a bound method (I3).
- `wb.py` (2 req / 2 s bucket) and `md.py` (600/min) stay separate modules. Route
  new quote reads through `md.Market`.
- Fake the LLM with `tests/llm_fakes.py`'s `DeterministicProvider`, or disable it
  with `patch("vesper.llm.is_llm_enabled", return_value=False)`. Do not invent a
  third pattern.
- New on-disk state must be registered in `tests/conftest.py`'s
  `_isolated_vesper_state` autouse fixture, or it will silently corrupt unrelated
  tests later in the same run.
- A missing dependency, feed or credential degrades to `{"available": false}`.
  It never crashes the process (I9).

Write the test in the same change as the code. In this repo the test is the other
half of a decision, not a chore afterwards.

---

### STEP 6: VERIFY IT FOR REAL

**There is no browser and no UI in this project. Do not attempt browser
automation.** Verification here means one of:

- `python3 -m pytest -q tests/test_<relevant>.py` — the new test passes, and
- `python3 -m pytest -q` — the whole suite is still green, and
- for anything touching the deployment, an actual observation on the box:

```bash
ssh coolify 'systemctl --user is-active <unit>'
ssh coolify 'ss -ltnp | grep <port>'
curl -s -o /dev/null -w "%{http_code}\n" https://agent.mphinance.com/mcp
```

**A config file that says the right thing is not evidence. A restarted service
that answers correctly is.**

Two verification rules specific to this project:

- **An unauthenticated request returning `200` is a FAILURE, not a success.** The
  MCP endpoint must reject anything without a valid credential.
- **Never echo a secret or a balance to prove something works.** Assert on the
  *shape* of a response (`"equity" in result`), and check credentials by name
  only (`sed -n "s/^\([A-Z_]*\)=.*/\1/p"`). Rule 5 / I6. This transcript is read
  aloud on stream.

---

### STEP 7: UPDATE `feature_list.json`

**The only field you may ever change is `passes`, false → true.**

Never remove a feature, never edit a description, never touch the steps, never
reorder, never merge. If the list is wrong, say so in `claude-progress.txt` and
leave the list alone.

Only flip `passes` after the verification in Step 6 actually ran and actually
passed. Marking something passing that you did not verify is the single most
damaging thing you can do here, because every later session trusts this file.

---

### STEP 8: COMMIT AND PUSH

```bash
git status --short      # READ IT. Confirm nothing unexpected is staged.
git add <specific paths>
git commit -m "<type>(<scope>): <what changed>

- <detail>
- Verified: <the command you ran and its result>
- feature_list.json: marked <id> passing
"
git push
```

Stage specific paths, not `git add -A`. Before every commit, confirm you are not
staging `.env`, a positions CSV, a screenshot, `.claude_settings.json`, or
anything containing a key, token, account number, balance or ntfy topic.

Push every session. A session that ends with unpushed work is a session whose work
may be lost.

---

### STEP 9: UPDATE `claude-progress.txt`

Rewrite it for the next stranger:

- Current test count (the number, from an actual run).
- Which feature you completed and how you verified it.
- Which milestone is next.
- Anything you discovered that is not obvious from the code.
- Current status of the HUMAN_BLOCKED items H1–H4.
- Anything you broke, or suspect you broke.

---

### STEP 10: END CLEANLY

1. `python3 -m pytest -q` is green and the count did not go down.
2. Everything is committed and pushed; `git status` is clean.
3. `claude-progress.txt` is current.
4. Both remote units (if you touched them) are `active`.
5. `VESPER_TRADING` is still `0` unless a human explicitly armed it.

---

## STANDING PROHIBITIONS

Doing any of these fails the round regardless of what else you achieved:

- Setting `VESPER_TRADING=1`. That is a human keystroke, always (H3).
- Placing, previewing, or attempting any real order.
- Binding anything to `0.0.0.0`.
- Committing, printing, or logging a credential, balance, account number or
  ntfy topic.
- Touching `supermcp`, the vultr box, or any container on coolify that is not
  `trading-agent` / `vesper-*`.
- Restarting docker, or any `coolify-*` service.
- Narrowing `tests/test_trading_mcp.py`'s AST pin, or deleting a test to make a
  suite pass.
- Editing any field of `feature_list.json` other than `passes`.
- Working around a lack of `sudo` instead of marking the item blocked.

If a feature appears to require one of these, it is the feature that is wrong.
Mark it, explain it in `claude-progress.txt`, and move on to the next one.

---

Begin with Step 1.
