"""All wizard copy and fixed values, as constants. No logic here.

Plain English, one line per option, the rebull provider-card tone. START, the PRECHECK rows, REVIEW
and DONE follow FIRST_RUN.md section 4; where v1 differs (no logged-in CLI as a provider, no OpenRouter
sign-in, library names are recorded not loaded) the v1 wording wins. No em or en dashes anywhere.
"""
from __future__ import annotations

DEFAULT_PROFILE_NAME = "trader"
HERMES_REQUIRES = ">=0.23.0"
HERMES_MIN_VERSION = (0, 23, 0)
# VERIFIED in docs/spike/T0-results.md answer 7. Shown, never run by the wizard.
INSTALL_CMD = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"

# AUTODETECTION.md section 5 and BROKER_CATALOG.md section 6. Ordering only, never a filter.
FIRST_SCREEN_SLUGS = (
    "interactive-brokers", "alpaca", "kraken", "tradier", "tastytrade", "bitget", "capital-com", "etoro",
    "trade-agent", "snaptrade",
)

# ---------------------------------------------------------------- START

START = (
    "Set up your trading agent.\n"
    "About five minutes. I look at what is already on this computer, ask before I use any of it, "
    "and never show you a key."
)
WHAT_IT_DOES = (
    "What this does: it creates one new Hermes profile for the trading agent and writes only into it.\n"
    "It looks for AI tools, saved key names, other apps' MCP settings and local model ports, by name only.\n"
    "Nothing is used until you say yes, item by item. Nothing is required, and you can skip any step.\n"
    "It never changes another app or another Hermes profile."
)

# ---------------------------------------------------------------- PRECHECK

OFFLINE_NOTE = ("No internet right now. Setup still works. Checks that need internet say Needs internet "
                "and are listed under finish later.")
HERMES_MISSING = "This agent runs on Hermes, a free open-source agent runtime. It is not installed yet."
HERMES_INSTALL_HINT = ("To install it, run this in a terminal (I never run it for you):\n  {cmd}\n"
                       "Then come back and choose Check again.")
HERMES_FAILED = ("Hermes is installed, but it did not answer a version check. Fix that in a terminal "
                 "(run hermes --version), then choose Check again.")
HERMES_NOT_BOOTSTRAPPED = ("Hermes found, data root not initialized yet. That is fine: creating the profile "
                           "sets it up.")
HERMES_OUT_OF_RANGE = "This setup needs Hermes {requires}. You have {version}."
HERMES_UPDATE_HINT = ("To update a normal install, run: hermes update\n"
                      "If you installed Hermes another way, upgrade it the way you installed it. "
                      "I never run this for you.")
HERMES_VERSION_UNREADABLE = ("I could not read the Hermes version from its answer. Carrying on; "
                             "creating the profile will tell us if it is too old.")
HERMES_WHAT_NOW = "What now?"
NAME_PROMPT = ("Name for the new profile (letters, digits, hyphen, underscore). "
               f"Press Enter for {DEFAULT_PROFILE_NAME}. Type < to go back.")
NAME_INVALID = ("That name will not work. Use lowercase letters, digits, hyphen or underscore, start with a "
                "letter, at most 32 characters, and avoid reserved names like hermes, default, test, tmp, "
                "root or sudo.")
NAME_KEEP = "The new profile will be called {name}."
PROFILE_TAKEN = "You already have a profile called {name}. I will not overwrite it."

# ---------------------------------------------------------------- SCAN

SCAN_PROMPT = ("Looking around: AI tools, saved keys, other apps' settings, local model ports. Names only, "
               "local, no network.")
SCAN_FOUND = "Found on this computer:"
SCAN_NOTHING = "I did not find anything yet. That is fine."
SCAN_COULD_NOT = "Could not check: {what}"
SCAN_FOOTER = "Nothing here has been used yet. I never show key values."

# ---------------------------------------------------------------- AI step (v1 rulings R23, R26)

AI_PROMPT = "Pick where the agent's brain comes from. You can change this any time."
AI_CLI_NOTE = ("I found {label} on this computer. Right now it cannot power this agent by itself. "
               "Pick a local model or an API key for the agent's brain; your {brand} sign-in stays untouched.")
