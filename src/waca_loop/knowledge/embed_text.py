"""What text a question's embedding is computed over (SPEC_phase2 §2.1).

**Why this module exists.** Computing the embedding over ``question_text`` alone would make
retrieval quality depend on the quality of a regex: importing lessons from an external corpus
turns a declarative lesson into a question by rewriting its ending, and where no rule fires
the "question" is the original sentence with a suffix glued on.
Such rows search badly — not because vectors are weak at absorbing 文体の揺らぎ, but
because the embedded string would be the rewritten headline rather than the lesson itself.

**The rule.** The lesson body (``feedback.body`` of the DECISION feedback) is the data;
``question_text`` is a headline over it. The embedding covers **both**, body first. A
headline is a derived artifact — improve the rewrite rules and regenerate it, no
re-embedding of the corpus required, because the body carries the meaning either way.

Pure stdlib (INV-2). The BigQuery twin builds the same concatenation in SQL
(``scripts/seed_sfaq.py`` backfill) — if you change the shape here, change it there.
"""

from __future__ import annotations

SEPARATOR = "\n"

# Long bodies are truncated before embedding: the models have an input limit and the
# lesson's meaning is front-loaded. Keep this in step with the SQL twin's SUBSTR.
MAX_BODY_CHARS = 2000


def embedding_text(question_text: str, body: str | None = None) -> str:
    """Return the string a question's embedding is computed over.

    Body first, headline second — the body is the data and the headline is a label for it.
    Either half may be empty; with no body only the headline is used,
    which is what a question carrying no DECISION feedback yet should do.
    """
    parts = []
    if body:
        parts.append(body.strip()[:MAX_BODY_CHARS])
    if question_text:
        parts.append(question_text.strip())
    return SEPARATOR.join(p for p in parts if p)
