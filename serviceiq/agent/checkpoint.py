"""Where the agent's conversation state is saved: Postgres in the cloud, a SQLite file locally.

One LangGraph thread per conversation, so a conversation resumes after a restart (and, in Phase 3, after an
approval). Checkpoints are deleted with the conversation when the retention period ends.
"""
import sqlite3
from pathlib import Path

from serviceiq.config import Settings


def make_checkpointer(settings: Settings):
    url = settings.database_url
    if url.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(url.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path.with_name(path.stem + "-checkpoints.db"), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    conninfo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    pool = ConnectionPool(conninfo, max_size=5, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool)
    saver.setup()
    return saver
