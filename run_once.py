"""run_once.py — run ONE research + Judge cycle and print it. Good for a first
smoke test of your API key and data before scheduling the daily job.

    python run_once.py            # first researcher
    python run_once.py Boone      # by name
"""
import os
import sys
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reslab import config, market, llm


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else config.RESEARCHERS[0]["name"]
    r = next((x for x in config.RESEARCHERS if x["name"].lower() == name.lower()), config.RESEARCHERS[0])
    if not config.ANTHROPIC_API_KEY:
        print("Set ANTHROPIC_API_KEY to run live research. (Offline: nothing to show.)")
        return
    print(f"[{r['name']} / {r['judge']}] researching a Robinhood-buyable small cap…\n")
    rec = llm.research(r, avoid=[])
    print("RECOMMENDATION:", rec.get("ticker"), "-", rec.get("name"))
    for k in ("driver", "thesis", "valuation", "catalyst", "risks", "conviction"):
        print(f"  {k}: {rec.get(k)}")
    tk = (rec.get("ticker") or "").upper()
    elig = market.eligibility(tk)
    print(f"\nELIGIBILITY: tradeable={elig['tradeable']} exchange={elig['exchange']} "
          f"cap={elig['market_cap']} price={elig['price']} {elig['reasons']}")
    if elig["tradeable"]:
        v = llm.judge_new(r, rec)
        print(f"\n{r['judge'].upper()}: {v['decision']} (priced_in={v.get('priced_in')})")
        print(f"  {v.get('reasoning')}")


if __name__ == "__main__":
    main()
