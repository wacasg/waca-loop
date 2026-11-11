"""Improvement loop — 特定 (identify) → 課題化 (tasks) → 抽出候補 (candidates).

Turns survey feedback into improvement tasks and turns AI-extracted term/SFAQ/ERN candidates
into approved grounding (AI提案＋利用者承認). Identification reuses `retrieval.hybrid_search`.
"""

from waca_loop.improvement.acl import task_acl_where_clause, task_visible_to
from waca_loop.improvement.candidates import (
    BigQueryCandidateStore,
    InMemoryCandidateStore,
    approve_candidate,
)
from waca_loop.improvement.identify import identify_content
from waca_loop.improvement.tasks import (
    BigQueryTaskStore,
    InMemoryTaskStore,
    make_tasks_from_response,
)

__all__ = [
    "task_visible_to",
    "task_acl_where_clause",
    "identify_content",
    "make_tasks_from_response",
    "BigQueryTaskStore",
    "InMemoryTaskStore",
    "BigQueryCandidateStore",
    "InMemoryCandidateStore",
    "approve_candidate",
]
