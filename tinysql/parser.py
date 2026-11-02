"""SQL tokeniser and recursive-descent parser. Expressions become nested tuples."""
import re

KEYWORDS = set("""SELECT FROM WHERE GROUP BY HAVING ORDER ASC DESC LIMIT OFFSET INSERT INTO VALUES UPDATE SET DELETE
CREATE TABLE INDEX UNIQUE DROP PRIMARY KEY NOT NULL DEFAULT AND OR IS IN LIKE BETWEEN AS ON JOIN INNER LEFT OUTER CROSS
DISTINCT CASE WHEN THEN ELSE END BEGIN COMMIT ROLLBACK EXPLAIN TRUE FALSE IF EXISTS""".split())
TOKEN = re.compile(r"""\s+|--[^\n]*|/\*.*?\*/
  |(?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
  |(?P<str>'(?:[^']|'')*')
  |(?P<qid>"(?:[^"]|"")*")
  |(?P<id>[A-Za-z_][A-Za-z_0-9]*)
  |(?P<op><=|>=|<>|!=|\|\||[-+*/%=<>(),.;?])""", re.X | re.S)


class SqlError(Exception):
    pass


def tokenize(sql):
    out, pos = [], 0
    while pos < len(sql):
        m = TOKEN.match(sql, pos)
        if not m:
            raise SqlError(f"unexpected character {sql[pos]!r} at position {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind is None:
            continue
        text = m.group()
        if kind == "num":
            out.append(("num", float(text) if any(c in text for c in ".eE") else int(text)))
        elif kind == "str":
            out.append(("str", text[1:-1].replace("''", "'")))
        elif kind == "qid":
            out.append(("id", text[1:-1].replace('""', '"')))
        elif kind == "id":
            up = text.upper()
            out.append(("kw", up) if up in KEYWORDS else ("id", text))
        else:
            out.append(("op", text))
    out.append(("eof", None))
    return out


class Parser:
    def __init__(self, sql):
        self.t = tokenize(sql)
        self.i = 0
        self.params = 0

    def peek(self, offset=0):
        return self.t[min(self.i + offset, len(self.t) - 1)]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, *words):
        tok = self.peek()
        return tok[0] in ("kw", "op") and tok[1] in words

    def accept(self, *words):
        if self.at(*words):
            return self.next()[1]
        return None

    def expect(self, word):
        if not self.accept(word):
            raise SqlError(f"expected {word} but found {self.peek()[1]!r}")

    def ident(self):
        kind, value = self.next()
        if kind != "id":
            raise SqlError(f"expected a name but found {value!r}")
        return value

    def statements(self):
        out = []
        while self.peek()[0] != "eof":
            if self.accept(";"):
                continue
            out.append(self.statement())
            if self.peek()[0] != "eof":
                self.expect(";")
        return out

    def statement(self):
        if self.accept("EXPLAIN"):
            return {"op": "explain", "stmt": self.statement()}
        if self.at("SELECT"):
            return self.select()
        word = self.next()[1]
        if word == "CREATE":
            unique = bool(self.accept("UNIQUE"))
            if self.accept("TABLE"):
                return self.create_table()
            self.expect("INDEX")
            name = self.ident()
            self.expect("ON")
            table = self.ident()
            self.expect("(")
            col = self.ident()
            self.expect(")")
            return {"op": "create_index", "name": name, "table": table, "column": col, "unique": unique}
        if word == "DROP":
            kind = self.next()[1]
            if kind not in ("TABLE", "INDEX"):
                raise SqlError("DROP needs TABLE or INDEX")
            exists = False
            if self.accept("IF"):
                self.expect("EXISTS")
                exists = True
            return {"op": "drop_" + kind.lower(), "name": self.ident(), "if_exists": exists}
        if word == "INSERT":
            return self.insert()
        if word == "UPDATE":
            table = self.ident()
            self.expect("SET")
            sets = []
            while True:
                col = self.ident()
                self.expect("=")
                sets.append((col, self.expr()))
                if not self.accept(","):
                    break
            where = self.expr() if self.accept("WHERE") else None
            return {"op": "update", "table": table, "sets": sets, "where": where}
        if word == "DELETE":
            self.expect("FROM")
            table = self.ident()
            where = self.expr() if self.accept("WHERE") else None
            return {"op": "delete", "table": table, "where": where}
        if word in ("BEGIN", "COMMIT", "ROLLBACK"):
            return {"op": word.lower()}
        raise SqlError(f"unknown statement starting with {word!r}")

    def create_table(self):
        if_not = False
        if self.accept("IF"):
            self.expect("NOT")
            self.expect("EXISTS")
            if_not = True
        name = self.ident()
        self.expect("(")
        cols = []
        while True:
            col = {"name": self.ident(), "type": "TEXT", "pk": False, "notnull": False, "unique": False, "default": None}
            if self.peek()[0] == "id":
                col["type"] = self.next()[1].upper()
                if self.accept("("):
                    while not self.accept(")"):
                        self.next()
            while True:
                if self.accept("PRIMARY"):
                    self.expect("KEY")
                    col["pk"] = col["notnull"] = True
                elif self.accept("NOT"):
                    self.expect("NULL")
                    col["notnull"] = True
                elif self.accept("UNIQUE"):
                    col["unique"] = True
                elif self.accept("DEFAULT"):
                    col["default"] = self.primary()
                else:
                    break
            cols.append(col)
            if not self.accept(","):
                break
        self.expect(")")
        return {"op": "create_table", "name": name, "columns": cols, "if_not_exists": if_not}

    def insert(self):
        self.expect("INTO")
        table = self.ident()
        cols = None
        if self.accept("("):
            cols = [self.ident()]
            while self.accept(","):
                cols.append(self.ident())
            self.expect(")")
        if self.at("SELECT"):
            return {"op": "insert", "table": table, "columns": cols, "select": self.select(), "rows": None}
        self.expect("VALUES")
        rows = []
        while True:
            self.expect("(")
            row = [self.expr()]
            while self.accept(","):
                row.append(self.expr())
            self.expect(")")
            rows.append(row)
            if not self.accept(","):
                break
        return {"op": "insert", "table": table, "columns": cols, "rows": rows, "select": None}

    def select(self):
        self.expect("SELECT")
        q = {"op": "select", "distinct": bool(self.accept("DISTINCT")), "from": None, "joins": [], "where": None, "group": [], "having": None, "order": [], "limit": None, "offset": None}
        items = []
        while True:
            if self.accept("*"):
                items.append(("star", None))
            elif self.peek()[0] == "id" and self.peek(1) == ("op", ".") and self.peek(2) == ("op", "*"):
                items.append(("star", self.ident()))
                self.next()
                self.next()
            else:
                e = self.expr()
                alias = None
                if self.accept("AS"):
                    alias = self.ident()
                elif self.peek()[0] == "id":
                    alias = self.ident()
                items.append(("expr", e, alias))
            if not self.accept(","):
                break
        q["items"] = items
        if self.accept("FROM"):
            q["from"] = self.table_ref()
            while True:
                kind = None
                if self.accept(","):
                    kind = "cross"
                elif self.accept("JOIN"):
                    kind = "inner"
                elif self.accept("INNER"):
                    self.expect("JOIN")
                    kind = "inner"
                elif self.accept("LEFT"):
                    self.accept("OUTER")
                    self.expect("JOIN")
                    kind = "left"
                elif self.accept("CROSS"):
                    self.expect("JOIN")
                    kind = "cross"
                if not kind:
                    break
                ref = self.table_ref()
                on = self.expr() if kind != "cross" and self.accept("ON") else None
                q["joins"].append((kind, ref, on))
        if self.accept("WHERE"):
            q["where"] = self.expr()
        if self.accept("GROUP"):
            self.expect("BY")
            q["group"].append(self.expr())
            while self.accept(","):
                q["group"].append(self.expr())
        if self.accept("HAVING"):
            q["having"] = self.expr()
        if self.accept("ORDER"):
            self.expect("BY")
            while True:
                e = self.expr()
                desc = bool(self.accept("DESC"))
                if not desc:
                    self.accept("ASC")
                q["order"].append((e, desc))
                if not self.accept(","):
                    break
        if self.accept("LIMIT"):
            q["limit"] = self.expr()
            if self.accept("OFFSET"):
                q["offset"] = self.expr()
        return q

    def table_ref(self):
        name = self.ident()
        alias = name
        if self.accept("AS"):
            alias = self.ident()
        elif self.peek()[0] == "id":
            alias = self.ident()
        return (name, alias)

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        e = self.and_expr()
        while self.accept("OR"):
            e = ("or", e, self.and_expr())
        return e

    def and_expr(self):
        e = self.not_expr()
        while self.accept("AND"):
            e = ("and", e, self.not_expr())
        return e

    def not_expr(self):
        if self.accept("NOT"):
            return ("not", self.not_expr())
        return self.comparison()

    def comparison(self):
        e = self.addition()
        while True:
            if self.at("=", "<", ">", "<=", ">=", "<>", "!="):
                op = self.next()[1]
                e = ("cmp", "<>" if op == "!=" else op, e, self.addition())
            elif self.accept("IS"):
                negate = bool(self.accept("NOT"))
                self.expect("NULL")
                e = ("isnull", e, negate)
            else:
                negate = False
                if self.at("NOT") and self.peek(1)[1] in ("IN", "LIKE", "BETWEEN"):
                    self.next()
                    negate = True
                if self.accept("IN"):
                    self.expect("(")
                    if self.at("SELECT"):
                        q = self.select()
                        self.expect(")")
                        e = ("insel", e, q)
                    else:
                        items = [self.expr()]
                        while self.accept(","):
                            items.append(self.expr())
                        self.expect(")")
                        e = ("in", e, items)
                elif self.accept("LIKE"):
                    e = ("like", e, self.addition())
                elif self.accept("BETWEEN"):
                    lo = self.addition()
                    self.expect("AND")
                    e = ("between", e, lo, self.addition())
                else:
                    return e
                if negate:
                    e = ("not", e)

    def addition(self):
        e = self.term()
        while self.at("+", "-", "||"):
            e = ("bin", self.next()[1], e, self.term())
        return e

    def term(self):
        e = self.unary()
        while self.at("*", "/", "%"):
            e = ("bin", self.next()[1], e, self.unary())
        return e

    def unary(self):
        if self.accept("-"):
            e = self.unary()
            return ("lit", -e[1]) if e[0] == "lit" and isinstance(e[1], (int, float)) else ("neg", e)
        self.accept("+")
        return self.primary()

    def primary(self):
        kind, value = self.next()
        if kind == "num" or kind == "str":
            return ("lit", value)
        if kind == "kw":
            if value == "NULL":
                return ("lit", None)
            if value in ("TRUE", "FALSE"):
                return ("lit", 1 if value == "TRUE" else 0)
            if value == "CASE":
                return self.case()
        if kind == "op" and value == "(":
            if self.at("SELECT"):
                q = self.select()
                self.expect(")")
                return ("subq", q)
            e = self.expr()
            self.expect(")")
            return e
        if kind == "op" and value == "?":
            self.params += 1
            return ("param", self.params - 1)
        if kind == "id":
            if self.accept("("):
                distinct = bool(self.accept("DISTINCT"))
                if self.accept("*"):
                    self.expect(")")
                    return ("call", value.upper(), [], "star")
                args = []
                if not self.at(")"):
                    args.append(self.expr())
                    while self.accept(","):
                        args.append(self.expr())
                self.expect(")")
                return ("call", value.upper(), args, "distinct" if distinct else None)
            if self.accept("."):
                return ("col", value, self.ident())
            return ("col", None, value)
        raise SqlError(f"unexpected {value!r} in expression")

    def case(self):
        base = None if self.at("WHEN") else self.expr()
        whens = []
        while self.accept("WHEN"):
            cond = self.expr()
            self.expect("THEN")
            whens.append((cond, self.expr()))
        other = self.expr() if self.accept("ELSE") else ("lit", None)
        self.expect("END")
        return ("case", base, whens, other)


def parse(sql):
    p = Parser(sql)
    return p.statements()
