import asyncio
import time

from app.config import get_sqlite_settings
from app.db import db_connection
from app.search import hybrid_search


async def run():
    query = "как только мастер мастер истина"
    db_path = get_sqlite_settings()

    print(f"Database path: {db_path}")
    print(f"Testing query: '{query}'")

    modes = ["hybrid", "quote", "lexical", "semantic"]

    with db_connection(db_path) as conn:
        for mode in modes:
            start_time = time.perf_counter()
            results = await hybrid_search(conn, query, search_mode=mode, limit=10)
            elapsed = time.perf_counter() - start_time
            print(f"\n=== Mode: {mode} (took {elapsed:.4f}s, found {len(results)} results) ===")
            for i, r in enumerate(results[:10]):
                print(
                    f" {i + 1}. [Score: {r.combined_score:.4f} | Lex: {r.lexical_score:.4f} | Sem: {r.semantic_score:.4f}] Video: {r.title} (ID: {r.video_id}), Start: {r.start_ts}"
                )
                print(f"    Text: {r.text[:200]}...")


if __name__ == "__main__":
    asyncio.run(run())
