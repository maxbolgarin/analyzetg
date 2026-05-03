"""Regression tests for AskCfg.doc_full_text_cutoff_tokens."""

from __future__ import annotations


def test_ask_cfg_default_doc_cutoff_is_32000() -> None:
    """The new doc_full_text_cutoff_tokens defaults to 32000."""
    from unread.config import AskCfg

    cfg = AskCfg()
    assert cfg.doc_full_text_cutoff_tokens == 32000


def test_ask_cfg_doc_cutoff_can_be_overridden() -> None:
    """The new field accepts overrides via ctor kwargs."""
    from unread.config import AskCfg

    cfg = AskCfg(doc_full_text_cutoff_tokens=8000)
    assert cfg.doc_full_text_cutoff_tokens == 8000
