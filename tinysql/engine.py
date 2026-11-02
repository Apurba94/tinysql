"""The database: catalog, query planning and execution."""
import json
import math
import re
import struct
from collections import defaultdict
from functools import cmp_to_key

from .parser import SqlError, parse
from .storage import BTree, Pager, encode_key

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL"}
OPS = {"=": lambda c: c == 0, "<>": lambda c: c != 0, "<": lambda c: c < 0, "<=": lambda c: c <= 0, ">": lambda c: c > 0, ">=": lambda c: c >= 0}
FLIP = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def rank(v):
    return 0 if v is None else 1 if isinstance(v, (int, float)) else 2


def compare(a, b):
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 0:
        return 0
    return -1 if a < b else 1 if a > b else 0


def order_key(values, flags):
    def cmp(x, y):
        for a, b, desc in zip(x[0], y[0], flags):
            c = compare(a, b)
            if c:
                return -c if desc else c
        return 0
    return cmp_to_key(cmp)


def affinity(value, kind):
    """Coerce a value to the column's declared type, the way SQLite's affinity does."""
    if value is None:
        return None
    if kind in ("INTEGER", "INT"):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                n = float(value)
                return int(n) if n.is_integer() else n
            except ValueError:
                return value
        return value
    if kind in ("REAL", "FLOAT", "DOUBLE"):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except ValueError:
            return value
    if kind in ("TEXT", "VARCHAR", "CHAR") and isinstance(value, (int, float)):
        return str(value)
    return value


def truth(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return float(v) != 0
        except ValueError:
            return False
    return v != 0


def as_number(v):
    if isinstance(v, str):
        m = re.match(r"\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", v)
        if not m:
            return 0
        n = float(m.group())
        return int(n) if n.is_integer() and "." not in m.group() and "e" not in m.group().lower() else n
    return v


def sql_text(v):
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return f"{v:.1f}"
    return str(v)


def like(value, pattern):
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value, re.I | re.S) is not None


def call(name, a):
    if name == "COALESCE":
        return next((v for v in a if v is not None), None)
    if name == "IFNULL":
        return a[0] if a[0] is not None else a[1]
    if name == "NULLIF":
        return None if compare(a[0], a[1]) == 0 else a[0]
    if any(v is None for v in a):
        return None
    if name == "ABS":
        return abs(as_number(a[0]))
    if name == "LENGTH":
        return len(sql_text(a[0]))
    if name == "LOWER":
        return sql_text(a[0]).lower()
    if name == "UPPER":
        return sql_text(a[0]).upper()
    if name == "TRIM":
        return sql_text(a[0]).strip()
    if name == "ROUND":
        digits = int(a[1]) if len(a) > 1 else 0
        x = float(as_number(a[0]))
        f = 10 ** digits
        return math.floor(abs(x) * f + 0.5) / f * (1 if x >= 0 else -1)
    if name in ("SUBSTR", "SUBSTRING"):
        s, start = sql_text(a[0]), int(a[1])
        start = start - 1 if start > 0 else max(len(s) + start, 0) if start < 0 else 0
        return s[start:start + int(a[2])] if len(a) > 2 else s[start:]
    if name == "REPLACE":
        return sql_text(a[0]).replace(sql_text(a[1]), sql_text(a[2]))
    raise SqlError(f"no such function: {name}")


class Scope:
    """Resolves column references against the tables in a query."""

    def __init__(self):
        self.cols = []        # (alias, column name)

    def add(self, alias, names):
        for n in names:
            self.cols.append((alias, n))

    def find(self, table, name):
        low = name.lower()
        hits = [i for i, (a, n) in enumerate(self.cols) if n.lower() == low and (table is None or a.lower() == table.lower())]
        if not hits:
            raise SqlError(f"no such column: {table + '.' if table else ''}{name}")
        if len(hits) > 1:
            raise SqlError(f"ambiguous column: {name}")
        return hits[0]


def has_aggregate(e):
    if not isinstance(e, tuple):
        return False
    if e[0] == "call" and e[1] in AGGREGATES and (len(e[2]) <= 1):
        return True
    if e[0] in ("subq", "insel"):
        return e[0] == "insel" and has_aggregate(e[1])
    return any(has_aggregate(x) or (isinstance(x, list) and any(has_aggregate(y) for y in x)) for x in e[1:])


