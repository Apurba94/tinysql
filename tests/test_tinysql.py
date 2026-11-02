import os
import random
import shutil
import sqlite3
import tempfile
import unittest

from tinysql import SqlError, connect
from tinysql.storage import BTree, Pager, SimulatedCrash, encode_key


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir)

    def reopen(self):
        self.db.close()
        self.db = connect(self.path)


class TestBTree(Base):
    def test_many_keys_stay_sorted_and_survive_reopen(self):
        p = Pager(os.path.join(self.dir, "b.db"))
        t = BTree(p)
        keys = [f"{i:06d}".encode() for i in range(5000)]
        shuffled = keys[:]
        random.Random(1).shuffle(shuffled)
        for k in shuffled:
            t.put(k, b"v" + k)
        p.root = t.root
        p.commit()
        root = t.root
        p.close()
        p = Pager(os.path.join(self.dir, "b.db"))
        t = BTree(p, root)
        self.assertEqual([k for k, _ in t.scan()], keys)
        self.assertEqual(t.get(b"002500"), b"v002500")
        self.assertIsNone(t.get(b"nope"))
        self.assertEqual([k for k, _ in t.scan(b"000010", b"000012")], [b"000010", b"000011", b"000012"])
        self.assertEqual([k for k, _ in t.scan(b"000010", b"000012", True)], [b"000010", b"000011"])
        p.close()

    def test_delete_and_large_values(self):
        p = Pager(os.path.join(self.dir, "b.db"))
        t = BTree(p)
        big = os.urandom(20000)
        t.put(b"a", big)
        t.put(b"b", b"x")
        self.assertEqual(t.get(b"a"), big)
        t.put(b"a", b"small")
        self.assertEqual(t.get(b"a"), b"small")
        self.assertTrue(t.delete(b"b"))
        self.assertFalse(t.delete(b"b"))
        self.assertEqual([k for k, _ in t.scan()], [b"a"])
        p.close()

    def test_freed_pages_are_reused(self):
        p = Pager(os.path.join(self.dir, "b.db"))
        t = BTree(p)
        t.put(b"a", os.urandom(50000))
        p.commit()
        before = p.count
        t.delete(b"a")
        p.commit()
        t.put(b"b", os.urandom(50000))
        p.commit()
        self.assertEqual(p.count, before)
        p.close()

    def test_key_encoding_preserves_order(self):
        values = [None, -1e300, -5, -0.5, 0, 0.5, 3, 7.5, 1e300, "", "a", "a\x00b", "ab", "b", "é"]
        encoded = [encode_key(v) for v in values]
        self.assertEqual(encoded, sorted(encoded))


