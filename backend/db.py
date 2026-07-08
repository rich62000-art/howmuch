import sqlite3
import time
import json
import psycopg2
from psycopg2.pool import SimpleConnectionPool

DB_NAME = "real_deals.db"

PG_CONFIG = {
    "host": "aws-1-ap-northeast-2.pooler.supabase.com",
    "port": 5432,
    "dbname": "postgres",
    "user": "postgres.oznagajgjqoojzvyacuu",
    "password": "pUbbDDe6ceZ_GUf"
}
DB_CACHE = {}
CACHE_TTL = 3600
pg_pool = None

def get_pg_connection():
    global pg_pool

    if pg_pool is None:
        pg_pool = SimpleConnectionPool(
            1,
            50,
            **PG_CONFIG
        )

    return pg_pool.getconn()

def release_pg_connection(conn):
    global pg_pool

    if not conn:
        return

    if pg_pool:
        try:
            pg_pool.putconn(conn)
        except Exception as e:
            print("❌ DB 연결 반환 오류:", e)
            try:
                conn.close()
            except:
                pass
    else:
        conn.close()


def get_connection():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")

    return conn


def create_tables():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS apt_sale_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT,
            sigungu TEXT,
            dong TEXT,
            apt_name TEXT,
            size REAL,
            contract_date TEXT,
            price INTEGER,
            floor INTEGER,
            source_month TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            UNIQUE (
                region,
                sigungu,
                dong,
                apt_name,
                size,
                contract_date,
                price,
                floor
            )
        )
    """)

    # ✅ 아파트 전월세 거래 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS apt_rent_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        region TEXT,
        sigungu TEXT,
        dong TEXT,
        apt_name TEXT,
        size REAL,
        contract_date TEXT,
        deposit INTEGER,
        monthly_rent INTEGER,
        floor INTEGER,
        source_month TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,

        UNIQUE (
            region,
            sigungu,
            dong,
            apt_name,
            size,
            contract_date,
            deposit,
            monthly_rent,
            floor
        )
    )
    """)

    # ✅ 분양권 거래 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS presale_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        region TEXT,
        sigungu TEXT,
        dong TEXT,
        apt_name TEXT,
        size REAL,
        contract_date TEXT,
        price INTEGER,
        floor INTEGER,
        source_month TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,

        UNIQUE (
            region,
            sigungu,
            dong,
            apt_name,
            size,
            contract_date,
            price,
            floor
        )
    )
    """)

    # 전월세 조회
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_region_sigungu_dong
        ON apt_rent_trades(region, sigungu, dong)
    """)

    # 🔍 조회 로그 테이블
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_type TEXT,
            region TEXT,
            sigungu TEXT,
            dong TEXT,
            apt_name TEXT,
            size TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ✅ 전월세 단지 조회 속도 개선 인덱스
    # 지역 + 시군구 + 동 + 단지명 기준으로 빠르게 조회한다.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_region_sigungu_dong_apt
        ON apt_rent_trades(region, sigungu, dong, apt_name)
    """)

    # ✅ 전월세 평형 조회 속도 개선 인덱스
    # 지역 + 시군구 + 단지명 + 전용면적 기준으로 빠르게 조회한다.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_region_sigungu_apt_size
        ON apt_rent_trades(region, sigungu, apt_name, size)
    """)
    
    # ✅ 전월세 분석 조회 속도 개선 인덱스
    # 단지명 + 전용면적 + 계약일 기준으로 빠르게 최근 거래를 찾는다.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_apt_size_date
        ON apt_rent_trades(apt_name, size, contract_date)
    """)

    # 📊 분석 로그 테이블
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analysis_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT,
            sigungu TEXT,
            dong TEXT,
            apt_name TEXT,
            size TEXT,
            user_price INTEGER,
            ai_price INTEGER,
            result TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ✅ 전국 시군구 코드 테이블
    cur.execute("""
        CREATE TABLE IF NOT EXISTS region_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sido TEXT NOT NULL,
            sigungu TEXT NOT NULL,
            lawd_cd TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ✅ 검색 속도 향상 인덱스
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_region_sigungu
        ON apt_sale_trades(region, sigungu)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_region_sigungu_dong
        ON apt_sale_trades(region, sigungu, dong)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_apt_size
        ON apt_sale_trades(apt_name, size)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_region_sigungu_apt
        ON apt_sale_trades(
            region,
            sigungu,
            apt_name
        )
    """)

    # ✅ 분양권 검색 속도 향상 인덱스
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_region_sigungu
        ON presale_trades(region, sigungu)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_region_sigungu_apt
        ON presale_trades(region, sigungu, apt_name)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_apt_size
        ON presale_trades(apt_name, size)
    """)

    # ✅ 전월세 검색 속도 향상 인덱스
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_region_sigungu_dong
        ON presale_trades(region, sigungu, dong)
    """)
    
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_region_sigungu
        ON apt_rent_trades(region, sigungu)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_region_sigungu_apt
        ON apt_rent_trades(region, sigungu, apt_name)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rent_apt_size
        ON apt_rent_trades(apt_name, size)
    """)

    # ✅ 관리자 페이지 속도 향상 인덱스
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_logs_created_at
        ON search_logs(created_at)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_logs_apt_name
        ON search_logs(apt_name)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_logs_region_sigungu
        ON search_logs(region, sigungu)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_analysis_logs_created_at
        ON analysis_logs(created_at)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_region_sigungu_dong
        ON presale_trades(region, sigungu, dong)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_presale_region_sigungu_apt
        ON presale_trades(region, sigungu, apt_name)
    """)

    conn.commit()
    conn.close()


def insert_apt_sale_trade(trade):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO apt_sale_trades (
                region, sigungu, dong, apt_name, size,
                contract_date, price, floor, source_month
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            trade["region"],
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"],
            trade["size"],
            trade["contract_date"],
            trade["price"],
            trade["floor"],
            trade["source_month"]
        ))

        conn.commit()

        print(
            "✅ Supabase 매매 저장:",
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"]
        )

    except Exception as e:
        conn.rollback()
        print("❌ 매매 저장 오류:", e)
        print("❌ 데이터:", trade)

    finally:
        cur.close()
        release_pg_connection(conn)

# ✅ 아파트 전월세 거래 저장
def insert_apt_rent_trade(trade):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO apt_rent_trades (
                region, sigungu, dong, apt_name, size,
                contract_date, deposit, monthly_rent, floor, source_month
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            trade["region"],
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"],
            trade["size"],
            trade["contract_date"],
            trade["deposit"],
            trade["monthly_rent"],
            trade["floor"],
            trade["source_month"]
        ))

        conn.commit()

        print(
            "✅ Supabase 전월세 저장:",
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"]
        )

    except Exception as e:
        conn.rollback()
        print("❌ 전월세 저장 오류:", e)
        print("❌ 데이터:", trade)

    finally:
        cur.close()
        release_pg_connection(conn)

# ✅ 분양권 거래 저장
def insert_presale_trade(trade):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO presale_trades (
                region, sigungu, dong, apt_name, size,
                contract_date, price, floor, source_month
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            trade["region"],
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"],
            trade["size"],
            trade["contract_date"],
            trade["price"],
            trade["floor"],
            trade["source_month"]
        ))

        conn.commit()

        print(
            "✅ Supabase 분양권 저장:",
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"]
        )

    except Exception as e:
        conn.rollback()
        print("❌ 분양권 저장 오류:", e)
        print("❌ 데이터:", trade)

    finally:
        cur.close()
        release_pg_connection(conn)


def get_apt_sale_trades(apt_name, size):
    size_key = f"{float(size):.4f}"
    cache_key = f"sale_trades:{apt_name}:{size_key}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        region,
        sigungu,
        dong,
        apt_name,
        size,
        contract_date,
        price,
        floor,
        source_month
    FROM apt_sale_trades
    WHERE apt_name = %s
    AND ROUND(size::numeric, 4) = ROUND(%s::numeric, 4)
    AND source_month >= TO_CHAR(CURRENT_DATE - INTERVAL '24 months', 'YYYYMM')
ORDER BY contract_date DESC
    """, (
        apt_name,
        float(size)
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": rows
    }

    return rows

