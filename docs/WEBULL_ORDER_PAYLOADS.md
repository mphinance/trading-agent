# Webull order payloads — verified against the live account

*Established 2026-08-29 by sending real `preview_order` / `preview_option` calls
against the live account and iterating until Webull accepted them. Preview is
non-committal — it returns a cost estimate and places nothing — so this was
established without placing an order.*

Both shapes below were **accepted by Webull**, which returned
`{estimated_cost, estimated_transaction_fee, currency}`. This supersedes
guesswork: `vesper/nodes/executor.py` was wrong about both.

---

## Single-leg equity

```python
wb.trade.order_v2.place_order(account_id, [        # NOT place_order(payload)
    {
        "client_order_id": uuid.uuid4().hex,
        "combo_type": "NORMAL",
        "instrument_type": "EQUITY",               # NOT "asset_type"
        "market": "US",
        "symbol": "F",
        "side": "BUY",
        "order_type": "LIMIT",
        "limit_price": "1.00",                     # strings, not floats
        "quantity": "1",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "support_trading_session": "N",
    }
])
```

**What `executor.py` had wrong** (all three, not just the signature):
1. Called `place_order(payload)` with a single dict. The real signature is
   `place_order(account_id, new_orders, client_combo_order_id=None)` — the
   account id is a separate positional and the orders are a **list**.
2. Sent `asset_type`; the field is `instrument_type`.
3. Omitted `combo_type`, `market`, `entrust_type`, `support_trading_session`
   and `client_order_id`, each of which Webull rejects the request without.

**Asymmetry worth knowing:** the request field is `quantity`, but the same
value comes back as `total_quantity` in `get_order_history`. Do not assume the
response shape is the request shape.

---

## Single-leg option

Confirmed against the official sample in the SDK's GitHub source
(`samples/order/order_option_client.py`, which the PyPI package does **not**
ship) and then verified live.

```python
wb.trade.order_v2.place_option(account_id, [
    {
        "client_order_id": uuid.uuid4().hex,
        "combo_type": "NORMAL",
        "order_type": "LIMIT",
        "quantity": "1",
        "limit_price": "0.01",
        "option_strategy": "SINGLE",               # "VERTICAL" also valid
        "side": "BUY",                             # ALSO required at order level
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "legs": [
            {
                "side": "BUY",                     # AND on each leg
                "quantity": "1",
                "symbol": "SPY",                   # the UNDERLYING ticker
                "strike_price": "770",
                "option_expire_date": "2026-12-18",
                "instrument_type": "OPTION",
                "option_type": "CALL",
                "market": "US",
            }
        ],
    }
])
```

**The load-bearing correction:** a leg is identified by
**underlying + strike + expiry + option_type**, *not* by an options contract
symbol. `vesper/nodes/executor.py`'s `_execute_webull_multileg` currently sends
`"symbol": leg.contract_symbol` — that is wrong and will be rejected. It also
omits `option_strategy` and the order-level `side`.

`option_strategy` values confirmed by probing: `SINGLE` and `VERTICAL` are
accepted; `NORMAL` and `CUSTOM` are rejected outright. (`NORMAL` is a
`combo_type` value, not an `option_strategy` value — easy to conflate.)

---

## Option-chain gotchas (found while building the above)

`md.Market.option_chain()` does **not** return a clean list of tradable
standard contracts. Filter before using anything from it:

- **Filter `def_type == "STANDARD"`.** The chain also contains `FLEX`
  contracts.
- **Filter `root_symbol == <underlying>`.** Adjusted roots (`2SPY`, `4SPY` —
  the result of past corporate actions) appear alongside real ones and carry
  nonsense strikes such as `0.01`, `0.02`, `0.11`.
- **Pass `expire_date`.** Without it the response is one 200-row page that may
  contain no near-dated expiries at all — an unfiltered SPY call chain returned
  only 2026-12-18 and 2027-01-15 in its STANDARD subset.
- **Strikes arrive as `"359.0000000000"`.** Normalise (`f"{float(k):g}"`)
  before putting one in an order payload.

Skipping these is how you end up previewing a 2034 LEAPS at a $359 strike
against a $769 spot, which is exactly what happened on the first attempt.

**Useful fields the chain does carry:** `underlying_symbol`,
`underlying_instrument_id`, `instrument_id`, `multiplier`, `settlement_method`,
`tradable_status`. `underlying_symbol` matters — see below.

---

## What this does and does not unblock

**Unblocked.** The documented signature mismatch in `_execute_webull`, and the
contract-symbol error in `_execute_webull_multileg`. Both can now be fixed
against verified shapes rather than guesses.

**Partially unblocked — the live-position metadata gap.** A *position* payload
still carries no strategy tag, no order linkage and no underlying field
(verified: the raw `get_account_position` response has only `symbol`,
`instrument_type`, `quantity`, `cost`, `cost_price`, `last_price`,
`market_value`, `position_id`, `proportion`, `currency`, and P&L fields). So a
local registry is still the only way to know which playbook opened a position.
**But** an option *contract* carries `underlying_symbol`, so the
sector-concentration bucket's live-mode gap — "cannot resolve an option
position's underlying" — is closable with a chain lookup, independently of the
registry. Those are two separate gaps and only one of them needs the registry.

**Still unverified.** Nothing here proves Webull *accepts an actual order* —
preview validates the payload, not the fill. Multi-leg combos beyond `SINGLE`
(a real `VERTICAL`, and Thega's mixed equity+options shape) are unverified;
`VERTICAL` is only known to be an accepted enum value. Per CLAUDE.md's Status
section, the first live order should still be one share of something cheap with
Webull Desktop open.
