"""cmd_ask_document picks whole-doc vs retrieval based on the cutoff."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def fake_chat_complete():
    """Patch the provider-agnostic chat_complete so tests run offline."""
    fake_result = SimpleNamespace(text="ANSWER", cost_usd=0.001, finish_reason="stop")
    mock = AsyncMock(return_value=fake_result)
    with patch("unread.ask.sources.core.chat_complete", mock):
        yield mock


async def test_under_cutoff_calls_chat_complete_once_with_full_text(
    fake_chat_complete, monkeypatch, tmp_path
) -> None:
    """Short text → exactly one chat_complete call carrying the entire extracted text."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    from unread.ask.sources.core import DocCitation, cmd_ask_document

    text = "This is a short article body about cats."
    await cmd_ask_document(
        extracted_text=text,
        citations=[DocCitation(uri="https://example.com", label="p.1", offset_start=0, offset_end=len(text))],
        source_label="example.com",
        source_id="abc123",
        content_hash="def456",
        question="What is the article about?",
        no_followup=True,
    )
    assert fake_chat_complete.await_count == 1
    _, kwargs = fake_chat_complete.await_args
    messages = kwargs["messages"]
    user_content = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    assert text in user_content
    assert "What is the article about?" in user_content


async def test_over_cutoff_invokes_retrieval_path(fake_chat_complete, monkeypatch, tmp_path) -> None:
    """Text over cutoff → retrieval path runs and feeds top-K to one chat_complete call."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    # Force the cutoff low so the test text trips it without needing 32k tokens.
    from unread.config import get_settings

    s = get_settings()
    s.ask.doc_full_text_cutoff_tokens = 50  # very small cutoff for test

    from unread.ask.sources.core import DocCitation, cmd_ask_document

    long_text = " ".join(["paragraph " + str(i) for i in range(500)])
    await cmd_ask_document(
        extracted_text=long_text,
        citations=[
            DocCitation(uri="file:///tmp/big.txt", label="p.1", offset_start=0, offset_end=len(long_text))
        ],
        source_label="big.txt",
        source_id="aaa",
        content_hash="bbb",
        question="Summarize",
        no_followup=True,
    )
    # Retrieval path still ends with one chat_complete call.
    assert fake_chat_complete.await_count == 1
    _, kwargs = fake_chat_complete.await_args
    messages = kwargs["messages"]
    user_content = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    # User content is the retrieved-chunks payload, NOT the full text.
    assert len(user_content) < len(long_text)
    assert "Summarize" in user_content
