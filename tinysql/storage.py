"""Disk storage: a page cache with a write-ahead log, and a B+ tree on top of it.

File layout: page 0 is the header, every other page is a B+ tree node, an overflow
page or a free page. A commit first appends all dirty pages and a checksummed commit
record to the log, syncs it, and only then writes the pages into the main file. If
the process dies, the next open replays complete transactions and ignores torn ones.
"""
import os
import struct
import zlib

PAGE_SIZE = 4096
MAGIC = b"TINYSQL1"
COMMIT = 0xFFFFFFFF
LEAF, INTERNAL, OVERFLOW = 1, 2, 3
MAX_INLINE = 1024
MAX_KEY = 480
_HEAD = struct.Struct(">8sIII")  # magic, page count, free list head, catalog root
_NODE = struct.Struct(">BHI")    # kind, entries, next leaf or leftmost child
_OVER = struct.Struct(">BIH")    # kind, next page, bytes used


class StorageError(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by the crash hook in tests, to stop a commit part of the way through."""


class Node:
    __slots__ = ("kind", "keys", "values", "link")

    def __init__(self, kind):
        self.kind = kind
        self.keys = []
        self.values = []   # leaf: bytes (or an overflow reference); internal: child pages
        self.link = 0      # leaf: next leaf; internal: leftmost child

    def size(self):
        n = _NODE.size
        for k, v in zip(self.keys, self.values):
            n += 2 + len(k) + (4 if self.kind == INTERNAL else 4 + len(v[1]))
        return n

    def dump(self):
        out = [_NODE.pack(self.kind, len(self.keys), self.link)]
        for k, v in zip(self.keys, self.values):
            out.append(struct.pack(">H", len(k)))
            out.append(k)
            if self.kind == INTERNAL:
                out.append(struct.pack(">I", v))
            else:
                out.append(struct.pack(">I", (len(v[1]) << 1) | (1 if v[0] else 0)))
                out.append(v[1])
        return b"".join(out).ljust(PAGE_SIZE, b"\0")

    @classmethod
    def load(cls, data):
        kind, count, link = _NODE.unpack_from(data)
        node = cls(kind)
        node.link = link
        p = _NODE.size
        for _ in range(count):
            n = struct.unpack_from(">H", data, p)[0]
            p += 2
            node.keys.append(bytes(data[p:p + n]))
            p += n
            if kind == INTERNAL:
                node.values.append(struct.unpack_from(">I", data, p)[0])
                p += 4
            else:
                packed = struct.unpack_from(">I", data, p)[0]
                p += 4
                n = packed >> 1
                node.values.append((bool(packed & 1), bytes(data[p:p + n])))
                p += n
        return node


class Pager:
    """Pages in memory until commit. `crash_at` lets tests stop a commit at a chosen step."""

    def __init__(self, path):
        self.path = path
        self.wal_path = path + "-wal"
        self.crash_at = None
        self.cache = {}
        self.dirty = {}
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "w+b" if new else "r+b")
        if new:
            self.count, self.free, self.root = 1, 0, 0
            self.f.write(self._header())
            self.f.flush()
        else:
            self._recover()
            self._read_header()

    def _header(self):
        return _HEAD.pack(MAGIC, self.count, self.free, self.root).ljust(PAGE_SIZE, b"\0")

    def _read_header(self):
        self.f.seek(0)
        data = self.f.read(PAGE_SIZE)
        magic, self.count, self.free, self.root = _HEAD.unpack_from(data)
        if magic != MAGIC:
            raise StorageError("not a TinySQL file")

    def _recover(self):
        if not os.path.exists(self.wal_path):
            return
        with open(self.wal_path, "rb") as w:
            data = w.read()
        pos, pages, crc = 0, [], 0
        rec = 4 + PAGE_SIZE
        while pos + 4 <= len(data):
            num = struct.unpack_from(">I", data, pos)[0]
            if num == COMMIT:
                if pos + 12 > len(data):
                    break
                count, want = struct.unpack_from(">II", data, pos + 4)
                if count != len(pages) or want != crc:
                    break
                for page, body in pages:
                    self.f.seek(page * PAGE_SIZE)
                    self.f.write(body)
                self.f.flush()
                os.fsync(self.f.fileno())
                pos += 12
                pages, crc = [], 0
                continue
            if pos + rec > len(data):
                break
            body = data[pos + 4:pos + rec]
            pages.append((num, body))
            crc = zlib.crc32(data[pos:pos + rec], crc)
            pos += rec
        os.remove(self.wal_path)

    def _raw(self, page):
        self.f.seek(page * PAGE_SIZE)
        return self.f.read(PAGE_SIZE)

    def node(self, page):
        if page in self.dirty:
            return self.dirty[page]
        n = self.cache.get(page)
        if n is None:
            n = Node.load(self._raw(page))
            self.cache[page] = n
        return n

    def touch(self, page, node):
        self.cache.pop(page, None)
        self.dirty[page] = node

    def allocate(self):
        if self.free:
            page = self.free
            self.free = struct.unpack_from(">I", self.dirty_bytes(page))[0]
            return page
        page = self.count
        self.count += 1
        return page

    def dirty_bytes(self, page):
        v = self.dirty.get(page)
        if isinstance(v, (bytes, bytearray)):
            return v
        return self._raw(page)

    def release(self, page):
        self.cache.pop(page, None)
        self.dirty[page] = struct.pack(">I", self.free).ljust(PAGE_SIZE, b"\0")
        self.free = page

    def write_blob(self, data):
        """Store a large value in a chain of overflow pages and return the first page."""
        head = 0
        room = PAGE_SIZE - _OVER.size
        parts = [data[i:i + room] for i in range(0, len(data), room)] or [b""]
        for part in reversed(parts):
            page = self.allocate()
            self.dirty[page] = (_OVER.pack(OVERFLOW, head, len(part)) + part).ljust(PAGE_SIZE, b"\0")
            head = page
        return head

    def read_blob(self, page):
        out = []
        while page:
            raw = self.dirty_bytes(page)
            kind, nxt, used = _OVER.unpack_from(raw)
            if kind != OVERFLOW:
                raise StorageError(f"page {page} is not an overflow page")
            out.append(raw[_OVER.size:_OVER.size + used])
            page = nxt
        return b"".join(out)

    def free_blob(self, page):
        while page:
            nxt = _OVER.unpack_from(self.dirty_bytes(page))[1]
            self.release(page)
            page = nxt

    def commit(self):
        if not self.dirty:
            return
        pages = [(p, n.dump() if isinstance(n, Node) else bytes(n)) for p, n in sorted(self.dirty.items())]
        pages.insert(0, (0, self._header()))
        buf, crc = [], 0
        for page, body in pages:
            rec = struct.pack(">I", page) + body
            crc = zlib.crc32(rec, crc)
            buf.append(rec)
        buf.append(struct.pack(">III", COMMIT, len(pages), crc))
        blob = b"".join(buf)
        if self.crash_at == "torn-log":
            with open(self.wal_path, "wb") as w:
                w.write(blob[:len(blob) // 2])
            raise SimulatedCrash
        with open(self.wal_path, "wb") as w:
            w.write(blob)
            w.flush()
            os.fsync(w.fileno())
        if self.crash_at == "after-log":
            raise SimulatedCrash
        for page, body in pages:
            self.f.seek(page * PAGE_SIZE)
            self.f.write(body)
        self.f.flush()
        os.fsync(self.f.fileno())
        os.remove(self.wal_path)
        for p, n in self.dirty.items():
            if isinstance(n, Node):
                self.cache[p] = n
        self.dirty.clear()

    def rollback(self):
        self.dirty.clear()
        self.cache.clear()
        self._read_header()

    def close(self):
        self.f.close()


class BTree:
    """A B+ tree from byte keys to byte values. Leaves are linked for range scans.

    Deletes remove entries but do not merge underfull nodes: space inside a page is
    reused by later inserts, which keeps the code simple at some cost in density.
    """

    def __init__(self, pager, root=0):
        self.p = pager
        if not root:
            root = pager.allocate()
            pager.touch(root, Node(LEAF))
        self.root = root

    def _path(self, key):
        path, page = [], self.root
        node = self.p.node(page)
        while node.kind == INTERNAL:
            i = _upper(node.keys, key)
            child = node.link if i == 0 else node.values[i - 1]
            path.append((page, i))
            page = child
            node = self.p.node(page)
        return path, page, node

    def get(self, key):
        _, _, node = self._path(key)
        i = _lower(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            return self._value(node.values[i])
        return None

    def _value(self, v):
        big, data = v
        return self.p.read_blob(struct.unpack(">I", data)[0]) if big else data

    def _entry(self, value):
        if len(value) > MAX_INLINE:
            return True, struct.pack(">I", self.p.write_blob(value))
        return False, value

    def put(self, key, value):
        if len(key) > MAX_KEY:
            raise StorageError(f"key too long: {len(key)} bytes, limit {MAX_KEY}")
        path, page, node = self._path(key)
        node = _copy(node)
        i = _lower(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            old = node.values[i]
            if old[0]:
                self.p.free_blob(struct.unpack(">I", old[1])[0])
            node.values[i] = self._entry(value)
        else:
            node.keys.insert(i, key)
            node.values.insert(i, self._entry(value))
        self.p.touch(page, node)
        if node.size() > PAGE_SIZE:
            self._split(path, page, node)

    def _split(self, path, page, node):
        while True:
            total, half, cut = node.size(), 0, 0
            for k, v in zip(node.keys, node.values):
                half += 2 + len(k) + (4 if node.kind == INTERNAL else 4 + len(v[1]))
                cut += 1
                if half >= total / 2:
                    break
            cut = min(max(cut, 1), len(node.keys) - 1)
            right = Node(node.kind)
            right_page = self.p.allocate()
            if node.kind == LEAF:
                right.keys, right.values = node.keys[cut:], node.values[cut:]
                node.keys, node.values = node.keys[:cut], node.values[:cut]
                right.link, node.link = node.link, right_page
                sep = right.keys[0]
            else:
                sep = node.keys[cut]
                right.link = node.values[cut]
                right.keys, right.values = node.keys[cut + 1:], node.values[cut + 1:]
                node.keys, node.values = node.keys[:cut], node.values[:cut]
            self.p.touch(page, node)
            self.p.touch(right_page, right)
            if not path:
                root = Node(INTERNAL)
                root.link = page
                root.keys = [sep]
                root.values = [right_page]
                self.root = self.p.allocate()
                self.p.touch(self.root, root)
                return
            parent_page, i = path.pop()
            parent = _copy(self.p.node(parent_page))
            parent.keys.insert(i, sep)
            parent.values.insert(i, right_page)
            self.p.touch(parent_page, parent)
            if parent.size() <= PAGE_SIZE:
                return
            page, node = parent_page, parent

    def delete(self, key):
        _, page, node = self._path(key)
        i = _lower(node.keys, key)
        if i >= len(node.keys) or node.keys[i] != key:
            return False
        node = _copy(node)
        if node.values[i][0]:
            self.p.free_blob(struct.unpack(">I", node.values[i][1])[0])
        del node.keys[i]
        del node.values[i]
        self.p.touch(page, node)
        return True

    def scan(self, lo=None, hi=None, hi_open=False):
        """Yield (key, value) from lo up to hi, in key order."""
        if lo is None:
            page, node = self.root, self.p.node(self.root)
            while node.kind == INTERNAL:
                page = node.link
                node = self.p.node(page)
            i = 0
        else:
            _, page, node = self._path(lo)
            i = _lower(node.keys, lo)
        while True:
            keys, values, link = list(node.keys), list(node.values), node.link
            for k, v in zip(keys[i:], values[i:]):
                if hi is not None and (k > hi or (hi_open and k >= hi)):
                    return
                yield k, self._value(v)
            if not link:
                return
            node, i = self.p.node(link), 0

    def pages(self):
        """Every node page in the tree, for the integrity check."""
        out, todo = [], [self.root]
        while todo:
            page = todo.pop()
            out.append(page)
            node = self.p.node(page)
            if node.kind == INTERNAL:
                todo.append(node.link)
                todo.extend(node.values)
        return out

    def drop(self):
        for page in self.pages():
            node = self.p.node(page)
            if node.kind == LEAF:
                for big, data in node.values:
                    if big:
                        self.p.free_blob(struct.unpack(">I", data)[0])
            self.p.release(page)


def _copy(node):
    n = Node(node.kind)
    n.keys, n.values, n.link = list(node.keys), list(node.values), node.link
    return n


def _lower(keys, key):
    lo, hi = 0, len(keys)
    while lo < hi:
        mid = (lo + hi) // 2
        if keys[mid] < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _upper(keys, key):
    lo, hi = 0, len(keys)
    while lo < hi:
        mid = (lo + hi) // 2
        if key < keys[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def encode_key(value):
    """Encode a value so that byte order matches SQL order: NULL, then numbers, then text."""
    if value is None:
        return b"\x00"
    if isinstance(value, (int, float)):
        bits = struct.unpack(">Q", struct.pack(">d", float(value)))[0]
        bits = bits ^ 0xFFFFFFFFFFFFFFFF if bits >> 63 else bits | (1 << 63)
        return b"\x01" + struct.pack(">Q", bits)
    return b"\x02" + value.encode("utf-8").replace(b"\x00", b"\x00\xff") + b"\x00\x00"
