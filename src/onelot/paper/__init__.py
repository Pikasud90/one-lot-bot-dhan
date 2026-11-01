"""Paper book: every desk alert opens a paper trade; nothing here can send an order."""

from .book import PaperAlertError, PaperBook, PaperEvent, PaperTrade

__all__ = ["PaperAlertError", "PaperBook", "PaperEvent", "PaperTrade"]