class Evaluator:
    def __init__(self, db, scope, params):
        self.db, self.scope, self.params = db, scope, params

    def __call__(self, e, row, agg=None):
        k = e[0]
        if k == "lit":
            return e[1]
        if k == "col":
            return row[self.scope.find(e[1], e[2])]
        if k == "param":
            return self.params[e[1]]
        if agg is not None and k == "call" and id(e) in agg:
            return agg[id(e)]
        if k == "and":
            a = truth(self(e[1], row, agg))
            if a is False:
                return 0
            b = truth(self(e[2], row, agg))
            return 0 if b is False else None if a is None or b is None else 1
        if k == "or":
            a = truth(self(e[1], row, agg))
            if a is True:
                return 1
            b = truth(self(e[2], row, agg))
            return 1 if b is True else None if a is None or b is None else 0
        if k == "not":
            a = truth(self(e[1], row, agg))
            return None if a is None else 0 if a else 1
        if k == "cmp":
            a, b = self(e[2], row, agg), self(e[3], row, agg)
            return None if a is None or b is None else 1 if OPS[e[1]](compare(a, b)) else 0
        if k == "bin":
            a, b = self(e[2], row, agg), self(e[3], row, agg)
            if a is None or b is None:
                return None
            op = e[1]
            if op == "||":
                return sql_text(a) + sql_text(b)
            a, b = as_number(a), as_number(b)
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                if b == 0:
                    return None
                if isinstance(a, int) and isinstance(b, int):
                    q = abs(a) // abs(b)
                    return q if (a >= 0) == (b >= 0) else -q
                return a / b
            if b == 0:
                return None
            a, b = int(a), int(b)
            return int(math.fmod(a, b))
        if k == "neg":
            a = self(e[1], row, agg)
            return None if a is None else -as_number(a)
        if k == "isnull":
            a = self(e[1], row, agg) is None
            return 1 if a != e[2] else 0
        if k == "in" or k == "insel":
            v = self(e[1], row, agg)
            items = [self(x, row, agg) for x in e[2]] if k == "in" else [r[0] for r in self.db._select(e[2], self.params)[1]]
            if v is None:
                return None if items else 0
            if any(x is not None and compare(v, x) == 0 for x in items):
                return 1
            return None if any(x is None for x in items) else 0
        if k == "like":
            a, b = self(e[1], row, agg), self(e[2], row, agg)
            return None if a is None or b is None else 1 if like(sql_text(a), sql_text(b)) else 0
        if k == "between":
            v, lo, hi = (self(x, row, agg) for x in e[1:])
            if v is None or lo is None or hi is None:
                return None
            return 1 if compare(v, lo) >= 0 and compare(v, hi) <= 0 else 0
        if k == "case":
            base = None if e[1] is None else self(e[1], row, agg)
            for cond, value in e[2]:
                c = self(cond, row, agg)
                if (e[1] is None and truth(c)) or (e[1] is not None and base is not None and c is not None and compare(base, c) == 0):
                    return self(value, row, agg)
            return self(e[3], row, agg)
        if k == "subq":
            rows = self.db._select(e[1], self.params)[1]
            return rows[0][0] if rows else None
        if k == "call":
            return call(e[1], [self(x, row, agg) for x in e[2]])
        raise SqlError(f"cannot evaluate {k}")


def aggregates_in(e, out):
    if not isinstance(e, tuple):
        return out
    if e[0] == "call" and e[1] in AGGREGATES and len(e[2]) <= 1:
        out.append(e)
        return out
    for x in e[1:]:
        if isinstance(x, tuple):
            aggregates_in(x, out)
        elif isinstance(x, list):
            for y in x:
                aggregates_in(y, out)
                if isinstance(y, tuple) and len(y) == 2 and isinstance(y[0], tuple):
                    aggregates_in(y[0], out)
                    aggregates_in(y[1], out)
    return out