# ✅ 분양권 거래 조회
def get_presale_trades(apt_name, size):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        region,
        sigungu,
        dong,
        apt_name,
        size,
        contract_date,
        price,
        floor,
        source_month
    FROM presale_trades
    WHERE apt_name = %s
    AND ROUND(size::numeric, 4) = ROUND(%s::numeric, 4)
    AND source_month >= TO_CHAR(CURRENT_DATE - INTERVAL '24 months', 'YYYYMM')
    ORDER BY contract_date DESC
    
    """, (
        apt_name,
        float(size)
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    return rows

def get_dongs_from_db(region, sigungu):
    cache_key = f"sale_dongs:{region}:{sigungu}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT DISTINCT dong
    FROM apt_sale_list
    WHERE region = %s
    AND sigungu = %s
    AND dong IS NOT NULL
    AND dong <> ''
    ORDER BY dong
    """, (
        region,
        sigungu
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

# ✅ 분양권 동 목록 조회
def get_presale_dongs_from_db(region, sigungu):
    cache_key = f"presale_dongs:{region}:{sigungu}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT dong
        FROM presale_list
        WHERE region = %s
        AND sigungu = %s
        AND dong IS NOT NULL
        AND dong <> ''
        ORDER BY dong
    """, (
        region,
        sigungu
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

def get_apts_from_db(region, sigungu, dong):
    cache_key = f"sale_apts:{region}:{sigungu}:{dong}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT apt_name
        FROM apt_sale_list
        WHERE region = %s
        AND sigungu = %s
        AND dong = %s
        AND apt_name IS NOT NULL
        AND apt_name <> ''
        ORDER BY apt_name
    """, (
        region,
        sigungu,
        dong
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

# ✅ 분양권 단지 목록 조회
def get_presale_apts_from_db(region, sigungu, dong):
    cache_key = f"presale_apts:{region}:{sigungu}:{dong}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT apt_name
        FROM presale_list
        WHERE region = %s
        AND sigungu = %s
        AND dong = %s
        AND apt_name IS NOT NULL
        AND apt_name <> ''
        ORDER BY apt_name
    """, (
        region,
        sigungu,
        dong
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

def get_sizes_from_db(region, sigungu, apt_name):
    cache_key = f"sale_sizes:{region}:{sigungu}:{apt_name}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ROUND(size::numeric, 4)
        FROM apt_sale_list
        WHERE region = %s
        AND sigungu = %s
        AND apt_name = %s
        AND size IS NOT NULL
        ORDER BY ROUND(size::numeric, 4)
    """, (
        region,
        sigungu,
        apt_name
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [float(row[0]) for row in rows if row[0] is not None]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

# ✅ 분양권 평형 목록 조회
def get_presale_sizes_from_db(region, sigungu, apt_name):
    cache_key = f"presale_sizes:{region}:{sigungu}:{apt_name}:real"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ROUND(size::numeric, 4)
        FROM presale_list
        WHERE region = %s
        AND sigungu = %s
        AND apt_name = %s
        AND size IS NOT NULL
        ORDER BY ROUND(size::numeric, 4)
    """, (
        region,
        sigungu,
        apt_name
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [float(row[0]) for row in rows if row[0] is not None]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

def get_rent_dongs_from_db(region, sigungu):
    cache_key = f"rent_dongs:{region}:{sigungu}:real"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT dong
        FROM rent_list
        WHERE region = %s
        AND sigungu = %s
        AND dong IS NOT NULL
        AND dong <> ''
        ORDER BY dong
    """, (
        region,
        sigungu
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

# ✅ 전월세 단지 목록 조회
# 선택한 지역(region, sigungu)과 동(dong)에 있는
# 전월세 거래 단지명을 DB에서 중복 없이 가져온다.
def get_rent_apts_from_db(region, sigungu, dong):
    cache_key = f"rent_apts:{region}:{sigungu}:{dong}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT apt_name
        FROM rent_list
        WHERE region = %s
        AND sigungu = %s
        AND dong = %s
        AND apt_name IS NOT NULL
        AND apt_name <> ''
        ORDER BY apt_name
    """, (
        region,
        sigungu,
        dong
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result

# ✅ 전월세 거래 목록 조회
# 선택한 단지명(apt_name)과 전용면적(size)의
# 최근 전월세 거래 데이터를 DB에서 가져온다.
def get_rent_trades(apt_name, size):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM apt_rent_trades
        WHERE apt_name = %s
        AND ROUND(size::numeric, 4) = ROUND(%s::numeric, 4)
        AND source_month >= TO_CHAR(CURRENT_DATE - INTERVAL '24 months', 'YYYYMM')
        ORDER BY contract_date DESC
    """, (
        apt_name,
        float(size)
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    return rows

# ✅ 전월세 평형 목록 조회
# 선택한 지역(region, sigungu)과 단지명(apt_name)에 있는
# 전월세 거래 전용면적을 DB에서 중복 없이 가져온다.
def get_rent_sizes_from_db(region, sigungu, apt_name):
    cache_key = f"rent_sizes:{region}:{sigungu}:{apt_name}:real"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ROUND(size::numeric, 4)
        FROM rent_list
        WHERE region = %s
        AND sigungu = %s
        AND apt_name = %s
        AND size IS NOT NULL
        ORDER BY ROUND(size::numeric, 4)
    """, (
        region,
        sigungu,
        apt_name
    ))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [float(row[0]) for row in rows if row[0] is not None]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result


# 🔍 조회 로그 저장
def insert_search_log(search_type, region=None, sigungu=None, dong=None, apt_name=None, size=None):
    conn = None
    cur = None

    try:
        conn = get_pg_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO search_logs (
                search_type, region, sigungu, dong, apt_name, size
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            search_type, region, sigungu, dong, apt_name, size
        ))

        conn.commit()

    except Exception as e:
        print("❌ 검색로그 저장 실패:", e)

    finally:
        if cur:
            cur.close()

        if conn:
            release_pg_connection(conn)

def get_analysis_cache_from_db(cache_key):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT result_json
        FROM analysis_result_cache
        WHERE cache_key = %s
        AND created_at >= NOW() - INTERVAL '6 hours'
        LIMIT 1
    """, (
        cache_key,
    ))

    row = cur.fetchone()

    cur.close()
    release_pg_connection(conn)

    if not row:
        return None

    cached_result = row[0]

    # ✅ null 캐시는 사용하지 않음
    if cached_result is None:
        return None

    # ✅ 문자열로 저장된 JSON이면 dict로 변환
    if isinstance(cached_result, str):
        if cached_result.strip().lower() in ("", "null"):
            return None

        try:
            cached_result = json.loads(cached_result)
        except:
            return None

    # ✅ 예전 메모리 캐시 형태가 DB에 저장된 경우 방어
    # {"time": ..., "data": {...}} 형태면 data만 반환
    if isinstance(cached_result, dict) and "data" in cached_result:
        cached_result = cached_result.get("data")

    # ✅ 최종적으로 정상 dict만 반환
    if not isinstance(cached_result, dict) or not cached_result:
        return None

    return cached_result


def save_analysis_cache_to_db(cache_key, result):
    if not isinstance(result, dict) or not result:
        return
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO analysis_result_cache (
            cache_key,
            result_json,
            created_at
        )
        VALUES (%s, %s, NOW())
        ON CONFLICT (cache_key)
        DO UPDATE SET
            result_json = EXCLUDED.result_json,
            created_at = NOW()
    """, (
        cache_key,
        json.dumps(result, ensure_ascii=False)
    ))

    conn.commit()

    cur.close()
    release_pg_connection(conn)    

# 📊 분석 로그 저장
def insert_analysis_log(region=None, sigungu=None, dong=None, apt_name=None, size=None, user_price=None, ai_price=None, result=None):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO analysis_logs (
            region, sigungu, dong, apt_name, size, user_price, ai_price, result
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        region, sigungu, dong, apt_name, size, user_price, ai_price, result
    ))

    conn.commit()

    cur.close()
    release_pg_connection(conn)

# 📌 오늘 조회수 가져오기
def get_today_search_count():
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM search_logs
        WHERE created_at >= CURRENT_DATE
        AND created_at < CURRENT_DATE + INTERVAL '1 day'
    """)

    count = cur.fetchone()[0]

    cur.close()
    release_pg_connection(conn)

    return count

# 📊 오늘 분석수
def get_today_analysis_count():
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM analysis_logs
        WHERE created_at >= CURRENT_DATE
        AND created_at < CURRENT_DATE + INTERVAL '1 day'
    """)

    count = cur.fetchone()[0]

    cur.close()
    release_pg_connection(conn)

    return count

# 🏢 인기 단지 TOP 5
def get_popular_apts(limit=5):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            apt_name,
            COUNT(*) as cnt
        FROM search_logs
        WHERE apt_name IS NOT NULL
        AND apt_name <> ''
        AND created_at >= CURRENT_DATE
        GROUP BY apt_name
        ORDER BY cnt DESC
        LIMIT %s
    """, (limit,))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    return [
        {
            "아파트": row[0],
            "조회수": row[1]
        }
        for row in rows
    ]

# 🌎 인기 지역 TOP 5
def get_popular_regions(limit=5):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            region,
            sigungu,
            COUNT(*) as cnt
        FROM search_logs
        WHERE region IS NOT NULL
        AND region <> ''
        AND sigungu IS NOT NULL
        AND sigungu <> ''
        AND created_at >= CURRENT_DATE
        GROUP BY region, sigungu
        ORDER BY cnt DESC
        LIMIT %s
    """, (limit,))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    return [
        {
            "지역": f"{row[0]} {row[1]}",
            "조회수": row[2]
        }
        for row in rows
    ]

# 📋 최근 분석 TOP 10
def get_recent_analysis(limit=10):
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            apt_name,
            size,
            user_price,
            ai_price,
            result,
            created_at
        FROM analysis_logs
        ORDER BY created_at DESC
        LIMIT %s
    """, (limit,))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    return [
        {
            "아파트": row[0],
            "평형": row[1],
            "입력가": row[2],
            "AI추천가": row[3],
            "판단": row[4],
            "분석시간": row[5]
        }
        for row in rows
    ]

# ✅ 시군구 코드 저장
def insert_region_code(sido, sigungu, lawd_cd):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO region_codes (
            sido, sigungu, lawd_cd
        ) VALUES (?, ?, ?)
    """, (
        sido, sigungu, lawd_cd
    ))

    conn.commit()
    conn.close()

# ✅ 전체 시군구 코드 가져오기
def get_all_region_codes():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT sido, sigungu, lawd_cd
        FROM region_codes
        ORDER BY sido, sigungu
    """)

    rows = cur.fetchall()
    conn.close()

    return rows

