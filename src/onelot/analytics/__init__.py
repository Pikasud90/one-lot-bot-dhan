"""Pure analytics: indicators, option pricing, volatility ranking.

Everything here is a deterministic function of its inputs — no I/O, no clock,
no config. That is what makes the replay harness able to reproduce a live
session exactly, and what makes look-ahead bugs testable.
"""
