"""`atg ask` — Q&A across the synced corpus.

Lightweight retrieval (keyword LIKE on `messages.text` + recency weighting)
followed by a single LLM call with citations. No vector DB, no embeddings —
intentional: the corpus is your already-synced Telegram chats, the answer
quality from a flagship model on 100–300 well-chosen messages is high
enough that we get away without semantic retrieval. Re-evaluate if users
report retrieval misses on small or vague questions.
"""

from analyzetg.ask.commands import cmd_ask
from analyzetg.ask.retrieval import retrieve_messages

__all__ = ["cmd_ask", "retrieve_messages"]