class TestSql(Base):
    def seed(self):
        self.db.execute("""
            CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, dept INTEGER, pay REAL DEFAULT 10.5);
            INSERT INTO dept (name) VALUES ('ict'), ('wash'), ('empty');
            INSERT INTO emp (name, dept, pay) VALUES ('a', 1, 100), ('b', 1, 200), ('c', 2, 300), ('d', NULL, 50);
        """)

    def test_select_where_order_limit(self):
        self.seed()
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE pay >= 100 ORDER BY pay DESC LIMIT 2"), [["c"], ["b"]])
        self.assertEqual(self.db.query("SELECT name FROM emp ORDER BY pay LIMIT 1 OFFSET 1"), [["a"]])

    def test_null_logic(self):
        self.seed()
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE dept = NULL"), [])
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE dept IS NULL"), [["d"]])
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE NOT dept = 1 ORDER BY name"), [["c"]])
        self.assertEqual(self.db.query("SELECT NULL AND 0, NULL OR 1, NULL AND 1"), [[0, 1, None]])

    def test_joins(self):
        self.seed()
        self.assertEqual(self.db.query("SELECT e.name, d.name FROM emp e JOIN dept d ON e.dept = d.id ORDER BY e.name"), [["a", "ict"], ["b", "ict"], ["c", "wash"]])
        self.assertEqual(self.db.query("SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept = d.id WHERE e.name IS NULL"), [["empty", None]])
        self.assertEqual(len(self.db.query("SELECT * FROM emp, dept")), 12)
        self.assertEqual(self.db.query("SELECT e.name FROM emp e JOIN dept d ON e.pay > 250 AND d.id = 1"), [["c"]])

    def test_group_by_having(self):
        self.seed()
        self.assertEqual(self.db.query("SELECT dept, COUNT(*), SUM(pay) FROM emp WHERE dept IS NOT NULL GROUP BY dept HAVING COUNT(*) > 1"), [[1, 2, 300.0]])
        self.assertEqual(self.db.query("SELECT COUNT(*), MIN(pay), MAX(name), AVG(pay) FROM emp"), [[4, 50.0, "d", 162.5]])
        self.assertEqual(self.db.query("SELECT COUNT(*), SUM(pay) FROM emp WHERE id > 99"), [[0, None]])
        self.assertEqual(self.db.query("SELECT COUNT(DISTINCT dept) FROM emp"), [[2]])

    def test_update_delete_defaults(self):
        self.seed()
        self.assertEqual(self.db.query("UPDATE emp SET pay = pay * 2 WHERE dept = 1"), [[2]])
        self.assertEqual(self.db.query("SELECT pay FROM emp WHERE name = 'b'"), [[400.0]])
        self.assertEqual(self.db.query("DELETE FROM emp WHERE dept = 1"), [[2]])
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM emp"), [[2]])
        self.db.execute("INSERT INTO emp (name) VALUES ('z')")
        self.assertEqual(self.db.query("SELECT pay FROM emp WHERE name = 'z'"), [[10.5]])

    def test_constraints(self):
        self.seed()
        with self.assertRaises(SqlError):
            self.db.execute("INSERT INTO dept (name) VALUES ('ict')")
        with self.assertRaises(SqlError):
            self.db.execute("INSERT INTO dept (name) VALUES (NULL)")
        with self.assertRaises(SqlError):
            self.db.execute("INSERT INTO emp (id, name) VALUES (1, 'dup')")
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM dept"), [[3]])

    def test_failed_multi_row_insert_leaves_nothing(self):
        self.seed()
        with self.assertRaises(SqlError):
            self.db.execute("INSERT INTO dept (name) VALUES ('new'), ('ict')")
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM dept WHERE name = 'new'"), [[0]])

    def test_expressions_and_functions(self):
        self.assertEqual(self.db.query("SELECT 7 / 2, 7 % 3, 1 + 2 * 3, 'a' || 'b', ABS(-3), LENGTH('abc'), UPPER('x')"), [[3, 1, 7, "ab", 3, 3, "X"]])
        self.assertEqual(self.db.query("SELECT CASE WHEN 1 > 2 THEN 'x' ELSE 'y' END, COALESCE(NULL, 2), ROUND(2.567, 1)"), [["y", 2, 2.6]])
        self.assertEqual(self.db.query("SELECT 'abc' LIKE 'A%', 5 BETWEEN 1 AND 9, 2 IN (1, 2), 3 NOT IN (1, 2)"), [[1, 1, 1, 1]])

    def test_subqueries_and_params(self):
        self.seed()
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE dept IN (SELECT id FROM dept WHERE name = 'wash')"), [["c"]])
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE pay = (SELECT MAX(pay) FROM emp)"), [["c"]])
        self.assertEqual(self.db.query("SELECT name FROM emp WHERE name = ? AND pay > ?", ("a", 1)), [["a"]])

    def test_sql_injection_through_params_is_just_text(self):
        self.seed()
        self.db.execute("INSERT INTO emp (name) VALUES (?)", ("x'; DROP TABLE emp; --",))
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM emp"), [[5]])

    def test_errors(self):
        self.seed()
        for bad in ("SELECT nope FROM emp", "SELECT * FROM nope", "SELEC 1", "SELECT id FROM emp, dept"):
            with self.assertRaises(SqlError):
                self.db.execute(bad)