def compute_aggregate(name, values, mode):
    if mode == "star":
        return len(values)
    vals = [v for v in values if v is not None]
    if mode == "distinct":
        seen, uniq = set(), []
        for v in vals:
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        vals = uniq
    if name == "COUNT":
        return len(vals)
    if name == "TOTAL":
        return float(sum(as_number(v) for v in vals))
    if not vals:
        return None
    if name == "SUM":
        return sum(as_number(v) for v in vals)
    if name == "AVG":
        return sum(as_number(v) for v in vals) / len(vals)
    best = vals[0]
    for v in vals[1:]:
        c = compare(v, best)
        if (name == "MIN" and c < 0) or (name == "MAX" and c > 0):
            best = v
    return best


class Table:
    def __init__(self, db, meta):
        self.db = db
        self.meta = meta
        self.name = meta["name"]
        self.cols = meta["columns"]
        self.names = [c["name"] for c in self.cols]
        self.index_of = {n.lower(): i for i, n in enumerate(self.names)}
        self.tree = BTree(db.pager, meta["root"])

    def rows(self):
        for k, v in self.tree.scan():
            yield struct.unpack(">Q", k)[0], json.loads(v)

    def get(self, rowid):
        v = self.tree.get(struct.pack(">Q", rowid))
        return None if v is None else json.loads(v)

    def index_tree(self, idx):
        return BTree(self.db.pager, idx["root"])

    def pk_column(self):
        for i, c in enumerate(self.cols):
            if c["pk"] and c["type"] in ("INTEGER", "INT"):
                return i
        return None


