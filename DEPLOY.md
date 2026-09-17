# Research Lab — deploy guide

A self-running research system: two AI **researchers** hunt Robinhood-buyable US
**small caps**, each defends its picks to its own skeptical **Judge**, and a
**paper portfolio** tracks the results against the market (S&P 500 and the
Russell 2000 small-cap index). It runs once a day on a small cloud machine and
you can change it in plain English with Claude Code.

**It places no real orders.** It researches and paper-trades so you can see, over
months, whether the picks beat an index before any real money is involved.

---

## What it does each day

1. Backs up its state.
2. Prices your holdings; each Judge re-challenges every position → **hold / buy
   more / sell** (with reasons, recorded).
3. Each researcher hunts new small-cap ideas via live web research; each idea is
   **gated** (must be a real major-exchange small cap you could buy on
   Robinhood — no OTC/pink sheets, inside the market-cap band) and then
   **interrogated** by its Judge; accepted names are bought on paper.
4. Marks the portfolio value vs SPY and IWM; writes `daily_report.md` and
   `summary.md`; saves state.

## Costs & requirements

- A small **Ubuntu cloud VM** (~$4–6/mo).
- An **Anthropic API key** (Console credits) — the researchers/judges use it for
  live web research. Cost scales with `RESLAB_NEW_IDEAS_PER_DAY` and how many
  holdings get re-challenged daily; start small.
- Internet on the box (for prices via yfinance, with a Stooq fallback).

## Setup (details in the commands the script prints)

```bash
# on a fresh Ubuntu VM, inside this folder:
bash setup.sh
nano .env                     # add ANTHROPIC_API_KEY, confirm RESLAB_MODEL
claude                        # log Claude Code in once
set -a && . ./.env
python3 run_once.py           # smoke test: one live research + Judge cycle
python3 daily_job.py          # run a full day now (then cron runs it daily)
```

Results: `summary.md` (plain-English bullets), `daily_report.md` (full),
`state/reslab_state.json` (positions, cost basis, closed trades, benchmark curve).

## What "Robinhood-buyable small cap" means here

`reslab/config.py` restricts buys to **major US exchanges** — Nasdaq
(NMS/NGM/NCM), NYSE (NYQ), NYSE American/AMEX (ASE), Cboe (BATS/PCX) — and a
market-cap band (default **$300M–$5B**) above a **$2 price floor**. That matches
what Robinhood lets you trade (it does **not** trade OTC/pink-sheet stocks). The
check is best-effort from market data; confirm a name is tradeable in your own
Robinhood app before ever acting on it. Tune the band and exchanges via the
`RESLAB_*` variables in `.env`.

Note: this system does **not** connect to Robinhood to place orders. Automated
equity execution is a separate, higher-risk step (Robinhood's agentic-trading
beta, or manual). Prove the paper record first.

## Changing the system without losing anything

Ask **Claude Code** on the box (`cd research-lab && claude`) for changes in plain
English — "add a third researcher", "raise the cap ceiling to $10B", "email me
the summary". It edits the real files; you restart and the next run uses them.
Your **portfolio and everything learned are safe**: all of that lives in
`state/reslab_state.json` (backed up before every run in `state/backups/`),
which is separate from the code. Change code → restart → state reloads → nothing
lost.

## Honest caveats

- Beating an index is genuinely hard; public fundamentals are largely priced in.
  The **portfolio-vs-benchmark record over months** is the only real proof —
  don't trust it early.
- LLM research is confident and sometimes wrong; two researchers + skeptical
  judges + forward tracking guard against fooling yourself, not perfectly.
- Free price data (yfinance/Stooq) is unofficial; swap in Alpaca/Polygon for
  reliability by editing `market.get_daily()`.
- Not investment advice.
