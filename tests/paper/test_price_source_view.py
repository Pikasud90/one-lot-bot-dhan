"""US-122 A7: the trade view says whether an option fill was priced from the contract's real
traded price or from the price model (`price_source`, from the FILLED event); stock trades and
trades with nothing stored carry no such key. The row's words are in test_us122_page_fixes."""

from __future__ import annotations

from .conftest import THU, ist, make_alert
from .test_real_premium import OPT_FILL, Real, run
from .test_real_premium import make_real_book as make_real_book  # noqa: F401 - fixture
from .test_real_premium import model as model  # noqa: F401 - fixture


def test_a_real_priced_option_trade_says_real(make_real_book) -> None:  # noqa: F811
    book = make_real_book(Real(lambda option, u, ts: 300.00))
    run(book, make_alert("INDEX"), [OPT_FILL])
    (view,) = book.recent()
    assert view["price_source"] == "real"


def test_a_model_priced_option_trade_says_model(make_real_book) -> None:  # noqa: F811
    book = make_real_book(Real(lambda option, u, ts: None))
    run(book, make_alert("INDEX"), [OPT_FILL])
    (view,) = book.recent()
    assert view["price_source"] == "model"


def test_a_stock_trade_has_no_price_source(make_real_book) -> None:  # noqa: F811
    book = make_real_book(None)
    run(book, make_alert("STOCK"), [OPT_FILL])
    (view,) = book.recent()
    assert "price_source" not in view


def test_a_trade_with_nothing_stored_has_no_price_source(make_real_book) -> None:  # noqa: F811
    book = make_real_book(Real(lambda option, u, ts: 300.00))
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    (view,) = book.recent()  # not filled yet: no FILLED event
    assert "price_source" not in view
