"""Backtest Lab: instrument-agnostic one-unit backtests, optimisation and the research gate.

The simulator (`simulator.py`) decides entries and exits on the UNDERLYING
price series using closed bars. The execution models (`execution.py`) then
translate each simulated trade into rupees for the instrument actually traded
(one share, one future lot, one option lot, one currency lot) with the full
Indian cost stack. Nothing in here sizes a position: the one-lot rule is law.
"""
