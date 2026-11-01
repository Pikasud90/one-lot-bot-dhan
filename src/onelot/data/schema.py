"""Re-exports of the corpus/Lab tables (defined once, in store.schema)."""

from ..store.schema import (  # noqa: F401
    METADATA,
    account_cache,
    app_settings,
    backtest_runs,
    backtest_trades,
    bars,
    data_jobs,
    desk_positions,
    desk_signals,
    engine_events,
    news_items,
    param_sets,
    sentiment_scores,
    universe,
)

metadata = METADATA
