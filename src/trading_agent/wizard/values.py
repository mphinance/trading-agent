"""The value and consent engine: the only code in the wizard that handles secret values.

Consent is per CONSUMER (the AI step, one broker route, one library) and per PROCESS. A value decision is keyed
``<consumer>:<NAME>``; the consumer names where the value will be used, and the prompt says so in plain words.
Nothing that grants a copy is ever persisted: a restart asks again. Only non-secret outcomes ("skip",
"keep-existing") and ``declined:<finding id>`` are saved in the state file.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading_agent.findings import Finding
from trading_agent.secrets import check_secret
from trading_agent.wizard import screens as S
from trading_agent.wizard.catalog import NameSpec
from trading_agent.wizard.io import BACK_INPUT, Option

_LOGIN_SHELL_WHERE = "login shell environment"  # detect/env.py: present in the login shell only, value unreachable
_PERSISTED_MODES = ("skip", "keep-existing")
MODE_VALUES = frozenset({"true", "false", "paper", "live", "0", "1"})


@dataclass(frozen=True)
class Consumer:
    key: str            # "ai" | "broker:<slug>/<route id>" | "lib:<library id>"
    label: str          # "the Alpaca connection": where a value will be used, in plain words
    who: str            # "Alpaca": prefix for prompts
    kinds: tuple[str, ...]   # finding kinds that may be offered for reuse here (the AI/broker/data scoping)


class BackSignal(Exception):
    """The user typed < on a plain prompt: go back one screen."""


class ValueMixin:
    """Mixed into the wizard. Needs: io, d (deps), p (platform), ws (WizardState), say, decide, forget, dec,
    findings(), profile_dir(), _consumers() (the consumers that are still in the plan)."""

    def _init_values(self) -> None:
        self._values: dict[str, str] = {}                    # NAME -> value: memory only
        self._grants: dict[tuple[str, str], str] = {}        # (consumer key, NAME) -> reuse|paste|plain|same
        self._labels: dict[str, str] = {}                    # consumer key -> label, for "the value you gave X"
        self.refused: set[str] = set()                       # consumers left out of the plan (process only)
        self.left_out: dict[str, tuple[str, str]] = {}       # consumer key -> (label, NAME), for REVIEW and DONE

    # ---- bookkeeping

    @staticmethod
    def _dk(c_key: str, name: str) -> str:
        return f"{c_key}:{name}"

    def _decided(self, c: Consumer, name: str) -> bool:
        return (c.key, name) in self._grants or self.dec(self._dk(c.key, name)) in _PERSISTED_MODES

    def _grant(self, c: Consumer, name: str, mode: str) -> None:
        self._grants[(c.key, name)] = mode
        self._labels[c.key] = c.label
        self.forget(self._dk(c.key, name))

    def _mark(self, c: Consumer, name: str, mode: str) -> None:
        """A non-secret outcome: skip or keep-existing. No value is held for this consumer."""
        self._revoke(c.key, name)
        self.decide(self._dk(c.key, name), mode)

    def _revoke(self, c_key: str, name: str) -> None:
        self._grants.pop((c_key, name), None)
        if not any(n == name for _, n in self._grants):
            self._values.pop(name, None)

    def _revoke_consumer(self, c_key: str) -> None:
        for (k, n) in [p for p in self._grants if p[0] == c_key]:
            self._revoke(k, n)
        for key in [k for k in self.ws.decisions if k.startswith(c_key + ":")]:
            self.forget(key)

    def _prune(self, expected: set[tuple[str, str]]) -> None:
        """Forget grants and saved outcomes for pairs the current choices no longer use."""
        for k, n in [p for p in self._grants if p not in expected]:
            self._revoke(k, n)
        for key in list(self.ws.decisions):
            head, _, name = key.rpartition(":")
            if (head == "ai" or head.startswith(("broker:", "lib:"))) and (head, name) not in expected:
                self.forget(key)

    # ---- the one place a found value is read

    def _copy_from_environment(self, name: str) -> bool:
        """The ONE place a found key's value is read, only after a per-consumer yes. The value goes into
        ``self._values`` and is never returned, printed or persisted."""
        value = self.p.environ().get(name)
        if not value:
            return False
        try:
            check_secret(name, value)
        except ValueError:
            return False
        self._values[name] = value
        return True

    # ---- asking

    def _store_has(self, name: str) -> bool | None:
        """Is NAME already in the target profile's .env? None if the file could not be checked."""
        try:
            return bool(self.d.store_factory(self.profile_dir()).exists(name))
        except Exception:  # noqa: BLE001
            return None

    def _collision_kept(self, c: Consumer, name: str) -> bool:
        has = self._store_has(name)
        if has is False:
            return False
        prompt = (S.EXISTS_UNKNOWN if has is None else S.EXISTS_PROMPT).format(name=name)
        a = self.io.choose(prompt, [Option("keep", "Keep what is there"), Option("replace", "Replace it")])
        if a == "keep":
            self._mark(c, name, "keep-existing")
            return True
        return False

    def _holders(self, name: str, *, besides: str = "") -> list[str]:
        """Consumers holding an explicit grant for NAME, in the order they were given one."""
        return [k for (k, n) in self._grants if n == name and k != besides]

    def _ask_share(self, c: Consumer, name: str) -> bool:
        """A profile .env has one value per NAME, so C would be able to READ the value another consumer was given.
        That needs C's own explicit grant. True if granted; a refusal removes C from the plan entirely."""
        holder = self._labels[self._holders(name, besides=c.key)[0]]
        a = self.io.choose(S.SHARE_PROMPT.format(who=cap(c.label), name=name, holder=holder),
                           [Option("same", S.SHARE_YES.format(who=c.label)),
                            Option("refuse", S.SHARE_NO.format(who=c.label))])
        if a == "same":
            self._grant(c, name, "same")
            return True
        self.refuse(c, name)
        return False

    def refuse(self, c: Consumer, name: str) -> None:
        """Take C out of the plan: no config for it, no ${NAME} reference, no value held for it."""
        self._revoke_consumer(c.key)
        self.refused.add(c.key)
        self.left_out[c.key] = (c.label, name)
        self.say(S.LEFT_OUT.format(who=cap(c.label), name=name))

    def _shared(self, c: Consumer, name: str) -> bool:
        """NAME already has a value for another consumer: ask, never use it silently."""
        if name not in self._values or not self._holders(name, besides=c.key):
            return False
        self._ask_share(c, name)
        return True

    def resolve_sharing(self) -> None:
        """Run before REVIEW and again before WRITE: every consumer that references a NAME whose value exists
        needs its own grant, whichever order the values were given in."""
        for name in list(self._values):
            for c, specs in self._consumers():
                if c.key in self.refused or not any(s.name == name for s in specs):
                    continue
                if (c.key, name) in self._grants or self.dec(self._dk(c.key, name)) == "keep-existing":
                    continue
                if self._holders(name, besides=c.key):
                    self._ask_share(c, name)

    def _paste(self, c: Consumer, name: str) -> bool:
        """Ask for a secret with no echo. True if a value was stored; empty input skips."""
        while True:
            v = self.io.secret(S.PASTE_PROMPT.format(who=c.who, name=name))
            if v == "":
                self._mark(c, name, "skip")
                return False
            try:
                check_secret(name, v)
            except ValueError:
                self.say(S.BAD_VALUE)
                continue
            self._values[name] = v
            self._grant(c, name, "paste")
            return True

    def _plain(self, c: Consumer, name: str) -> None:
        """A mode switch, typed in plain, from a short allowlist. ``<`` goes back."""
        while True:
            v = self.io.ask(S.PLAIN_PROMPT.format(who=c.who, name=name)).strip()
            if v == BACK_INPUT:
                raise BackSignal
            if v == "":
                self._mark(c, name, "skip")
                return
            if v.lower() not in MODE_VALUES:
                self.say(S.MODE_NOT_OK)
                continue
            self._values[name] = v.lower()
            self._grant(c, name, "plain")
            return

    def _live_finding(self, c: Consumer, name: str) -> Finding | None:
        for f in self.findings():
            if f.kind in c.kinds and f.names == (name,) and f.state == "present":
                return f
        return None

    def _offer_reuse(self, c: Consumer, name: str) -> str:
        """'reused', 'declined' (remembered per finding id) or 'unavailable' (nothing copyable; paste instead).
        Needs a LIVE finding of the right kind for this consumer that is copyable and not declined."""
        f = self._live_finding(c, name)
        if f is None:
            return "unavailable"
        if f.reusable != "copy-with-consent":
            self.say(S.REFERENCE_ONLY.format(name=name))
            return "unavailable"
        if f.where == _LOGIN_SHELL_WHERE:
            self.say(S.LOGIN_SHELL_ONLY.format(name=name))
            return "unavailable"
        if self.dec(f"declined:{f.id}"):
            return "declined"
        if not self.io.confirm(S.REUSE_PROMPT.format(name=name, who=c.label)):
            self.decide(f"declined:{f.id}", "1")
            return "declined"
        if self._copy_from_environment(name):
            self._grant(c, name, "reuse")
            return "reused"
        self.say(S.VANISHED.format(name=name))
        return "unavailable"

    def _obtain(self, c: Consumer, spec: NameSpec, *, how: str = "auto") -> str:
        """Get a value for NAME for this consumer, or an explicit skip. ``how``: auto (reuse card if a found key
        exists, else paste or skip), card (reuse card only; 'declined' goes back to the menu), paste (straight to paste).
        Returns 'done' or 'declined'."""
        name = spec.name
        if c.key in self.refused:
            return "done"
        if self._decided(c, name) or self._shared(c, name) or self._collision_kept(c, name):
            return "done"
        if not spec.secret:
            self._plain(c, name)
            return "done"
        if how != "paste":
            r = self._offer_reuse(c, name)
            if r == "reused":
                return "done"
            if r == "declined" and how == "card":
                return "declined"
        if how == "auto":
            a = self.io.choose(S.PASTE_ONE.format(who=c.who, name=name),
                               [Option("paste", "Paste it"), Option("skip", "Skip, finish later")])
            if a == "skip":
                self._mark(c, name, "skip")
                return "done"
        self._paste(c, name)
        return "done"


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]
