import psycopg2

conn = psycopg2.connect(
    host="aws-1-ap-northeast-2.pooler.supabase.com",
    port=5432,
    dbname="postgres",
    user="postgres.oznagajgjqoojzvyacuu",
    password="pUbbDDe6ceZ_GUf"
)

cur = conn.cursor()

tables = [
    "apt_sale_trades",
    "apt_rent_trades",
    "presale_trades"
]

for table in tables:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    print(table, cur.fetchone()[0])

cur.close()
conn.close()