"""The phone tests' fixtures (a real Engine over a throw-away folder, the app, the fake
Telegram) for the other tests of this folder, by their own names."""

from .test_telegram_phone import (  # noqa: F401 - fixtures, found by name
    _no_redactor_left_on_the_shared_capture_handler,
    client,
    desk_root,
    engine,
    fake,
)