class TestIndexes(Base):
    def setUp(self):
        super().setUp()
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)")
        self.db.execute("BEGIN")
        for i in range(2000):
            self.db.execute("INSERT INTO t (a, b) VALUES (?, ?)", (i % 100, f"s{i}"))
        self.db.execute("COMMIT")

    def test_plans(self):
        plan = lambda q: self.db.query("EXPLAIN " + q)[0][0]
        self.assertIn("FULL SCAN", plan("SELECT * FROM t WHERE a = 5"))
        self.assertIn("ROWID LOOKUP", plan("SELECT * FROM t WHERE id = 5"))
        self.db.execute("CREATE INDEX ia ON t (a)")
        self.assertIn("INDEX RANGE SCAN", plan("SELECT * FROM t WHERE a = 5"))
        self.assertIn("INDEX RANGE SCAN", plan("SELECT * FROM t WHERE a > 5 AND a < 8"))

    def test_index_gives_same_answers(self):
        q = ["SELECT id FROM t WHERE a = 7 ORDER BY id", "SELECT id FROM t WHERE a > 90 ORDER BY id", "SELECT id FROM t WHERE a < 3 ORDER BY id", "SELECT id FROM t WHERE a BETWEEN 4 AND 6 ORDER BY id", "SELECT id FROM t WHERE 5 >= a ORDER BY id"]
        before = [self.db.query(x) for x in q]
        self.db.execute("CREATE INDEX ia ON t (a)")
        self.assertEqual([self.db.query(x) for x in q], before)
        self.db.execute("UPDATE t SET a = 1000 WHERE a = 7")
        self.db.execute("DELETE FROM t WHERE a = 8")
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM t WHERE a = 1000"), [[20]])
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM t WHERE a = 8"), [[0]])
        self.assertEqual(self.db.check(), [])

    def test_unique_index_rejects_existing_duplicates(self):
        with self.assertRaises(SqlError):
            self.db.execute("CREATE UNIQUE INDEX ua ON t (a)")
        self.assertEqual(self.db.check(), [])

    def test_null_is_not_found_by_range(self):
        self.db.execute("CREATE INDEX ia ON t (a)")
        self.db.execute("INSERT INTO t (a, b) VALUES (NULL, 'n')")
        self.assertEqual(self.db.query("SELECT b FROM t WHERE a < 0"), [])


