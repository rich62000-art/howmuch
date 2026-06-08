import sqlite3

conn = sqlite3.connect("real_deals.db")
cur = conn.cursor()

cur.execute("""
SELECT name
FROM sqlite_master
WHERE type='index'
AND tbl_name='presale_trades'
""")

for row in cur.fetchall():
    print(row[0])

conn.close()