# ✅ 시군구 코드 개수 확인
def get_region_code_count():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM region_codes
    """)

    count = cur.fetchone()[0]
    conn.close()

    return count

# ✅ 시군구 코드 전체 삭제
def clear_region_codes():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM region_codes
    """)

    conn.commit()

    conn.close()

# ✅ SQLite DB 최적화
def optimize_database():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("ANALYZE")
    cur.execute("VACUUM")

    conn.close()

    print("DB 최적화 완료")

# ✅ DB 검색속도 테스트
def test_db_speed():
    conn = get_connection()
    cur = conn.cursor()

    start = time.time()

    cur.execute("""
        SELECT DISTINCT apt_name
        FROM apt_sale_trades
        WHERE region = ?
        AND sigungu = ?
        ORDER BY apt_name
        LIMIT 100
    """, (
        "경기도",
        "의왕시"
    ))

    rows = cur.fetchall()

    end = time.time()
    conn.close()

    print("검색 결과 수:", len(rows))
    print("검색 소요 시간:", round(end - start, 4), "초")

# ✅ DB 전체 상태 점검
def check_db_status():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM apt_sale_trades")
    trade_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM search_logs")
    search_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM analysis_logs")
    analysis_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM region_codes")
    region_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM presale_trades")
    presale_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM apt_rent_trades")
    rent_count = cur.fetchone()[0]

    conn.close()

    print("아파트 매매 데이터 수:", trade_count)
    print("분양권 데이터 수:", presale_count)
    print("전월세 데이터 수:", rent_count)
    print("조회 로그 수:", search_count)
    print("분석 로그 수:", analysis_count)
    print("시군구 코드 수:", region_count)