class Database:
    """Open with `Database(path)`, then call `execute(sql, params)`."""

    def __init__(self, path):
        self.pager = Pager(path)
        self.in_txn = False
        self.last_rowid = 0
        self.catalog_tree = BTree(self.pager, self.pager.root)
        self._sync_root()
        self.pager.commit()
        self.tables = {}
        for _, v in self.catalog_tree.scan():
            meta = json.loads(v)
            self.tables[meta["name"].lower()] = meta

    def _sync_root(self):
        self.pager.root = self.catalog_tree.root

    def _save_meta(self, meta):
        self.catalog_tree.put(meta["name"].lower().encode(), json.dumps(meta).encode())
        self._sync_root()

    def table(self, name):
        meta = self.tables.get(name.lower())
        if not meta:
            raise SqlError(f"no such table: {name}")
        return Table(self, meta)

    def close(self):
        if self.in_txn:
            self.rollback()
        self.pager.close()

    def rollback(self):
        self.pager.rollback()
        self.in_txn = False
        self.catalog_tree = BTree(self.pager, self.pager.root)
        self.tables = {}
        for _, v in self.catalog_tree.scan():
            meta = json.loads(v)
            self.tables[meta["name"].lower()] = meta

    def execute(self, sql, params=()):
        """Run one or more statements and return the last result: (column names, rows)."""
        result = ([], [])
        for stmt in parse(sql):
            result = self._run(stmt, list(params))
        return result

    def query(self, sql, params=()):
        return self.execute(sql, params)[1]

    def _run(self, s, params):
        op = s["op"]
        if op == "select":
            return self._select(s, params)
        if op == "explain":
            return (["plan"], [[line] for line in self._explain(s["stmt"], params)])
        if op == "begin":
            if self.in_txn:
                raise SqlError("already in a transaction")
            self.in_txn = True
            return [], []
        if op == "commit":
            self.pager.commit()
            self.in_txn = False
            return [], []
        if op == "rollback":
            self.rollback()
            return [], []
        try:
            result = getattr(self, "_" + op)(s, params)
        except (SqlError, ValueError):
            if not self.in_txn:
                self.rollback()
            raise
        if not self.in_txn:
            self.pager.commit()
        return result

    def _create_table(self, s, params):
        key = s["name"].lower()
        if key in self.tables:
            if s["if_not_exists"]:
                return [], []
            raise SqlError(f"table {s['name']} already exists")
        if sum(c["pk"] for c in s["columns"]) > 1:
            raise SqlError("only one PRIMARY KEY column is supported")
        seen = set()
        for c in s["columns"]:
            if c["name"].lower() in seen:
                raise SqlError(f"duplicate column: {c['name']}")
            seen.add(c["name"].lower())
            if c["default"] is not None:
                c["default"] = c["default"][1] if c["default"][0] == "lit" else None
        tree = BTree(self.pager)
        meta = {"name": s["name"], "columns": s["columns"], "root": tree.root, "next": 1, "indexes": []}
        self.tables[key] = meta
        self._save_meta(meta)
        for c in s["columns"]:
            if (c["unique"] or c["pk"]) and not (c["pk"] and c["type"] in ("INTEGER", "INT")):
                self._add_index(meta, f"auto_{s['name']}_{c['name']}", c["name"], True)
        return [], []

    def _add_index(self, meta, name, column, unique):
        table = Table(self, meta)
        col = table.index_of.get(column.lower())
        if col is None:
            raise SqlError(f"no such column: {column}")
        tree = BTree(self.pager)
        idx = {"name": name, "column": col, "unique": unique, "root": tree.root}
        for rowid, row in table.rows():
            self._index_add(tree, idx, row, rowid, table)
        idx["root"] = tree.root
        meta["indexes"].append(idx)
        self._save_meta(meta)

    def _index_add(self, tree, idx, row, rowid, table):
        v = row[idx["column"]]
        key = encode_key(v)
        if idx["unique"] and v is not None and any(True for _ in tree.scan(key + b"\x00" * 8, key + b"\xff" * 8)):
            raise SqlError(f"UNIQUE constraint failed: {table.name}.{table.names[idx['column']]}")
        tree.put(key + struct.pack(">Q", rowid), b"")

    def _create_index(self, s, params):
        meta = self.tables.get(s["table"].lower())
        if not meta:
            raise SqlError(f"no such table: {s['table']}")
        if any(i["name"].lower() == s["name"].lower() for m in self.tables.values() for i in m["indexes"]):
            raise SqlError(f"index {s['name']} already exists")
        self._add_index(meta, s["name"], s["column"], s["unique"])
        return [], []

    def _drop_table(self, s, params):
        meta = self.tables.get(s["name"].lower())
        if not meta:
            if s["if_exists"]:
                return [], []
            raise SqlError(f"no such table: {s['name']}")
        BTree(self.pager, meta["root"]).drop()
        for idx in meta["indexes"]:
            BTree(self.pager, idx["root"]).drop()
        del self.tables[s["name"].lower()]
        self.catalog_tree.delete(s["name"].lower().encode())
        self._sync_root()
        return [], []

    def _drop_index(self, s, params):
        for meta in self.tables.values():
            for idx in meta["indexes"]:
                if idx["name"].lower() == s["name"].lower():
                    BTree(self.pager, idx["root"]).drop()
                    meta["indexes"].remove(idx)
                    self._save_meta(meta)
                    return [], []
        if s["if_exists"]:
            return [], []
        raise SqlError(f"no such index: {s['name']}")

    def _store(self, table, rowid, row, replace=False):
        meta, cols = table.meta, table.cols
        for i, c in enumerate(cols):
            row[i] = affinity(row[i], c["type"])
            if c["notnull"] and row[i] is None:
                raise SqlError(f"NOT NULL constraint failed: {table.name}.{c['name']}")
        for idx in meta["indexes"]:
            tree = table.index_tree(idx)
            self._index_add(tree, idx, row, rowid, table)
            idx["root"] = tree.root
        table.tree.put(struct.pack(">Q", rowid), json.dumps(row).encode())
        meta["root"] = table.tree.root

    def _unindex(self, table, rowid, row):
        for idx in table.meta["indexes"]:
            tree = table.index_tree(idx)
            tree.delete(encode_key(row[idx["column"]]) + struct.pack(">Q", rowid))
            idx["root"] = tree.root

    def _insert(self, s, params):
        table = self.table(s["table"])
        meta = table.meta
        cols = [c.lower() for c in s["columns"]] if s["columns"] else [n.lower() for n in table.names]
        for c in cols:
            if c not in table.index_of:
                raise SqlError(f"no such column: {c}")
        ev = Evaluator(self, Scope(), params)
        rows = self._select(s["select"], params)[1] if s["select"] else [[ev(e, []) for e in r] for r in s["rows"]]
        pk = table.pk_column()
        for values in rows:
            if len(values) != len(cols):
                raise SqlError(f"{len(values)} values for {len(cols)} columns")
            row = [c["default"] for c in table.cols]
            for c, v in zip(cols, values):
                row[table.index_of[c]] = v
            rowid = meta["next"]
            if pk is not None:
                if row[pk] is None:
                    row[pk] = rowid
                row[pk] = affinity(row[pk], "INTEGER")
                if not isinstance(row[pk], int):
                    raise SqlError("datatype mismatch: INTEGER PRIMARY KEY")
                rowid = row[pk]
                if rowid < 0:
                    raise SqlError("negative INTEGER PRIMARY KEY is not supported")
                if table.get(rowid) is not None:
                    raise SqlError(f"UNIQUE constraint failed: {table.name}.{table.names[pk]}")
            meta["next"] = max(meta["next"], rowid + 1)
            self._store(table, rowid, row)
            self.last_rowid = rowid
        self._save_meta(meta)
        return ["changes"], [[len(rows)]]

    def _matching(self, table, where, params):
        scope = Scope()
        scope.add(table.name, table.names)
        ev = Evaluator(self, scope, params)
        out = []
        for rowid, row in self._scan(table, table.name, where, ev)[0]:
            if where is None or truth(ev(where, row)):
                out.append((rowid, row))
        return out, ev

    def _update(self, s, params):
        table = self.table(s["table"])
        rows, ev = self._matching(table, s["where"], params)
        sets = []
        for col, e in s["sets"]:
            if col.lower() not in table.index_of:
                raise SqlError(f"no such column: {col}")
            sets.append((table.index_of[col.lower()], e))
        pk = table.pk_column()
        for rowid, row in rows:
            new = list(row)
            for i, e in sets:
                new[i] = ev(e, row)
            new_id = rowid
            if pk is not None:
                new_id = new[pk] = affinity(new[pk], "INTEGER")
                if new_id != rowid and table.get(new_id) is not None:
                    raise SqlError(f"UNIQUE constraint failed: {table.name}.{table.names[pk]}")
            self._unindex(table, rowid, row)
            if new_id != rowid:
                table.tree.delete(struct.pack(">Q", rowid))
            self._store(table, new_id, new)
        self._save_meta(table.meta)
        return ["changes"], [[len(rows)]]

    def _delete(self, s, params):
        table = self.table(s["table"])
        rows, _ = self._matching(table, s["where"], params)
        for rowid, row in rows:
            self._unindex(table, rowid, row)
            table.tree.delete(struct.pack(">Q", rowid))
        table.meta["root"] = table.tree.root
        self._save_meta(table.meta)
        return ["changes"], [[len(rows)]]

    def _sargs(self, where, table, alias, ev, out):
        """Collect `column op constant` conditions from the top-level ANDs of a WHERE."""
        if where is None:
            return out
        if where[0] == "and":
            self._sargs(where[1], table, alias, ev, out)
            self._sargs(where[2], table, alias, ev, out)
        elif where[0] == "cmp" and where[1] in FLIP:
            a, b, op = where[2], where[3], where[1]
            if b[0] == "col" and a[0] in ("lit", "param"):
                a, b, op = b, a, FLIP[op]
            if a[0] == "col" and b[0] in ("lit", "param") and (a[1] is None or a[1].lower() == alias.lower()):
                i = table.index_of.get(a[2].lower())
                if i is not None:
                    v = b[1] if b[0] == "lit" else ev.params[b[1]]
                    if v is not None:
                        out.append((i, op, affinity(v, table.cols[i]["type"])))
        elif where[0] == "between" and where[1][0] == "col" and where[2][0] == "lit" and where[3][0] == "lit" and (where[1][1] is None or where[1][1].lower() == alias.lower()):
            i = table.index_of.get(where[1][2].lower())
            if i is not None and where[2][1] is not None and where[3][1] is not None:
                out.append((i, ">=", affinity(where[2][1], table.cols[i]["type"])))
                out.append((i, "<=", affinity(where[3][1], table.cols[i]["type"])))
        return out

    def _scan(self, table, alias, where, ev):
        """Pick an access path. Returns (iterator of (rowid, row), a line describing it)."""
        sargs = self._sargs(where, table, alias, ev, [])
        pk = table.pk_column()
        for i, op, v in sargs:
            if i == pk and op == "=" and isinstance(v, int) and v >= 0:
                row = table.get(v)
                return ([(v, row)] if row is not None else []), f"ROWID LOOKUP on {table.name}"
        for idx in table.meta["indexes"]:
            lo = hi = None
            hi_open = False
            used = False
            for i, op, v in sargs:
                if i != idx["column"]:
                    continue
                key = encode_key(v)
                if op == "=":
                    lo, hi, hi_open, used = key, key + b"\xff" * 8, False, True
                    break
                if op in (">", ">="):
                    lo = key if op == ">=" else key + b"\xff" * 8
                    used = True
                elif op in ("<", "<="):
                    hi = key if op == "<" else key + b"\xff" * 8
                    hi_open = op == "<"
                    used = True
            if used:
                if lo is None:
                    lo = b"\x01"       # an index range never includes NULL
                tree = table.index_tree(idx)

                def gen(tree=tree, lo=lo, hi=hi, hi_open=hi_open):
                    for k, _ in tree.scan(lo, hi, hi_open):
                        rowid = struct.unpack(">Q", k[-8:])[0]
                        yield rowid, table.get(rowid)
                return gen(), f"INDEX RANGE SCAN on {table.name} using {idx['name']}"
        return table.rows(), f"FULL SCAN of {table.name}"

    def _explain(self, s, params):
        if s["op"] != "select" or not s["from"]:
            return [s["op"]]
        scope = Scope()
        table = self.table(s["from"][0])
        scope.add(s["from"][1], table.names)
        ev = Evaluator(self, scope, params)
        lines = [self._scan(table, s["from"][1], s["where"], ev)[1]]
        for kind, ref, on in s["joins"]:
            lines.append(f"{'HASH' if self._equi(on) else 'NESTED LOOP'} {kind.upper()} JOIN {ref[0]}")
        return lines

    @staticmethod
    def _equi(on):
        return on is not None and on[0] == "cmp" and on[1] == "=" and on[2][0] == "col" and on[3][0] == "col"

    def _join(self, rows, left_scope, kind, ref, on, ev_params):
        """Join accumulated rows with one more table, by hash when ON is a column equality."""
        table = self.table(ref[0])
        width = len(table.names)
        right = [r for _, r in table.rows()]
        scope = Scope()
        scope.cols = list(left_scope.cols)
        scope.add(ref[1], table.names)
        ev = Evaluator(self, scope, ev_params)
        out = []
        if self._equi(on):
            a = scope.find(on[2][1], on[2][2])
            b = scope.find(on[3][1], on[3][2])
            split = len(left_scope.cols)
            if a >= split and b < split:
                a, b = b, a
            if a < split <= b:
                buckets = defaultdict(list)
                for r in right:
                    k = r[b - split]
                    if k is not None:
                        buckets[float(k) if isinstance(k, (int, float)) else k].append(r)
                for l in rows:
                    k = l[a]
                    match = buckets.get(float(k) if isinstance(k, (int, float)) else k, []) if k is not None else []
                    for r in match:
                        out.append(l + r)
                    if not match and kind == "left":
                        out.append(l + [None] * width)
                return out, scope
        for l in rows:
            hit = False
            for r in right:
                joined = l + r
                if on is None or truth(ev(on, joined)):
                    out.append(joined)
                    hit = True
            if not hit and kind == "left":
                out.append(l + [None] * width)
        return out, scope

    def _select(self, s, params):
        scope = Scope()
        where_done = False
        if s["from"]:
            table = self.table(s["from"][0])
            scope.add(s["from"][1], table.names)
            ev = Evaluator(self, scope, params)
            if s["joins"]:
                rows = [list(r) for _, r in table.rows()]
            else:
                source, _ = self._scan(table, s["from"][1], s["where"], ev)
                rows = []
                for _, r in source:
                    if s["where"] is None or truth(ev(s["where"], r)):
                        rows.append(r)
                where_done = True
        else:
            rows = [[]]
        for kind, ref, on in s["joins"]:
            rows, scope = self._join(rows, scope, kind, ref, on, params)
        ev = Evaluator(self, scope, params)
        if s["where"] is not None and not where_done:
            rows = [r for r in rows if truth(ev(s["where"], r))]
        items, names = [], []
        for it in s["items"]:
            if it[0] == "star":
                found = False
                for i, (a, n) in enumerate(scope.cols):
                    if it[1] is None or a.lower() == it[1].lower():
                        items.append(("col_at", i))
                        names.append(n)
                        found = True
                if not found:
                    raise SqlError("no columns for *" if it[1] is None else f"no such table: {it[1]}")
            else:
                items.append(it[1])
                names.append(it[2] or self._label(it[1]))
        aliases = {it[2].lower(): it[1] for it in s["items"] if it[0] == "expr" and it[2]}
        order = [(self._resolve(e, aliases, items), d) for e, d in s["order"]]
        group = [self._resolve(g, aliases, items) for g in s["group"]]
        for e in items:
            self._check(e, scope)
        grouped = bool(group) or any(has_aggregate(e) for e in items if e[0] != "col_at") or (s["having"] is not None) or any(has_aggregate(e) for e, _ in order)

        def value(e, row, agg=None):
            return row[e[1]] if e[0] == "col_at" else ev(e, row, agg)

        if grouped:
            aggs = []
            for e in items + [e for e, _ in order] + ([s["having"]] if s["having"] else []):
                if e[0] != "col_at":
                    aggregates_in(e, aggs)
            buckets, keys = {}, []
            for r in rows:
                k = tuple(("n", float(v)) if isinstance(v, (int, float)) else v for v in (ev(g, r) for g in group))
                if k not in buckets:
                    buckets[k] = []
                    keys.append(k)
                buckets[k].append(r)
            if not group and not buckets:
                keys = [()]
                buckets[()] = []
            out = []
            width = len(scope.cols)
            for k in keys:
                members = buckets[k]
                sample = members[0] if members else [None] * width
                table_agg = {}
                for a in aggs:
                    vals = [ev(a[2][0], r) for r in members] if a[2] else [1] * len(members)
                    table_agg[id(a)] = compute_aggregate(a[1], vals, a[3])
                if s["having"] is not None and not truth(ev(s["having"], sample, table_agg)):
                    continue
                out.append(([value(e, sample, table_agg) for e in items], [value(e, sample, table_agg) for e, _ in order]))
        else:
            out = [([value(e, r) for e in items], [value(e, r) for e, _ in order]) for r in rows]
        if s["distinct"]:
            seen, uniq = set(), []
            for row, o in out:
                k = tuple(("n", float(v)) if isinstance(v, (int, float)) else v for v in row)
                if k not in seen:
                    seen.add(k)
                    uniq.append((row, o))
            out = uniq
        if order:
            out = sorted(((o, row) for row, o in out), key=order_key(None, [d for _, d in order]))
            result = [row for _, row in out]
        else:
            result = [row for row, _ in out]
        offset = ev(s["offset"], []) if s["offset"] else 0
        limit = ev(s["limit"], []) if s["limit"] else None
        if offset or limit is not None:
            result = result[offset:None if limit is None or limit < 0 else offset + limit]
        return names, result

    def _check(self, e, scope):
        if e[0] == "col":
            scope.find(e[1], e[2])

    @staticmethod
    def _label(e):
        if e[0] == "col":
            return e[2]
        if e[0] == "call":
            return f"{e[1]}(*)" if e[3] == "star" else e[1]
        return "expr"

    @staticmethod
    def _resolve(e, aliases, items):
        if e[0] == "lit" and isinstance(e[1], int) and 1 <= e[1] <= len(items):
            return items[e[1] - 1]
        if e[0] == "col" and e[1] is None and e[2].lower() in aliases:
            return aliases[e[2].lower()]
        return e

    def check(self):
        """Integrity check: every index entry matches a row, and rows match every index."""
        problems = []
        for meta in self.tables.values():
            table = Table(self, meta)
            rows = dict(table.rows())
            for idx in meta["indexes"]:
                tree = table.index_tree(idx)
                found = 0
                for k, _ in tree.scan():
                    rowid = struct.unpack(">Q", k[-8:])[0]
                    found += 1
                    if rowid not in rows or encode_key(rows[rowid][idx["column"]]) != k[:-8]:
                        problems.append(f"{idx['name']}: stale entry for row {rowid}")
                if found != len(rows):
                    problems.append(f"{idx['name']}: {found} entries for {len(rows)} rows")
        return problems


def connect(path):
    return Database(path)