class TestTransactions(Base):
    def test_rollback(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("INSERT INTO t VALUES (1)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t VALUES (2)")
        self.db.execute("CREATE TABLE u (b INTEGER)")
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM t"), [[2]])
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM t"), [[1]])
        with self.assertRaises(SqlError):
            self.db.query("SELECT * FROM u")

    def test_data_survives_reopen(self):
        self.db.execute("CREATE TABLE t (a TEXT)")
        self.db.execute("INSERT INTO t VALUES ('kept')")
        self.reopen()
        self.assertEqual(self.db.query("SELECT a FROM t"), [["kept"]])

    def test_crash_after_log_replays_the_commit(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("BEGIN")
        for i in range(500):
            self.db.execute("INSERT INTO t VALUES (?)", (i,))
        self.db.pager.crash_at = "after-log"
        with self.assertRaises(SimulatedCrash):
            self.db.execute("COMMIT")
        self.db.pager.close()
        self.db = connect(self.path)
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM t"), [[500]])
        self.assertFalse(os.path.exists(self.path + "-wal"))

    def test_torn_log_is_ignored(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("INSERT INTO t VALUES (1)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t VALUES (2)")
        self.db.pager.crash_at = "torn-log"
        with self.assertRaises(SimulatedCrash):
            self.db.execute("COMMIT")
        self.db.pager.close()
        self.db = connect(self.path)
        self.assertEqual(self.db.query("SELECT a FROM t"), [[1]])


class TestAgainstSqlite(Base):
    """Run random statements on both engines: every result must match SQLite's."""

    def test_random_workload(self):
        rng = random.Random(7)
        ref = sqlite3.connect(":memory:")
        schema = "CREATE TABLE a (id INTEGER PRIMARY KEY, x INTEGER, y REAL, s TEXT); CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER, v INTEGER)"
        ref.executescript(schema)
        self.db.execute(schema)
        self.db.execute("CREATE INDEX ax ON a (x); CREATE INDEX bv ON b (v)")
        words = ["ant", "bee", "cat", "dog", "Elk", None, ""]
        for _ in range(300):
            vals = (rng.choice([None, rng.randint(-20, 20)]), rng.choice([None, rng.randint(-40, 40) / 4]), rng.choice(words))
            ref.execute("INSERT INTO a (x, y, s) VALUES (?, ?, ?)", vals)
            self.db.execute("INSERT INTO a (x, y, s) VALUES (?, ?, ?)", vals)
            vals = (rng.randint(1, 320), rng.choice([None, rng.randint(0, 9)]))
            ref.execute("INSERT INTO b (a_id, v) VALUES (?, ?)", vals)
            self.db.execute("INSERT INTO b (a_id, v) VALUES (?, ?)", vals)
        conds = ["x > 3", "x <= -2", "x = 5", "y < 0", "y BETWEEN -2 AND 3", "s = 'cat'", "s LIKE 'd%'", "s IS NULL", "x IS NOT NULL", "x IN (1, 2, 3)", "x + y > 2", "x % 3 = 1", "NOT x > 0", "s <> 'bee'", "x > 0 OR y < 0", "x > 0 AND s IS NOT NULL", "id < 50", "id = 17"]
        checked = 0
        for _ in range(400):
            c1, c2 = rng.choice(conds), rng.choice(conds)
            shape = rng.randrange(7)
            if shape == 0:
                q = f"SELECT id, x, y, s FROM a WHERE {c1} ORDER BY id"
            elif shape == 1:
                q = f"SELECT x, COUNT(*), SUM(y), MIN(s), MAX(id) FROM a WHERE {c1} GROUP BY x ORDER BY x"
            elif shape == 2:
                q = f"SELECT a.id, b.v FROM a JOIN b ON b.a_id = a.id WHERE {c1.replace('id', 'a.id') if c1.startswith('id') else c1} ORDER BY a.id, b.id"
            elif shape == 3:
                q = f"SELECT a.id, b.id FROM a LEFT JOIN b ON b.a_id = a.id AND b.v > 5 WHERE a.id < 40 ORDER BY a.id, b.id"
            elif shape == 4:
                q = f"SELECT DISTINCT s FROM a WHERE {c1} ORDER BY s DESC"
            elif shape == 5:
                q = f"SELECT s, COUNT(x), AVG(y) FROM a WHERE {c1} AND {c2} GROUP BY s HAVING COUNT(*) > 1 ORDER BY 2 DESC, s"
            else:
                q = f"SELECT id, x * 2 - id, CASE WHEN x > 0 THEN 'p' ELSE 'n' END FROM a WHERE {c1} ORDER BY x, id DESC LIMIT 7 OFFSET 2"
            want = [list(r) for r in ref.execute(q).fetchall()]
            got = self.db.query(q)
            self.assertEqual(len(got), len(want), q)
            for g, w in zip(got, want):
                for p, r in zip(g, w):
                    if isinstance(r, float) or isinstance(p, float):
                        self.assertAlmostEqual(p, r, places=9, msg=q)
                    else:
                        self.assertEqual(p, r, q)
            checked += 1
            if rng.random() < 0.15:
                u = f"UPDATE a SET x = x + 1 WHERE {c1}" if rng.random() < 0.5 else f"DELETE FROM b WHERE v = {rng.randint(0, 9)}"
                ref.execute(u)
                self.db.execute(u)
        self.assertEqual(checked, 400)
        self.assertEqual(self.db.check(), [])


if __name__ == "__main__":
    unittest.main()