def get_apt_sigungu_list_from_db(region):
    cache_key = f"sale_sigungu:{region}"

    if cache_key in DB_CACHE:
        cached = DB_CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["data"]

    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT sigungu
        FROM apt_sale_list
        WHERE region = %s
        AND sigungu IS NOT NULL
        AND sigungu <> ''
        ORDER BY sigungu
    """, (region,))

    rows = cur.fetchall()

    cur.close()
    release_pg_connection(conn)

    result = [row[0] for row in rows]

    DB_CACHE[cache_key] = {
        "time": time.time(),
        "data": result
    }

    return result
    
def get_presale_sigungu_list_from_db(region):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT DISTINCT sigungu
            FROM presale_list
            WHERE region = %s
              AND sigungu IS NOT NULL
            ORDER BY sigungu
        """, (region,))

        rows = cur.fetchall()
        return [row[0] for row in rows]

    except Exception as e:
        print("❌ get_presale_sigungu_list_from_db 오류:", e)
        return []

    finally:
        cur.close()
        release_pg_connection(conn)
    
def get_rent_sigungu_list_from_db(region):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT DISTINCT sigungu
            FROM rent_list
            WHERE region = %s
              AND sigungu IS NOT NULL
            ORDER BY sigungu
        """, (region,))

        rows = cur.fetchall()
        return [row[0] for row in rows]

    except Exception as e:
        print("❌ get_rent_sigungu_list_from_db 오류:", e)
        return []

    finally:
        cur.close()
        release_pg_connection(conn)
    
def rebuild_apt_sale_list():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        print("✅ apt_sale_list 재생성 시작")

        cur.execute("DELETE FROM apt_sale_list")

        cur.execute("""
            INSERT INTO apt_sale_list (
                region,
                sigungu,
                dong,
                apt_name,
                size
            )
            SELECT DISTINCT
                region,
                sigungu,
                dong,
                apt_name,
                size
            FROM apt_sale_trades
            WHERE region IS NOT NULL
              AND sigungu IS NOT NULL
              AND dong IS NOT NULL
              AND apt_name IS NOT NULL
              AND size IS NOT NULL
              AND region <> ''
              AND sigungu <> ''
              AND dong <> ''
              AND apt_name <> ''
        """)

        conn.commit()

        print("✅ apt_sale_list 재생성 완료")

    except Exception as e:
        conn.rollback()
        print("❌ apt_sale_list 재생성 오류:", e)

    finally:
        cur.close()
        release_pg_connection(conn)    

if __name__ == "__main__":
    create_tables()
    check_db_status()
    
