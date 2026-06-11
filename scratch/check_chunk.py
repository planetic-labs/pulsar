import sqlite3


def check():
    conn = sqlite3.connect("data/pulsar.db")
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT id, chunk_index, text FROM chunks WHERE video_id = 7017 LIMIT 10").fetchall()
    print("Chunks of video 7017 in SQLite:")
    for r in rows:
        print(f"  ID: {r['id']}, Chunk Index: {r['chunk_index']}, Text: {r['text'][:100]}...")
    conn.close()


if __name__ == "__main__":
    check()
