# Contributing / working on this repo

## Rules that are not negotiable

1. **No order placement, ever.** `tests/test_invariants.py` scans every identifier in `src/`
   for order verbs. Do not rename things to dodge it; the scanner is the point.
2. **One unit per trade.** No position sizing, no capital-based gates. If a change needs
   the account size to decide anything, it is the wrong change.
3. **Closed bars only.** A strategy must produce identical signals on a truncated history
   (`tests/lab/test_strategies.py::test_signals_do_not_depend_on_future_bars`).
4. **Secrets only in `.env`.** Never in YAML, never in code, never in logs or test fixtures.
5. **Local git only** until the owner says otherwise.

## Adding a strategy

1. Create `src/onelot/strategies/<name>.py`: a `Params(StrategyParams)` with pydantic fields
   (use `opt(lo, hi, step)` on everything the optimizer may touch) and a `Strategy` subclass
   implementing `raw_signals(f) -> DataFrame` with `long_entry/short_entry/long_stop/short_stop`
   (+ optional `long_exit/short_exit` and `pl_<pillar>_<side>` scores).
2. Register it in `strategies/registry.py` (`REGISTRY`, `SUITABLE`, `INDICATORS`).
3. Add a search space to `lab/research.py::SPACES`.
4. Run `pytest tests/lab` — the generic strategy tests cover window discipline, stop side,
   look-ahead and volume-free series automatically.

## Adding an instrument

Edit `config/universe.yaml`. Indices/stocks with `fno: true` get `_FUT` and `_OPT`
instruments automatically; lot sizes there are fallbacks overridden by the Dhan scrip master.

## Running everything

`scripts/gate.sh` is the quality gate. From the repo root it runs exactly these commands, in
this order, with `.venv/bin/python` (else `python`, or `python3`, from `PATH`), and stops with a
non-zero exit at the first one that fails. CI will run the same script (US-008).

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python scripts/mypy_ratchet.py
.venv/bin/python -m pytest -m "not live"
```

Tests marked `live` need real broker credentials and never run in the gate; a bare `pytest`
skips them too (`addopts` in `pyproject.toml`).

The mypy ratchet lets type errors fall but never rise, file by file, against the counts in
`scripts/mypy_baseline.json`. Keep that file in step with the code:

- When the ratchet prints `run --update`, run `.venv/bin/python scripts/mypy_ratchet.py --update`
  and commit the baseline in the same PR, so the improvement is locked in.
- Deleting a file that has a baseline entry: run the ratchet with `--update` and commit the
  baseline in the same commit (until then it warns about the stale entry).
- Renaming one: run it with `--update --allow-regression` and commit the baseline in the same
  commit, because the new path counts up from 0; check that the baseline diff shows only the move.
- Any other use of `--allow-regression` accepts new type errors and needs the owner's agreement.

**Ruff format policy:** all Python is formatted by `ruff format` with ruff==0.17.3, pinned in
`requirements-dev.txt`. Format before committing, keep formatting-only changes in their own
`style:` commit, and move the pin only in a dedicated commit that also reformats. Markdown under
`.agile/` and `docs/` is excluded, so code fences in prose are never rewritten.

## Commit messages

Imperative mood, say what and why. Attribution lines as configured for this session.
