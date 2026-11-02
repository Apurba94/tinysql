"""Interactive shell: python -m tinysql my.db"""
import sys

from . import SqlError, StorageError, connect


def show(cols, rows):
    if not cols:
        return
    text = [[("NULL" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in text]) for i, c in enumerate(cols)]
    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in text:
        print(" | ".join(v.ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} rows)")


def main(argv):
    if len(argv) < 2:
        print("usage: python -m tinysql FILE [SQL]")
        return 2
    db = connect(argv[1])
    if len(argv) > 2:
        show(*db.execute(argv[2]))
        db.close()
        return 0
    print("TinySQL. End statements with ; and type .quit to leave, .check to verify the file.")
    buf = ""
    while True:
        try:
            line = input("... " if buf else "sql> ")
        except EOFError:
            break
        if not buf and line.strip() == ".quit":
            break
        if not buf and line.strip() == ".check":
            print(db.check() or "ok")
            continue
        buf += line + "\n"
        if line.rstrip().endswith(";"):
            try:
                show(*db.execute(buf))
            except (SqlError, StorageError) as exc:
                print("error:", exc)
            buf = ""
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
