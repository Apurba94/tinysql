# TinySQL

A relational database engine written from scratch in pure Python, to show how a real database works inside. It stores data in a single file, survives crashes, and answers SQL.

```python
from tinysql import connect

db = connect("school.db")
db.execute("CREATE TABLE learners (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER)")
db.execute("INSERT INTO learners (name, age) VALUES (?, ?)", ("Rahima", 14))
print(db.query("SELECT name FROM learners WHERE age > 12 ORDER BY name"))
```

Or use the shell:

```
python -m tinysql school.db
```

## What is inside

| Layer | File | How it works |
|---|---|---|
| Pages | `storage.py` | The file is split into 4 KB pages, with a free list so deleted space is reused |
| Crash safety | `storage.py` | A commit writes a checksummed record to a log and syncs it before touching the main file; opening the file replays complete commits and ignores torn ones |
| B+ tree | `storage.py` | Tables and indexes are B+ trees with linked leaves; large values spill into overflow pages |
| SQL | `parser.py` | A hand-written tokeniser and recursive-descent parser |
| Planner | `engine.py` | Uses rowid lookups and index ranges when it can, and a hash join on column equality; `EXPLAIN` shows the choice |
| Executor | `engine.py` | Three-valued NULL logic, joins, `GROUP BY`, `HAVING`, `DISTINCT`, subqueries, `CASE`, `LIKE` and more |

## How it is tested

Besides unit tests, one test runs 400 random queries, with random updates and deletes in between, on both TinySQL and SQLite and requires the same answers. Other tests stop a commit part way through and check that recovery gives either the whole transaction or none of it.

## Limits

This is a teaching engine, so use SQLite for real data. It allows one writer at a time with no locking between processes, holds an open transaction in memory, supports single-column indexes, and does not merge underfull B+ tree nodes after deletes.

## Tests

```
python -m unittest discover -s tests
```

MIT licence.
