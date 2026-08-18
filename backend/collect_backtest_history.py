import sqlite3
import time
import requests
import xml.etree.ElementTree as ET

from datetime import datetime


# =========================================================
# ✅ 백테스트 전용 과거 매매 데이터 수집기
#
# 운영 Supabase와 완전히 분리
# 저장 위치:
#   backtest_history.db
#
# 수집 목적:
#   미래예측 엔진의 상승장 / 하락장 / 보합장 백테스트
# =========================================================


# =========================================================
# ✅ 기존 update_trades.py에서 사용하는
# 국토부 SERVICE_KEY를 여기에 동일하게 넣으세요.
#
# ⚠️ ChatGPT에 인증키를 보내지는 마세요.
# =========================================================

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"


# =========================================================
# ✅ 국토부 아파트 매매 실거래 API
# =========================================================

APT_SALE_URL = (
    "https://apis.data.go.kr/1613000/"
    "RTMSDataSvcAptTrade/"
    "getRTMSDataSvcAptTrade"
)


# =========================================================
# ✅ 로컬 백테스트 DB
# =========================================================

DB_PATH = "backtest_history.db"


# =========================================================
# ✅ 수집 기간
#
# 2022년 하락장부터
# 2026년 현재까지 확보
# =========================================================

START_MONTH = "202201"
END_MONTH = "202607"


# =========================================================
# ✅ 1차 대표 단지
#
# lawd_cd = 시군구 법정동 코드
#
# size는 우리가 이미 검증에 사용한 정확한 전용면적
# =========================================================

TARGET_APARTMENTS = [

    {
        "region": "서울특별시",
        "sigungu": "강동구",
        "lawd_cd": "11740",
        "apt_name": "고덕그라시움",
        "size": 59.785
    },

    {
        "region": "서울특별시",
        "sigungu": "송파구",
        "lawd_cd": "11710",
        "apt_name": "헬리오시티",
        "size": 84.99
    },

    {
        "region": "서울특별시",
        "sigungu": "마포구",
        "lawd_cd": "11440",
        "apt_name": "마포래미안푸르지오4단지",
        "size": 84.5978
    },

    {
        "region": "서울특별시",
        "sigungu": "송파구",
        "lawd_cd": "11710",
        "apt_name": "잠실엘스",
        "size": 84.8
    },

    {
        "region": "경기도",
        "sigungu": "화성시 동탄구",
        "lawd_cd": "41597",
        "apt_name": "동탄역시범예미지아파트",
        "size": 84.8
    },

    {
        "region": "경기도",
        "sigungu": "수원시 영통구",
        "lawd_cd": "41117",
        "apt_name": "힐스테이트영통",
        "size": 84.8897
    },
    {
        "region": "경기도",
        "sigungu": "성남시 분당구",
        "lawd_cd": "41135",
        "apt_name": "파크뷰",
        "size": 84.99
    },

    {
        "region": "경기도",
        "sigungu": "의왕시",
        "lawd_cd": "41430",
        "apt_name": "의왕더샵캐슬",
        "size": 84.9681
    }
]


# =========================================================
# ✅ 이름 비교용
# 공백 차이 정도만 제거
# =========================================================

def normalize_name(value):

    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .replace(" ", "")
    )


# =========================================================
# ✅ 면적 비교
# =========================================================

def is_same_size(target_size, api_size):

    try:
        return round(
            float(target_size),
            4
        ) == round(
            float(api_size),
            4
        )

    except:
        return False


# =========================================================
# ✅ YYYYMM 월 목록 생성
# =========================================================

def generate_months(start_month, end_month):

    start = datetime.strptime(
        start_month,
        "%Y%m"
    )

    end = datetime.strptime(
        end_month,
        "%Y%m"
    )

    months = []

    current = start

    while current <= end:

        months.append(
            current.strftime("%Y%m")
        )

        if current.month == 12:

            current = current.replace(
                year=current.year + 1,
                month=1
            )

        else:

            current = current.replace(
                month=current.month + 1
            )

    return months


# =========================================================
# ✅ SQLite 연결
# =========================================================

def get_connection():

    return sqlite3.connect(
        DB_PATH
    )


# =========================================================
# ✅ 백테스트 테이블 생성
# =========================================================

