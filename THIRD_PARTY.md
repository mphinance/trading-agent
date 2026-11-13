# Third-party libraries and attribution

> [!NOTE]
> Status: pointer entries only; one lineage question is unresolved. Nothing below is vendored, copied or enabled by default.

**What this project points at, under which license, and what still needs checking before anything is listed.**

The user picks a library in the setup wizard, and it is installed from the library's own source under the library's own license. Facts here come from the maintainer's design notes (not in this repository) and from reading the upstream repositories. Anything marked UNVERIFIED has not been confirmed. Licenses are what each repository's own license file says, not legal advice, and this project has not had a legal review.

> [!WARNING]
> **alpha-skills lineage: unresolved.** alpha-skills is catalogued but not listed (`listed: false`) and is never offered in the wizard until the lineage below is checked.

## At a glance

| Entry | License | Offered in the wizard? |
|---|---|---|
| [alpha-skills](#alpha-skills-trading-subset) | MIT (lineage unresolved) | No, `listed: false` |
| [broker-skill-docs](#broker-skill-docs) | CC0 for the catalog text; each broker's repo has its own | Yes, license shown next to the name; the pick is only recorded, nothing is installed in this version |
| [momentum-mcp](#momentum-mcp) | Apache-2.0 | Optional |
| [awesome-broker-mcp](#awesome-broker-mcp-catalog-generator-input) | CC0 1.0 | Input to the catalog generator only |
| [Hermes](#hermes-runtime-dependency) | MIT | Runtime dependency, installed separately |

## alpha-skills (trading subset)

- Source: https://github.com/mphinance/alpha-skills (per its README)
- License: MIT, copyright "Momentum Phinance LLC"
- Contains: 136 skill folders in total (counted from a local clone on 2026-11-13); 64 are trading skills, 56 of which are shippable after dropping personal and brokerage-specific ones (these two counts are from the design notes and [UNVERIFIED]: recount against the clone)
- Keys a pick may need (names only): `FMP_API_KEY`, `FINVIZ_API_KEY`, `TD_API_KEY`. UNVERIFIED which keyed skills fall back gracefully.
- Attribution: the README says "Methodologies adapted from TraderMonty, William O'Neil, and Mark Minervini". Credit those sources.
- **alpha-skills lineage: unresolved.** Many skills are probably derived from a public open-source trading-skill pack. Evidence: the README credit above, several skills naming "TraderMonty" directly, and skill names that match a public pack. Upstream identity, its license, and which skills are copies versus rewrites are UNVERIFIED. Before listing: identify the upstream, read its license, add an entry crediting it, and mark any skill whose license does not allow redistribution.
- Also UNVERIFIED: whether alpha-skills and the older trading-agent copy name the same rights holder; provenance of images in `sector-analyst` and `breadth-chart-analyst`; terms for public CSVs that `market-breadth-analyzer`, `uptrend-analyzer` and `breadth-chart-analyst` fetch from "TraderMonty" at runtime.
- `stanley-druckenmiller-investment` is named after a real person: inspired by, not endorsed.

## broker-skill-docs

- Source: https://github.com/mphinance/awesome-broker-mcp (the docs live under `docs/skills/`)
- License: CC0 for the catalog text. Each broker's own skill repo has its own license, recorded per entry in the catalog; some have none at repo root. The wizard shows the license next to the name when you pick one; this version only records the pick and installs nothing.
- Contains: 44 per-broker skill docs (counted from a local clone on 2026-11-13).
- Keys: whatever each broker's own server documents. Nothing from us.
- Attribution: broker names are their owners' marks; text only, no logos.

## momentum-mcp

- Source: https://github.com/mphinance/momentum-mcp (its git remote)
- License: Apache-2.0 (LICENSE file). Its README once said MIT; the file is Apache-2.0.
- Contains: 56 quant tools plus 17 TickerTrace ETF flow tools. The README says 73 tools while a decorator count gives 74; which of the 74 is not a real tool is UNVERIFIED.
- Optional. Keys a pick may need (names only): `TD_API_KEY`, `SEC_USER_AGENT`, `OPENROUTER_API_KEY`.
- Attribution: keep the LICENSE and notices if redistributed. As a pointer we copy nothing. The EDGAR client says it was "Ported from a friend's discount-bloomberg repo"; origin license UNVERIFIED.
- Note for users: free-tier results may carry vendor promotional text (UNVERIFIED exact output).

## awesome-broker-mcp (catalog generator input)

The catalog generator reads the awesome-broker-mcp catalog, licensed CC0 1.0. Broker names are their owners' marks.

- Source: https://github.com/mphinance/awesome-broker-mcp

## Hermes (runtime dependency)

Hermes agent, by Nous Research, is MIT licensed. It is a runtime dependency the user installs separately; it is not vendored here.

## Related docs

- [Publish checklist](docs/PUBLISH_CHECKLIST.md): the open items that close the lineage question.
- [README](README.md): what the tool is and what it does not include.
- [Docs index](docs/README.md): all pages.