AI_NO_LOCAL = ("No local model found. A free, private option: install Ollama or LM Studio, start it, then "
               "go Back and scan again.")
AI_UNSUPPORTED_KEY = "I found {name} in your environment. This agent cannot use that provider yet, so I left it alone."
AI_PRIVATE_ON = "Showing only private options: a model on this computer, or Venice."
AI_ASK_ADDRESS = ("Address of the OpenAI-compatible service, for example https://api.example.com/v1. "
                  "Type < to go back.")
AI_ADDRESS_INVALID = "That address will not work. It must start with http:// or https:// and carry no password."
AI_ASK_KEY_NAME = ("Name of the setting that holds its key, for example MY_LLM_KEY (the name only, never the key). "
                   "Press Enter if it needs no key.")
AI_KEY_NAME_INVALID = ("That does not look like a setting name. Use capital letters, digits and at least one "
                       "underscore, like MY_LLM_KEY. Never type the key itself here.")
# UNVERIFIED: the Venice base URL and key name, and the claim that Venice belongs in "keep it private"
# (R26: only local models or Venice). Confirm against Venice's own docs before relying on either.
VENICE_BASE_URL = "https://api.venice.ai/api/v1"
VENICE_KEY_ENV = "VENICE_API_KEY"
# UNVERIFIED: conventional OpenAI base URL and key name.
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_KEY_ENV = "OPENAI_API_KEY"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"
VENICE_KEYS_URL = "https://venice.ai/settings/api"  # UNVERIFIED

AI_NOTHING_TO_TEST = "Nothing to test yet: no AI is set up for the agent's brain."
NEEDS_INTERNET = "Needs internet"
NOT_TESTED = "not tested yet"
AI_VERIFY_PROMPT = "Check that it answers? One tiny test message."
AI_CHECK_PASSED = "AI check: passed."
AI_CHECK_NOT_TESTED = "AI check: not tested yet."
AI_CHECK_FAILED = "AI check did not pass: {detail}"
AI_CHECK_CRASHED = "AI check could not run."

# ---------------------------------------------------------------- key handling

REUSE_PROMPT = ("I found {name} in your environment. I have not used it. Copy it into the new profile's "
                "private .env file? I will never show it.")
LOGIN_SHELL_ONLY = ("I can see {name} is set in your login shell, but I cannot copy it from here. "
                    "You can paste it instead; nothing you type is shown.")
REFERENCE_ONLY = "I will not copy this one ({name}). Make a new key for this agent."
VANISHED = "{name} is no longer set in the environment, so there is nothing to copy. You can paste it instead."
EXISTS_PROMPT = ("{name} is already in this profile's private .env file. I will not overwrite it silently. "
                 "Keep what is there, or replace it?")
PASTE_ONE = "{name}: paste it now, or skip and finish later?"
PASTE_PROMPT = "Paste {name} (nothing is shown). Press Enter to skip it."
PASTE_AGAIN = ("I did not save {name} anywhere, so I need it again. Paste it (nothing is shown), "
               "or press Enter to skip it.")
PLAIN_PROMPT = "Value for {name} (not a secret, so you will see it). Press Enter to skip it."
PLAIN_AGAIN = "I did not save {name} anywhere, so I need it again. Type it, or press Enter to skip it."
BAD_VALUE = ("That value cannot be stored safely in a .env file (it holds something the loader would expand, "
             "a NUL character, or text that is not valid UTF-8). Try again.")

# ---------------------------------------------------------------- brokers

BROKER_PROMPT = ("Pick a broker MCP, or skip. A broker MCP lets the agent read your account and, if the broker "
                 "offers it, place orders. What it can do is set by the broker's own connector. Below is what "
                 "each broker says about itself.")
