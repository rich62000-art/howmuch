import psycopg2

conn = psycopg2.connect(
    host="aws-1-ap-northeast-2.pooler.supabase.com",
    port=5432,
    dbname="postgres",
    user="postgres.oznagajgjqoojzvyacuu",
    password="pUbbDDe6ceZ_GUf"
)

print("연결 성공!")

conn.close()