import sqlite3

conn = sqlite3.connect("real_deals.db")
cur = conn.cursor()

tables = [
    "apt_sale_trades",
    "apt_rent_trades",
    "presale_trades"
]

print("=== COLUMNS ===")

for table in tables:
    print(f"\n[{table}]")

    cur.execute(f"PRAGMA table_info({table})")

    for col in cur.fetchall():
        print(col)

conn.close()