BROKER_NONE_LABEL = "No broker, research only"
BROKER_NONE_DETAIL = "Skip for now. The agent still works for research."
BROKER_ALL_LABEL = "Show every broker (A to Z)"
BROKER_SEARCH_LABEL = "Search or filter"
SEARCH_TEXT = "Search words (name, slug or region). Press Enter for none. Type < to go back."
SEARCH_REGION = "Region code, like US or UK. Press Enter for any. Type < to go back."
SEARCH_FILTER = "Only show:"
SEARCH_NONE = "Nothing matched."
REGION_NOT_CURATED = "region not curated"
TAG_COMMUNITY = "Made by a third party, not the broker"
TAG_NO_DIRECT = "No direct AI connection"
TAG_OFFICIAL = "Official"
NOT_INSTALLABLE = ("{name}: {banner}.\n"
                   "How to set this up yourself: see {page} ({url}).\n"
                   "This setup cannot install it yet.")
ROUTE_PROMPT = "{name} has more than one way to connect. Pick one."
INSTALL_PROMPT = "Setting up {name}. Next I ask for each name this connection needs."
UNSET_SENTENCE = ("If a name stays unset, the broker connection will not start until it is set. "
                  "You can set it later in the profile's private .env file.")
OAUTH_ONLY = ("This route signs in on the broker's own site. Setup cannot do that sign-in for you, "
              "so it is listed under finish later.")
BROKER_VERIFY_PROMPT = "Connection test for {name}."
BROKER_VERIFY_LATER = "I will test the connection after setup, once the profile exists. Right now: {status}."

# ---------------------------------------------------------------- skills and data keys

SKILLS_PROMPT = ("Pick skill libraries. Skills are small how-to packs the agent reads before it acts. "
                 "Pick any, or none, then choose Done.")
SKILLS_NONE_LISTED = "No skill libraries are listed yet."
DATA_KEYS_NONE = "None of your picks needs a key. Nothing to ask here."
DATA_KEYS_PROMPT = ("Optional data services. Your picks use: {names}. Not needed. Skip is fine, and you can "
                    "add keys later.")
DATA_KEYS_OFFLINE = "Saved. I will test it the first time it is used."

# ---------------------------------------------------------------- REVIEW, WRITE, DONE

REVIEW_HEAD = "Ready. Here is exactly what I will do."
REVIEW_PROFILE = "- Create a Hermes profile named {name}."
REVIEW_AI = {
    "none": "- AI: none yet. You can add one later.",
    "cli": "- AI: none yet. Your {brand} sign-in stays untouched and cannot power the agent yet.",
    "openrouter": "- AI: OpenRouter.",
    "custom": "- AI: an OpenAI-compatible service at {ref}.",
    "ollama": "- AI: Ollama on this computer.",
    "lm-studio": "- AI: LM Studio on this computer.",
}
REVIEW_BROKER = "- Broker MCP: {name}, installed the way its page says."
REVIEW_KEYS = "- Put {n} key(s) and setting(s) into the profile's private .env: {names}."
REVIEW_SKILLS = "- Record {n} skill librar{y}: {names} (names only; nothing is downloaded yet)."
REVIEW_NOCHANGE = "- Change nothing in any other app or Hermes profile."

WRITE_EXISTS = "A profile called {name} already exists. Nothing was changed."
WRITE_FAILED = "Setup did not finish. {report}"
WRITE_FAILED_PLAIN = "Setup did not finish: {detail}"
WRITE_WHAT_NOW = "What now?"

VERIFY_HEAD = "Checking what I can:"
DONE_TITLE = "You are set up."
DONE_BODY = "AI: {ai}. Broker: {broker}. Skills: {n} picked."
DONE_UNDO = "To see what was created, open the setup receipt for {name}. To remove it all, run: hermes profile delete {name}"
FINISH_LATER_TITLE = "Left to finish later:"
LATER_AI = "Pick an AI for the agent's brain: a local model or an API key."
LATER_AI_TEST = "Test the AI: {status}."
LATER_BROKER = "Pick a broker (optional; research works without one)."
LATER_NAME = "Add {name} (you skipped it)."
LATER_OAUTH = "Sign in to {name} on its own site (setup cannot do that for you)."
LATER_BROKER_TEST = "Test the {name} connection: {status}."
