# Robinhood support

Vesper can place equity orders through Robinhood's agentic account, next to
Webull and Public.com. It goes through the same `execution_guard` (preview, then
ticket, then place) as every other broker.

## What it can and cannot do

| | |
|---|---|
| Equities and ETFs, limit or market, `gfd` | yes |
| Options | **no.** The adapter refuses anything but equities. Option ideas stay alerts you place by hand. |
| Accounts | only `ROBINHOOD_ACCOUNT_ID`, and only if Robinhood marks it `agentic_allowed`. Your other accounts are read-only to the agent, by Robinhood and again by this adapter. |
| Approval | two gates: Vesper's Telegram/Discord card, then (when Robinhood asks) approval in the Robinhood app. An order waiting there is reported as `PENDING_BROKER_APPROVAL`, never as a fill. |

## Setup

1. In the Robinhood app, enable the agentic account and note its account number.
2. `pip install -r requirements.txt` (the `mcp` client is already pinned).
3. Log in once. A browser opens, you approve, tokens are stored outside the repo:

   ```
   python -m vesper.brokers.robinhood_auth login
   python -m vesper.brokers.robinhood_auth status
   ```

4. In `.env`:

   ```
   EXECUTION_BROKER=robinhood
   ROBINHOOD_ACCOUNT_ID=<agentic account number>
   ```

Tokens live in `~/.vesper/robinhood_oauth.json` (override with
`VESPER_RH_TOKEN_PATH`). They are credentials: do not commit or share them.
Unattended runs never open a browser; if the refresh token has died they fail
with "Robinhood login required" and you re-run `login`.

## How it connects

Robinhood publishes trading as a hosted MCP server
(`https://agent.robinhood.com/mcp/trading`) behind standard OAuth: dynamic client
registration, PKCE, public client, refresh tokens. Vesper is its own MCP client;
it does not need Claude running. Each broker call opens a short session and closes
it. `place_order` sends an idempotency `ref_id` derived from the proposal id, so
a retried transport failure cannot place the order twice.

## Household risk checks

`core/portfolio_risk.py` is pure arithmetic over the whole book rather than one
order: leverage and margin debit, concentration as a share of net equity, and what
leverage becomes if every ITM short put is assigned. Robinhood reserves put
collateral when the put is opened, so reported buying power is already net of it;
only puts marked `collateral_reserved: false` can create a funding shortfall.

## Tests

`tests/test_robinhood_broker.py` and `tests/test_portfolio_risk.py` run offline
with a fake transport. Nothing in the suite touches Robinhood.
