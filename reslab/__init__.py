"""reslab — an LLM-driven equity research system with a paper portfolio.

Two researchers, each with a skeptical Judge, hunt Robinhood-buyable US stocks
of any size, defend picks to their Judge, and run a paper portfolio benchmarked
against the market. Nothing here places real orders.
"""
from . import config, market, llm, portfolio, store, report, trends

__all__ = ["config", "market", "llm", "portfolio", "store", "report", "trends"]
