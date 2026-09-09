"""
SQL 연습용 DB 만들기 — data/*.json 을 읽기만 하고 절대 안 건드림.
매번 이 스크립트를 다시 돌리면 practice.db 가 최신 데이터로 새로 만들어진다.
연습하다 테이블을 지우거나 잘못 고쳐도 이 스크립트만 다시 돌리면 원상복구된다.
"""
import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent.parent
DATA = BASE / "data"
DB_PATH = Path(__file__).parent / "practice.db"


def load(name):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main():
    products = load("products.json")
    history = load("history.json")
    inventory = load("inventory.json")

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE products (
            product_key TEXT PRIMARY KEY,
            brand TEXT,
            unit TEXT,
            code TEXT,
            cost REAL,
            is_new INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE shipments (
            product_key TEXT,
            ym TEXT,
            qty INTEGER,
            FOREIGN KEY(product_key) REFERENCES products(product_key)
        )
    """)
    cur.execute("""
        CREATE TABLE stock_snapshots (
            product_key TEXT,
            ym TEXT,
            qty INTEGER,
            day INTEGER,
            FOREIGN KEY(product_key) REFERENCES products(product_key)
        )
    """)
    cur.execute("""
        CREATE TABLE inbound (
            product_key TEXT,
            ym TEXT,
            qty INTEGER,
            FOREIGN KEY(product_key) REFERENCES products(product_key)
        )
    """)

    for key, meta in products.items():
        cur.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?)",
            (key, meta.get("brand"), meta.get("unit"), meta.get("code"),
             meta.get("cost"), 1 if meta.get("new") else 0),
        )

    for key, months in history.items():
        for ym, qty in months.items():
            cur.execute("INSERT INTO shipments VALUES (?,?,?)", (key, ym, qty))

    for key, inv in inventory.items():
        for ym, qty in (inv.get("stock") or {}).items():
            day = (inv.get("stock_day") or {}).get(ym)
            cur.execute("INSERT INTO stock_snapshots VALUES (?,?,?,?)", (key, ym, qty, day))
        for ym, qty in (inv.get("inbound") or {}).items():
            cur.execute("INSERT INTO inbound VALUES (?,?,?)", (key, ym, qty))

    con.commit()
    n_p = cur.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_s = cur.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    n_k = cur.execute("SELECT COUNT(*) FROM stock_snapshots").fetchone()[0]
    n_i = cur.execute("SELECT COUNT(*) FROM inbound").fetchone()[0]
    con.close()
    print("practice.db 생성 완료: products=%d shipments=%d stock_snapshots=%d inbound=%d"
          % (n_p, n_s, n_k, n_i))


if __name__ == "__main__":
    main()