def init_database():

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_sale_trades (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            region TEXT NOT NULL,
            sigungu TEXT NOT NULL,
            dong TEXT,

            apt_name TEXT NOT NULL,

            size REAL NOT NULL,

            contract_date TEXT NOT NULL,

            price INTEGER NOT NULL,

            floor INTEGER,

            apt_dong TEXT,

            source_month TEXT NOT NULL,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 조회용 가벼운 인덱스
    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_backtest_apt_size
        ON backtest_sale_trades (
            apt_name,
            size
        )
    """)

    conn.commit()

    conn.close()

    print(
        "✅ backtest_history.db 초기화 완료"
    )


# =========================================================
# ✅ API 요청
# =========================================================

def request_api(
    lawd_cd,
    deal_ymd,
    page_no=1,
    num_rows=1000
):

    params = {

        "serviceKey": SERVICE_KEY,

        "LAWD_CD": lawd_cd,

        "DEAL_YMD": deal_ymd,

        "pageNo": str(page_no),

        "numOfRows": str(num_rows)
    }

    last_error = None

    for attempt in range(1, 4):

        try:

            response = requests.get(
                APT_SALE_URL,
                params=params,
                timeout=30
            )

            response.raise_for_status()

            return response.text

        except Exception as e:

            last_error = e

            print(
                f"⚠️ API 요청 실패 "
                f"{lawd_cd} {deal_ymd} "
                f"page={page_no} "
                f"시도={attempt}/3 : {e}"
            )

            time.sleep(2)

    raise RuntimeError(
        f"API 요청 최종 실패: {last_error}"
    )


# =========================================================
# ✅ XML 안전 숫자 변환
# =========================================================

def safe_int(value):

    try:

        if value is None:
            return 0

        value = (
            str(value)
            .replace(",", "")
            .strip()
        )

        if not value:
            return 0

        return int(float(value))

    except:
        return 0


def safe_float(value):

    try:

        if value is None:
            return 0.0

        value = str(value).strip()

        if not value:
            return 0.0

        return float(value)

    except:
        return 0.0


# =========================================================
# ✅ XML 한 거래 파싱
# =========================================================

def parse_trade_item(item):

    apt_name = (
        item.findtext("aptNm", "")
        or ""
    ).strip()

    dong = (
        item.findtext("umdNm", "")
        or ""
    ).strip()

    apt_dong = (
        item.findtext("aptDong", "")
        or ""
    ).strip()

    size = safe_float(
        item.findtext("excluUseAr", "")
    )

    price = safe_int(
        item.findtext("dealAmount", "")
    )

    floor = safe_int(
        item.findtext("floor", "")
    )

    year = safe_int(
        item.findtext("dealYear", "")
    )

    month = safe_int(
        item.findtext("dealMonth", "")
    )

    day = safe_int(
        item.findtext("dealDay", "")
    )

    if (
        year <= 0
        or month <= 0
        or day <= 0
    ):

        return None

    contract_date = (
        f"{year:04d}-"
        f"{month:02d}-"
        f"{day:02d}"
    )

    return {

        "dong": dong,

        "apt_name": apt_name,

        "size": size,

        "contract_date": contract_date,

        "price": price,

        "floor": floor,

        "apt_dong": apt_dong
    }


# =========================================================
# ✅ 특정 시군구 + 월 전체 API 수집
# =========================================================

def fetch_month_items(
    lawd_cd,
    deal_ymd
):

    all_items = []

    page_no = 1

    total_count = None

    while True:

        xml_text = request_api(
            lawd_cd,
            deal_ymd,
            page_no=page_no
        )

        root = ET.fromstring(
            xml_text
        )

        # API 결과 코드 확인
        result_code = (
            root.findtext(
                ".//resultCode",
                ""
            )
            or ""
        ).strip()

        result_msg = (
            root.findtext(
                ".//resultMsg",
                ""
            )
            or ""
        ).strip()

        if result_code not in (
            "",
            "00",
            "000"
        ):

            raise RuntimeError(
                f"API 오류 "
                f"{result_code} "
                f"{result_msg}"
            )

        page_items = root.findall(
            ".//item"
        )

        if total_count is None:

            total_count = safe_int(
                root.findtext(
                    ".//totalCount",
                    "0"
                )
            )

        all_items.extend(
            page_items
        )

        if len(all_items) >= total_count:
            break

        if not page_items:
            break

        page_no += 1

    return all_items


# =========================================================
# ✅ 해당 단지 + 면적만 필터
# =========================================================

def filter_target_trades(
    items,
    target
):

    result = []

    target_name = normalize_name(
        target["apt_name"]
    )

    target_size = float(
        target["size"]
    )

    for item in items:

        trade = parse_trade_item(
            item
        )

        if not trade:
            continue

        api_name = normalize_name(
            trade["apt_name"]
        )

        if api_name != target_name:
            continue

        if not is_same_size(
            target_size,
            trade["size"]
        ):
            continue

        result.append(
            trade
        )

    return result


# =========================================================
# ✅ 월별 저장
#
# 같은 단지/면적/월을 다시 실행해도
# 데이터가 누적 중복되지 않도록
# 먼저 그 월의 기존 데이터를 삭제 후 저장
# =========================================================

def save_month_trades(
    target,
    source_month,
    trades
):

    conn = get_connection()

    cur = conn.cursor()

    try:

        cur.execute("""
            DELETE FROM backtest_sale_trades
            WHERE region = ?
              AND sigungu = ?
              AND apt_name = ?
              AND ABS(size - ?) < 0.01
              AND source_month = ?
        """, (

            target["region"],

            target["sigungu"],

            target["apt_name"],

            float(target["size"]),

            source_month
        ))

        for trade in trades:

            cur.execute("""
                INSERT INTO backtest_sale_trades (

                    region,
                    sigungu,
                    dong,

                    apt_name,
                    size,

                    contract_date,
                    price,
                    floor,

                    apt_dong,
                    source_month

                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (

                target["region"],

                target["sigungu"],

                trade["dong"],

                trade["apt_name"],

                trade["size"],

                trade["contract_date"],

                trade["price"],

                trade["floor"],

                trade["apt_dong"],

                source_month
            ))

        conn.commit()

    finally:

        conn.close()


# =========================================================
# ✅ 한 단지 전체 기간 수집
# =========================================================

def collect_apartment(
    target,
    months
):

    print()
    print("=" * 70)

    print(
        f"🏢 백테스트 과거자료 수집 시작"
    )

    print(
        f"지역 : "
        f"{target['region']} "
        f"{target['sigungu']}"
    )

    print(
        f"단지 : "
        f"{target['apt_name']}"
    )

    print(
        f"면적 : "
        f"{target['size']}㎡"
    )

    print("=" * 70)

    total_saved = 0

    for index, ym in enumerate(
        months,
        start=1
    ):

        try:

            all_items = fetch_month_items(
                target["lawd_cd"],
                ym
            )

            trades = filter_target_trades(
                all_items,
                target
            )

            save_month_trades(
                target,
                ym,
                trades
            )

            total_saved += len(
                trades
            )

            print(
                f"[{index}/{len(months)}] "
                f"{ym} "
                f"API {len(all_items)}건 "
                f"→ 대상 거래 "
                f"{len(trades)}건 저장"
            )

            # API 과부하 방지
            time.sleep(0.15)

        except Exception as e:

            print(
                f"❌ {ym} 수집 오류 : "
                f"{e}"
            )

    print(
        f"✅ {target['apt_name']} "
        f"전체 저장 완료 : "
        f"{total_saved}건"
    )


# =========================================================
# ✅ 저장 결과 확인
# =========================================================

def print_summary():

    conn = get_connection()

    cur = conn.cursor()

    print()
    print("=" * 70)
    print("📊 백테스트 과거자료 저장 결과")
    print("=" * 70)

    cur.execute("""
        SELECT
            region,
            sigungu,
            apt_name,
            ROUND(size, 4),
            COUNT(*),
            MIN(contract_date),
            MAX(contract_date)
        FROM backtest_sale_trades
        GROUP BY
            region,
            sigungu,
            apt_name,
            ROUND(size, 4)
        ORDER BY
            region,
            sigungu,
            apt_name
    """)

    rows = cur.fetchall()

    for row in rows:

        print(
            f"{row[0]} {row[1]} | "
            f"{row[2]} | "
            f"{row[3]}㎡ | "
            f"{row[4]}건 | "
            f"{row[5]} ~ {row[6]}"
        )

    conn.close()

    print("=" * 70)

def print_apartment_sizes(
    lawd_cd,
    apt_name,
    deal_ymd="202201"
):
    items = fetch_month_items(
        lawd_cd,
        deal_ymd
    )

    target_name = normalize_name(
        apt_name
    )

    sizes = set()

    for item in items:

        trade = parse_trade_item(item)

        if not trade:
            continue

        if normalize_name(
            trade["apt_name"]
        ) != target_name:
            continue

        sizes.add(
            round(
                float(trade["size"]),
                4
            )
        )

    print()
    print(
        f"🔍 {apt_name} "
        f"{deal_ymd} 면적 목록:"
    )

    print(
        sorted(sizes)
    )

def debug_month_apartment_names(
    lawd_cd,
    deal_ymd="202201"
):
    items = fetch_month_items(
        lawd_cd,
        deal_ymd
    )

    print()
    print("=" * 80)
    print(
        f"🔍 API 원본 확인 : "
        f"{lawd_cd} / {deal_ymd}"
    )
    print(
        f"전체 API item 수 : "
        f"{len(items)}건"
    )
    print("=" * 80)

    found = []

    for item in items:

        trade = parse_trade_item(item)

        if not trade:
            continue

        dong = str(
            trade.get("dong", "")
        ).strip()

        apt_name = str(
            trade.get("apt_name", "")
        ).strip()

        size = trade.get("size")

        if not apt_name:
            continue

        if size is None:
            continue

        try:
            size_float = float(size)
        except (TypeError, ValueError):
            continue

        # ---------------------------------------------
        # 외부검증 후보 검색
        # 전용 84㎡ 계열만 출력
        # ---------------------------------------------
        if not (
            84.0 <= size_float < 85.0
        ):
            continue

        found.append(
            (
                dong,
                apt_name,
                size_float
            )
        )

    print()
    print(
        f"84㎡ 계열 거래건수 : "
        f"{len(found)}건"
    )
    print()

    for dong, apt_name, size in found:

        print(
            f"[{dong}] | "
            f"[{apt_name}] | "
            f"{size}㎡"
        )

    print("=" * 80)

def debug_api_region_code(
    deal_ymd="202201"
):

    test_codes = [
        ("과거 화성시", "41590"),
        ("현재 동탄구", "41597"),
    ]

    print()
    print("=" * 70)
    print(f"🔍 화성 지역코드 API 비교 : {deal_ymd}")
    print("=" * 70)

    for label, lawd_cd in test_codes:

        try:

            xml_text = request_api(
                lawd_cd,
                deal_ymd,
                page_no=1,
                num_rows=1000
            )

            root = ET.fromstring(
                xml_text
            )

            result_code = (
                root.findtext(
                    ".//resultCode",
                    ""
                )
                or ""
            ).strip()

            result_msg = (
                root.findtext(
                    ".//resultMsg",
                    ""
                )
                or ""
            ).strip()

            total_count = safe_int(
                root.findtext(
                    ".//totalCount",
                    "0"
                )
            )

            items = root.findall(
                ".//item"
            )

            print()
            print(
                f"[{label}] "
                f"LAWD_CD={lawd_cd}"
            )

            print(
                f"resultCode : "
                f"{result_code}"
            )

            print(
                f"resultMsg : "
                f"{result_msg}"
            )

            print(
                f"totalCount : "
                f"{total_count}"
            )

            print(
                f"현재 페이지 item : "
                f"{len(items)}건"
            )

        except Exception as e:

            print(
                f"❌ {label} "
                f"{lawd_cd} 오류 : {e}"
            )

    print()
    print("=" * 70)

# =========================================================
# ✅ MAIN
# =========================================================

if __name__ == "__main__":

    if (
        not SERVICE_KEY
        or SERVICE_KEY
        == "여기에 서비스 키를 입력"
    ):

        print(
            "❌ SERVICE_KEY를 먼저 입력하세요."
        )

        raise SystemExit

    init_database()

    months = generate_months(
        START_MONTH,
        END_MONTH
    )

    print(
        f"✅ 수집 월 수 : "
        f"{len(months)}개월"
    )

    print(
        f"✅ 대표 단지 수 : "
        f"{len(TARGET_APARTMENTS)}개"
    )

    for target in TARGET_APARTMENTS:

        collect_apartment(
            target,
            months
        )

    print_summary()

    print()
    print(
        "🎉 백테스트 과거 데이터 수집 완료"
    )