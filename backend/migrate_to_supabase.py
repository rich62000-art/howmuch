import sqlite3
import psycopg2
from psycopg2.extras import execute_values

SQLITE_DB = "real_deals.db"

SUPABASE_DB_URL = "postgresql://postgres.oznagajgjqoojzvyacuu:pUbbDDe6ceZ_GUf@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres"

MONTH_LIMIT = "202404"

TABLES = {
    "apt_sale_trades": [
        "region", "sigungu", "dong", "apt_name", "size",
        "contract_date", "price", "floor", "source_month"
    ],
    "apt_rent_trades": [
        "region", "sigungu", "dong", "apt_name", "size",
        "contract_date", "deposit", "monthly_rent", "floor", "source_month"
    ],
    "presale_trades": [
        "region", "sigungu", "dong", "apt_name", "size",
        "contract_date", "price", "floor", "source_month"
    ],
}


def migrate_table(sqlite_cur, pg_cur, table, columns):
    col_text = ", ".join(columns)

    sqlite_cur.execute(
        f"""
        SELECT {col_text}
        FROM {table}
        WHERE source_month >= ?
        """,
        (MONTH_LIMIT,)
    )

    rows = sqlite_cur.fetchall()
    print(f"{table}: {len(rows)}건 이전 시작")

    if not rows:
        return

    insert_sql = f"""
        INSERT INTO {table} ({col_text})
        VALUES %s
    """

    execute_values(pg_cur, insert_sql, rows, page_size=5000)

    print(f"{table}: 이전 완료")


def main():
    sqlite_conn = sqlite3.connect(SQLITE_DB)
    sqlite_cur = sqlite_conn.cursor()

    pg_conn = psycopg2.connect(SUPABASE_DB_URL)
    pg_cur = pg_conn.cursor()

    for table, columns in TABLES.items():
        migrate_table(sqlite_cur, pg_cur, table, columns)
        pg_conn.commit()

    pg_cur.close()
    pg_conn.close()
    sqlite_conn.close()

    print("전체 이전 완료")


if __name__ == "__main__":
    main()