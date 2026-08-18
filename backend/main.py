from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import json
import requests
session = requests.Session()
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
import statistics
import math
import time
import threading
import zipfile
import os

import sqlite3

from db import (
    get_apt_sale_trades, get_dongs_from_db, 
    get_apts_from_db, get_sizes_from_db, 
    insert_search_log, insert_analysis_log, 
    get_today_search_count, get_today_analysis_count, 
    get_popular_apts, get_popular_regions, get_recent_analysis,
    get_presale_trades, get_presale_dongs_from_db, get_presale_apts_from_db,
    get_presale_sizes_from_db,

    get_analysis_cache_from_db, save_analysis_cache_to_db,

    get_rent_dongs_from_db, get_rent_apts_from_db,
    get_rent_sizes_from_db, get_rent_trades, 
    # get_pg_connection,

    get_apt_sigungu_list_from_db, get_presale_sigungu_list_from_db,
    get_rent_sigungu_list_from_db,

    get_pg_connection, release_pg_connection
)
from fastapi.staticfiles import StaticFiles
from difflib import SequenceMatcher

DEBUG_PRICE_ENGINE = False
DEBUG_FUTURE_ENGINE = False

BACKTEST_DB_PATH = "backtest_history.db"

app = FastAPI()

@app.get("/manifest.json")
def manifest():
    return FileResponse(
        "manifest.json",
        media_type="application/manifest+json"
    )

@app.get("/app_icon_v1.png")
def app_icon():
    return FileResponse(
        "app_icon_v1.png",
        media_type="image/png"
    )
@app.get("/favicon.ico")
def favicon():
    return FileResponse(
        "app_icon_v1.png",
        media_type="image/png"
    )

app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 데이터 로드
# =========================================================
# 최신 법정동 코드 자동 로드
# region_codes.txt는 실제로 ZIP 압축파일이며,
# 내부의 '법정동코드 전체자료.txt'를 읽어서 시군구 코드를 생성한다.
# =========================================================
def load_lawd_map_from_region_codes():
    result = {}

    try:
        with zipfile.ZipFile("region_codes.txt", "r") as zip_file:

            # 압축파일 내부의 첫 번째 txt 파일 읽기
            inner_file_name = zip_file.namelist()[0]
            raw_data = zip_file.read(inner_file_name)

            # 법정동 코드 원본은 CP949 인코딩
            text = raw_data.decode("cp949")

            for line in text.splitlines():
                parts = line.strip().split()

                if len(parts) < 3:
                    continue

                full_code = parts[0]
                status = parts[-1]
                region_name = " ".join(parts[1:-1])

                # 존재하는 행정구역만 사용
                if status != "존재":
                    continue

                # 10자리 숫자 코드만 처리
                if not full_code.isdigit() or len(full_code) != 10:
                    continue

                # 시군구 대표 코드만 사용
                # 예: 2827500000 → 인천광역시 서해구
                if not full_code.endswith("00000"):
                    continue

                lawd_code = full_code[:5]
                result[region_name] = lawd_code

        if not result:
            raise ValueError("법정동 코드가 한 건도 생성되지 않았습니다.")

        print(f"✅ 최신 법정동 코드 로드 완료: {len(result)}개")
        print(
            "✅ 인천광역시 서해구 코드:",
            result.get("인천광역시 서해구")
        )

        return result

    except Exception as e:
        print("❌ 최신 법정동 코드 로드 실패:", e)

        # 오류 발생 시 기존 JSON을 임시 백업으로 사용
        with open("lawd_codes.json", "r", encoding="utf-8") as f:
            fallback_map = json.load(f)

        print(f"⚠️ 기존 lawd_codes.json 사용: {len(fallback_map)}개")
        return fallback_map


# 서버 시작 시 최신 코드 자동 생성
lawd_map = load_lawd_map_from_region_codes()


trade_cache = {}
analysis_cache = {}
MAX_ANALYSIS_CACHE = 1000
dong_cache = {}
apt_cache = {}
areas_cache = {}
lawd_cache = {}

MAX_ANALYSIS_CACHE = 1000
MAX_APT_CACHE = 300
MAX_DONG_CACHE = 300
MAX_AREAS_CACHE = 300

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"

DEBUG = False

ADMIN_PASSWORD = "리치1234"

def normalize_region(text: str) -> str:
    """
    지역명을 문자열 비교와 캐시 키에 사용하기 위한 정규화 함수.
    region_alias 기반 DB 보정 함수와는 역할이 다르다.
    """
    text = (text or "").strip().replace(" ", "")

    # 긴 명칭부터 먼저 제거해야 한다.
    text = text.replace("특별시", "")
    text = text.replace("광역시", "")
    text = text.replace("자치시", "")
    text = text.replace("자치도", "")
    text = text.replace("특별자치시", "")
    text = text.replace("특별자치도", "")

    # 축약형 보정
    replacements = {
        "전라남": "전남",
        "전라북": "전북",
        "경상남": "경남",
        "경상북": "경북",
        "충청남": "충남",
        "충청북": "충북",
    }

    for full, short in replacements.items():
        text = text.replace(full, short)

    return text

def normalize_region_for_db(region):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT db_region
            FROM region_alias
            WHERE input_region = %s
            LIMIT 1
        """, (region,))

        row = cur.fetchone()

        if row and row[0]:
            return row[0]

        return region

    except Exception as e:
        print("지역 보정 조회 실패:", e)
        return region

    finally:
        cur.close()
        release_pg_connection(conn)

# ✅ 아파트 매매 분석 엔진
def analyze_apt_sale_engine(
    region,
    apt_name,
    size,
    user_price=None,
    direction=None,
    floor_level=None,
    interior=None
):
    pass

def find_lawd_cd(region: str):
    if "세종" in region:
        return "36110"

   
    region_norm = normalize_region(region)

    if region_norm in lawd_cache:
        return lawd_cache[region_norm]

    for name, code in lawd_map.items():
        if region_norm in normalize_region(name):
            lawd_cache[region_norm] = code
            return code

    return None

def warmup_region(region: str):
    return


# 문자열 정규화
def normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())

def normalize_dong_name(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())

def normalize_apt_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\s+", "", name)
    name = re.sub(r"[^\w가-힣]", "", name)

    remove_words = [
        "아파트",
        "단지",
        "주상복합"
    ]

    for word in remove_words:
        name = name.replace(word, "")

    return name

def apt_name_similarity(name1: str, name2: str) -> float:
    n1 = normalize_apt_name(name1)
    n2 = normalize_apt_name(name2)

    if not n1 or not n2:
        return 0

    return SequenceMatcher(None, n1, n2).ratio()

def is_same_apartment_name(user_name: str, data_name: str) -> bool:
    user_norm = normalize_apt_name(user_name)
    data_norm = normalize_apt_name(data_name)

    return user_norm == data_norm

def is_same_size(user_size, data_size) -> bool:
    return round(float(user_size), 4) == round(float(data_size), 4)

def db_rows_to_items(rows):
    items = []

    for row in rows:
        items.append({
            "apt_name": row[3],
            "dong": row[2],

            # apt_dong 컬럼이 있는 새 조회 결과는 row[9] 사용
            # 이전 캐시처럼 컬럼이 9개뿐인 경우에는 빈 문자열 처리
            "apt_dong": (row[9] or "") if len(row) > 9 else "",

            "size": round(float(row[4]), 4),
            "price": int(row[6]),
            "date": row[5],
            "floor": row[7]
        })

    return items

# ✅ 전월세 DB row를 분석용 dict로 변환
# apt_rent_trades 테이블 구조:
# id, region, sigungu, dong, apt_name, size, contract_date,
# deposit, monthly_rent, floor, source_month, created_at
def rent_rows_to_items(rows):
    items = []

    for row in rows:
        items.append({
            "apt_name": row[4],
            "dong": row[3],
            "size": round(float(row[5]), 4),
            "date": row[6],
            "deposit": int(row[7] or 0),
            "monthly_rent": int(row[8] or 0),
            "floor": row[9]
        })

    return items

# ✅ 전월세 분석 엔진
# 보증금과 월세 데이터를 분리해서
# 전세 / 월세 여부를 판단하고 평균 보증금, 평균 월세를 계산한다.
def analyze_rent_engine(region, apt_name, size):

    rows = get_rent_trades(region, apt_name, size)
    items = rent_rows_to_items(rows)

    if not items:
        return {
            "결과": "데이터 없음",
            "한줄결론": "최근 전월세 실거래 데이터가 부족합니다."
        }

    # ✅ 전세: 월세가 0원인 거래
    jeonse_items = [
        item for item in items
        if item["monthly_rent"] == 0
    ]

    # ✅ 월세: 월세가 0원보다 큰 거래
    monthly_items = [
        item for item in items
        if item["monthly_rent"] > 0
    ]

    # ✅ 전세 평균 보증금
    # 월세 거래의 낮은 보증금과 섞지 않고,
    # 월세 0원인 전세 거래만 따로 평균 계산한다.
    avg_jeonse_deposit = round(
        sum(item["deposit"] for item in jeonse_items) / len(jeonse_items)
    ) if jeonse_items else 0

    # ✅ 최근 전세 5건
    recent_jeonse_trades = sorted(
        [x for x in items if x["monthly_rent"] == 0],
        key=lambda x: x["date"],
        reverse=True
    )[:5]

    # ✅ 최근 월세 5건
    recent_monthly_trades = sorted(
        monthly_items,
        key=lambda x: x["date"],
        reverse=True
    )[:5]

    # ✅ 전세 상승률 계산
    # 최근 3개월 전세 평균 VS 이전 3개월 전세 평균 기준
    # 거래일(date)을 기준으로 시간 구간을 나눠 계산한다.
    from datetime import datetime, timedelta

    all_jeonse_trades = sorted(
        [x for x in items if x["monthly_rent"] == 0],
        key=lambda x: x["date"],
        reverse=True
    )

    today = datetime.today().date()
    recent_start = today - timedelta(days=90)
    previous_start = today - timedelta(days=180)

    recent_3m_jeonse = []
    previous_3m_jeonse = []

    for x in all_jeonse_trades:
        try:
            trade_date = x["date"]

            if isinstance(trade_date, str):
                trade_date = datetime.strptime(trade_date[:10], "%Y-%m-%d").date()

            if trade_date >= recent_start:
                recent_3m_jeonse.append(x)
            elif previous_start <= trade_date < recent_start:
                previous_3m_jeonse.append(x)

        except Exception:
            continue

    if len(recent_3m_jeonse) >= 2 and len(previous_3m_jeonse) >= 2:
        recent_avg_jeonse = round(
            sum(x["deposit"] for x in recent_3m_jeonse) / len(recent_3m_jeonse)
        )

        previous_avg_jeonse = round(
            sum(x["deposit"] for x in previous_3m_jeonse) / len(previous_3m_jeonse)
        )

        jeonse_change_rate = round(
            ((recent_avg_jeonse - previous_avg_jeonse) / previous_avg_jeonse) * 100,
            1
        ) if previous_avg_jeonse else None

        jeonse_trend_reliability = "높음"

    elif len(recent_3m_jeonse) >= 1 and len(previous_3m_jeonse) >= 1:
        jeonse_change_rate = None
        jeonse_trend_reliability = "보통"

    elif len(all_jeonse_trades) >= 2:
        jeonse_change_rate = None
        jeonse_trend_reliability = "낮음"

    else:
        jeonse_change_rate = None
        jeonse_trend_reliability = "산정 불가"

    jeonse_trend_basis = "최근 3개월 평균 vs 이전 3개월 평균"
    jeonse_recent_3m_count = len(recent_3m_jeonse)
    jeonse_previous_3m_count = len(previous_3m_jeonse)

    # ✅ 월세 평균 보증금
    # 월세 거래는 보증금 + 월세 구조이므로,
    # 월세 거래의 보증금만 따로 평균 계산한다.
    avg_monthly_deposit = round(
        sum(item["deposit"] for item in monthly_items) / len(monthly_items)
    ) if monthly_items else 0


    # ✅ 평균 월세
    # 월세가 0보다 큰 거래만 대상으로 월세 평균을 계산한다.
    avg_monthly_rent = round(
        sum(item["monthly_rent"] for item in monthly_items) / len(monthly_items)
    ) if monthly_items else 0

    # ✅ 거래 활성도 판단
    # 최근 12개월 전월세 거래건수를 기준으로 시장 활동성을 판단한다.
    total_rent_trade_count = len(items)

    if total_rent_trade_count >= 20:
        rent_activity_level = "활발 🟢"
    elif total_rent_trade_count >= 10:
        rent_activity_level = "보통 🟡"
    elif total_rent_trade_count >= 5:
        rent_activity_level = "적음 🔴"
    else:
        rent_activity_level = "데이터 부족 ⚪"

    # ✅ 전월세 거래 구성에 따른 한줄 결론
    if len(jeonse_items) > 0 and len(monthly_items) == 0:
        rent_conclusion = "최근 거래는 전세 중심으로 형성되어 있습니다."
        rent_ai_comment = "해당 평형은 최근 월세 거래보다 전세 거래가 중심입니다. 평균 전세보증금을 기준으로 임대 수준을 참고하는 것이 좋습니다."

    elif len(monthly_items) > 0 and len(jeonse_items) == 0:
        rent_conclusion = "최근 거래는 월세 중심으로 형성되어 있습니다."
        rent_ai_comment = "해당 평형은 최근 전세 거래보다 월세 거래가 중심입니다. 보증금과 월세를 함께 비교해 부담 수준을 판단하는 것이 좋습니다."

    else:
        rent_conclusion = "최근 거래는 전세와 월세가 함께 형성되어 있습니다."
        rent_ai_comment = "전세 보증금과 월세 거래가 모두 확인됩니다. 전세는 보증금 수준을, 월세는 매월 부담액을 함께 비교하는 것이 좋습니다."

    # ✅ 시장 유형 판단
    if len(jeonse_items) > 0 and len(monthly_items) == 0:
        rent_market_type = "전세 중심"

    elif len(monthly_items) > 0 and len(jeonse_items) == 0:
        rent_market_type = "월세 중심"

    else:
        rent_market_type = "혼합 시장"   

    # ✅ 최근 12개월 전월세 거래량 집계
    # contract_date 앞 7자리(YYYY-MM)를 기준으로 월별 거래건수를 계산한다.
    rent_monthly_volume_map = {}

    for item in items:
        month = str(item["date"])[:7]

        if not month:
            continue

        if month not in rent_monthly_volume_map:
            rent_monthly_volume_map[month] = {
                "month": month,
                "jeonse": 0,
                "monthly": 0,
                "count": 0
            }

        if item["monthly_rent"] > 0:
            rent_monthly_volume_map[month]["monthly"] += 1
        else:
            rent_monthly_volume_map[month]["jeonse"] += 1

        rent_monthly_volume_map[month]["count"] += 1

    rent_monthly_volume = list(rent_monthly_volume_map.values())
    rent_monthly_volume = sorted(rent_monthly_volume, key=lambda x: x["month"])[-12:]

    # ✅ 신뢰도 있는 AI 설명 생성
    total_trade_count = len(items)
    jeonse_count = len(jeonse_items)
    monthly_count = len(monthly_items)

    if jeonse_change_rate is None:
        trend_text = "전세 상승률은 거래 분포가 부족해 산정하지 않았습니다."
    else:
        if jeonse_change_rate > 0:
            trend_text = f"최근 3개월 평균 전세보증금은 이전 3개월 대비 {jeonse_change_rate}% 상승했습니다."
        elif jeonse_change_rate < 0:
            trend_text = f"최근 3개월 평균 전세보증금은 이전 3개월 대비 {abs(jeonse_change_rate)}% 하락했습니다."
        else:
            trend_text = "최근 3개월 평균 전세보증금은 이전 3개월과 유사한 수준입니다."

    rent_ai_comment = (
        f"최근 전월세 거래는 총 {total_trade_count}건이며, "
        f"전세 {jeonse_count}건, 월세 {monthly_count}건으로 구성되어 있습니다. "
        f"시장 유형은 {rent_market_type}이며, 거래 활성도는 {rent_activity_level} 수준입니다. "
        f"{trend_text} "
        f"본 판단은 {jeonse_trend_basis}과 최근 거래량을 함께 고려한 참고 분석입니다."
    )

    for t in recent_jeonse_trades:
        print(t)

    for t in recent_monthly_trades:
        print(t)

    result = {
        "유형": "전월세",
        "아파트": apt_name,
        "평형": size,
        "거래건수": len(items),
        "전세거래건수": len(jeonse_items),
        "월세거래건수": len(monthly_items),
        "평균전세보증금": avg_jeonse_deposit,
        "평균월세보증금": avg_monthly_deposit,
        "평균월세": avg_monthly_rent,
        "최근거래5건": items[:5],
        "시장유형": rent_market_type,
        "시장수준": rent_activity_level,
        "거래활성도": rent_activity_level,
        "최근전세5건": recent_jeonse_trades,
        "최근월세5건": recent_monthly_trades,
        "전월세월별거래량": rent_monthly_volume,
        "전세상승률": jeonse_change_rate,
        "전세신뢰도": jeonse_trend_reliability,
        "전세분석거래건수": len(all_jeonse_trades),
        "전세추세기준": jeonse_trend_basis,
        "최근3개월전세건수": jeonse_recent_3m_count,
        "이전3개월전세건수": jeonse_previous_3m_count,
        "한줄결론": rent_conclusion,
        "AI설명": rent_ai_comment,
    }

    return result

# 최근 n개월
def get_recent_months(n=6):
    months = []
    today = datetime.today()

    for i in range(n):
        month = today.month - i
        year = today.year

        if month <= 0:
            month += 12
            year -= 1

        months.append(f"{year}{month:02d}")

    return months


def parse_price(text: str) -> int:
    return int(text.strip().replace(",", "")) if text and text.strip() else 0

def parse_area(text: str) -> float:
    return round(float(text), 4) if text and text.strip() else 0

def item_to_dict(item) -> dict:
    apt_name = item.findtext("aptNm", "").strip()
    dong = item.findtext("umdNm", "").strip()
    size = item.findtext("excluUseAr", "").strip()
    price = item.findtext("dealAmount", "").strip()
    apt_dong = (
        item.findtext("aptDong", "")
        or item.findtext("aptDongNm", "")
        or ""
    ).strip()
    year = item.findtext("dealYear", "").strip()
    month = item.findtext("dealMonth", "").strip()
    day = item.findtext("dealDay", "").strip()

    floor_text = item.findtext("floor", "").strip()

    try:
        floor = int(floor_text) if floor_text else None
    except:
        floor = None

    return {
        "apt_name": apt_name,
        "dong": dong,
        "apt_dong": apt_dong,
        "size": parse_area(size),
        "price": parse_price(price),
        "date": f"{year}-{int(month):02d}-{int(day):02d}" if year and month and day else "",
        "floor": int(floor) if floor else None
    }



# 🔥 공통 거래 데이터 가져오기 (여기에 추가)
def fetch_trade_items(region: str, months_count: int = 6):
    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return []

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
    months = get_recent_months(months_count)
    all_items = []
    
    num_rows = 500

    for month in months:
        
        cache_key = f"trade_items_{normalize_region(region)}_{month}"

        if cache_key in trade_cache:
            cache_data = trade_cache[cache_key]

            # ✅ 예전 방식 캐시(list)도 안전하게 처리
            if isinstance(cache_data, list):
                all_items.extend(cache_data)
                continue

            # ✅ 새 방식 캐시(dict) 처리
            if time.time() - cache_data["time"] < 3600:
                all_items.extend(cache_data["data"])
                continue

            del trade_cache[cache_key]



        month_items = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            params = {
                "serviceKey": SERVICE_KEY,
                "pageNo": str(page),
                "numOfRows": str(num_rows),
                "LAWD_CD": LAWD_CD,
                "DEAL_YMD": month
            }

            try:
                if DEBUG:
                    print("🔥 분양권 요청 직전")

                res = session.get(url, params=params, timeout=10)

                if DEBUG:
                    print("🔥 분양권 요청 완료")
                    

                if not res.text.strip().startswith("<"):
                    if DEBUG:
                        print("분양권 XML 아님, 건너뜀:", month, res.text[:100])
                    break
               
                root = ET.fromstring(res.content)

            except Exception as e:
                if DEBUG:
                    print("요청/파싱 오류:", e)
                break

            if page == 1:
                total_count_text = root.findtext(".//totalCount", "0").strip()
                total_count = int(total_count_text) if total_count_text.isdigit() else 0
                total_pages = (total_count + num_rows - 1) // num_rows if total_count > 0 else 1

            items = root.findall(".//item")

            if not items:
                break

            # ✅ 여기만 유지 (핵심)
            month_items.extend([item_to_dict(item) for item in items])

            page += 1

        trade_cache[cache_key] = {
            "time": time.time(),
            "data": month_items
        }
        all_items.extend(month_items)

    return all_items

# 🔥 분양권 거래 데이터 가져오기
def fetch_presale_items(region: str, months_count: int = 6):
    
    LAWD_CD = find_lawd_cd(region)

    if not LAWD_CD:
        return []

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"
    
    months = []

    today = datetime.today()

    for i in range(months_count):
        year = today.year
        month = today.month - i

        while month <= 0:
            month += 12
            year -= 1

        months.append(f"{year}{month:02d}")

    all_items = []

    for month in months:
        month_items = []
        cache_key = f"presale_{normalize_region(region)}_{month}"
        if cache_key in trade_cache:
            cache_data = trade_cache[cache_key]

            # ✅ 예전 방식 캐시(list)도 안전 처리
            if isinstance(cache_data, list):
                all_items.extend(cache_data)
                continue

            # ✅ 새 방식 캐시(dict) 처리
            if time.time() - cache_data["time"] < 3600:
                all_items.extend(cache_data["data"])
                continue

            del trade_cache[cache_key]
       
        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": "1",
            "numOfRows": "100",
            "LAWD_CD": LAWD_CD,
            "DEAL_YMD": month
        }

        try:
            if DEBUG:
                print("🔥 분양권 요청 직전")

            res = session.get(url, params=params, timeout=10)

            if DEBUG:
                print("🔥 분양권 요청 완료")
                print("분양권 응답 상태:", res.status_code)

            text = res.text.strip()
            if DEBUG:
                print("분양권 응답 앞부분:", text[:300])

            if not text.startswith("<"):

                if DEBUG:
                    print("분양권 XML 오류:", month)

                continue
            
            try:
                root = ET.fromstring(text)
            except Exception as e:

                if DEBUG:
                    print("XML 파싱 실패:", month, e)

                continue

        except Exception as e:
            if DEBUG:
                print("분양권 요청/파싱 오류:", e)
            continue

        items = root.findall(".//item")
        
        for item in items:

            apt_name = item.findtext("aptNm", "").strip()

            dong = (
                item.findtext("umdNm", "")
                or item.findtext("umdNm1", "")
                or item.findtext("dong", "")
                or item.findtext("legalDong", "")
                or ""
            ).strip()

            if apt_name:

                price = item.findtext("dealAmount", "").replace(",", "").strip()
                size = item.findtext("excluUseAr", "").strip()


                year = item.findtext("dealYear", "").strip()
                month2 = item.findtext("dealMonth", "").strip()
                day = item.findtext("dealDay", "").strip()

                date = ""
                if year and month2 and day:
                    date = f"{year}-{int(month2):02d}-{int(day):02d}"

                month_items.append({
                    "apt_name": apt_name,
                    "dong": dong,
                    "apt_dong": "",
                    "size": int(float(size)) if size else 0,
                    "price": int(price) if price else 0,
                    "date": date,
                    "floor": None,
                    "name": apt_name,
                    "name_norm": normalize(apt_name)
                })

        trade_cache[cache_key] = {
            "time": time.time(),
            "data": month_items
        }
        all_items.extend(month_items)                

    return all_items

def build_internal_size_type_map(size_values, target_size=None):
    """
    같은 명목 평형 안에 존재하는 세부 전용면적을
    TYPE-1, TYPE-2, TYPE-3 형태로 분류한다.

    예:
        84.8765 -> TYPE-1
        84.9116 -> TYPE-2
        84.9521 -> TYPE-3

    주의:
    - TYPE 번호는 실제 건설사의 A/B/C 타입을 의미하지 않는다.
    - 내부 비교대상 구분을 위한 임시 코드다.
    - 전용면적 오름차순으로 번호를 부여해 결과가 항상 일정하게 유지된다.
    """

    normalized_sizes = []

    for value in size_values or []:
        try:
            size_value = round(float(value), 4)
        except (TypeError, ValueError):
            continue

        normalized_sizes.append(size_value)

    if not normalized_sizes:
        return {}

    # 사용자가 선택한 면적과 동일한 명목 평형만 남긴다.
    # 예: target_size가 84.9116이면 84.xxxx 면적만 분류한다.
    if target_size is not None:
        try:
            target_nominal_size = int(float(target_size))

            normalized_sizes = [
                size_value
                for size_value in normalized_sizes
                if int(size_value) == target_nominal_size
            ]
        except (TypeError, ValueError):
            pass

    unique_sizes = sorted(set(normalized_sizes))

    return {
        size_value: f"TYPE-{index}"
        for index, size_value in enumerate(unique_sizes, start=1)
    }

def evaluate_internal_type_reliability(trade_count):
    """
    내부 TYPE의 매매 거래건수를 기준으로
    추천가 계산 신뢰도와 보완 필요 여부를 판정한다.

    기준:
    - 10건 이상: 신뢰도 높음, 동일 TYPE만 사용 가능
    - 3~9건: 신뢰도 보통, 동일 명목 평형 보조 권장
    - 0~2건: 신뢰도 낮음, 동일 명목 평형 보완 필요
    """

    try:
        count = int(trade_count or 0)
    except (TypeError, ValueError):
        count = 0

    if count >= 10:
        return {
            "trade_count": count,
            "reliability": "높음",
            "needs_fallback": False,
            "type_weight": 1.0,
            "nominal_size_weight": 0.0,
            "description": "동일 TYPE 거래만으로 추천가 계산 가능"
        }

    if count >= 3:
        return {
            "trade_count": count,
            "reliability": "보통",
            "needs_fallback": True,
            "type_weight": 0.75,
            "nominal_size_weight": 0.25,
            "description": "동일 TYPE 거래를 우선하고 동일 명목 평형 거래를 일부 보조"
        }

    return {
        "trade_count": count,
        "reliability": "낮음",
        "needs_fallback": True,
        "type_weight": 0.4,
        "nominal_size_weight": 0.6,
        "description": "동일 TYPE 거래가 부족하여 동일 명목 평형 거래 보완 필요"
    }

def calculate_type_quality_score(trade_count):
    """
    내부 TYPE의 거래건수만 기준으로
    0~100점 품질점수를 계산한다.

    현재 단계에서는 최근성, 가격 변동성,
    이상치 비율은 아직 반영하지 않는다.
    """

    try:
        count = int(trade_count or 0)
    except (TypeError, ValueError):
        count = 0

    if count >= 20:
        return 100

    if count >= 15:
        return 90

    if count >= 10:
        return 80

    if count >= 5:
        return 60

    if count >= 3:
        return 45

    if count >= 1:
        return 25

    return 0

def calculate_final_type_score(
    quality_score,
    recent_3m_ratio,
    recent_6m_ratio
):
    """
    TYPE 최종 점수
    거래건수 + 최근성 반영
    """

    score = quality_score

    # 최근 3개월 최대 +15점
    score += min(recent_3m_ratio * 0.3, 15)

    # 최근 6개월 최대 +10점
    score += min(recent_6m_ratio * 0.2, 10)

    return round(min(score, 100), 1)

def select_type_reference_price(
    average_price,
    median_price,
    trimmed_average_price,
    final_type_score
):
    """
    TYPE 대표가격 선택

    - 95점 이상:
      절사평균 70% + 중앙값 30%

    - 80점 이상:
      절사평균 50% + 중앙값 50%

    - 60점 이상:
      절사평균 50% + 평균 50%

    - 60점 미만:
      평균 사용
    """

    try:
        average_price = float(average_price or 0)
        median_price = float(median_price or 0)
        trimmed_average_price = float(
            trimmed_average_price or 0
        )
        final_type_score = float(final_type_score or 0)

    except (TypeError, ValueError):
        return 0

    if final_type_score >= 95:
        reference_price = (
            trimmed_average_price * 0.70
            + median_price * 0.30
        )

    elif final_type_score >= 80:
        reference_price = (
            trimmed_average_price * 0.50
            + median_price * 0.50
        )

    elif final_type_score >= 60:
        reference_price = (
            trimmed_average_price * 0.50
            + average_price * 0.50
        )

    else:
        reference_price = average_price

    return round(reference_price)

def calculate_trimmed_average(sorted_prices):
    """
    거래량에 따라 절사평균 계산
    """

    trade_count = len(sorted_prices)

    if trade_count < 10:
        trim_count = 0

    elif trade_count < 20:
        trim_count = 1

    else:
        trim_count = max(
            1,
            int(trade_count * 0.10)
        )

    if trim_count == 0:
        trimmed_prices = sorted_prices

    else:
        trimmed_prices = sorted_prices[
            trim_count:-trim_count
        ]

        if len(trimmed_prices) == 0:
            trimmed_prices = sorted_prices

    trimmed_average = round(
        sum(trimmed_prices) / len(trimmed_prices)
    )

    return (
        trimmed_average,
        trim_count,
        len(trimmed_prices)
    )

def analyze_recent_market_signal(
    type_reference_price,
    recent_3m_avg,
    recent_3m_count
):
    """
    장기 TYPE 대표가격과 최근 3개월 평균가격의 차이를 측정한다.

    아직 가중치나 미래가격 계산에는 사용하지 않고,
    시장 방향과 표본 신뢰도를 설명하기 위한 검증용 함수다.
    """

    try:
        reference_price = float(type_reference_price or 0)
        recent_price = float(recent_3m_avg or 0)
        trade_count = int(recent_3m_count or 0)

    except (TypeError, ValueError):
        reference_price = 0
        recent_price = 0
        trade_count = 0

    if reference_price <= 0 or recent_price <= 0:
        return {
            "price_gap": 0,
            "premium_rate": 0.0,
            "direction": "판단 불가",
            "sample_level": "부족",
            "description": (
                "최근 거래가격 또는 TYPE 대표가격이 부족해 "
                "최근 시장 흐름을 비교하지 않았습니다."
            )
        }

    price_gap = round(recent_price - reference_price)

    premium_rate = round(
        price_gap / reference_price * 100,
        2
    )

    if premium_rate >= 3:
        direction = "상승 신호"
    elif premium_rate <= -3:
        direction = "하락 신호"
    else:
        direction = "보합 신호"

    if trade_count >= 10:
        sample_level = "충분"
        sample_text = "최근 거래 표본이 비교적 충분합니다."

    elif trade_count >= 5:
        sample_level = "보통"
        sample_text = (
            "최근 시장 방향은 확인되지만 "
            "표본이 아주 많지는 않습니다."
        )

    elif trade_count >= 3:
        sample_level = "주의"
        sample_text = (
            "최근 거래 표본이 적어 "
            "가격 신호를 제한적으로 해석해야 합니다."
        )

    else:
        sample_level = "부족"
        sample_text = (
            "최근 거래 표본이 매우 적어 "
            "최근 평균가격을 그대로 신뢰하기 어렵습니다."
        )

    if premium_rate >= 3:
        price_text = (
            f"최근 3개월 평균가격이 TYPE 대표가격보다 "
            f"{premium_rate}% 높아 상승 신호가 나타났습니다."
        )

    elif premium_rate <= -3:
        price_text = (
            f"최근 3개월 평균가격이 TYPE 대표가격보다 "
            f"{abs(premium_rate)}% 낮아 조정 신호가 나타났습니다."
        )

    else:
        price_text = (
            f"최근 3개월 평균가격과 TYPE 대표가격의 차이는 "
            f"{abs(premium_rate)}%로 보합 범위에 가깝습니다."
        )

    description = (
        f"최근 3개월 동일 TYPE 거래는 {trade_count}건이며, "
        f"{price_text} "
        f"{sample_text} "
        f"따라서 최근 평균가격을 그대로 기준가격으로 사용하기보다 "
        f"TYPE 대표가격과 함께 검토하는 것이 안전합니다."
    )

    return {
        "price_gap": price_gap,
        "premium_rate": premium_rate,
        "direction": direction,
        "sample_level": sample_level,
        "description": description
    }

def build_market_weight_scenarios(
    type_reference_price,
    recent_3m_avg
):
    """
    TYPE 대표가격과 최근 3개월 평균가격을
    여러 가중치 후보로 결합한다.

    현재 단계에서는 최적 가중치를 선택하거나
    실제 미래예측에 적용하지 않는다.
    백테스트용 후보값만 생성한다.
    """

    try:
        type_price = float(type_reference_price or 0)
        recent_price = float(recent_3m_avg or 0)

    except (TypeError, ValueError):
        type_price = 0
        recent_price = 0

    if type_price <= 0:
        return []

    # 최근 3개월 거래가격이 없으면
    # TYPE 대표가격 100%만 반환
    if recent_price <= 0:
        return [
            {
                "recent_weight": 0.0,
                "type_weight": 1.0,
                "candidate_price": round(type_price)
            }
        ]

    scenarios = []

    for recent_weight in [
        0.0,
        0.1,
        0.2,
        0.3,
        0.4,
        0.5,
        0.6
    ]:
        type_weight = 1.0 - recent_weight

        candidate_price = round(
            type_price * type_weight
            + recent_price * recent_weight
        )

        scenarios.append({
            "recent_weight": recent_weight,
            "type_weight": type_weight,
            "candidate_price": candidate_price
        })

    return scenarios

@app.get("/dongs")
def get_dongs(region: str, type: str = "apt"):

    start_time = time.time()
    print("⏱️ /dongs 시작:", region, type)

    parts = region.split()

    if len(parts) >= 2:
        db_region = parts[0]
        db_sigungu = " ".join(parts[1:])

        
        # ✅ 거래 유형별 동 목록 DB 조회
        # apt      : 아파트 매매
        # presale  : 분양권
        # rent     : 전월세
        if type == "presale":
            db_dongs = get_presale_dongs_from_db(db_region, db_sigungu)

        elif type == "rent":
            db_dongs = get_rent_dongs_from_db(db_region, db_sigungu)

        else:
            db_dongs = get_dongs_from_db(db_region, db_sigungu)

        insert_search_log(
            search_type="dong",
            region=db_region,
            sigungu=db_sigungu
        )

        print("⏱️ /dongs 완료:", round(time.time() - start_time, 3), "초", "개수:", len(db_dongs))
        return {
            "동목록": db_dongs,
            "dongs": db_dongs,
            "동리목록": db_dongs
        }
    
    
    return {
        "동목록": [],
        "dongs": [],
        "동리목록": []
    }

    
# 🔥 지역 검색
@app.get("/regions")
def search_region(keyword: str):
    keyword_norm = normalize_region(keyword)

    results = []
    seen = set()

    for name in lawd_map.keys():
        name_norm = normalize_region(name)

        if keyword_norm not in name_norm:
            continue

        # 광역단위 단독 항목 제외
        # 예: "제주특별자치도", "경기도" 같은 것 제외
        parts = name.split()
        if len(parts) < 2 and "세종" not in name:
            continue

        # 중복 비슷한 이름 제거
        if name_norm in seen:
            continue

        seen.add(name_norm)
        results.append(name)

    return {"검색결과": results[:20]}

@app.get("/sigungu")
def get_sigungu(sido: str, type: str = "apt"):

    try:
        print("✅ /sigungu 요청:", sido, type)

        if type == "presale":
            result = get_presale_sigungu_list_from_db(sido)

        elif type == "rent":
            result = get_rent_sigungu_list_from_db(sido)

        else:
            result = get_apt_sigungu_list_from_db(sido)

        print("✅ 시군구 개수:", len(result))
        print("✅ 시군구 목록:", result[:20])

        return {
            "검색결과": result
        }

    except Exception as e:
        print("❌ /sigungu 오류:", e)

        return {
            "검색결과": []
        }

@app.get("/apts")
def search_apts(
    region: str,
    keyword: str = "",
    dong: str = "",
    type: str = "apt"
):

    if dong:
        parts = region.split()

        if len(parts) >= 2:
            db_region = parts[0]
            db_sigungu = " ".join(parts[1:])

            # ✅ 거래 유형별 단지 목록 DB 조회
            # apt      : 아파트 매매
            # presale  : 분양권
            # rent     : 전월세
            if type == "presale":
                db_apts = get_presale_apts_from_db(
                    db_region,
                    db_sigungu,
                    dong
                )

            elif type == "rent":
                db_apts = get_rent_apts_from_db(
                    db_region,
                    db_sigungu,
                    dong
                )

            else:
                db_apts = get_apts_from_db(
                    db_region,
                    db_sigungu,
                    dong
                )

            insert_search_log(
                search_type="apt",
                region=db_region,
                sigungu=db_sigungu,
                dong=dong
            )

            result = []

            for apt_name in db_apts:
                if keyword:
                    if normalize(keyword) not in normalize(apt_name):
                        continue

                result.append({
                    "name": apt_name,
                    "real_name": apt_name,
                    "dong": dong,
                    "name_norm": normalize(apt_name)
                })

            return {"검색결과": result[:300]}

    return {"검색결과": []}

@app.get("/price")
def get_price(region: str, apt_name: str):

    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return {"거래목록": []}

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

    months = get_recent_months(6)
    trades = []

    for month in months:
        page = 1

        while page <= 3:
            params = {
                "serviceKey": SERVICE_KEY,
                "pageNo": str(page),
                "numOfRows": "100",
                "LAWD_CD": LAWD_CD,
                "DEAL_YMD": month
            }

            res = session.get(url, params=params, timeout=10)

            try:
                root = ET.fromstring(res.content)
            except:
                break

            items = root.findall(".//item")
            if not items:
                break

            for item in items:
                name = item.findtext("aptNm", "").strip()

                if is_same_apartment_name(apt_name, name):
                    size = item.findtext("excluUseAr", "").strip()
                    price = item.findtext("dealAmount", "").strip().replace(",", "")
                    year = item.findtext("dealYear", "").strip()
                    month2 = item.findtext("dealMonth", "").strip()
                    day = item.findtext("dealDay", "").strip()

                    if size and price:
                        trades.append({
                            "아파트": name,
                            "전용면적": round(float(size), 4),
                            "거래금액": int(price),
                            "계약일": f"{year}-{int(month2):02d}-{int(day):02d}"
                        })

            page += 1

    return {"거래목록": trades}


@app.get("/avg_price")
def get_avg_price(region: str, apt_name: str, size: int):

    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return {"결과": "지역 오류"}

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

    months = get_recent_months(6)
    prices = []
    floors = []

    for month in months:
        page = 1

        while page <= 3:
            params = {
                "serviceKey": SERVICE_KEY,
                "pageNo": str(page),
                "numOfRows": "100",
                "LAWD_CD": LAWD_CD,
                "DEAL_YMD": month
            }

            res = session.get(url, params=params, timeout=10)

            try:
                root = ET.fromstring(res.content)
            except:
                break

            items = root.findall(".//item")
            if not items:
                break

            for item in items:
                name = item.findtext("aptNm", "").strip()

                if is_same_apartment_name(apt_name, name):
                    s = item.findtext("excluUseAr", "")
                    p = item.findtext("dealAmount", "").replace(",", "")

                    if s and p and int(float(s)) == size:
                        prices.append(int(p))

            page += 1

    if not prices:
        return {"결과": "데이터 없음"}

    avg_price = sum(prices) // len(prices)

    return {
        "아파트": apt_name,
        "평형": size,
        "평균가": avg_price,
        "거래수": len(prices)
    }


@app.get("/analyze_price")
def analyze_price(
    region: str, 
    apt_name: str, 
    size: float, 
    user_price: int | None = None,
    direction: str | None = None,
    floor_level: str | None = None,
    interior: str | None = None,
    type: str = "apt"
):
    # =========================================================
    # ✅ analyze_price 디버그 로그
    # =========================================================

    if DEBUG_PRICE_ENGINE:
        print("★★★★ analyze_price 시작 ★★★★")

        print(
            f"type=[{type}], region=[{region}], "
            f"apt_name=[{apt_name}], size=[{size}]",
            flush=True
        )
    
    def presale_fallback():
        fallback_price = user_price or 0

        return {
            "아파트": apt_name,
            "평형": size,
            "추천가": fallback_price,
            "보정추천가": fallback_price,
            "추천매수가": fallback_price,
            "평균가": fallback_price,
            "최고가": 0,
            "최저가": 0,
            "거래수": 0,
            "거래건수": 0,
            "최근거래5건": [
                {
                    "date": "분양권 참고",
                    "price": fallback_price,
                    "floor": None,
                    "apt_dong": "",
                    "size": size
                }
            ],
            "한줄결론": "분양권 실거래 데이터가 부족해 입력한 총 매입가 기준으로 참고 분석을 제공합니다.",
            "AI설명": "분양권 거래 데이터가 부족하여 예상 총 매입가를 기준으로 참고 분석했습니다.",
            "가격판단": "참고",
            "최근3개월거래건수": 0,
            "거래활발도": "-",
            "추세": "데이터 부족",
            "추세신뢰도": "낮음",
            "상승률(%)": 0,
            "상승률텍스트": "0%",
            "추세해석": "분양권 실거래 데이터가 부족하여 추세 판단이 제한적입니다.",
            "실거래가기준안내": "분양권은 거래량이 적어 분양가, 프리미엄, 옵션비를 포함한 총 매입가 기준으로 참고 판단해야 합니다.",
            "참고": "분양권 분석은 실거래 데이터 부족 시 입력가 기준 참고 분석으로 표시됩니다."
        }
        


           
    cache_key = f"{type}_{normalize_region(region)}_{normalize(apt_name)}_{round(float(size), 4)}_{direction or 'none'}_{floor_level or 'none'}_{interior or 'none'}_{user_price or 'none'}"

    if cache_key in analysis_cache:
        cached = analysis_cache[cache_key]

        if isinstance(cached, dict) and "time" in cached and "data" in cached:
            if time.time() - cached["time"] < 21600:
                cached_data = cached.get("data")
                if isinstance(cached_data, dict) and cached_data:
                    print("🔥 메모리 분석 캐시 사용")
                    return cached_data

            del analysis_cache[cache_key]

        else:
            del analysis_cache[cache_key]

    # ✅ Supabase 분석 결과 캐시 확인
    # 같은 단지/면적/입력가 조건으로 1시간 이내 분석한 결과가 있으면
    # 거래 데이터를 다시 계산하지 않고 즉시 반환한다.
    db_cached_result = get_analysis_cache_from_db(cache_key)

    if isinstance(db_cached_result, dict) and db_cached_result:
        print("🔥 DB 분석 캐시 사용")
        return db_cached_result
    
    # ✅ 전월세는 전용 분석 엔진으로 처리
    if type == "rent":
        print(
            
            f"region={region}, apt_name={apt_name}, size={size}"
        )

        result = analyze_rent_engine(
            region,
            apt_name,
            size
        )

        if len(analysis_cache) >= MAX_ANALYSIS_CACHE:
            analysis_cache.pop(next(iter(analysis_cache)))

        analysis_cache[cache_key] = {
            "time": time.time(),
            "data": result
        }

        save_analysis_cache_to_db(cache_key, result)

        return result
    
    # ✅ 지역 처리 시스템 V3
    # 행정구역 변경/별칭 지역을 DB 기준 지역명으로 보정
    region = normalize_region_for_db(region)
        
    LAWD_CD = find_lawd_cd(region)
    
    if not LAWD_CD:
        return {"결과": "지역 오류"}

    apt_name_norm = normalize(apt_name)

    trades = []

    if type == "rent":
        if DEBUG_PRICE_ENGINE:
            print(
                f"🔥 전월세 분석 분기 진입: "
                f"apt_name={apt_name}, size={size}"
            )

        db_rows = get_rent_trades(apt_name, size)
        items = rent_rows_to_items(db_rows)

    elif type == "presale":
        
        db_rows = get_presale_trades(region, apt_name, size)
        items = db_rows_to_items(db_rows)

    else:
        # ✅ 사용자가 선택한 전용면적으로 매매 거래 조회
        db_rows = get_apt_sale_trades(
            apt_name,
            size,
            region=region
        )
        items = db_rows_to_items(db_rows)
    if not items:
        items = []
    trades = items
    
    if not trades:
        
        if type == "presale":
            result = presale_fallback()
            if len(analysis_cache) >= MAX_ANALYSIS_CACHE:
                analysis_cache.pop(next(iter(analysis_cache)))
            analysis_cache[cache_key] = result

            save_analysis_cache_to_db(cache_key, result)
            return result
        

        return {"결과": "데이터 없음"}
    

    # 최신순 정렬
    if DEBUG_PRICE_ENGINE:
        print(
            "🔥 analyze_price 거래조회 결과:",
            len(trades),
            region,
            apt_name,
            size
        )
    trades.sort(key=lambda x: x["date"], reverse=True)


    if DEBUG_PRICE_ENGINE:
        print("필터 전 =", len(trades))

    trades = [
        t for t in trades
        if is_same_size(size, t.get("size"))
    ]

    if DEBUG_PRICE_ENGINE:
        print("필터 후 =", len(trades))
        print("첫 거래 =", trades[0] if trades else "없음")

    # 🔥 최근 12개월 월별 거래량
   
    # ✅ 12개월 그래프의 최근 3개 월 합계와 기준 통일
    
    
    recent_prices = []
    past_prices = []


    # 🔥 최근 12개월 월별 거래량
    today = datetime.today()
    monthly_volume_map = {}

    for i in range(12):
        target_month = today.month - i
        target_year = today.year

        while target_month <= 0:
            target_month += 12
            target_year -= 1

        month_key = f"{target_year}-{target_month:02d}"
        monthly_volume_map[month_key] = 0

    for t in trades:
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
            month_key = d.strftime("%Y-%m")

            if month_key in monthly_volume_map:
                monthly_volume_map[month_key] += 1

        except:
            continue

    monthly_volume = [
        {
            "month": key,
            "count": monthly_volume_map[key]
        }
        for key in sorted(monthly_volume_map.keys())
    ]

    recent_3m_count = sum(
        int(v.get("count", 0))
        for v in monthly_volume[-3:]
    )

    # ✅ 최근 12개월 실제 거래건수
    recent_12m_count = sum(
        int(v.get("count", 0))
        for v in monthly_volume
    )

    # =========================================================
    # ✅ 최근 12개월 거래량 기준 데이터 신뢰도
    # =========================================================

    if recent_12m_count >= 3:
        data_reliability = "정상"

    elif recent_12m_count >= 1:
        data_reliability = "거래 부족"

    else:
        data_reliability = "분석 보류"

    if DEBUG_PRICE_ENGINE:
        print(
            f"🔍 데이터 신뢰도 = {data_reliability}"
        )

    if DEBUG_PRICE_ENGINE:
        print(
            f"🔍 최근 12개월 거래건수 = {recent_12m_count}건"
        )

    # =========================================================
    # ✅ 최근 12개월 거래가 없는 경우
    # 오래된 거래로 현재 추천 매수가를 산정하지 않는다.
    # =========================================================

    if recent_12m_count <= 0:

        last_trade = trades[0] if trades else None

        last_trade_price = (
            int(last_trade.get("price", 0))
            if last_trade
            else 0
        )

        last_trade_date = (
            last_trade.get("date", "")
            if last_trade
            else ""
        )

        result = {
            "아파트": apt_name,
            "평형": size,

            "거래건수": 0,
            "최근12개월거래건수": 0,
            "최근3개월거래건수": 0,

            "추천가": 0,
            "추천매수가": 0,
            "보정추천가": 0,

            "평균가": 0,
            "가중평균가격": 0,
            "최고가": 0,
            "최저가": 0,

            "최근12개월거래건수": recent_12m_count,
            "데이터신뢰도": data_reliability,

            "추세": "데이터 부족",
            "추세신뢰도": "낮음",
            "상승률(%)": 0,
            "상승률텍스트": "0%",

            "거래활발도": "(비활성)",

            "마지막거래가": last_trade_price,
            "마지막거래일": last_trade_date,

            "최근거래5건": (
                trades[:5]
                if trades
                else []
            ),

            "거래량월별": monthly_volume,

            "가격판단": "분석 보류",

            "한줄결론":
                "최근 12개월 실거래가 없어 현재 추천 매수가 산정을 보류합니다.",

            "AI설명":
                "최근 12개월 내 동일 평형의 실거래가 확인되지 않아 "
                "과거 거래가격만으로 현재 적정가격을 판단하기 어렵습니다.",

            "추세해석":
                "최근 거래가 없어 현재 시장의 가격 방향성을 신뢰성 있게 판단하기 어렵습니다.",

            "실거래가기준안내":
                "최근 12개월 동일 평형 실거래가 없는 경우 추천 매수가는 제공하지 않습니다.",

            "참고":
                (
                    f"마지막 확인 거래는 {last_trade_date}, "
                    f"{last_trade_price:,}만원입니다."
                    if last_trade_price > 0
                    else "최근 확인 가능한 거래도 없습니다."
                )
        }

        return result

    # ==========================================
    # 최근 3개월 평균가격 계산
    # ==========================================

    three_months_ago = datetime.today() - timedelta(days=90)

    recent_3m_prices = []

    for t in trades:

        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")

            if d >= three_months_ago and t.get("price"):
                recent_3m_prices.append(t["price"])

        except:
            continue

    if recent_3m_prices:
        recent_3m_avg = round(
            sum(recent_3m_prices) / len(recent_3m_prices)
        )
    else:
        recent_3m_avg = 0

    # 최근 3개월 가격 안정성 계산
    if len(recent_3m_prices) >= 2:

        recent_3m_std = round(
            statistics.stdev(recent_3m_prices)
        )

        variation_rate = round(
            recent_3m_std / recent_3m_avg * 100,
            2
        )

    else:

        recent_3m_std = 0
        variation_rate = 0
    if DEBUG_PRICE_ENGINE:
        print(
            f"🔍 최근가격목록={recent_3m_prices} "
            f"/ 개수={len(recent_3m_prices)}"
        )

    # 🔥 최근 1년 거래만 표시
    one_year_ago = datetime.today() - timedelta(days=365)

    recent_trades = []

    for t in trades:

        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")

            if d >= one_year_ago:
                recent_trades.append(t)

        except:
            continue

    # 최근 1년 거래 중 최신 5건만 표시
    recent_trades = recent_trades[:5]

    recent_price = user_price or 0

    for t in recent_trades:
        if t.get("price"):
            recent_price = t["price"]
            break

    # 🔥 평균가 개선: 최근 거래 중심
    prices = [t["price"] for t in trades if t.get("price")]

    if not prices:
        result = presale_fallback()
        analysis_cache[cache_key] = {
            "time": time.time(),
            "data": result
        }
        # ✅ Supabase 분석 결과 캐시 저장
        save_analysis_cache_to_db(cache_key, result)
        return result

    recent_5_prices = [t["price"] for t in recent_trades if t.get("price")]

    if len(recent_5_prices) >= 3:
        avg_price = round(sum(recent_5_prices) / len(recent_5_prices))
    elif prices:
        avg_price = round(sum(prices) / len(prices))
    else:
        avg_price = 0

    # 최근 3개월 거래 건수 / 활발도
    # ✅ 거래량 그래프의 최근 3개월 합계와 동일 기준
    recent_3m_count = sum(
        int(v.get("count", 0))
        for v in monthly_volume[-3:]
    )

    if recent_3m_count >= 6:
        trade_activity = "(거래 활발)"
    elif recent_3m_count >= 3:
        trade_activity = "(거래 보통)"
    else:
        trade_activity = "(비활성)"

    prices = [t["price"] for t in trades]

    # 1. 평균가격
    prices = [t["price"] for t in trades if t.get("price")]

    recent_5_prices = [
        t["price"]
        for t in recent_trades
        if t.get("price")
    ]

    if len(recent_5_prices) >= 3:

        avg_price = round(
            sum(recent_5_prices) / len(recent_5_prices)
        )

    elif prices:

        avg_price = round(
            sum(prices) / len(prices)
        )

    else:

        avg_price = 0

    # 2. 가중평균가격 (최신 거래일수록 가중치 높게)
    weighted_sum = 0
    weight_total = 0
    n = len(prices)

    for i, price in enumerate(prices):
        weight = n - i
        weighted_sum += price * weight
        weight_total += weight

    weighted_avg_price = round(weighted_sum / weight_total)

    # 3. 최고가 / 최저가
    high_price = max(prices)
    low_price = min(prices)

    # 4. 추세 / 상승률 (개선 버전)

    total_trade_count = len(trades)

    trend = "보류"
    change_rate = 0
    change_rate_text = "0%"
    trend_confidence = "참고"
    trend_comment = "거래 데이터가 충분하지 않아 추세 판단이 제한적입니다."
    ai_comment = "최근 거래 흐름을 기반으로 참고 수준의 분석입니다."

    recent_prices = []
    past_prices = []

    today = datetime.today()
    three_months_ago = today - timedelta(days=90)
    six_months_ago = today - timedelta(days=180)

    # 🔥 거래 분리
    for t in trades:
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
        except:
            continue

        if d >= three_months_ago:
            recent_prices.append(t["price"])

        elif d < three_months_ago:
            past_prices.append(t["price"])

    # 🔥 최근 거래 기준 강화
    if len(recent_prices) >= 2 and len(past_prices) >= 1:

        recent_med = statistics.median(recent_prices)
        past_med = statistics.median(past_prices)

        # 🔥 실제 계산
        raw_change_rate = round(
            ((recent_med - past_med) / past_med) * 100,
            2
        )

        # 🔥 화면 표시용 제한
        display_change_rate = max(
            min(raw_change_rate, 25),
            -25
        )

        change_rate = display_change_rate

        # 🔥 화면 표시 텍스트
        if raw_change_rate >= 25:
            change_rate_text = "+25%"

        elif raw_change_rate <= -25:
            change_rate_text = "-25%"

        else:
            change_rate_text = f"{change_rate}%"

        # 🔥 추세 판단
        if raw_change_rate >= 15:
            trend = "강한 상승"

        elif raw_change_rate >= 8:
            trend = "상승 우세"

        elif raw_change_rate >= 2:
            trend = "약상승"

        elif raw_change_rate <= -15:
            trend = "강한 하락"

        elif raw_change_rate <= -8:
            trend = "하락 우세"

        elif raw_change_rate <= -2:
            trend = "약하락"

        else:
            trend = "보합"

        # 🔥 신뢰도
        if len(recent_prices) >= 5:
            trend_confidence = "높음"

        elif len(recent_prices) >= 3:
            trend_confidence = "보통"

        else:
            trend_confidence = "참고"

        trend_comment = "최근 거래 흐름과 과거 거래 흐름을 비교하여 시장 추세를 분석했습니다."

    # 🔥 거래 부족
    else:
        # ✅ 아파트: 최근 거래는 충분한데 과거 비교 데이터가 없는 경우
        if type == "apt" and len(recent_prices) >= 6 and len(past_prices) == 0:
            trend = "거래 회복"
            change_rate = 0
            change_rate_text = "회복 초기"
            trend_confidence = "보통"

            trend_comment = (
                "최근 3개월 거래가 집중적으로 발생했습니다. "
                "이전 기간 거래가 부족해 상승률 계산은 제한적이지만, "
                "거래 분위기는 회복된 상태로 볼 수 있습니다."
            )

            ai_comment = (
                "최근 3개월 거래량이 크게 늘어난 단지입니다. "
                "가격 상승률은 과거 비교 거래가 부족해 계산이 어렵지만, "
                "시장 관심도와 거래 회복 흐름은 확인됩니다."
            )

         # ✅ 분양권: 최근 거래는 충분한데 과거 비교 데이터가 없는 경우
        elif type == "presale" and len(recent_prices) >= 6 and len(past_prices) == 0:
            trend = "거래 회복"
            change_rate = 0
            change_rate_text = "회복 초기"
            trend_confidence = "보통"

            trend_comment = (
                "최근 3개월 거래는 활발하지만, 이전 기간 비교 데이터가 부족해 상승률 계산은 제한적입니다."
            )

            ai_comment = (
                "최근 3개월 거래가 활발하게 발생하고 있어 시장 관심도는 높은 편입니다. "
                "다만 이전 기간 거래 데이터가 부족하여 상승 또는 하락 추세를 강하게 단정하기보다는, "
                "최근 거래가 집중된 분양권으로 참고 판단하는 것이 적절합니다."
            )

        elif type == "presale" and len(recent_prices) >= 3 and len(past_prices) == 0:
            trend = "거래 보통"
            change_rate = 0
            change_rate_text = "비교 기준 부족"
            trend_confidence = "참고"

            trend_comment = (
                "최근 거래는 확인되지만, 이전 기간 비교 데이터가 부족해 방향성 판단은 제한적입니다."
            )

            ai_comment = (
                "최근 거래는 확인되지만 이전 기간과 비교할 거래 데이터가 부족합니다. "
                "따라서 상승·하락보다는 최근 거래 형성 가격과 매물 호가를 함께 보는 것이 좋습니다."
            )

        else:
            trend_confidence = "낮음"

            trend_comment = (
                "최근 거래 건수가 부족하여 시장 흐름을 명확히 판단하기 어려운 상태입니다."
            )

            ai_comment = (
                "거래 데이터가 충분하지 않아 강한 추세를 판단하기는 어렵습니다. "
                "현재 분석 결과는 참고 수준으로 활용하는 것이 좋으며, "
                "최근 호가와 인근 단지 거래 흐름을 함께 확인하는 것이 안전합니다."
            )

    # 🔥 AI 설명 생성
    if "상승" in trend:

        ai_comment = (
            "최근 거래 흐름은 상승세를 보이고 있습니다. "
            "실거래 기준 가격이 이전 거래 대비 높아지는 흐름이 확인되며, "
            "매수 수요가 유지되는 구간으로 해석됩니다. "
            "다만 단기 급등 이후에는 일시적인 조정 가능성도 함께 고려할 필요가 있습니다."
        )

    elif "하락" in trend:

        ai_comment = (
            "최근 거래 가격은 하락 흐름을 보이며 매수세가 다소 약해진 구간입니다. "
            "가격 조정이 진행 중인 단계로, 추가 하락 가능성도 고려할 필요가 있습니다. "
            "급매 위주 거래 여부를 함께 확인하는 것이 중요합니다."
        )

    elif trend == "보합":

        ai_comment = (
            "최근 거래는 큰 변동 없이 보합 흐름을 유지하고 있습니다. "
            "가격 방향성이 뚜렷하지 않은 구간으로, 시장 관망세가 이어지고 있는 상태입니다. "
            "추세가 명확해질 때까지 거래 흐름을 조금 더 지켜보는 전략도 유효합니다."
        )


    # 5. 추천 매수가
    # 🔥 추천 매수가 개선: 최근 거래 중심

    recent_5_prices = [t["price"] for t in recent_trades if t.get("price")]

    if len(recent_5_prices) >= 3:
        base_price = round(statistics.median(recent_5_prices))
    else:
        base_price = weighted_avg_price

    # 추세별 할인율
    if "하락" in trend:
        recommended_buy_price = round(base_price * 0.96)

    elif "상승" in trend:
        recommended_buy_price = round(base_price * 0.99)

    elif trend == "보합":
        recommended_buy_price = round(base_price * 0.98)

    else:
        recommended_buy_price = round(base_price * 0.97)
    
    # 5-1. 조건 보정 비율
    adjustment_rate = 0.0

    # 방향 보정
    if direction == "남향":
        adjustment_rate += 0.02
    elif direction == "북향":
        adjustment_rate -= 0.02

    # 층 보정
    if floor_level == "고층":
        adjustment_rate += 0.02
    elif floor_level == "저층":
        adjustment_rate -= 0.02

    # 내부 상태 보정
    if interior == "좋음":
        adjustment_rate += 0.03
    elif interior == "나쁨":
        adjustment_rate -= 0.03

    adjusted_buy_price = round(recommended_buy_price * (1 + adjustment_rate))

    # 🔥 보정 추천가 현실성 제한
    # 최근 거래 중앙값 기준으로 과도한 상승 제한

    recent_5_prices = [
        t["price"]
        for t in recent_trades
        if t.get("price")
    ]

    if recent_5_prices:

        recent_median_price = statistics.median(
            recent_5_prices
        )

        # 상승장
        if "상승" in trend:
            max_adjusted_price = round(
                recent_median_price * 1.02
            )

        # 보합
        elif trend == "보합":
            max_adjusted_price = round(
                recent_median_price * 1.01
            )

        # 하락장
        elif "하락" in trend:
            max_adjusted_price = round(
                recent_median_price * 1.00
            )

        # 기타
        else:
            max_adjusted_price = round(
                recent_median_price * 1.01
            )

        # 상한 제한
        if adjusted_buy_price > max_adjusted_price:
            adjusted_buy_price = max_adjusted_price

    # 6. 가격 판단
    if user_price is not None:
        ratio = user_price / adjusted_buy_price

        # 🔥 기본 판단
        if ratio > 1.05:
            judgment = "고평가"
        elif ratio < 0.95:
            judgment = "저평가"
        else:
            judgment = "적정"

        # 🔥 한줄결론 업그레이드
        if judgment == "고평가":
            if trend == "상승":
                conclusion = "상승 흐름이지만 현재 가격은 상단 구간으로, 급매가 아니라면 신중 접근이 필요합니다."
            elif trend == "하락":
                conclusion = "하락 흐름에서 높은 가격으로, 추가 하락 가능성을 고려한 접근이 필요합니다."
            else:
                conclusion = "최근 거래 기준 상단 가격대로, 급매 여부 확인 후 접근하는 것이 좋습니다."

        elif judgment == "저평가":
            if trade_activity == "활발":
                conclusion = "거래가 활발한 가운데 가격이 낮은 편으로, 경쟁 매수 가능성이 있는 구간입니다."
            else:
                conclusion = "가격 메리트가 있는 구간으로, 조건이 괜찮다면 매수 검토 가치가 있습니다."

        else:  # 적정
            if trend == "상승":
                conclusion = "상승 흐름 내 적정 가격으로, 실거주 목적 접근이 가능한 구간입니다."
            elif trend == "하락":
                conclusion = "하락 흐름 내 적정 가격으로, 추가 조정 가능성은 열어두는 것이 좋습니다."
            else:
                conclusion = "최근 거래 기준 안정적인 가격 구간입니다."

    else:
        judgment = "판단보류"
        conclusion = "사용자 입력 가격이 없어 시세 기준 정보만 제공합니다."

    if "ai_comment" not in locals():
        ai_comment = "최근 거래 흐름을 기준으로 가격 흐름과 매수 판단을 종합적으로 분석했습니다."

    if "trend_confidence" not in locals():
        trend_confidence = "보통"

    if "trend_comment" not in locals():
        trend_comment = "최근 거래 데이터를 기준으로 시장 흐름을 산출했습니다."

    # 🔥 거래 활성도 점수 계산
    annual_count = sum(int(v.get("count", 0)) for v in monthly_volume)
    recent_count = recent_3m_count

    if annual_count > 0:
        activity_score = round((recent_count / annual_count) * 100)
    else:
        activity_score = 0

    activity_score = min(activity_score, 100)

    if DEBUG_PRICE_ENGINE:
        print("★★★★ recent_trades =", recent_trades)

    result = {
        "아파트": apt_name,
        "평형": size,
        "거래건수": sum(int(v.get("count", 0)) for v in monthly_volume),
        "최근12개월거래건수": recent_12m_count,
        "데이터신뢰도": data_reliability,
        "평균가격": avg_price,
        "가중평균가격": weighted_avg_price,
        "최고가": high_price,
        "최저가": low_price,
        "추세": trend,
        "상승률(%)": change_rate,
        "상승률텍스트": change_rate_text,
        "활성도": activity_score,
        "추세신뢰도": trend_confidence,
        "추세해석": trend_comment,
        "AI설명": ai_comment,
        "추천매수가": recommended_buy_price,
        "보정추천가": adjusted_buy_price,
        "최근거래5건": recent_trades,
        "거래량월별": monthly_volume,
        "최근3개월거래건수": recent_3m_count,
        "거래활발도": trade_activity,
        "사용자입력가격": user_price,
        "가격판단": judgment,
        "한줄결론": conclusion,
        "실거래가기준안내": "이 앱의 분석은 국토교통부 실거래가 공개시스템에 등록된 실거래 신고 자료를 기준으로 합니다.",
        "참고": "동·층·향·내부상태·급매 여부는 반영되지 않은 참고용 분석입니다."
    }

    insert_analysis_log(
            apt_name=apt_name,
            size=str(size),
            user_price=user_price,
            ai_price=adjusted_buy_price,
            result=judgment
        )

    if type == "presale" or result.get("거래건수", 0) >= 3:
        if len(analysis_cache) >= MAX_ANALYSIS_CACHE:
            analysis_cache.pop(next(iter(analysis_cache)))
        analysis_cache[cache_key] = {
            "time": time.time(),
            "data": result
        }
        # ✅ Supabase 분석 결과 캐시 저장
        save_analysis_cache_to_db(cache_key, result)

    
    print(
        "🔥 최종 result 신뢰도 확인:",
        result.get("최근12개월거래건수"),
        result.get("데이터신뢰도")
    )
    
    return result


# 🔥 미래 예측 조회

@app.get("/future_prediction")
def future_prediction(
    region: str,
    apt_name: str,
    size: str = "",
    reference_date: str | None = None
):
    if DEBUG_FUTURE_ENGINE:
        print("★★★★★ future_prediction 실행 ★★★★★")
    region = normalize_region_for_db(region)

    # 백테스트 기준일
    # 값이 없으면 기존과 동일하게 오늘을 사용한다.
    if reference_date:
        try:
            analysis_date = datetime.strptime(
                reference_date,
                "%Y-%m-%d"
            )
        except ValueError:
            return {
                "오류": "reference_date는 YYYY-MM-DD 형식이어야 합니다."
            }
    else:
        analysis_date = datetime.today()

    if DEBUG_FUTURE_ENGINE:
        print(
            f"분석 기준일: "
            f"{analysis_date.strftime('%Y-%m-%d')}"
        )
    
    try:
        parts = region.split()

        if len(parts) >= 2:
            db_region = parts[0]
            db_sigungu = " ".join(parts[1:])
        else:
            db_region = region
            db_sigungu = ""

        sizes = get_sizes_from_db(
            db_region,
            db_sigungu,
            apt_name
        )

        # ✅ 입력한 면적과 같은 정수부를 가진 DB 면적 찾기
        input_size = str(size).replace("㎡", "").strip()
        matched_size = None

        for s in sizes:
            if str(s).strip() == input_size:
                matched_size = s
                break
        
        if matched_size is None:
            return {
                "지역": region,
                "동": "",
                "단지명": apt_name,
                "면적": size,
                "결과": "면적 매칭 실패",
                "DB면적목록": sizes,
                "AI총평": "입력한 면적과 일치하는 DB 면적을 찾지 못했습니다."
                
            }

        # -------------------------------------------------
        # 같은 단지의 전체 전용면적을 내부 TYPE으로 분류
        # -------------------------------------------------

        size_type_map = build_internal_size_type_map(
            sizes,
            target_size=matched_size
        )

        try:
            normalized_matched_size = round(float(matched_size), 4)
        except (TypeError, ValueError):
            normalized_matched_size = None

        current_size_type = (
            size_type_map.get(normalized_matched_size)
            if normalized_matched_size is not None
            else None
        )

        current_type_trade_count = 0
        current_type_reliability = evaluate_internal_type_reliability(0)

        # 현재 선택된 내부 TYPE의 검증 결과
        current_type_average_price = 0
        current_type_trimmed_average_price = 0
        current_type_median_price = 0
        current_type_reference_price = 0
        current_type_final_score = 0.0
        current_type_recent_3m_ratio = 0.0
        current_type_recent_6m_ratio = 0.0

        current_type_recent_3m_avg = 0
        current_type_recent_3m_count = 0

        current_type_recent_3m_std = 0
        current_type_variation_rate = 0

        current_type_observed_min_floor = 0
        current_type_observed_max_floor = 0
        current_type_median_floor = 0
        current_type_unique_floor_count = 0
        current_type_floor_count = 0

        current_type_floor_distribution = {
            "count": 0,
            "q1": 0,
            "median": 0,
            "q3": 0,
            "low_count": 0,
            "middle_count": 0,
            "high_count": 0,
            "description": ""
        }
        current_type_floor_analysis_method = {
            "method": "층 분석 제외",
            "level": "매우 낮음",
            "description": ""
        }
        current_type_floor_regression = {
            "available": False
        }

        current_type_recent_3m_prices = []

        if DEBUG_FUTURE_ENGINE:
            print("\n========== 내부 평형 TYPE 분석 ==========")
            print(f"단지명: {apt_name}")
            print(f"전체 면적 목록: {sizes}")
            print(f"동일 명목 평형 TYPE: {size_type_map}")
            print(f"현재 매칭 면적: {matched_size}")
            print(f"현재 내부 TYPE: {current_size_type}")
            print("========================================\n")

        # -------------------------------------------------
        # 내부 TYPE별 실제 매매가격 검증 로그
        # 추천가 계산에는 아직 사용하지 않음
        # -------------------------------------------------
        if DEBUG_FUTURE_ENGINE:
            print("\n========== 내부 TYPE별 매매가격 검증 ==========")

        for type_size, type_name in size_type_map.items():
            try:
                
                if reference_date:
                    type_items = get_backtest_sale_trades(
                        region=region,
                        apt_name=apt_name,
                        size=type_size
                    )
                else:
                    type_db_rows = get_apt_sale_trades(
                        apt_name,
                        type_size,
                        region=region
                    )

                    type_items = db_rows_to_items(
                        type_db_rows
                    )

                

                # ✅ 백테스트 기준일 이후 거래 제외
                filtered_type_items = []

                for type_item in type_items:
                    try:
                        trade_date = datetime.strptime(
                            str(type_item.get("date", "")),
                            "%Y-%m-%d"
                        )

                        if trade_date <= analysis_date:
                            filtered_type_items.append(type_item)

                    except (ValueError, TypeError):
                        continue

                type_items = filtered_type_items

                if DEBUG_FUTURE_ENGINE:
                    print(
                        f"{type_name} 기준일 필터 후 거래수: "
                        f"{len(type_items)}건"
                    )

                type_prices = []

                # 최근 3개월 거래가격 저장
                recent_3m_prices = []

                # 동일 TYPE에서 실제 거래가 확인된 층 목록
                type_floors = []
                low_floor_prices = []
                middle_floor_prices = []
                high_floor_prices = []
                type_floor_price_pairs = []

                recent_3m_count = 0
                recent_6m_count = 0
                today = analysis_date.date()

                for type_item in type_items:
                    raw_price = type_item.get("price")

                    if raw_price is None:
                        continue

                    try:
                        if isinstance(raw_price, str):
                            raw_price = raw_price.replace(",", "").strip()

                        price_value = int(float(raw_price))

                        if price_value > 0:
                            type_prices.append(price_value)

                        # 거래층 저장
                        raw_floor = type_item.get("floor")

                        try:
                            if raw_floor is not None:
                                floor_value = int(float(raw_floor))

                                # 지하층·비정상값은 이번 분석에서 제외
                                if floor_value > 0:
                                    type_floors.append(floor_value)

                                    if floor_value > 0 and price_value > 0:
                                        type_floor_price_pairs.append({
                                            "floor": floor_value,
                                            "price": price_value
                                        })

                        except (TypeError, ValueError):
                            pass

                        # 최근 거래 비중 계산
                        trade_date = type_item.get("date")

                        try:
                            trade_date = datetime.strptime(
                                str(trade_date),
                                "%Y-%m-%d"
                            ).date()

                            days = (today - trade_date).days

                            if 0 <= days <= 90:
                                recent_3m_count += 1
                                recent_3m_prices.append(price_value)
                                if (
                                    type_name == current_size_type
                                    and 0 <= days <= 90
                                ):
                                    if DEBUG_FUTURE_ENGINE:
                                        print(
                                            "🔍 최근 TYPE 거래:",
                                            f"날짜={trade_date}",
                                            f"가격={price_value:,}만원",
                                            f"층={type_item.get('floor')}"
                                        )

                            if 0 <= days <= 180:
                                recent_6m_count += 1

                        except Exception:
                            pass

                    except (TypeError, ValueError):
                        continue

                if type_prices:
                    sorted_prices = sorted(type_prices)
                    trade_count = len(sorted_prices)
                    average_price = round(sum(sorted_prices) / trade_count)

                    reliability = evaluate_internal_type_reliability(trade_count)

                    quality_score = calculate_type_quality_score(trade_count)

                    # 최근 거래 비율을 먼저 계산
                    if trade_count > 0:
                        recent_3m_ratio = round(
                            recent_3m_count / trade_count * 100,
                            1
                        )

                        recent_6m_ratio = round(
                            recent_6m_count / trade_count * 100,
                            1
                        )
                    else:
                        recent_3m_ratio = 0.0
                        recent_6m_ratio = 0.0

                    # 최근 3개월 실제 거래가격 평균
                    if recent_3m_prices:
                        recent_3m_avg = round(
                            sum(recent_3m_prices) / len(recent_3m_prices)
                        )
                    else:
                        recent_3m_avg = 0

                    # ✅ 최근 3개월 거래가격 변동성 계산
                    if len(recent_3m_prices) >= 2:
                        recent_3m_std = round(
                            statistics.pstdev(recent_3m_prices)
                        )

                        if recent_3m_avg > 0:
                            variation_rate = round(
                                recent_3m_std / recent_3m_avg * 100,
                                2
                            )
                        else:
                            variation_rate = 0.0

                    else:
                        recent_3m_std = 0
                        variation_rate = 0.0

                    if DEBUG_FUTURE_ENGINE:
                        print(
                            f"🔍 {type_name} 최근3개월가격={recent_3m_prices} | "
                            f"건수={len(recent_3m_prices)} | "
                            f"평균={recent_3m_avg} | "
                            f"표준편차={recent_3m_std} | "
                            f"변동률={variation_rate}%"
                        )

                    # 비율 계산이 끝난 뒤 최종점수 계산
                    final_type_score = calculate_final_type_score(
                        quality_score,
                        recent_3m_ratio,
                        recent_6m_ratio
                    )        

                    if type_name == current_size_type:
                        current_type_trade_count = trade_count
                        current_type_reliability = reliability

                    middle_index = trade_count // 2

                    if trade_count % 2 == 1:
                        median_price = sorted_prices[middle_index]
                    else:
                        median_price = round(
                            (
                                sorted_prices[middle_index - 1]
                                + sorted_prices[middle_index]
                            ) / 2
                        )

                    minimum_price = sorted_prices[0]
                    maximum_price = sorted_prices[-1]

                    # 동일 TYPE 거래의 관측 층 통계
                    if type_floors:
                        sorted_floors = sorted(type_floors)

                        observed_min_floor = sorted_floors[0]
                        observed_max_floor = sorted_floors[-1]

                        # ✅ 서로 다른 거래층 수를 먼저 계산
                        unique_floor_count = len(set(sorted_floors))

                        # ✅ 층 분포 계산
                        floor_distribution = calculate_floor_distribution(
                            type_floors
                        )

                        q1_floor = floor_distribution["q1"]
                        q3_floor = floor_distribution["q3"]

                        # ✅ 저층·중층·고층별 가격 분류
                        for pair in type_floor_price_pairs:
                            pair_floor = pair["floor"]
                            pair_price = pair["price"]

                            if pair_floor <= q1_floor:
                                low_floor_prices.append(pair_price)

                            elif pair_floor < q3_floor:
                                middle_floor_prices.append(pair_price)

                            else:
                                high_floor_prices.append(pair_price)

                        # ✅ 층 그룹별 가격 통계
                        low_floor_stats = calculate_floor_price_stats(
                            low_floor_prices
                        )

                        middle_floor_stats = calculate_floor_price_stats(
                            middle_floor_prices
                        )

                        high_floor_stats = calculate_floor_price_stats(
                            high_floor_prices
                        )

                        # ✅ 층 분석 가능 수준 판정
                        # unique_floor_count가 먼저 계산된 뒤 실행해야 함
                        floor_analysis_method = evaluate_floor_analysis_method(
                            len(type_floor_price_pairs),
                            unique_floor_count
                        )

                        floor_regression = calculate_floor_regression(
                            type_floor_price_pairs
                        )
                        if DEBUG_FUTURE_ENGINE:
                            print(
                                f"🔍 {type_name} 층회귀 계산 결과: "
                                f"{floor_regression}"
                            )

                        # ✅ 거래층 중앙값
                        floor_middle_index = len(sorted_floors) // 2

                        if len(sorted_floors) % 2 == 1:
                            median_floor = sorted_floors[floor_middle_index]

                        else:
                            median_floor = round(
                                (
                                    sorted_floors[floor_middle_index - 1]
                                    + sorted_floors[floor_middle_index]
                                ) / 2,
                                1
                            )

                    else:
                        sorted_floors = []

                        observed_min_floor = 0
                        observed_max_floor = 0
                        median_floor = 0
                        unique_floor_count = 0

                        floor_distribution = {
                            "count": 0,
                            "q1": 0,
                            "median": 0,
                            "q3": 0,
                            "low_count": 0,
                            "middle_count": 0,
                            "high_count": 0,
                            "description": "층 거래자료가 없습니다."
                        }

                        low_floor_stats = calculate_floor_price_stats([])
                        middle_floor_stats = calculate_floor_price_stats([])
                        high_floor_stats = calculate_floor_price_stats([])

                        floor_analysis_method = evaluate_floor_analysis_method(
                            0,
                            0
                        )

                    trimmed_average_price, trim_count, trimmed_count = (
                        calculate_trimmed_average(
                            sorted_prices
                        )
                    )

                    reference_price = select_type_reference_price(
                                            average_price,
                                            median_price,
                                            trimmed_average_price,
                                            final_type_score
                                        )
                    # ✅ 백테스트용 TYPE 대표가격 상세 검증
                    if type_name == current_size_type:
                        if DEBUG_FUTURE_ENGINE:
                            print()
                            print("========== TYPE 대표가격 상세 검증 ==========")
                            print(f"현재 TYPE : {type_name}")
                            print(f"전체 거래건수 : {trade_count}건")
                            print(f"전체 거래가격 : {sorted_prices}")
                            print(f"평균가격 : {average_price:,}만원")
                            print(f"중앙가격 : {median_price:,}만원")
                            print(f"절사평균가격 : {trimmed_average_price:,}만원")
                            print(f"최종점수 : {final_type_score}점")
                            print(f"선택된 TYPE대표가격 : {reference_price:,}만원")
                            print(f"최근3개월 거래가격 : {recent_3m_prices}")
                            print(f"최근3개월 평균가격 : {recent_3m_avg:,}만원")
                            print("============================================")
                            print()

                    # 사용자가 선택한 면적의 TYPE 결과 저장
                    if type_name == current_size_type:
                        current_type_trade_count = trade_count
                        current_type_reliability = reliability

                        current_type_average_price = average_price
                        current_type_trimmed_average_price = trimmed_average_price
                        current_type_median_price = median_price
                        current_type_reference_price = reference_price
                        current_type_final_score = final_type_score
                        current_type_recent_3m_ratio = recent_3m_ratio
                        current_type_recent_6m_ratio = recent_6m_ratio
                        current_type_recent_3m_avg = recent_3m_avg
                        current_type_recent_3m_std = recent_3m_std
                        current_type_variation_rate = variation_rate
                        current_type_recent_3m_count = recent_3m_count
                        current_type_recent_3m_prices = list(
                            recent_3m_prices
                        )
                        current_type_observed_min_floor = observed_min_floor
                        current_type_observed_max_floor = observed_max_floor
                        current_type_median_floor = median_floor
                        current_type_unique_floor_count = unique_floor_count
                        current_type_floor_count = len(type_floors)
                        current_type_floor_distribution = dict(
                            floor_distribution
                        )
                        current_type_floor_analysis_method = dict(
                            floor_analysis_method
                        )
                        current_type_floor_regression = dict(
                            floor_regression
                        )
                        if DEBUG_FUTURE_ENGINE:
                            print(
                                f"✅ 현재 TYPE 층회귀 저장 확인: "
                                f"{current_type_floor_regression}"
                            )
                            print(
                                "✅ 현재 TYPE 저장 확인:",
                                f"거래수={current_type_recent_3m_count},",
                                f"평균={current_type_recent_3m_avg},",
                                f"표준편차={current_type_recent_3m_std},",
                                f"변동률={current_type_variation_rate}"
                            )

                    if DEBUG_FUTURE_ENGINE:
                        print(
                            f"{type_name} | "
                            f"면적={type_size}㎡ | "
                            f"거래={trade_count}건 | "
                            f"층자료={len(type_floors)}건 | "
                            f"관측층={observed_min_floor}~{observed_max_floor}층 | "
                            f"층중앙값={median_floor}층 | "
                            f"층Q1={floor_distribution['q1']}층 | "
                            f"층Q3={floor_distribution['q3']}층 | "
                            f"저·중·고="
                            f"{floor_distribution['low_count']}/"
                            f"{floor_distribution['middle_count']}/"
                            f"{floor_distribution['high_count']}건 | "
                            f"거래층종류={unique_floor_count}개 | "
                            f"최근3개월={recent_3m_count}건({recent_3m_ratio}%) | "
                            f"최근3개월평균={recent_3m_avg:,}만원 | "
                            f"최근6개월={recent_6m_count}건({recent_6m_ratio}%) | "
                            f"신뢰도={reliability['reliability']} | "
                            f"품질점수={quality_score}점 | "
                            f"최종점수={final_type_score}점 | "
                            f"평균={average_price:,}만원 | "
                            f"절사평균={trimmed_average_price:,}만원 | "
                            f"대표가격={reference_price:,}만원 | "
                            f"중앙값={median_price:,}만원 | "
                            f"최저={minimum_price:,}만원 | "
                            f"최고={maximum_price:,}만원"
                            f"절사={trim_count}건 | "
                            f"사용거래={trimmed_count}건 | "
                        )

                        print(
                            f"    └ 층별가격 | "
                            f"저층={low_floor_stats['count']}건 "
                            f"(평균={low_floor_stats['average']:,}, "
                            f"중앙={low_floor_stats['median']:,}, "
                            f"절사={low_floor_stats['trimmed_average']:,}) | "

                            f"중층={middle_floor_stats['count']}건 "
                            f"(평균={middle_floor_stats['average']:,}, "
                            f"중앙={middle_floor_stats['median']:,}, "
                            f"절사={middle_floor_stats['trimmed_average']:,}) | "

                            f"고층={high_floor_stats['count']}건 "
                            f"(평균={high_floor_stats['average']:,}, "
                            f"중앙={high_floor_stats['median']:,}, "
                            f"절사={high_floor_stats['trimmed_average']:,})"
                        )

                        print(
                            f"    └ 층분석판정 | "
                            f"방법={floor_analysis_method['method']} | "
                            f"신뢰도={floor_analysis_method['level']} | "
                            f"{floor_analysis_method['description']}"
                        )

                else:
                    if DEBUG_FUTURE_ENGINE:
                        print(
                            f"{type_name} | "
                            f"면적={type_size}㎡ | "
                            f"거래 데이터 없음"
                        )

            except Exception as type_error:
                if DEBUG_FUTURE_ENGINE:
                    print(
                        f"{type_name} | "
                        f"면적={type_size}㎡ | "
                        f"검증 오류={type_error}"
                    )
        if DEBUG_FUTURE_ENGINE:
            print("================================================\n")
            print("\n========== 현재 TYPE 추천가 적용 기준 ==========")
            print(f"현재 면적: {matched_size}㎡")
            print(f"현재 TYPE: {current_size_type}")
            print(f"동일 TYPE 거래건수: {current_type_trade_count}건")
            print(
                f"TYPE 신뢰도: "
                f"{current_type_reliability['reliability']}"
            )
            print(
                f"동일 TYPE 반영비율: "
                f"{current_type_reliability['type_weight'] * 100:.0f}%"
            )
            print(
                f"동일 명목평형 보조비율: "
                f"{current_type_reliability['nominal_size_weight'] * 100:.0f}%"
            )
            print(
                f"적용 설명: "
                f"{current_type_reliability['description']}"
            )
            print("===============================================\n")
        recent_market_signal = analyze_recent_market_signal(
            current_type_reference_price,
            current_type_recent_3m_avg,
            current_type_recent_3m_count
        )
        market_weight = calculate_market_weight(
            current_type_recent_3m_count,
            recent_market_signal["premium_rate"],
            current_type_variation_rate
        )
        type_weight = 100 - market_weight

        if (
            current_type_reference_price > 0
            and current_type_recent_3m_avg > 0
        ):
            market_adjusted_reference_price = round(
                current_type_reference_price
                * (type_weight / 100)
                +
                current_type_recent_3m_avg
                * (market_weight / 100)
            )
        else:
            market_adjusted_reference_price = (
                current_type_reference_price
            )

        market_adjustment_amount = round(
            market_adjusted_reference_price
            - current_type_reference_price
        )

        if current_type_reference_price > 0:
            market_adjustment_rate = round(
                market_adjustment_amount
                / current_type_reference_price
                * 100,
                2
            )
        else:
            market_adjustment_rate = 0.0

        recent_price_uncertainty = (
            calculate_recent_price_uncertainty(
                current_type_recent_3m_prices,
                current_type_reference_price
            )
        )
        market_weight_scenarios = build_market_weight_scenarios(
            current_type_reference_price,
            current_type_recent_3m_avg
        )
        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 시장 반영 기준가격 후보 ==========")

            print(
                f"TYPE 대표가격: "
                f"{current_type_reference_price:,}만원"
            )

            print(
                f"최근 3개월 평균가격: "
                f"{current_type_recent_3m_avg:,}만원"
            )

            print(
                f"TYPE 반영비율: "
                f"{type_weight}%"
            )

            print(
                f"최근시장 반영비율: "
                f"{market_weight}%"
            )

            print(
                f"시장 반영 기준가격 후보: "
                f"{market_adjusted_reference_price:,}만원"
            )

            print(
                f"TYPE 대표가격 대비 보정: "
                f"{market_adjustment_amount:+,}만원 "
                f"({market_adjustment_rate:+.2f}%)"
            )

            print("============================================")
            print()
            print("========== 현재 선택 TYPE 새 엔진 결과 ==========")
            print(f"선택 면적: {matched_size}㎡")
            print(f"내부 TYPE: {current_size_type}")
            print(f"거래건수: {current_type_trade_count}건")
            print(
                f"신뢰도: "
                f"{current_type_reliability.get('reliability', '낮음')}"
            )
            print(f"최근 3개월 비율: {current_type_recent_3m_ratio}%")
            print(f"최근 6개월 비율: {current_type_recent_6m_ratio}%")
            print(f"최종점수: {current_type_final_score}점")
            print(f"기존 평균가격: {current_type_average_price:,}만원")
            print(
                f"절사평균가격: "
                f"{current_type_trimmed_average_price:,}만원")
            print(f"중앙가격: {current_type_median_price:,}만원")
            print(
                f"새 TYPE 대표가격: "
                f"{current_type_reference_price:,}만원")

            print(f"최근 3개월 평균가격: " 
                f"{current_type_recent_3m_avg:,}만원")

            print(
                f"최근3개월 표준편차: "
                f"{current_type_recent_3m_std:,}만원"
            )

            print(
                f"가격 변동률: "
                f"{current_type_variation_rate}%"
            )

            print(
                f"가격 안정성: "
                f"{evaluate_price_stability(current_type_variation_rate)}"
            )

            print(
                f"최근 평균 표준오차: "
                f"{recent_price_uncertainty['standard_error']:,}만원"
            )

            print(
                f"95% 오차범위: "
                f"±{recent_price_uncertainty['margin_of_error']:,}만원"
            )

            print(
                f"최근 평균 95% 추정구간: "
                f"{recent_price_uncertainty['ci_low']:,}만원"
                f" ~ "
                f"{recent_price_uncertainty['ci_high']:,}만원"
            )

            print(
                f"TYPE 대표가격 위치: "
                f"{recent_price_uncertainty['reference_position']}"
            )

            print(
                f"통계 해석: "
                f"{recent_price_uncertainty['interpretation']}"
            )
            print(
                f"최근 3개월 거래건수: "
                f"{current_type_recent_3m_count}건"
            )

            print(
                f"TYPE 대비 가격차이: "
                f"{recent_market_signal['price_gap']:+,}만원"
            )

            print(
                f"TYPE 대비 가격변화율: "
                f"{recent_market_signal['premium_rate']:+.2f}%"
            )

            print(
                f"최근시장 방향: "
                f"{recent_market_signal['direction']}"
            )

            print(
                f"최근 표본 수준: "
                f"{recent_market_signal['sample_level']}"
            )

            print(
                f"시장 해설: "
                f"{recent_market_signal['description']}"
            )
            print(
                f"층정보 확보 거래: "
                f"{current_type_floor_count}건"
            )

            print(
                f"관측 거래층 범위: "
                f"{current_type_observed_min_floor}층"
                f" ~ "
                f"{current_type_observed_max_floor}층"
            )

            print(
                f"거래층 중앙값: "
                f"{current_type_median_floor}층"
            )

            print(
                f"거래가 확인된 층 수: "
                f"{current_type_unique_floor_count}개"
            )
            print(
                f"층 분포 Q1/Q2/Q3: "
                f"{current_type_floor_distribution['q1']}층 / "
                f"{current_type_floor_distribution['median']}층 / "
                f"{current_type_floor_distribution['q3']}층"
            )

            print(
                f"저층·중층·고층 거래건수: "
                f"{current_type_floor_distribution['low_count']}건 / "
                f"{current_type_floor_distribution['middle_count']}건 / "
                f"{current_type_floor_distribution['high_count']}건"
            )

            print(
                f"층 분류 설명: "
                f"{current_type_floor_distribution['description']}"
            )
            
            print(
                f"층 분석 방법: "
                f"{current_type_floor_analysis_method['method']}"
            )

            print(
                f"층 분석 신뢰도: "
                f"{current_type_floor_analysis_method['level']}"
            )

            print(
                f"층 분석 설명: "
                f"{current_type_floor_analysis_method['description']}"
            )
            
            print(
                f"🔍 최종 층회귀 상태: "
                f"{current_type_floor_regression}"
            )
        if DEBUG_FUTURE_ENGINE:
            if current_type_floor_regression["available"]:

                print()
                print("========== 층 회귀 분석 ==========")

                print(
                    f"거래수 : "
                    f"{current_type_floor_regression['count']}건"
                )

                print(
                    f"층당 가격변화 : "
                    f"{current_type_floor_regression['slope']:.0f}만원"
                )

                print(
                    f"기준가격(절편) : "
                    f"{current_type_floor_regression['intercept']:.0f}만원"
                )

                print(
                    f"R² : "
                    f"{current_type_floor_regression['r2']}"
                )

                print("==============================")
            else:
                
                print()
                print("========== 층 회귀 분석 ==========")
                print("회귀분석 미적용")
                print(
                    f"사유: "
                    f"{current_type_floor_regression.get('reason', '알 수 없음')}"
                )
                print("==================================")
        
            print()
            print("---------- 최근시장 가중치 후보 ----------")

            for scenario in market_weight_scenarios:
                recent_weight_percent = round(
                    scenario["recent_weight"] * 100
                )

                type_weight_percent = round(
                    scenario["type_weight"] * 100
                )

                print(
                    f"TYPE {type_weight_percent}% + "
                    f"최근시장 {recent_weight_percent}%"
                    f" → "
                    f"{scenario['candidate_price']:,}만원"
                )
        
            print("------------------------------------------")
            print("================================================")

        # ✅ 매칭된 실제 면적으로 거래 조회
        
        if reference_date:
            items = get_backtest_sale_trades(
                region=region,
                apt_name=apt_name,
                size=matched_size
            )
        else:
            db_rows = get_apt_sale_trades(
                apt_name,
                matched_size,
                region=region
            )

            items = db_rows_to_items(
                db_rows
            )

        trades = []

        for item in items:
            item_size = str(
                item.get("size", "")
            ).replace("㎡", "").strip()

            target_size = str(
                matched_size
            ).replace("㎡", "").strip()

            # ✅ 거래일 파싱
            try:
                item_trade_date = datetime.strptime(
                    str(item.get("date", "")),
                    "%Y-%m-%d"
                )
            except (ValueError, TypeError):
                continue

            # ✅ 분석 기준일 이후 거래 제외
            if item_trade_date > analysis_date:
                continue

            if (
                is_same_apartment_name(
                    apt_name,
                    item["apt_name"]
                )
                and item_size == target_size
            ):
                trades.append({
                    "price": item["price"],
                    "date": item["date"],
                    "size": item["size"],
                    "dong": item.get("dong", "")
                })

        if DEBUG_FUTURE_ENGINE:
            print(
                f"✅ 선택 면적 기준일 필터 후 거래수: "
                f"{len(trades)}건 "
                f"(기준일: {analysis_date.strftime('%Y-%m-%d')})"
            )

        if not trades:
            return {
                "지역": region,
                "동": "",
                "단지명": apt_name,
                "면적": size,
                "매칭면적": matched_size,
                "결과": "데이터 없음",
                "거래추세": "데이터 부족",
                "거래상승률": 0,
                "최근6개월평균": 0,
                "이전6개월평균": 0,
                "AI총평": "최근 매매 거래 데이터가 부족해 미래예측을 산정하지 않았습니다."
            }

        trades.sort(key=lambda x: x["date"], reverse=True)

        from collections import defaultdict

        # 월별 평균가 계산
        monthly_price_map = defaultdict(list)

        for t in trades:

            if not t.get("price"):
                continue

            month = str(t["date"])[:7]

            monthly_price_map[month].append(
                int(t["price"])
            )

        # 월 정렬
        all_months = sorted(monthly_price_map.keys())

        # ✅ 현재 기준 최근 12개월 월 배열 생성
        today = analysis_date

        recent_12_months = []

        for i in range(11, -1, -1):
            target_month = today.month - i
            target_year = today.year

            while target_month <= 0:
                target_month += 12
                target_year -= 1

            recent_12_months.append(
                f"{target_year}-{target_month:02d}"
            )

        prev_6_months = recent_12_months[:6]
        recent_6_months = recent_12_months[6:]

        # 평균 계산용
        from collections import defaultdict

        # 검증 코드
        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== monthly_price_map 원본 trades 검증 ==========")

            print(
                f"analysis_date : "
                f"{analysis_date.strftime('%Y-%m-%d')}"
            )

            print(
                f"trades 전체건수 : "
                f"{len(trades)}건"
            )

        for t in trades:

            try:
                trade_date = str(
                    t.get("date", "")
                )

                trade_price = t.get(
                    "price",
                    0
                )

                # 분석기준월 거래만 확인
                if DEBUG_FUTURE_ENGINE:
                    if trade_date[:7] == analysis_date.strftime("%Y-%m"):
                        
                        print(
                            f"기준월 거래 : "
                            f"날짜={trade_date} | "
                            f"가격={trade_price} | "
                            f"층={t.get('floor')}"
                        )

            except Exception:
                continue
        if DEBUG_FUTURE_ENGINE:
            print("======================================================")

        # ✅ 최근 6건이 아니라, 월별 평균가 기준으로 거래추세 계산
        monthly_price_map = defaultdict(list)

        for t in trades:
            if not t.get("price"):
                continue

            month = str(t.get("date", ""))[:7]

            if not month:
                continue

            monthly_price_map[month].append(int(t["price"]))

        all_months = sorted(monthly_price_map.keys())

        # ✅ 현재 기준 최근 12개월 월 배열 생성
        today = analysis_date

        recent_12_months = []

        for i in range(11, -1, -1):
            target_month = today.month - i
            target_year = today.year

            while target_month <= 0:
                target_month += 12
                target_year -= 1

            recent_12_months.append(
                f"{target_year}-{target_month:02d}"
            )

        prev_6_months = recent_12_months[:6]
        recent_6_months = recent_12_months[6:]

        
        # ✅ 현재 기준 최근 12개월 생성
        today = analysis_date

        recent_12_months = []

        for i in range(11, -1, -1):
            target_month = today.month - i
            target_year = today.year

            while target_month <= 0:
                target_month += 12
                target_year -= 1

            recent_12_months.append(
                f"{target_year}-{target_month:02d}"
            )

        prev_6_months = recent_12_months[:6]
        recent_6_months = recent_12_months[6:]

        # ✅ 최근 6개월 월평균
        trend_chart = []

        for month in recent_6_months:
            prices = monthly_price_map.get(month, [])

            trend_chart.append({
                "month": month,
                "price": round(sum(prices) / len(prices)) if prices else 0,
                "count": len(prices)
            })

        # ✅ 이전 6개월 월평균
        prev_trend_chart = []

        for month in prev_6_months:
            prices = monthly_price_map.get(month, [])

            prev_trend_chart.append({
                "month": month,
                "price": round(sum(prices) / len(prices)) if prices else 0,
                "count": len(prices)
            })

        recent_6_prices = [
            x["price"]
            for x in trend_chart
            if x["price"] > 0
        ]

        prev_6_prices = [
            x["price"]
            for x in prev_trend_chart
            if x["price"] > 0
        ]
    
        recent_avg = round(sum(recent_6_prices) / len(recent_6_prices)) if recent_6_prices else 0
        prev_avg = round(sum(prev_6_prices) / len(prev_6_prices)) if prev_6_prices else 0
        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 거래추세 계산 검증 ==========")

            print(f"이전 6개월 월목록 : {prev_6_months}")
            print(f"최근 6개월 월목록 : {recent_6_months}")
            print(f"이전 6개월 월평균 목록 : {prev_6_prices}")
            print(f"최근 6개월 월평균 목록 : {recent_6_prices}")
            print(f"이전 6개월 평균가격 : {prev_avg:,}만원")
            print(f"최근 6개월 평균가격 : {recent_avg:,}만원")

            print("======================================")

        # ==========================================
        # ✅ 최근 월가격 기반 보조 추세 검증
        # 아직 미래예측 가격에는 반영하지 않음
        # ==========================================

        recent_fallback_rate = 0.0
        recent_fallback_available = False
        recent_fallback_direction = "데이터 부족"

        if len(recent_6_prices) >= 2:

            first_recent_price = recent_6_prices[0]
            last_recent_price = recent_6_prices[-1]

            if first_recent_price > 0:
                recent_fallback_rate = round(
                    (
                        last_recent_price
                        - first_recent_price
                    )
                    / first_recent_price
                    * 100,
                    2
                )

                recent_fallback_available = True

                if recent_fallback_rate > 1:
                    recent_fallback_direction = "상승"

                elif recent_fallback_rate < -1:
                    recent_fallback_direction = "하락"

                else:
                    recent_fallback_direction = "보합"

        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 최근 월가격 보조추세 검증 ==========")

            print(
                f"최근 월평균 가격목록 : "
                f"{recent_6_prices}"
            )

            print(
                f"보조추세 계산가능 : "
                f"{recent_fallback_available}"
            )

            print(
                f"첫 월가격 : "
                f"{recent_6_prices[0]:,}만원"
                if recent_6_prices else
                "첫 월가격 : 없음"
            )

            print(
                f"마지막 월가격 : "
                f"{recent_6_prices[-1]:,}만원"
                if recent_6_prices else
                "마지막 월가격 : 없음"
            )

            print(
                f"보조 상승률 : "
                f"{recent_fallback_rate:+.2f}%"
            )

            print(
                f"보조 방향 : "
                f"{recent_fallback_direction}"
            )

            print("==========================================")

        # =================================================
        # ✅ 최근 월가격 방향성 검증
        # =================================================

        up_count = 0
        down_count = 0

        if len(recent_6_prices) >= 2:

            for i in range(
                1,
                len(recent_6_prices)
            ):

                prev_price = recent_6_prices[i - 1]
                current_price = recent_6_prices[i]

                if current_price > prev_price:
                    up_count += 1

                elif current_price < prev_price:
                    down_count += 1


        recent_direction = "혼조"

        if up_count > down_count:
            recent_direction = "상승 우세"

        elif down_count > up_count:
            recent_direction = "하락 우세"

        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 최근 월가격 방향성 검증 ==========")

            print(
                f"월평균 가격목록 : "
                f"{recent_6_prices}"
            )

            print(
                f"상승 월간구간 : "
                f"{up_count}개"
            )

            print(
                f"하락 월간구간 : "
                f"{down_count}개"
            )

            print(
                f"최근 방향성 : "
                f"{recent_direction}"
            )

            print("============================================")

        # =================================================
        # ✅ 추세 지속성 검증
        # =================================================

        total_direction_count = (
            up_count + down_count
        )

        direction_match_rate = 0

        if total_direction_count > 0:

            # 최근 보조추세가 상승이면 상승구간 비율
            if recent_fallback_rate > 0:

                direction_match_rate = round(
                    up_count
                    / total_direction_count
                    * 100,
                    1
                )

            # 최근 보조추세가 하락이면 하락구간 비율
            elif recent_fallback_rate < 0:

                direction_match_rate = round(
                    down_count
                    / total_direction_count
                    * 100,
                    1
                )

        # =================================================
        # ✅ 추세 신뢰도 판정
        # 방향일치율 + 관측구간 수를 함께 사용
        # =================================================

        if total_direction_count <= 1:

            # 월 데이터가 2개뿐이면
            # 방향이 100% 일치해도 신뢰하기 어려움
            trend_confidence = "낮음"

        elif total_direction_count == 2:

            # 방향구간 2개에서는
            # 최대 신뢰도를 '보통'으로 제한
            if direction_match_rate >= 50:
                trend_confidence = "보통"
            else:
                trend_confidence = "낮음"

        elif total_direction_count == 3:

            # 방향구간 3개에서는
            # 최대 '높음'
            if direction_match_rate >= 75:
                trend_confidence = "높음"

            elif direction_match_rate >= 50:
                trend_confidence = "보통"

            else:
                trend_confidence = "낮음"

        else:

            # 방향구간 4개 이상부터
            # 매우 높은 신뢰도 허용
            if direction_match_rate >= 80:
                trend_confidence = "매우 높음"

            elif direction_match_rate >= 60:
                trend_confidence = "높음"

            elif direction_match_rate >= 50:
                trend_confidence = "보통"

            else:
                trend_confidence = "낮음"
        # =================================================
        # ✅ 추세신뢰도 계수
        # =================================================

        trend_confidence_factor_map = {
            "매우 높음": 1.00,
            "높음": 1.00,
            "보통": 0.75,
            "낮음": 0.25
        }

        trend_confidence_factor = (
            trend_confidence_factor_map.get(
                trend_confidence,
                0.50
            )
        )
        if DEBUG_FUTURE_ENGINE:
            print(
                f"추세신뢰도 계수 : "
                f"{trend_confidence_factor:.2f}"
            )

            print()
            print("========== 추세 지속성 검증 ==========")

            print(
                f"월평균 가격 : "
                f"{recent_6_prices}"
            )

            print(
                f"상승구간 : "
                f"{up_count}개"
            )

            print(
                f"하락구간 : "
                f"{down_count}개"
            )

            print(
                f"전체 방향구간 : "
                f"{total_direction_count}개"
            )

            print(
                f"방향일치율 : "
                f"{direction_match_rate:.1f}%"
            )

            print(
                f"추세신뢰도 : "
                f"{trend_confidence}"
            )

            print("====================================")

        if recent_avg > 0 and prev_avg > 0:
            rise_rate = round(
                ((recent_avg - prev_avg) / prev_avg) * 100,
                1
            )
        else:
            rise_rate = 0

        # =====================================================
        # ✅ 최근 월별 가격 + 거래량 검증
        # 아직 미래예측 공식에는 반영하지 않음
        # =====================================================

        recent_month_price_volume = []

        for month in recent_6_months:

            month_prices = []

            for item in trades:

                try:
                    raw_date = str(
                        item.get("date", "")
                    ).strip()

                    if not raw_date:
                        continue

                    # YYYY-MM-DD → YYYY-MM
                    item_month = raw_date[:7]

                    if item_month != month:
                        continue

                    raw_price = item.get("price")

                    if raw_price is None:
                        continue

                    if isinstance(raw_price, str):
                        raw_price = (
                            raw_price
                            .replace(",", "")
                            .strip()
                        )

                    price_value = int(
                        float(raw_price)
                    )

                    if price_value > 0:
                        month_prices.append(
                            price_value
                        )

                except (ValueError, TypeError):
                    continue

            if not month_prices:
                continue

            month_avg_price = round(
                sum(month_prices)
                / len(month_prices)
            )

            recent_month_price_volume.append({
                "month": month,
                "avg_price": month_avg_price,
                "trade_count": len(month_prices)
            })

        if DEBUG_FUTURE_ENGINE:
            print()
            print(
                "========== 최근 월별 가격·거래량 검증 =========="
            )

            for row in recent_month_price_volume:

                print(
                    f"{row['month']} | "
                    f"평균가격 {row['avg_price']:,}만원 | "
                    f"거래량 {row['trade_count']}건"
                )
        
            print(
                "============================================"
            )


        # =====================================================
        # ✅ 첫 거래월 대비 마지막 거래월 거래량 변화
        # =====================================================

        volume_change_rate = 0.0
        volume_direction = "계산 불가"

        if len(recent_month_price_volume) >= 2:

            first_volume = (
                recent_month_price_volume[0]["trade_count"]
            )

            last_volume = (
                recent_month_price_volume[-1]["trade_count"]
            )

            if first_volume > 0:

                volume_change_rate = round(
                    (
                        last_volume
                        - first_volume
                    )
                    / first_volume
                    * 100,
                    2
                )

                if volume_change_rate >= 20:
                    volume_direction = "증가"

                elif volume_change_rate <= -20:
                    volume_direction = "감소"

                else:
                    volume_direction = "보합"

        if DEBUG_FUTURE_ENGINE:
            print(
                f"월 거래량 변화율 : "
                f"{volume_change_rate:+.2f}%"
            )

            print(
                f"월 거래량 방향 : "
                f"{volume_direction}"
            )


        # ✅ 실제 전세 평균가 계산
        rent_size = str(size).replace("㎡", "").strip()

        rent_rows = get_rent_trades(
            region,
            apt_name,
            rent_size
        )
        if DEBUG_FUTURE_ENGINE:
            print("전세조회 면적 =", rent_size)
            print("전세거래 조회건수 =", len(rent_rows))
            print("전세거래 샘플 =", rent_rows[:5])

        rent_items = rent_rows_to_items(rent_rows)

        jeonse_prices = [
            item["deposit"]
            for item in rent_items
            if item.get("monthly_rent") == 0 and item.get("deposit", 0) > 0
        ]

        avg_jeonse = (
            round(sum(jeonse_prices) / len(jeonse_prices))
            if jeonse_prices else 0
        )

        jeonse_ratio = (
            round((avg_jeonse / recent_avg) * 100, 1)
            if recent_avg and avg_jeonse else 0
        )

        gap_price = (
            recent_avg - avg_jeonse
            if recent_avg and avg_jeonse else recent_avg
        )

        
        # ✅ 거래추세 계산
        # 이전/최근 기간에 실제 가격 데이터가 모두 있을 때만 추세 계산
        if (
            recent_avg > 0
            and prev_avg > 0
            and len(recent_6_prices) >= 1
            and len(prev_6_prices) >= 1
        ):
            rise_rate = round(
                ((recent_avg - prev_avg) / prev_avg) * 100,
                1
            )

            trend_available = True

        else:
            rise_rate = 0
            trend_available = False


        # ✅ 거래추세 판정
        if not trend_available:
            trend = "데이터 부족"

        elif rise_rate > 1:
            trend = "상승"

        elif rise_rate < -1:
            trend = "하락"

        else:
            trend = "보합"

        # ✅ 최근 12개월 월별 거래량 생성
        today = analysis_date
        monthly_volume_map = {}

        for i in range(12):
            target_month = today.month - i
            target_year = today.year

            while target_month <= 0:
                target_month += 12
                target_year -= 1

            month_key = f"{target_year}-{target_month:02d}"
            monthly_volume_map[month_key] = 0

        for t in trades:
            month = str(t.get("date", ""))[:7]

            if month in monthly_volume_map:
                monthly_volume_map[month] += 1

        monthly_volume = [
            {
                "month": key,
                "count": monthly_volume_map[key]
            }
            for key in sorted(monthly_volume_map.keys())
        ]

        # ✅ 미래예측 최근 12개월 실제 거래건수
        recent_12m_count = sum(
            int(v.get("count", 0))
            for v in monthly_volume
        )

        # ✅ 미래예측 거래 데이터 신뢰도
        if recent_12m_count >= 3:
            future_data_reliability = "정상"

        elif recent_12m_count >= 1:
            future_data_reliability = "거래 부족"

        else:
            future_data_reliability = "예측 보류"

        if DEBUG_FUTURE_ENGINE:
            print(
                f"🔍 미래예측 최근 12개월 거래건수 = "
                f"{recent_12m_count}건"
            )
            print(
                f"🔍 미래예측 데이터 신뢰도 = "
                f"{future_data_reliability}"
            )
        
        # ✅ 최근 12개월 가격 + 거래량 통합 그래프용
        recent_12m_price_volume_chart = []

        for month in recent_12_months:
            prices = monthly_price_map.get(month, [])
            volume = 0

            for v in monthly_volume:
                if v["month"] == month:
                    volume = v["count"]
                    break

            recent_12m_price_volume_chart.append({
                "month": month,
                "price": round(sum(prices) / len(prices)) if prices else 0,
                "count": volume
            })

        recent_3m_volume = monthly_volume[-3:]

        recent_3m_count = sum(
            int(v.get("count", 0))
            for v in recent_3m_volume
        )

        # =========================================================
        # ✅ 하락장 전환신호 V1용 이전 3개월 거래량
        # 최근 3개월 직전의 3개월 거래건수
        # =========================================================

        previous_3m_volume = monthly_volume[-6:-3]

        previous_3m_count = sum(
            int(v.get("count", 0))
            for v in previous_3m_volume
        )

        # 거래량 회복률
        if previous_3m_count > 0:
            volume_recovery_ratio = (
                recent_3m_count
                / previous_3m_count
            )
        else:
            volume_recovery_ratio = 0.0

        ai_summary_parts = []

        # 1. 거래활성도 먼저 계산
        if recent_3m_count >= 20:
            trade_activity = "활발"
        elif recent_3m_count >= 10:
            trade_activity = "보통"
        elif recent_3m_count >= 5:
            trade_activity = "적음"
        else:
            trade_activity = "부족"

        # 2. 그 다음 AI총평 생성
        ai_summary_parts = []

        if trend == "상승":
            ai_summary_parts.append(
                f"최근 매매가격은 상승 흐름을 보이고 있으며, 가격 상승률은 {rise_rate}% 수준입니다."
            )
        elif trend == "하락":
            ai_summary_parts.append(
                f"최근 매매가격은 조정 흐름을 보이고 있으며, 가격 변동률은 {rise_rate}% 수준입니다."
            )
        else:
            ai_summary_parts.append(
                "최근 매매가격은 뚜렷한 방향성 없이 보합권 흐름을 유지하고 있습니다."
            )

        ai_summary_parts.append(
            f"최근 3개월 거래량은 {recent_3m_count}건으로 거래활성도는 '{trade_activity}' 수준입니다."
        )

        if trade_activity == "부족" and rise_rate > 5:
            ai_summary_parts.append(
                "다만 최근 거래건수가 매우 적어 상승률의 신뢰도는 낮을 수 있으며, 추가 거래 확인이 필요합니다."
            )

        if jeonse_ratio >= 70:
            ai_summary_parts.append(
                f"전세가율은 {jeonse_ratio}%로 높은 편이며, 전세 수요에 따른 실수요 지지력이 비교적 강한 구간입니다."
            )
        elif jeonse_ratio >= 55:
            ai_summary_parts.append(
                f"전세가율은 {jeonse_ratio}%로 중간 수준이며, 매매가 대비 전세 수요는 보통 수준으로 판단됩니다."
            )
        elif jeonse_ratio > 0:
            ai_summary_parts.append(
                f"전세가율은 {jeonse_ratio}%로 낮은 편이며, 매매가 대비 전세 지지력은 다소 약한 구간입니다."
            )

        if jeonse_ratio >= 70:
            ai_summary_parts.append(
                "전세가율이 높은 편이라 실투자금 부담은 상대적으로 낮게 해석됩니다."
            )
        elif jeonse_ratio >= 55:
            ai_summary_parts.append(
                "전세가율은 중간 수준으로, 매매가 대비 전세 수요는 보통 수준으로 판단됩니다."
            )
        elif jeonse_ratio > 0:
            ai_summary_parts.append(
                "전세가율이 낮은 편이라 갭 부담이 크고, 투자 관점에서는 보수적인 접근이 필요합니다."
            )

        gap_eok = round(gap_price / 10000, 1)

        if gap_eok >= 8:
            ai_summary_parts.append(
                "갭차이가 큰 편이라 초기 자금 부담이 높고, 실거주 중심의 보수적인 판단이 필요합니다."
            )
        elif gap_eok >= 4:
            ai_summary_parts.append(
                "갭차이는 중간 수준으로, 자금 여력과 전세 수요를 함께 확인하는 것이 좋습니다."
            )
        else:
            ai_summary_parts.append(
                "갭차이가 비교적 낮아 진입 부담은 상대적으로 낮은 편입니다."
            )

        if trade_activity == "활발":
            ai_summary_parts.append(
                "거래가 꾸준히 발생하고 있어 시장 유동성은 양호한 수준으로 판단됩니다."
            )
        elif trade_activity == "보통":
            ai_summary_parts.append(
                "거래는 지속되고 있으나, 시장 분위기를 판단하기에는 추가 관찰이 필요합니다."
            )
        elif trade_activity == "적음":
            ai_summary_parts.append(
                "거래량이 많지 않아 가격 변동성에 주의할 필요가 있습니다."
            )
        else:
            ai_summary_parts.append(
                "최근 거래가 많지 않아 실제 시장 수요를 판단하기에는 다소 제한적입니다."
            )

        if trend == "상승" and trade_activity in ["활발", "보통"]:
            ai_summary_parts.append(
                "가격 흐름과 거래량이 함께 받쳐주는 구간으로, 단기 전망은 비교적 긍정적으로 해석됩니다."
            )
        elif trend == "하락":
            ai_summary_parts.append(
                "최근 가격 흐름이 약세를 보이고 있어, 매수 판단 시 추가 조정 가능성을 함께 확인하는 것이 좋습니다."
            )
        elif trend == "보합" and trade_activity in ["활발", "보통"]:
            ai_summary_parts.append(
                "가격은 보합권이지만 거래는 유지되고 있어, 급등보다는 안정적인 흐름에 가까워 보입니다."
            )
        else:
            ai_summary_parts.append(
                "가격과 거래량 모두 뚜렷한 방향성이 강하지 않아, 당분간은 관망 성격이 큰 구간으로 판단됩니다."
            )

        
        # ✅ 전망점수 계산
        outlook_score = 50

        if rise_rate >= 10:
            outlook_score += 20
        elif rise_rate >= 5:
            outlook_score += 15
        elif rise_rate >= 1:
            outlook_score += 8
        elif rise_rate <= -10:
            outlook_score -= 20
        elif rise_rate <= -5:
            outlook_score -= 15
        elif rise_rate <= -1:
            outlook_score -= 8

        if trade_activity == "활발":
            outlook_score += 15
        elif trade_activity == "보통":
            outlook_score += 8
        elif trade_activity == "적음":
            outlook_score -= 3
        else:
            outlook_score -= 10

        if jeonse_ratio >= 75:
            outlook_score += 12
        elif jeonse_ratio >= 65:
            outlook_score += 8
        elif jeonse_ratio >= 55:
            outlook_score += 3
        elif jeonse_ratio > 0 and jeonse_ratio < 45:
            outlook_score -= 10
        elif jeonse_ratio > 0 and jeonse_ratio < 55:
            outlook_score -= 5

        gap_eok_for_score = gap_price / 10000 if gap_price else 0

        if gap_eok_for_score >= 10:
            outlook_score -= 8
        elif gap_eok_for_score >= 7:
            outlook_score -= 5
        elif gap_eok_for_score >= 5:
            outlook_score -= 3
        elif gap_eok_for_score > 0 and gap_eok_for_score <= 3:
            outlook_score += 5

        

        outlook_score = max(0, min(100, outlook_score))

        if outlook_score >= 80:
            outlook_result = "매우긍정"
        elif outlook_score >= 65:
            outlook_result = "긍정"
        elif outlook_score >= 45:
            outlook_result = "보통"
        elif outlook_score >= 30:
            outlook_result = "주의"
        else:
            outlook_result = "위험"

        if outlook_result == "매우긍정":
            ai_summary_parts.append(
                    f"종합 전망은 '{outlook_result}' 단계로 평가됩니다."
                )

        elif outlook_result == "긍정":
            ai_summary_parts.append(
                f"종합 전망은 '{outlook_result}' 수준으로 판단됩니다."
            )

        elif outlook_result == "보통":
            ai_summary_parts.append(
                "상승 요인과 위험 요인이 함께 존재하는 구간으로, 추가 시장 흐름을 확인할 필요가 있습니다."
            )

        elif outlook_result == "주의":
            ai_summary_parts.append(
                "시장 흐름이 다소 약화된 상태로, 보수적인 접근이 필요한 구간입니다."
            )

        else:
            ai_summary_parts.append(
                "시장 위험도가 높은 구간으로 판단되며, 신중한 접근이 필요합니다."
            )

        ai_summary_parts.append(
            f"거래추세, 거래활성도, 전세가율, 갭차이를 종합한 전망점수는 {outlook_score}점이며, 종합 평가는 '{outlook_result}'입니다."
        )

        ai_summary = "\n".join(ai_summary_parts)

        positive_factors = []
        caution_factors = []

        if rise_rate >= 1:
            positive_factors.append("거래추세 상승")
        elif rise_rate <= -1:
            caution_factors.append("거래추세 하락")

        if trade_activity in ["활발", "보통"]:
            positive_factors.append("거래량 유지")
        else:
            caution_factors.append("거래량 부족")

        if jeonse_ratio >= 55:
            positive_factors.append("전세가율 양호")
        elif jeonse_ratio > 0:
            caution_factors.append("전세가율 낮음")

        if gap_price and gap_price / 10000 <= 4:
            positive_factors.append("갭 부담 낮음")
        elif gap_price and gap_price / 10000 >= 8:
            caution_factors.append("갭 부담 큼")

        # ✅ 6개월 예상가격 계산
        old_base_price = recent_avg
        new_base_price = market_adjusted_reference_price

        base_price = old_base_price

       
        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 미래예측 기준가격 비교 ==========")
            print(f"기존 기준가격 : {old_base_price:,}만원")
            print(f"새 TYPE가격 : {new_base_price:,}만원")

        diff = new_base_price - old_base_price

        if old_base_price > 0:
            diff_rate = round(diff / old_base_price * 100, 2)
        else:
            diff_rate = 0
        if DEBUG_FUTURE_ENGINE:
            print(f"차이금액 : {diff:+,}만원")
            print(f"차이비율 : {diff_rate:+.2f}%")
            print("==========================================")

        activity_factor_map = {
            "활발": 0.65,
            "보통": 0.50,
            "적음": 0.35,
            "부족": 0.20
        }

        volatility_map = {
            "활발": 3,
            "보통": 5,
            "적음": 7,
            "부족": 10
        }

        activity_factor = activity_factor_map.get(trade_activity, 0.35)
        volatility = volatility_map.get(trade_activity, 7)

        # =================================================
        # ✅ 보조추세와 최근 월간 방향의 일치 여부
        # =================================================

        fallback_direction_confirmed = False

        if recent_fallback_rate > 0:
            # 상승추세라면 상승구간이 더 많아야 인정
            fallback_direction_confirmed = (
                up_count > down_count
            )

        elif recent_fallback_rate < 0:
            # 하락추세라면 하락구간이 더 많아야 인정
            fallback_direction_confirmed = (
                down_count > up_count
            )

        # ✅ 거래추세 보정
        # 기본 추세가 계산 가능하면 기존 방식 사용
        if trend_available:

            trend_adjust = (
                rise_rate
                * activity_factor
            )

            # ✅ 하락 추세는 반등 가능성이 높으므로
            # 상승 추세보다 보수적으로 반영
            if trend_adjust < 0:
                trend_adjust *= 0.5

        # ✅ 이전 6개월 데이터가 없지만
        # 최근 월평균 가격이 4개 이상이고
        # 첫월→마지막월 추세와 월간 방향성이 일치하면
        # 최근 월가격 보조추세를 저가중으로 사용
        
        elif (
            recent_fallback_available
            and len(recent_6_prices) >= 4
            and fallback_direction_confirmed
        ):

            # 기본 fallback 가중치
            fallback_weight = 0.35

            # ==========================================
            # ✅ 하락추세 + 거래량 급증 보정
            #
            # 가격은 하락하지만 거래가 크게 증가하면
            # 하락 지속 신뢰도를 낮춘다.
            # ==========================================

            if (
                recent_fallback_rate < -1.0
                and volume_change_rate >= 100.0
            ):
                fallback_weight = 0.15

            trend_adjust = (
                recent_fallback_rate
                * fallback_weight
            )
            if DEBUG_FUTURE_ENGINE:
                print(
                    f"fallback_weight : "
                    f"{fallback_weight:.2f}"
                )

        else:
            trend_adjust = 0
        # 전망점수는 이미 종합 결과값이므로 예상가격 계산에는 중복 반영하지 않음
        # score_adjust = (outlook_score - 50) * 0.03

        if jeonse_ratio >= 70:
            jeonse_adjust = 0.6
        elif jeonse_ratio >= 60:
            jeonse_adjust = 0.3
        elif jeonse_ratio >= 50:
            jeonse_adjust = 0
        elif jeonse_ratio > 0:
            jeonse_adjust = -0.3
        else:
            jeonse_adjust = 0

        if recent_3m_count >= 20:
            volume_adjust = 0.4
        elif recent_3m_count >= 10:
            volume_adjust = 0.2
        elif recent_3m_count >= 5:
            volume_adjust = 0
        else:
            volume_adjust = -0.3

        # 5. 전체 거래건수 반영
        total_trade_count = len(trades)

        if total_trade_count >= 50:
            total_trade_adjust = 0.3
        elif total_trade_count >= 30:
            total_trade_adjust = 0.2
        elif total_trade_count >= 15:
            total_trade_adjust = 0.1
        elif total_trade_count >= 5:
            total_trade_adjust = 0
        else:
            total_trade_adjust = -0.3

        # 6. 갭차이 반영
        gap_eok_for_expected = gap_price / 10000 if gap_price else 0

        if gap_eok_for_expected >= 10:
            gap_adjust = -0.4
        elif gap_eok_for_expected >= 7:
            gap_adjust = -0.25
        elif gap_eok_for_expected >= 5:
            gap_adjust = -0.1
        elif gap_eok_for_expected > 0 and gap_eok_for_expected <= 3:
            gap_adjust = 0.2
        else:
            gap_adjust = 0

        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 미래예측 조정값 검증 ==========")

            print(
                f"거래추세 상승률 rise_rate : "
                f"{rise_rate:+.2f}%"
            )

            print(
                f"거래활성도 : "
                f"{trade_activity}"
            )

            print(
                f"activity_factor : "
                f"{activity_factor}"
            )

            print(
                f"trend_adjust : "
                f"{trend_adjust:+.2f}%"
            )

            print(
                f"전세가율 : "
                f"{jeonse_ratio}%"
            )

            print(
                f"jeonse_adjust : "
                f"{jeonse_adjust:+.2f}%"
            )

            print(
                f"최근3개월 거래량 : "
                f"{recent_3m_count}건"
            )

            print(
                f"volume_adjust : "
                f"{volume_adjust:+.2f}%"
            )

            print(
                f"전체 거래건수 : "
                f"{total_trade_count}건"
            )

            print(
                f"total_trade_adjust : "
                f"{total_trade_adjust:+.2f}%"
            )

            print(
                f"갭차이 : "
                f"{gap_eok_for_expected:.2f}억원"
            )

            print(
                f"gap_adjust : "
                f"{gap_adjust:+.2f}%"
            )
            print(
                f"보조추세 방향확인 : "
                f"{fallback_direction_confirmed}"
            )
            print(
                f"보조추세 적용여부 : "
                f"{not trend_available and recent_fallback_available and len(recent_6_prices) >= 3}"
            )
            print(
                f"추세신뢰도 계수 : "
                f"{trend_confidence_factor:.2f}"
            )

            print("=======================================")


        # ✅ 미래예측 최종 조정률
        # 가격추세는 그대로 사용하고
        # 전세·거래량·전체거래·갭 신호는 50%만 보조 반영

        support_adjust = (
            jeonse_adjust
            + volume_adjust
            + total_trade_adjust
            + gap_adjust
        )

        expected_rate = (
            trend_adjust
            + support_adjust * 0.5
        )

        expected_rate = round(
            expected_rate,
            2
        )

        # =========================================================
        # ✅ 하락장 전환신호 V1
        #
        # 백테스트 확정 조건
        # 1. 현재 거래추세가 하락
        # 2. 이전 3개월 거래량 > 0
        # 3. 최근/이전 3개월 거래량 >= 2.0배
        # 4. 기존 예상변동률 <= -2.0%
        #
        # 조건 충족 시 추가 하락예측을 중단하고
        # 예상변동률을 0%로 보정
        # =========================================================

        downturn_reversal_signal = False

        if (
            trend == "하락"
            and previous_3m_count > 0
            and volume_recovery_ratio >= 2.0
            and expected_rate <= -2.0
        ):
            downturn_reversal_signal = True

            expected_rate_before_reversal = expected_rate

            expected_rate = 0.0

        else:
            expected_rate_before_reversal = expected_rate

        # =========================================================
        # ✅ 상승장 거래량 둔화 V3
        #
        # 조건
        # 1. 현재 추세 = 상승
        # 2. 이전3개월 거래량 > 0
        # 3. 최근/이전 거래량 <= 0.70
        # 4. 기존 예상상승률 >= +4.0%
        # 5. 최근가격변동률 <= +6.0%
        # 6. 예상상승률 - 최근가격변동률 >= +0.5%p
        #
        # 조건 충족 시
        # → 기존 예상상승률의 50%만 적용
        # =========================================================

        uptrend_slowdown_signal = False

        expected_rate_before_slowdown = expected_rate

        momentum_gap = (
            expected_rate
            - current_type_variation_rate
        )

        if (
            trend == "상승"
            and previous_3m_count > 0
            and volume_recovery_ratio <= 0.70
            and expected_rate >= 4.0
            and current_type_variation_rate <= 6.0
            and momentum_gap >= 0.5
        ):
            uptrend_slowdown_signal = True

            expected_rate = (
                expected_rate
                * 0.5
            )

            expected_rate = round(
                expected_rate,
                2
            )

        # =========================================================
        # ✅ V1/V3 신호에 따른 최종 전망결론 보정
        # 전망점수 자체는 유지하고 표시 등급만 과도하지 않게 조정
        # =========================================================

        if downturn_reversal_signal:
            if outlook_result == "위험":
                outlook_result = "주의"
            elif outlook_result == "주의":
                outlook_result = "보통"

        if uptrend_slowdown_signal:
            if outlook_result == "매우긍정":
                outlook_result = "긍정"

        
        expected_center = base_price * (1 + expected_rate / 100)
        expected_low = expected_center * (1 - volatility / 100)
        expected_high = expected_center * (1 + volatility / 100)

        expected_center = int(round(expected_center))
        expected_low = int(round(expected_low))
        expected_high = int(round(expected_high))

        # ✅ 새 TYPE 기준 6개월 예상가격 비교
        new_expected_center = round(
            new_base_price * (1 + expected_rate / 100)
        )

        new_expected_low = round(
            new_expected_center * (1 - volatility / 100)
        )

        new_expected_high = round(
            new_expected_center * (1 + volatility / 100)
        )
        if DEBUG_FUTURE_ENGINE:
            print()
            print("========== 6개월 예상가격 비교 ==========")

            print("[기존 엔진]")
            print(f"기준가격 : {old_base_price:,}만원")
            print(f"예상하한가 : {expected_low:,}만원")
            print(f"예상중심가 : {expected_center:,}만원")
            print(f"예상상한가 : {expected_high:,}만원")

            print()

            print("[새 TYPE 기준]")
            print(f"기준가격 : {new_base_price:,}만원")
            print(f"예상하한가 : {new_expected_low:,}만원")
            print(f"예상중심가 : {new_expected_center:,}만원")
            print(f"예상상한가 : {new_expected_high:,}만원")

        center_diff = new_expected_center - expected_center

        if expected_center > 0:
            center_diff_rate = round(
                center_diff / expected_center * 100,
                2
            )
        else:
            center_diff_rate = 0

        if DEBUG_FUTURE_ENGINE:
            print()
            print(f"중심가 차이 : {center_diff:+,}만원")
            print(f"중심가 차이율 : {center_diff_rate:+.2f}%")
            print("========================================")

            print("★★★★★ 마지막 return 실행 ★★★★★")
            print()
            print("========== 시장가중 검증 ==========")

            print(f"분석기준일: {analysis_date.strftime('%Y-%m-%d')}")
            print(f"TYPE대표가격: {round(current_type_reference_price):,}만원")
            print(f"최근3개월평균가격: {round(current_type_recent_3m_avg):,}만원")
            print(f"최근3개월거래건수: {current_type_recent_3m_count}건")
            print(f"최근가격변동률: {current_type_variation_rate:.2f}%")
            print(
                f"TYPE대비최근가격변화율: "
                f"{recent_market_signal['premium_rate']:+.2f}%"
            )
            print(f"TYPE반영비율: {type_weight}%")
            print(f"최근시장반영비율: {market_weight}%")
            print(
                f"시장반영기준가격후보: "
                f"{round(market_adjusted_reference_price):,}만원"
            )
            print(
                f"층회귀R²: "
                f"{current_type_floor_regression.get('r2', 0):.3f}"
            )
            print("실제6개월후가격: 아직 미계산")
            print("후보가격오차: 아직 미계산")
            print(
                f"보조지표 합계 support_adjust : "
                f"{support_adjust:+.2f}%"
            )

            print(
                f"보조지표 50% 반영 : "
                f"{support_adjust * 0.5:+.2f}%"
            )
            # ==============================================
            # ✅ 하락장 전환신호 V1 디버그
            # ==============================================

            print(
                f"이전3개월 거래량 : "
                f"{previous_3m_count}건"
            )

            print(
                f"최근3개월 거래량 : "
                f"{recent_3m_count}건"
            )

            print(
                f"거래량 회복률 : "
                f"{volume_recovery_ratio:.2f}배"
            )

            print(
                f"하락장 전환신호 : "
                f"{downturn_reversal_signal}"
            )

            if downturn_reversal_signal:
                print(
                    f"전환신호 보정 : "
                    f"{expected_rate_before_reversal:+.2f}%"
                    f" → {expected_rate:+.2f}%"
                )

            print(
                f"상승장 둔화신호 : "
                f"{uptrend_slowdown_signal}"
            )

            print(
                f"가격변동률 : "
                f"{current_type_variation_rate:+.2f}%"
            )

            print(
                f"예상-가격차 : "
                f"{momentum_gap:+.2f}%p"
            )

            if uptrend_slowdown_signal:
                print(
                    f"상승둔화 보정 : "
                    f"{expected_rate_before_slowdown:+.2f}%"
                    f" → {expected_rate:+.2f}%"
                )

            print(
                f"최종 expected_rate : "
                f"{expected_rate:+.2f}%"
            )
            print("===================================")
        if DEBUG_FUTURE_ENGINE:
            print(
                "🔥 RETURN 직전 신뢰도:",
                recent_12m_count,
                future_data_reliability
            )
        return {
            "지역": region,
            "디버그테스트": "future_prediction_return_ok",
            "동": trades[0].get("dong", "") if trades else "",
            "단지명": apt_name,
            "면적": size,
            "매칭면적": matched_size,

            "거래추세": trend,
            "거래상승률": rise_rate,
            "최근6개월평균": recent_avg,
            "이전6개월평균": prev_avg,
            "거래건수": len(trades),
            "거래그래프": trend_chart,
            
            "디버그최근6개월월목록": recent_6_months,
            "디버그이전6개월월목록": prev_6_months,
            "디버그월별거래량": monthly_volume,
            "디버그거래그래프": trend_chart,
            "최근12개월가격거래그래프": recent_12m_price_volume_chart,

            "이전거래그래프": prev_trend_chart,
            # "최근6개월거래량그래프": recent_6m_volume,

            "예상매매가": recent_avg,

            "6개월예상하한가": new_expected_low,
            "6개월예상상한가": new_expected_high,
            "6개월예상중심가": new_expected_center,
            "6개월예상상승률": round(expected_rate, 2),

            "신뢰도테스트": "future_reliability_ok",
            "최근12개월거래건수": recent_12m_count,
            "데이터신뢰도": future_data_reliability,

            "시장가중검증": {
                "분석기준일": analysis_date.strftime("%Y-%m-%d"),

                "TYPE대표가격": round(
                    current_type_reference_price
                ),

                "최근3개월평균가격": round(
                    current_type_recent_3m_avg
                ),

                "최근3개월거래건수": (
                    current_type_recent_3m_count
                ),

                "최근가격변동률": round(
                    current_type_variation_rate,
                    2
                ),

                "TYPE대비최근가격변화율": round(
                    recent_market_signal["premium_rate"],
                    2
                ),

                "TYPE반영비율": type_weight,

                "최근시장반영비율": market_weight,

                "시장반영기준가격후보": round(
                    market_adjusted_reference_price
                ),

                "층회귀R2": round(
                    current_type_floor_regression.get("r2", 0),
                    3
                ),

                "실제6개월후가격": None,
                "후보가격오차": None,
                "후보가격오차율": None
            },
            "추세신뢰도": trend_confidence,
            "전세가": avg_jeonse,
            "전세평균가": avg_jeonse,
            "갭차이": gap_price,
            "전세가율": jeonse_ratio,
            "거래활성도": trade_activity,
            "최근3개월거래량": recent_3m_count,
            "최근3개월거래량그래프": recent_3m_volume,

            "이전3개월거래량": previous_3m_count,

            "거래량회복률": round(volume_recovery_ratio, 2),
            "하락장전환신호": downturn_reversal_signal,
            "상승장둔화신호": uptrend_slowdown_signal,

            "전망점수": outlook_score,
            "전망결론": outlook_result,
            "상승요인": positive_factors,
            "주의요인": caution_factors,
            "AI총평": ai_summary
        }

    except Exception as e:
        print("❌ future_prediction error:", e)
        return {
            "오류": str(e)
        }

# 🔥 평형 조회
@app.get("/sizes")
def get_sizes(region: str, apt_name: str, type: str = "apt"):

    parts = region.split()

    if len(parts) >= 2:
        db_region = parts[0]
        db_sigungu = " ".join(parts[1:])

        # ✅ 거래 유형별 평형 목록 DB 조회
        # apt      : 아파트 매매
        # presale  : 분양권
        # rent     : 전월세
        if type == "presale":
            db_sizes = get_presale_sizes_from_db(
                db_region,
                db_sigungu,
                apt_name
            )

        elif type == "rent":
            db_sizes = get_rent_sizes_from_db(
                db_region,
                db_sigungu,
                apt_name
            )

        else:
            db_sizes = get_sizes_from_db(
                db_region,
                db_sigungu,
                apt_name
            )

        insert_search_log(
            search_type="size",
            region=db_region,
            sigungu=db_sigungu,
            apt_name=apt_name
        )

        return {
            "아파트": apt_name,
            "평형목록": db_sizes
        }

    return {
        "아파트": apt_name,
        "평형목록": []
    }

@app.get("/")
def serve_index():
    return FileResponse("index_mobile_test.html")

from fastapi.responses import HTMLResponse

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <title>개인정보처리방침</title>
    </head>
    <body style="font-family: sans-serif; line-height: 1.7; padding: 24px;">
        <h1>개인정보처리방침</h1>

        <p><strong>얼마일까</strong>는 아파트 실거래가 분석 정보를 제공하는 서비스입니다.</p>

        <h2>1. 수집하는 개인정보</h2>
        <p>본 서비스는 회원가입을 받지 않으며, 이름, 전화번호, 이메일 등 개인정보를 수집하지 않습니다.</p>

        <h2>2. 입력 정보</h2>
        <p>사용자가 입력하는 지역, 아파트명, 평형, 희망 매매가는 분석 결과 제공을 위해서만 사용됩니다.</p>

        <h2>3. 개인정보 제3자 제공</h2>
        <p>본 서비스는 개인정보를 제3자에게 제공하지 않습니다.</p>

        <h2>4. 문의</h2>
        <p>서비스 관련 문의: yjs1000@hanmail.net</p>

        <p>시행일: 2026년 4월 27일</p>
        
    </body>
    </html>
    """

@app.get("/health")
@app.head("/health")
def health():
    return {"status": "ok"}

# 📊 관리자 - 오늘 조회수
@app.get("/admin/today")
def admin_today(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "오늘조회수": get_today_search_count()
    }

# 📊 관리자 - 오늘 분석수
@app.get("/admin/today-analysis")
def admin_today_analysis(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "오늘분석수": get_today_analysis_count()
    }

# 📊 관리자 - 대시보드 요약
@app.get("/admin/dashboard")
def admin_dashboard(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "오늘조회수": get_today_search_count(),
        "오늘분석수": get_today_analysis_count(),
        "인기단지": get_popular_apts()
    }

# 🏢 인기 단지 TOP 5
@app.get("/admin/popular-apts")
def admin_popular_apts(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "인기단지": get_popular_apts()
    }

# 🌎 인기 지역 TOP 5
@app.get("/admin/popular-regions")
def admin_popular_regions(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "인기지역": get_popular_regions()
    }

# 📋 최근 분석 TOP 10
@app.get("/admin/recent-analysis")
def admin_recent_analysis(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return {"error": "관리자 비밀번호가 필요합니다."}

    return {
        "최근분석": get_recent_analysis()
    }

@app.get("/admin/region-change-logs")
def get_region_change_logs(limit: int = 30):

    conn = get_pg_connection()
    cur = conn.cursor()

    try:

        cur.execute("""
            SELECT
                change_type,
                lawd_cd,
                old_sido,
                old_sigungu,
                new_sido,
                new_sigungu,
                detected_at
            FROM region_change_logs
            ORDER BY detected_at DESC
            LIMIT %s
        """, (limit,))

        rows = cur.fetchall()

        result = []

        for row in rows:
            result.append({
                "change_type": row[0],
                "lawd_cd": row[1],
                "old_sido": row[2],
                "old_sigungu": row[3],
                "new_sido": row[4],
                "new_sigungu": row[5],
                "detected_at": row[6]
            })

        return result

    finally:
        cur.close()
        release_pg_connection(conn)

def get_latest_collect_log():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                status,
                started_at,
                ended_at,
                elapsed_seconds,
                success_count,
                fail_count,
                    
                sale_trade_count,
                rent_trade_count,
                presale_trade_count,
                    
                sale_list_count,
                rent_list_count,
                presale_list_count,
                    
                last_sido,
                last_sigungu,
                last_lawd_cd,
                error_message
            FROM collect_logs
            ORDER BY id DESC
            LIMIT 1
        """)

        row = cur.fetchone()

        if not row:
            return None

        return {
            "status": row[0],
            "started_at": row[1],
            "ended_at": row[2],
            "elapsed_seconds": row[3],

            "success_count": row[4],
            "fail_count": row[5],

            "sale_trade_count": row[6],
            "rent_trade_count": row[7],
            "presale_trade_count": row[8],

            "sale_list_count": row[9],
            "rent_list_count": row[10],
            "presale_list_count": row[11],

            "last_sido": row[12],
            "last_sigungu": row[13],
            "last_lawd_cd": row[14],
            "error_message": row[15],
        }

    except Exception as e:
        print("자동수집 로그 조회 실패:", e)
        return None

    finally:
        cur.close()
        release_pg_connection(conn)

def evaluate_price_stability(cv):

    if cv <= 2:
        return "매우 안정"

    elif cv <= 4:
        return "안정"

    elif cv <= 7:
        return "보통"

    elif cv <= 10:
        return "불안"

    else:
        return "매우 불안"

def calculate_recent_price_uncertainty(
    recent_prices,
    type_reference_price
):
    """
    최근 거래가격 평균의 95% 추정구간 계산

    - 가격 안정성 표시용 표준편차와 달리
      평균 추정구간에는 표본표준편차(stdev)를 사용한다.
    - 거래건수가 적으므로 정규분포 1.96 대신
      자유도별 t 임계값을 사용한다.
    """

    try:
        prices = [
            float(price)
            for price in recent_prices
            if float(price) > 0
        ]

        reference_price = float(
            type_reference_price or 0
        )

    except (TypeError, ValueError):
        prices = []
        reference_price = 0

    trade_count = len(prices)

    if trade_count == 0:
        return {
            "count": 0,
            "mean": 0,
            "sample_std": 0,
            "standard_error": 0,
            "margin_of_error": 0,
            "ci_low": 0,
            "ci_high": 0,
            "reference_position": "판단 불가",
            "interpretation": (
                "최근 3개월 거래가 없어 "
                "가격 추정구간을 계산하지 않았습니다."
            )
        }

    mean_price = sum(prices) / trade_count

    if trade_count < 2:
        return {
            "count": trade_count,
            "mean": round(mean_price),
            "sample_std": 0,
            "standard_error": 0,
            "margin_of_error": 0,
            "ci_low": round(mean_price),
            "ci_high": round(mean_price),
            "reference_position": "판단 제한",
            "interpretation": (
                "최근 거래가 1건뿐이므로 평균가격의 "
                "통계적 신뢰구간을 판단하기 어렵습니다."
            )
        }

    # 95% 양측 t 임계값
    # 자유도 1~30
    t_critical_95 = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
        11: 2.201,
        12: 2.179,
        13: 2.160,
        14: 2.145,
        15: 2.131,
        16: 2.120,
        17: 2.110,
        18: 2.101,
        19: 2.093,
        20: 2.086,
        21: 2.080,
        22: 2.074,
        23: 2.069,
        24: 2.064,
        25: 2.060,
        26: 2.056,
        27: 2.052,
        28: 2.048,
        29: 2.045,
        30: 2.042
    }

    degrees_of_freedom = trade_count - 1

    if degrees_of_freedom <= 30:
        t_value = t_critical_95[
            degrees_of_freedom
        ]
    else:
        # 표본이 충분히 크면 정규분포에 근접
        t_value = 1.96

    sample_std = statistics.stdev(prices)

    standard_error = (
        sample_std / math.sqrt(trade_count)
    )

    margin_of_error = (
        t_value * standard_error
    )

    ci_low = mean_price - margin_of_error
    ci_high = mean_price + margin_of_error

    if reference_price <= 0:
        reference_position = "비교 불가"
        interpretation = (
            "TYPE 대표가격이 없어 최근 가격 "
            "추정구간과 비교하지 않았습니다."
        )

    elif reference_price < ci_low:
        reference_position = "추정구간 아래"

        interpretation = (
            "TYPE 대표가격이 최근 3개월 평균가격의 "
            "95% 추정구간보다 낮습니다. "
            "최근 가격 수준이 장기 TYPE 기준보다 "
            "상승했을 가능성이 비교적 뚜렷합니다."
        )

    elif reference_price > ci_high:
        reference_position = "추정구간 위"

        interpretation = (
            "TYPE 대표가격이 최근 3개월 평균가격의 "
            "95% 추정구간보다 높습니다. "
            "최근 가격 수준이 장기 TYPE 기준보다 "
            "낮아졌을 가능성이 있습니다."
        )

    else:
        reference_position = "추정구간 안"

        interpretation = (
            "TYPE 대표가격이 최근 3개월 평균가격의 "
            "95% 추정구간 안에 있습니다. "
            "현재 차이는 거래가격 변동 범위 안에서 "
            "발생했을 가능성도 함께 고려해야 합니다."
        )

    return {
        "count": trade_count,
        "mean": round(mean_price),
        "sample_std": round(sample_std),
        "standard_error": round(standard_error),
        "margin_of_error": round(margin_of_error),
        "ci_low": round(ci_low),
        "ci_high": round(ci_high),
        "reference_position": reference_position,
        "interpretation": interpretation
    }

def calculate_floor_distribution(type_floors):
    """
    실제 거래층 분포를 기준으로
    Q1, 중앙값, Q3와 층별 그룹 건수를 계산한다.

    아직 층 가격 가중치에는 사용하지 않는다.
    """

    try:
        floors = sorted(
            int(float(floor))
            for floor in type_floors
            if int(float(floor)) > 0
        )
    except (TypeError, ValueError):
        floors = []

    count = len(floors)

    if count == 0:
        return {
            "count": 0,
            "q1": 0,
            "median": 0,
            "q3": 0,
            "low_count": 0,
            "middle_count": 0,
            "high_count": 0,
            "description": "층 거래자료가 없습니다."
        }

    if count == 1:
        only_floor = floors[0]

        return {
            "count": 1,
            "q1": only_floor,
            "median": only_floor,
            "q3": only_floor,
            "low_count": 1,
            "middle_count": 0,
            "high_count": 0,
            "description": "층 거래자료가 1건뿐이라 층 구간 판단이 제한적입니다."
        }

    # statistics.quantiles의 inclusive 방식 사용
    # 실제 최소·최대 관측 범위 안에서 분위수를 계산한다.
    q1, median, q3 = statistics.quantiles(
        floors,
        n=4,
        method="inclusive"
    )

    q1 = round(q1, 1)
    median = round(median, 1)
    q3 = round(q3, 1)

    low_count = sum(
        1 for floor in floors
        if floor <= q1
    )

    middle_count = sum(
        1 for floor in floors
        if q1 < floor < q3
    )

    high_count = sum(
        1 for floor in floors
        if floor >= q3
    )

    return {
        "count": count,
        "q1": q1,
        "median": median,
        "q3": q3,
        "low_count": low_count,
        "middle_count": middle_count,
        "high_count": high_count,
        "description": (
            f"실제 거래층 분포를 기준으로 "
            f"{q1}층 이하를 저층 후보, "
            f"{q1}층 초과부터 {q3}층 미만을 중층 후보, "
            f"{q3}층 이상을 고층 후보로 분류했습니다."
        )
    }

def calculate_floor_price_stats(prices):
    """
    층 그룹별 가격 통계 계산
    """

    clean_prices = []

    for price in prices:
        try:
            price_value = int(float(price))

            if price_value > 0:
                clean_prices.append(price_value)

        except (TypeError, ValueError):
            continue

    if not clean_prices:
        return {
            "count": 0,
            "average": 0,
            "median": 0,
            "trimmed_average": 0,
            "minimum": 0,
            "maximum": 0
        }

    sorted_group_prices = sorted(clean_prices)
    count = len(sorted_group_prices)
    average = round(sum(sorted_group_prices) / count)

    middle_index = count // 2

    if count % 2 == 1:
        median = sorted_group_prices[middle_index]
    else:
        median = round(
            (
                sorted_group_prices[middle_index - 1]
                + sorted_group_prices[middle_index]
            ) / 2
        )

    trimmed_average, _, _ = calculate_trimmed_average(
        sorted_group_prices
    )

    return {
        "count": count,
        "average": average,
        "median": median,
        "trimmed_average": trimmed_average,
        "minimum": sorted_group_prices[0],
        "maximum": sorted_group_prices[-1]
    }

def evaluate_floor_analysis_method(
    floor_price_pair_count,
    unique_floor_count
):
    """
    층·가격 자료량과 서로 다른 거래층 수를 기준으로
    적용 가능한 층 분석 방식을 결정한다.

    아직 실제 가격 보정에는 사용하지 않는다.
    """

    try:
        pair_count = int(floor_price_pair_count or 0)
        floor_count = int(unique_floor_count or 0)
    except (TypeError, ValueError):
        pair_count = 0
        floor_count = 0

    if pair_count >= 20 and floor_count >= 8:
        return {
            "method": "층 회귀 검증 가능",
            "level": "높음",
            "description": (
                "층·가격 거래자료와 서로 다른 거래층이 충분해 "
                "층수와 가격의 관계를 회귀분석으로 검증할 수 있습니다."
            )
        }

    if pair_count >= 10 and floor_count >= 5:
        return {
            "method": "층 그룹 분석",
            "level": "보통",
            "description": (
                "층 회귀를 안정적으로 적용하기에는 자료가 충분하지 않아 "
                "저층·중층·고층 그룹별 가격 차이만 비교합니다."
            )
        }

    if pair_count >= 5 and floor_count >= 3:
        return {
            "method": "층 참고 분석",
            "level": "낮음",
            "description": (
                "층별 가격 차이를 참고할 수는 있지만 표본이 적어 "
                "추천가격에는 직접 반영하지 않는 것이 안전합니다."
            )
        }

    return {
        "method": "층 분석 제외",
        "level": "매우 낮음",
        "description": (
            "층·가격 거래자료가 부족해 해당 단지 자체의 "
            "층 프리미엄을 산정하지 않습니다."
        )
    }

def calculate_floor_regression(type_floor_price_pairs):
    """
    층과 가격의 선형회귀 계산
    """

    if len(type_floor_price_pairs) < 10:
        return {
            "available": False,
            "reason": "거래가 부족"
        }

    floors = []
    prices = []

    for pair in type_floor_price_pairs:

        floor = pair["floor"]
        price = pair["price"]

        if floor > 0 and price > 0:
            floors.append(floor)
            prices.append(price)

    n = len(floors)

    if n < 10:
        return {
            "available": False,
            "reason": "유효거래 부족"
        }

    mean_x = statistics.mean(floors)
    mean_y = statistics.mean(prices)

    numerator = 0
    denominator = 0

    for x, y in zip(floors, prices):

        numerator += (x - mean_x) * (y - mean_y)
        denominator += (x - mean_x) ** 2

    if denominator == 0:
        return {
            "available": False,
            "reason": "층 분산 없음"
        }

    slope = numerator / denominator

    intercept = mean_y - slope * mean_x

    ss_total = sum(
        (y - mean_y) ** 2
        for y in prices
    )

    ss_residual = 0

    for x, y in zip(floors, prices):

        predict = intercept + slope * x

        ss_residual += (y - predict) ** 2

    if ss_total == 0:
        r2 = 0

    else:
        r2 = 1 - ss_residual / ss_total

    return {

        "available": True,

        "slope": slope,

        "intercept": intercept,

        "r2": round(r2,3),

        "count": n

    }

def calculate_market_weight(
    recent_3m_count,
    recent_change_rate,
    variation_rate
):
    """
    최근 3개월 시장가격의 임시 반영비율을 계산한다.

    주의:
    현재 규칙은 백테스트 전의 후보 규칙이며,
    실제 추천가에는 아직 적용하지 않는다.
    """

    weight = 0

    # 1. 최근 거래 표본 수
    if recent_3m_count >= 10:
        weight += 20
    elif recent_3m_count >= 6:
        weight += 15
    elif recent_3m_count >= 3:
        weight += 10

    # 2. TYPE 대표가격과 최근 평균의 차이
    change_rate = abs(float(recent_change_rate or 0))

    if change_rate >= 8:
        weight += 20
    elif change_rate >= 5:
        weight += 15
    elif change_rate >= 3:
        weight += 10

    # 3. 최근 거래가격의 안정성
    variation = float(variation_rate or 0)

    if recent_3m_count >= 2:
        if variation <= 3:
            weight += 15
        elif variation <= 6:
            weight += 10
        elif variation <= 10:
            weight += 5

    return min(weight, 50)

def find_backtest_candidates(
    region,
    analysis_date,
    min_decline_rate=-3.0,
    limit=20
):
    """
    백테스트용 하락 후보 단지 검색

    조건
    1. 분석기준일 이전 6개월에 거래 존재
    2. 서로 다른 거래월이 3개월 이상
    3. 첫 월평균 → 마지막 월평균이 일정 비율 이상 하락
    4. 분석기준일 이후 6개월에도 실제 거래 존재
    """

    from datetime import datetime

    print()
    print("======================================")
    print("      BACKTEST CANDIDATE SEARCH")
    print("======================================")

    print(f"지역 : {region}")
    print(f"분석기준일 : {analysis_date}")
    print(f"최소 하락률 : {min_decline_rate}%")

    analysis_dt = datetime.strptime(
        analysis_date,
        "%Y-%m-%d"
    )

    conn = get_pg_connection()
    cur = conn.cursor()

    try:

        sql = """
        WITH monthly AS (

            SELECT
                apt_name,
                size,

                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                ) AS trade_month,

                AVG(price)::numeric AS month_avg,

                COUNT(*) AS month_count

            FROM apt_sale_trades

            WHERE
                TRIM(CONCAT_WS(' ', region, sigungu)) = %s

                AND contract_date::date >= (
                    DATE_TRUNC(
                        'month',
                        %s::date
                    ) - INTERVAL '5 months'
                )

                AND contract_date::date <= %s::date

            GROUP BY
                apt_name,
                size,
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                )
        ),

        past_stats AS (

            SELECT
                apt_name,
                size,

                COUNT(*) AS month_count,

                SUM(month_count) AS past_trade_count,

                (
                    ARRAY_AGG(
                        ROUND(month_avg)
                        ORDER BY trade_month
                    )
                )[1] AS first_month_price,

                (
                    ARRAY_AGG(
                        ROUND(month_avg)
                        ORDER BY trade_month DESC
                    )
                )[1] AS last_month_price,

                ARRAY_AGG(
                    ROUND(month_avg)
                    ORDER BY trade_month
                ) AS monthly_prices

            FROM monthly

            GROUP BY
                apt_name,
                size

            HAVING COUNT(*) >= 3
        ),

        future_stats AS (

            SELECT
                apt_name,
                size,
                COUNT(*) AS future_trade_count,
                ROUND(AVG(price)) AS future_avg_price

            FROM apt_sale_trades

            WHERE
                TRIM(CONCAT_WS(' ', region, sigungu)) = %s

                AND contract_date::date > %s::date

                AND contract_date::date <= (
                    %s::date + INTERVAL '6 months'
                )

            GROUP BY
                apt_name,
                size
        )

        SELECT
            p.apt_name,
            p.size,
            p.month_count,
            p.past_trade_count,
            p.first_month_price,
            p.last_month_price,

            ROUND(
                (
                    p.last_month_price
                    - p.first_month_price
                )
                / NULLIF(
                    p.first_month_price,
                    0
                )
                * 100,
                2
            ) AS trend_rate,

            p.monthly_prices,

            f.future_trade_count,
            f.future_avg_price

        FROM past_stats p

        JOIN future_stats f
            ON f.apt_name = p.apt_name
            AND f.size = p.size

        WHERE
            (
                (
                    p.last_month_price
                    - p.first_month_price
                )
                / NULLIF(
                    p.first_month_price,
                    0
                )
                * 100
            ) <= %s

            AND f.future_trade_count >= 3

        ORDER BY
            trend_rate ASC,
            f.future_trade_count DESC

        LIMIT %s
        """

        cur.execute(
            sql,
            (
                region,
                analysis_date,
                analysis_date,

                region,
                analysis_date,
                analysis_date,

                min_decline_rate,
                limit
            )
        )

        rows = cur.fetchall()

        print()
        print(
            f"검색된 하락 후보 : "
            f"{len(rows)}개"
        )

        print()

        for i, row in enumerate(
            rows,
            start=1
        ):

            apt_name = row[0]
            size = row[1]
            month_count = row[2]
            past_trade_count = row[3]
            first_price = row[4]
            last_price = row[5]
            trend_rate = row[6]
            monthly_prices = row[7]
            future_trade_count = row[8]
            future_avg_price = row[9]

            print(
                f"[{i}] "
                f"{apt_name} | "
                f"{size}㎡"
            )

            print(
                f"    최근 월평균 : "
                f"{monthly_prices}"
            )

            print(
                f"    월수 : "
                f"{month_count}개월"
            )

            print(
                f"    과거 거래수 : "
                f"{past_trade_count}건"
            )

            print(
                f"    첫 월평균 : "
                f"{int(first_price):,}만원"
            )

            print(
                f"    마지막 월평균 : "
                f"{int(last_price):,}만원"
            )

            print(
                f"    보조추세 : "
                f"{float(trend_rate):+.2f}%"
            )

            print(
                f"    이후 6개월 거래 : "
                f"{future_trade_count}건"
            )

            print(
                f"    이후 6개월 단순평균 : "
                f"{int(future_avg_price):,}만원"
            )

            print()

        print("======================================")

        return rows

    except Exception as e:

        print(
            "❌ 백테스트 후보 검색 오류:",
            e
        )

        return []

    finally:

        cur.close()
        release_pg_connection(conn)

def find_rising_backtest_candidates(
    region,
    analysis_date,
    min_rise_rate=3.0,
    limit=20
):
    """
    백테스트용 상승 후보 단지 검색

    조건
    1. 분석기준일 이전 6개월에 거래 존재
    2. 서로 다른 거래월이 3개월 이상
    3. 첫 월평균 → 마지막 월평균이 일정 비율 이상 하락
    4. 분석기준일 이후 6개월에도 실제 거래 존재
    """

    from datetime import datetime

    print()
    print("======================================")
    print("      BACKTEST CANDIDATE SEARCH")
    print("======================================")

    print(f"지역 : {region}")
    print(f"분석기준일 : {analysis_date}")
    print(f"최소 상승률 : {min_rise_rate}%")

    analysis_dt = datetime.strptime(
        analysis_date,
        "%Y-%m-%d"
    )

    conn = get_pg_connection()
    cur = conn.cursor()

    try:

        sql = """
        WITH monthly AS (

            SELECT
                apt_name,
                size,

                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                ) AS trade_month,

                AVG(price)::numeric AS month_avg,

                COUNT(*) AS month_count

            FROM apt_sale_trades

            WHERE
                TRIM(CONCAT_WS(' ', region, sigungu)) = %s

                AND contract_date::date >= (
                    DATE_TRUNC(
                        'month',
                        %s::date
                    ) - INTERVAL '5 months'
                )

                AND contract_date::date <= %s::date

            GROUP BY
                apt_name,
                size,
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                )
        ),

        past_stats AS (

            SELECT
                apt_name,
                size,

                COUNT(*) AS month_count,

                SUM(month_count) AS past_trade_count,

                (
                    ARRAY_AGG(
                        ROUND(month_avg)
                        ORDER BY trade_month
                    )
                )[1] AS first_month_price,

                (
                    ARRAY_AGG(
                        ROUND(month_avg)
                        ORDER BY trade_month DESC
                    )
                )[1] AS last_month_price,

                ARRAY_AGG(
                    ROUND(month_avg)
                    ORDER BY trade_month
                ) AS monthly_prices

            FROM monthly

            GROUP BY
                apt_name,
                size

            HAVING COUNT(*) >= 3
        ),

        future_stats AS (

            SELECT
                apt_name,
                size,
                COUNT(*) AS future_trade_count,
                ROUND(AVG(price)) AS future_avg_price

            FROM apt_sale_trades

            WHERE
                TRIM(CONCAT_WS(' ', region, sigungu)) = %s

                AND contract_date::date > %s::date

                AND contract_date::date <= (
                    %s::date + INTERVAL '6 months'
                )

            GROUP BY
                apt_name,
                size
        )

        SELECT
            p.apt_name,
            p.size,
            p.month_count,
            p.past_trade_count,
            p.first_month_price,
            p.last_month_price,

            ROUND(
                (
                    p.last_month_price
                    - p.first_month_price
                )
                / NULLIF(
                    p.first_month_price,
                    0
                )
                * 100,
                2
            ) AS trend_rate,

            p.monthly_prices,

            f.future_trade_count,
            f.future_avg_price

        FROM past_stats p

        JOIN future_stats f
            ON f.apt_name = p.apt_name
            AND f.size = p.size

        WHERE
            (
                (
                    p.last_month_price
                    - p.first_month_price
                )
                / NULLIF(
                    p.first_month_price,
                    0
                )
                * 100
            ) >= %s

            AND f.future_trade_count >= 3

        ORDER BY
            trend_rate DESC,
            f.future_trade_count DESC

        LIMIT %s
        """

        cur.execute(
            sql,
            (
                region,
                analysis_date,
                analysis_date,

                region,
                analysis_date,
                analysis_date,

                min_rise_rate,
                limit
            )
        )

        rows = cur.fetchall()

        print()
        print(
            f"검색된 상승 후보 : "
            f"{len(rows)}개"
        )

        print()

        for i, row in enumerate(
            rows,
            start=1
        ):

            apt_name = row[0]
            size = row[1]
            month_count = row[2]
            past_trade_count = row[3]
            first_price = row[4]
            last_price = row[5]
            trend_rate = row[6]
            monthly_prices = row[7]
            future_trade_count = row[8]
            future_avg_price = row[9]

            print(
                f"[{i}] "
                f"{apt_name} | "
                f"{size}㎡"
            )

            print(
                f"    최근 월평균 : "
                f"{monthly_prices}"
            )

            print(
                f"    월수 : "
                f"{month_count}개월"
            )

            print(
                f"    과거 거래수 : "
                f"{past_trade_count}건"
            )

            print(
                f"    첫 월평균 : "
                f"{int(first_price):,}만원"
            )

            print(
                f"    마지막 월평균 : "
                f"{int(last_price):,}만원"
            )

            print(
                f"    보조추세 : "
                f"{float(trend_rate):+.2f}%"
            )

            print(
                f"    이후 6개월 거래 : "
                f"{future_trade_count}건"
            )

            print(
                f"    이후 6개월 단순평균 : "
                f"{int(future_avg_price):,}만원"
            )

            print()

        print("======================================")

        return rows

    except Exception as e:

        print(
            "❌ 백테스트 후보 검색 오류:",
            e
        )

        return []

    finally:

        cur.close()
        release_pg_connection(conn)

def check_backtest_data_range(
    region,
    analysis_date
):
    """
    백테스트 기준일 이전 데이터가
    DB에 실제로 존재하는지 검증
    """

    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                MIN(contract_date::date),
                MAX(contract_date::date),
                COUNT(*),
                COUNT(
                    DISTINCT TO_CHAR(
                        contract_date::date,
                        'YYYY-MM'
                    )
                )
            FROM apt_sale_trades
            WHERE TRIM(CONCAT_WS(' ', region, sigungu)) = %s
            AND contract_date::date <= %s::date
        """, (
            region,
            analysis_date
        ))

        row = cur.fetchone()

        print()
        print("========== 백테스트 DB 기간 검증 ==========")
        print(f"지역 : {region}")
        print(f"분석기준일 : {analysis_date}")

        if row:
            print(f"최초 거래일 : {row[0]}")
            print(f"최종 거래일 : {row[1]}")
            print(f"기준일 이전 전체 거래 : {row[2]:,}건")
            print(f"존재하는 거래월 수 : {row[3]}개월")

        print("=========================================")

        # 기준일 직전 12개월 월별 거래량
        cur.execute("""
            SELECT
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                ) AS month,
                COUNT(*) AS trade_count
            FROM apt_sale_trades
            WHERE TRIM(CONCAT_WS(' ', region, sigungu)) = %s
            AND contract_date::date <= %s::date
            AND contract_date::date >= (
                    %s::date - INTERVAL '12 months'
            )
            GROUP BY
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                )
            ORDER BY month
        """, (
            region,
            analysis_date,
            analysis_date
        ))

        rows = cur.fetchall()

        print()
        print("========== 기준일 이전 월별 거래량 ==========")

        for month, count in rows:
            print(
                f"{month} : "
                f"{count:,}건"
            )

        print("==========================================")

    finally:
        cur.close()
        release_pg_connection(conn)

def check_sale_db_full_range():

    conn = get_pg_connection()
    cur = conn.cursor()

    try:

        cur.execute("""
            SELECT
                MIN(contract_date::date),
                MAX(contract_date::date),
                COUNT(*),
                COUNT(
                    DISTINCT TO_CHAR(
                        contract_date::date,
                        'YYYY-MM'
                    )
                )
            FROM apt_sale_trades
        """)

        row = cur.fetchone()

        print()
        print("========== 매매 DB 전체 기간 검증 ==========")

        print(f"최초 거래일 : {row[0]}")
        print(f"최종 거래일 : {row[1]}")
        print(f"전체 거래건수 : {row[2]:,}건")
        print(f"전체 거래월 수 : {row[3]}개월")

        print("=========================================")

        cur.execute("""
            SELECT
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                ) AS month,
                COUNT(*)
            FROM apt_sale_trades
            GROUP BY
                TO_CHAR(
                    contract_date::date,
                    'YYYY-MM'
                )
            ORDER BY month
        """)

        rows = cur.fetchall()

        print()
        print("========== 월별 전체 거래건수 ==========")

        for month, count in rows:
            print(
                f"{month} : "
                f"{count:,}건"
            )

        print("======================================")

    finally:
        cur.close()
        release_pg_connection(conn)

def find_backtest_candidates_multi_region(
    regions,
    analysis_date,
    min_decline_rate=-3.0,
    limit_per_region=5
):
    """
    여러 지역에서 하락 백테스트 후보를 순차 검색한다.
    기존 find_backtest_candidates()는 수정하지 않는다.
    """

    print()
    print("================================================")
    print("       MULTI REGION BACKTEST SEARCH")
    print("================================================")
    print(f"분석기준일 : {analysis_date}")
    print(f"최소 하락률 : {min_decline_rate}%")
    print(f"검색지역 수 : {len(regions)}개")
    print()

    all_candidates = []

    for region in regions:

        print()
        print("################################################")
        print(f"지역 검색 : {region}")
        print("################################################")

        try:
            rows = find_backtest_candidates(
                region=region,
                analysis_date=analysis_date,
                min_decline_rate=min_decline_rate,
                limit=limit_per_region
            )

            if not rows:
                continue

            for row in rows:
                all_candidates.append({
                    "region": region,
                    "row": row
                })

        except Exception as e:
            print(
                f"❌ 지역 검색 실패 : "
                f"{region} | {e}"
            )

    print()
    print("================================================")
    print("             전체 후보 검색 결과")
    print("================================================")

    print(
        f"전체 검색 후보 : "
        f"{len(all_candidates)}개"
    )

    region_counts = {}

    for candidate in all_candidates:
        region = candidate["region"]

        region_counts[region] = (
            region_counts.get(region, 0) + 1
        )

    for region, count in region_counts.items():
        print(
            f"{region} : "
            f"{count}개"
        )

    print("================================================")

    return all_candidates

def run_backtest(
    region,
    apt_name,
    size,
    analysis_date
):
    """
    가격엔진 백테스트
    """

    print()
    print("======================================")
    print("        PRICE ENGINE BACKTEST")
    print("======================================")

    print(f"지역 : {region}")
    print(f"단지 : {apt_name}")
    print(f"면적 : {size}")
    print(f"분석기준일 : {analysis_date}")

    # -----------------------------------------
    # 1. 과거 시점 기준 엔진 실행
    # -----------------------------------------
    result = future_prediction(
        region=region,
        apt_name=apt_name,
        size=size,
        reference_date=analysis_date
    )

    print()
    print("========== 엔진 결과 ==========")

    verify = result.get("시장가중검증", {})

    print(
        f"TYPE대표가격 : "
        f"{verify.get('TYPE대표가격', 0):,}만원"
    )

    print(
        f"최근3개월평균가격 : "
        f"{verify.get('최근3개월평균가격', 0):,}만원"
    )

    print(
        f"최근3개월거래건수 : "
        f"{verify.get('최근3개월거래건수', 0)}건"
    )

    print(
        f"최근시장후보 : "
        f"{verify.get('시장반영기준가격후보', 0):,}만원"
    )

    print(
        f"TYPE반영 : "
        f"{verify.get('TYPE반영비율', 0)}%"
    )

    print(
        f"시장반영 : "
        f"{verify.get('최근시장반영비율', 0)}%"
    )

    print("==============================")

    # -----------------------------------------
    # 2. 분석기준일 이후 6개월 실제 거래 조회
    # -----------------------------------------
    print()
    print("★★★★★ 미래 6개월 백테스트 시작 ★★★★★")

    from datetime import datetime
    
    backtest_date = datetime.strptime(
        analysis_date,
        "%Y-%m-%d"
    )

    # 분석기준일 기준 6개월 후 날짜 계산
    future_year = backtest_date.year
    future_month = backtest_date.month + 6

    if future_month > 12:
        future_year += 1
        future_month -= 12

    future_end_date = backtest_date.replace(
        year=future_year,
        month=future_month
    )

    # future_prediction이 실제로 사용한 면적
    matched_size = result.get(
        "매칭면적",
        size
    )

    print(f"백테스트 매칭면적 : {matched_size}")

    # ✅ 백테스트 전용 로컬 과거 DB 사용
    future_rows = get_backtest_sale_trades(
        region=region,
        apt_name=apt_name,
        size=matched_size
    )

    print(
        f"백테스트 DB 전체 조회건수 : "
        f"{len(future_rows)}건"
    )

    future_trades = []

    for row in future_rows:

        try:
            raw_date = row.get("date")
            raw_price = row.get("price")
            raw_floor = row.get("floor")

            # DB 날짜형 처리
            if isinstance(raw_date, datetime):
                trade_date = raw_date

            elif hasattr(raw_date, "year"):
                trade_date = datetime(
                    raw_date.year,
                    raw_date.month,
                    raw_date.day
                )

            else:
                trade_date = datetime.strptime(
                    str(raw_date)[:10],
                    "%Y-%m-%d"
                )

            # 기준일 초과 ~ 6개월 이하
            if not (
                backtest_date
                < trade_date
                <= future_end_date
            ):
                continue

            if raw_price is None:
                continue

            if isinstance(raw_price, str):
                raw_price = (
                    raw_price
                    .replace(",", "")
                    .strip()
                )

            price = int(float(raw_price))

            if price <= 0:
                continue

            future_trades.append({
                "date": trade_date.strftime(
                    "%Y-%m-%d"
                ),
                "price": price,
                "floor": raw_floor
            })

        except Exception as e:
            print(
                "⚠️ 미래거래 행 처리 오류:",
                row,
                e
            )
            continue

    future_trades.sort(
        key=lambda x: x["date"]
    )

    # -----------------------------------------
    # 3. 미래 실제 거래 출력
    # -----------------------------------------
    print()
    print(
        "========== 실제 미래 6개월 거래 =========="
    )

    print(
        f"검증기간 : "
        f"{backtest_date.strftime('%Y-%m-%d')}"
        f" 이후 ~ "
        f"{future_end_date.strftime('%Y-%m-%d')}"
    )

    print(
        f"미래 거래건수 : "
        f"{len(future_trades)}건"
    )

    for trade in future_trades:
        print(
            f"{trade['date']} | "
            f"{trade['price']:,}만원 | "
            f"{trade['floor']}층"
        )

    # -----------------------------------------
    # 4. 미래 6개월 실제 평균
    # -----------------------------------------
    if future_trades:

        future_prices = [
            trade["price"]
            for trade in future_trades
        ]

        # 1. 단순 평균
        actual_future_avg = round(
            sum(future_prices)
            / len(future_prices)
        )

        # 2. 중앙값
        sorted_future_prices = sorted(future_prices)

        future_count = len(sorted_future_prices)
        middle_index = future_count // 2

        if future_count % 2 == 1:
            actual_future_median = (
                sorted_future_prices[middle_index]
            )
        else:
            actual_future_median = round(
                (
                    sorted_future_prices[middle_index - 1]
                    + sorted_future_prices[middle_index]
                ) / 2
            )

        # 3. 기존 엔진과 동일한 절사평균 함수 사용
        (
            actual_future_trimmed_avg,
            future_trim_count,
            future_trimmed_count
        ) = calculate_trimmed_average(
            sorted_future_prices
        )

    else:
        actual_future_avg = 0
        actual_future_median = 0
        actual_future_trimmed_avg = 0
        future_trim_count = 0
        future_trimmed_count = 0

    candidate_price = verify.get(
        "시장반영기준가격후보",
        0
    )
    # -----------------------------------------
    # ✅ 6개월 미래예측 중심가격
    # -----------------------------------------

    future_predicted_center = result.get(
        "6개월예상중심가",
        0
    )

    try:
        future_predicted_center = round(
            float(future_predicted_center or 0)
        )
    except (ValueError, TypeError):
        future_predicted_center = 0

    # -----------------------------------------
    # 5. 엔진 가격 vs 실제 미래가격
    # -----------------------------------------
    print()
    print("========== 미래가격 통계 ==========")

    print(
        f"미래 거래건수 : "
        f"{len(future_trades)}건"
    )

    print(
        f"단순평균 : "
        f"{actual_future_avg:,}만원"
    )

    print(
        f"중앙값 : "
        f"{actual_future_median:,}만원"
    )

    print(
        f"절사평균 : "
        f"{actual_future_trimmed_avg:,}만원"
    )

    print(
        f"절사건수 : "
        f"{future_trim_count}건"
    )

    print(
        f"절사 후 사용거래 : "
        f"{future_trimmed_count}건"
    )

    print("===================================")

    print()
    print(
        "========== 백테스트 결과 =========="
    )

    print(
        f"엔진 후보가격 : "
        f"{candidate_price:,}만원"
    )

    print(
        f"실제 6개월 평균가격 : "
        f"{actual_future_avg:,}만원"
    )

    if candidate_price > 0:

        # 평균 기준
        if actual_future_avg > 0:
            avg_error_rate = round(
                (
                    candidate_price
                    - actual_future_avg
                )
                / actual_future_avg
                * 100,
                2
            )
        else:
            avg_error_rate = 0

        # 중앙값 기준
        if actual_future_median > 0:
            median_error_rate = round(
                (
                    candidate_price
                    - actual_future_median
                )
                / actual_future_median
                * 100,
                2
            )
        else:
            median_error_rate = 0

        # 절사평균 기준
        if actual_future_trimmed_avg > 0:
            trimmed_error_rate = round(
                (
                    candidate_price
                    - actual_future_trimmed_avg
                )
                / actual_future_trimmed_avg
                * 100,
                2
            )
        else:
            trimmed_error_rate = 0

        print(
            f"평균 기준 오차율 : "
            f"{avg_error_rate:+.2f}%"
        )

        print(
            f"중앙값 기준 오차율 : "
            f"{median_error_rate:+.2f}%"
        )

        print(
            f"절사평균 기준 오차율 : "
            f"{trimmed_error_rate:+.2f}%"
        )
        # -----------------------------------------
        # ✅ 백테스트 정확도 판정
        # 절사평균 기준 절대 오차율 사용
        # -----------------------------------------

        backtest_abs_error = abs(
            trimmed_error_rate
        )

        if backtest_abs_error <= 3:
            backtest_grade = "매우 우수"

        elif backtest_abs_error <= 5:
            backtest_grade = "우수"

        elif backtest_abs_error <= 8:
            backtest_grade = "보통"

        elif backtest_abs_error <= 10:
            backtest_grade = "주의"

        else:
            backtest_grade = "개선 필요"


        # 예측 방향 확인
        if trimmed_error_rate < 0:
            backtest_direction = "저평가"

        elif trimmed_error_rate > 0:
            backtest_direction = "고평가"

        else:
            backtest_direction = "정확"


        print()
        print("========== 백테스트 판정 ==========")

        print(
            f"대표 오차율 : "
            f"{backtest_abs_error:.2f}%"
        )

        print(
            f"정확도 등급 : "
            f"{backtest_grade}"
        )

        print(
            f"엔진 방향 : "
            f"{backtest_direction}"
        )

        print("=================================")

    else:

        print("백테스트 오차율 : 계산 불가")
        print("가격 오차 : 계산 불가")
        print("가격 오차율 : 계산 불가")

    print("===================================")

    # =================================================
    # ✅ 6개월 미래예측 엔진 백테스트
    # =================================================

    print()
    print("========== 6개월 미래예측 백테스트 ==========")

    print(
        f"현재 기준가격 : "
        f"{candidate_price:,}만원"
    )

    print(
        f"6개월 예상중심가 : "
        f"{future_predicted_center:,}만원"
    )

    print(
        f"미래 실제 절사평균 : "
        f"{actual_future_trimmed_avg:,}만원"
    )


    # 현재 기준가격 오차율
    if (
        candidate_price > 0
        and actual_future_trimmed_avg > 0
    ):
        current_price_error_rate = round(
            (
                candidate_price
                - actual_future_trimmed_avg
            )
            / actual_future_trimmed_avg
            * 100,
            2
        )
    else:
        current_price_error_rate = 0


    # 6개월 예상중심가 오차율
    if (
        future_predicted_center > 0
        and actual_future_trimmed_avg > 0
    ):
        future_prediction_error_rate = round(
            (
                future_predicted_center
                - actual_future_trimmed_avg
            )
            / actual_future_trimmed_avg
            * 100,
            2
        )
    else:
        future_prediction_error_rate = 0


    print(
        f"현재가격 기준 오차율 : "
        f"{current_price_error_rate:+.2f}%"
    )

    print(
        f"6개월예측 오차율 : "
        f"{future_prediction_error_rate:+.2f}%"
    )


    # -----------------------------------------
    # 미래예측 정확도 등급
    # -----------------------------------------

    future_abs_error = abs(
        future_prediction_error_rate
    )

    if (
        future_predicted_center <= 0
        or actual_future_trimmed_avg <= 0
    ):
        future_grade = "계산 불가"

    elif future_abs_error <= 3:
        future_grade = "매우 우수"

    elif future_abs_error <= 5:
        future_grade = "우수"

    elif future_abs_error <= 8:
        future_grade = "보통"

    elif future_abs_error <= 10:
        future_grade = "주의"

    else:
        future_grade = "개선 필요"


    # -----------------------------------------
    # 미래예측 방향
    # -----------------------------------------

    if future_predicted_center <= 0:
        future_direction = "계산 불가"

    elif future_prediction_error_rate < 0:
        future_direction = "저평가"

    elif future_prediction_error_rate > 0:
        future_direction = "고평가"

    else:
        future_direction = "정확"


    print(
        f"미래예측 정확도 : "
        f"{future_grade}"
    )

    print(
        f"미래예측 방향 : "
        f"{future_direction}"
    )

    print("========================================")

    # =================================================
    # ✅ 일괄 백테스트용 요약 결과 저장
    # =================================================

    result["백테스트요약"] = {
        "분석기준일": analysis_date,
        "현재기준가격": candidate_price,
        "6개월예상중심가": future_predicted_center,
        "미래실제절사평균": actual_future_trimmed_avg,

        "현재가격오차율": current_price_error_rate,
        "6개월예측오차율": future_prediction_error_rate,

        "현재가격절대오차율": abs(
            current_price_error_rate
        ),

        "6개월예측절대오차율": abs(
            future_prediction_error_rate
        ),

        "미래거래건수": len(future_trades),
            "추세신뢰도": result.get(
            "추세신뢰도",
            "미확인"
        )
    }

    
    # ⚠️ 반드시 맨 마지막
    return result

def run_batch_backtest(cases):
    """
    여러 단지/면적/기준일을 한 번에 백테스트하고
    현재가격 오차와 미래예측 오차를 비교한다.
    """

    results = []

    print()
    print("=" * 70)
    print("🚀 BATCH BACKTEST 시작")
    print(f"테스트 케이스 : {len(cases)}개")
    print("=" * 70)

    for idx, case in enumerate(cases, start=1):

        region = case["region"]
        apt_name = case["apt_name"]
        size = case["size"]
        analysis_date = case["analysis_date"]

        print()
        print(
            f"[{idx}/{len(cases)}] "
            f"{region} / {apt_name} / {size}㎡ / {analysis_date}"
        )

        try:
            result = run_backtest(
                region=region,
                apt_name=apt_name,
                size=size,
                analysis_date=analysis_date
            )

            if not isinstance(result, dict):
                print("⚠️ 백테스트 결과 없음")
                continue

            summary = result.get("백테스트요약", {})

            current_error = summary.get("현재가격오차율")
            future_error = summary.get("6개월예측오차율")

            # ✅ 계산 가능한 백테스트만 종합 통계에 포함
            current_price = summary.get("현재기준가격", 0)
            actual_future_price = summary.get("미래실제절사평균", 0)
            future_trade_count = summary.get("미래거래건수", 0)

            if (
                not current_price
                or not actual_future_price
                or future_trade_count <= 0
            ):
                print("⚠️ 과거 데이터 부족으로 종합 통계에서 제외")
                continue

            if current_error is None or future_error is None:
                print("⚠️ 오차율 계산 불가")
                continue

            monthly_volume = result.get(
                "디버그월별거래량",
                []
            )

            previous_3m_count = 0

            if isinstance(monthly_volume, list) and len(monthly_volume) >= 6:

                previous_3m_count = sum(
                    int(v.get("count", 0) or 0)
                    for v in monthly_volume[-6:-3]
                )

            results.append({
                "region": region,
                "apt_name": apt_name,
                "size": size,
                "analysis_date": analysis_date,

                "current_error": current_error,
                "future_error": future_error,

                "current_price": summary.get(
                    "현재기준가격",
                    0
                ),

                "actual_future_price": summary.get(
                    "미래실제절사평균",
                    0
                ),

                "expected_rate": result.get(
                    "6개월예상상승률",
                    0
                ),

                # ✅ 과거 가격 흐름
                "rise_rate": result.get(
                    "거래상승률",
                    0
                ),

                "recent_3m_avg": result.get(
                    "시장가중검증",
                    {}
                ).get(
                    "최근3개월평균가격",
                    0
                ),

                "type_reference_price": result.get(
                    "시장가중검증",
                    {}
                ).get(
                    "TYPE대표가격",
                    0
                ),

                "type_recent_gap_rate": result.get(
                    "시장가중검증",
                    {}
                ).get(
                    "TYPE대비최근가격변화율",
                    0
                ),

                # ✅ 미래예측 특성 분석용
                "expected_rate": result.get("6개월예상상승률", 0),

                "recent_price_variation_rate": result.get(
                    "시장가중검증",
                    {}
                ).get(
                    "최근가격변동률",
                    0
                ),


                # ✅ 미래예측 특성
                "trend": result.get(
                    "거래추세",
                    "미확인"
                ),

                "trend_confidence": result.get(
                    "추세신뢰도",
                    "미확인"
                ),

                "recent_3m_count": result.get(
                    "최근3개월거래량",
                    0
                ),

                "previous_3m_count": previous_3m_count,
                "trend": result.get("거래추세", "미확인"),

                # ✅ 상승둔화 V3 실제 엔진 신호
                "uptrend_slowdown_signal": result.get(
                    "상승장둔화신호",
                    False
                )
            })

        except Exception as e:
            print(
                f"❌ 백테스트 실패: "
                f"{apt_name} / {analysis_date} / {e}"
            )

    if not results:
        print()
        print("❌ 유효한 백테스트 결과가 없습니다.")
        return

    current_mae = sum(
        abs(r["current_error"])
        for r in results
    ) / len(results)

    future_mae = sum(
        abs(r["future_error"])
        for r in results
    ) / len(results)

    # =========================================================
    # ✅ 가상 정책 검증
    # 추세신뢰도가 낮으면 미래예측 보정을 적용하지 않고
    # 현재 기준가격을 그대로 사용한다고 가정
    # =========================================================

    virtual_errors = []

    for r in results:

        confidence = str(
            r.get("trend_confidence", "")
        ).replace(" ", "").strip()

        # 신뢰도가 낮으면 미래예측 보정 미적용
        if confidence == "낮음":
            virtual_error = abs(
                r["current_error"]
            )

        else:
            virtual_error = abs(
                r["future_error"]
            )

        virtual_errors.append(
            virtual_error
        )

    virtual_mae = sum(
        virtual_errors
    ) / len(virtual_errors)

    # =========================================================
    # ✅ 가상 정책 2
    # 기존 6개월 예상변동률의 50%만 적용
    # =========================================================

    half_rate_errors = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        if current_price <= 0 or actual_future_price <= 0:
            continue

        # ✅ 기존 예상변동률의 절반만 적용
        half_expected_rate = expected_rate * 0.5

        # ✅ 가상 6개월 예상가격
        half_predicted_price = (
            current_price *
            (1 + half_expected_rate / 100)
        )

        # ✅ 실제 미래가격 대비 절대 오차율
        half_error_rate = abs(
            (
                half_predicted_price
                - actual_future_price
            )
            / actual_future_price
            * 100
        )

        half_rate_errors.append(
            half_error_rate
        )

    half_rate_mae = (
        sum(half_rate_errors)
        / len(half_rate_errors)
        if half_rate_errors
        else 0
    )

    # =========================================================
    # ✅ 가상 정책 3
    # 미래예측 보정강도별 MAE 비교
    # 0% / 25% / 50% / 75% / 100%
    # =========================================================

    adjustment_strengths = [
        0.00,
        0.25,
        0.50,
        0.75,
        1.00
    ]

    strength_results = {}

    for strength in adjustment_strengths:

        errors = []

        for r in results:

            current_price = float(
                r.get("current_price", 0) or 0
            )

            actual_future_price = float(
                r.get("actual_future_price", 0) or 0
            )

            expected_rate = float(
                r.get("expected_rate", 0) or 0
            )

            if (
                current_price <= 0
                or actual_future_price <= 0
            ):
                continue

            # ✅ 보정강도 적용
            adjusted_rate = (
                expected_rate * strength
            )

            # ✅ 가상 미래예측 가격
            virtual_predicted_price = (
                current_price
                * (1 + adjusted_rate / 100)
            )

            # ✅ 실제 미래가격 대비 절대 오차율
            error_rate = abs(
                (
                    virtual_predicted_price
                    - actual_future_price
                )
                / actual_future_price
                * 100
            )

            errors.append(error_rate)

        mae = (
            sum(errors) / len(errors)
            if errors
            else 0
        )

        strength_results[strength] = mae

    improved_count = sum(
        1
        for r in results
        if abs(r["future_error"]) < abs(r["current_error"])
    )

    worsened_count = sum(
        1
        for r in results
        if abs(r["future_error"]) > abs(r["current_error"])
    )

    same_count = sum(
        1
        for r in results
        if abs(r["future_error"]) == abs(r["current_error"])
    )

    

    # ✅ 검증용 단순 시장국면 분류
    # =========================================================
    # ✅ 시장국면별 백테스트 검증 V2
    # 실제 과거 거래상승률 기준
    # =========================================================

    market_groups = {
        "상승": [],
        "보합": [],
        "하락": []
    }

    for r in results:

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )
        # ✅ 시장국면 분류
        if rise_rate >= 1.0:
            market_phase = "상승"

        elif rise_rate <= -1.0:
            market_phase = "하락"

        else:
            market_phase = "보합"

        # ✅ 개별 결과에도 시장국면 저장
        r["market_phase"] = market_phase

        # ✅ 해당 시장국면 그룹에 추가
        market_groups[
            market_phase
        ].append(r)

    # =========================================================
    # ✅ 시장국면별 결과 출력
    # =========================================================        
    print()
    print("=" * 70)
    print("📊 시장국면별 백테스트 비교")
    print("=" * 70)

    for phase in [
        "상승",
        "보합",
        "하락"
    ]:

        group = market_groups[phase]

        if not group:
            print()
            print(
                f"[{phase}] 테스트 없음"
            )
            continue

        count = len(group)

        current_group_mae = sum(
            abs(r["current_error"])
            for r in group
        ) / count

        future_group_mae = sum(
            abs(r["future_error"])
            for r in group
        ) / count

        improved = sum(
            1
            for r in group
            if abs(r["future_error"])
            < abs(r["current_error"])
        )

        worsened = sum(
            1
            for r in group
            if abs(r["future_error"])
            > abs(r["current_error"])
        )

        same = (
            count
            - improved
            - worsened
        )

        diff = (
            future_group_mae
            - current_group_mae
        )

        print()
        print(f"[{phase}]")
        print(
            f"테스트건수 : "
            f"{count}건"
        )

        print(
            f"현재가격 MAE : "
            f"{current_group_mae:.2f}%"
        )

        print(
            f"미래예측 MAE : "
            f"{future_group_mae:.2f}%"
        )

        print(
            f"개선 / 악화 / 동일 : "
            f"{improved} / "
            f"{worsened} / "
            f"{same}"
        )

        if diff < 0:
            print(
                f"✅ 미래예측 효과 : "
                f"{abs(diff):.2f}%p 개선"
            )

        elif diff > 0:
            print(
                f"⚠️ 미래예측 효과 : "
                f"{diff:.2f}%p 악화"
            )

        else:
            print(
                "➖ 미래예측 효과 : 동일"
            )

    print("=" * 70)

    # =========================================================
    # ✅ 보합장 상세 백테스트 검증
    # 엔진 수정 없음
    # =========================================================

    print()
    print("=" * 110)
    print("➖ 보합장 개별 백테스트 상세 검증")
    print("=" * 110)

    flat_results = [
        r
        for r in results
        if r.get("market_phase") == "보합"
    ]

    flat_improved = 0
    flat_worsened = 0
    flat_same = 0

    for r in flat_results:

        current_error = abs(
            float(
                r.get("current_error", 0) or 0
            )
        )

        future_error = abs(
            float(
                r.get("future_error", 0) or 0
            )
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        price_variation = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        previous_count = int(
            r.get(
                "previous_3m_count",
                0
            ) or 0
        )

        recent_count = int(
            r.get(
                "recent_3m_count",
                0
            ) or 0
        )

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        diff = (
            future_error
            - current_error
        )

        if diff < -0.001:

            judgment = (
                f"✅ 개선 {abs(diff):.2f}%p"
            )

            flat_improved += 1

        elif diff > 0.001:

            judgment = (
                f"⚠️ 악화 {diff:.2f}%p"
            )

            flat_worsened += 1

        else:

            judgment = "➖ 동일"
            flat_same += 1

        print(
            f'{r.get("apt_name", "")} | '
            f'{r.get("analysis_date", "")} | '
            f'현재 {current_error:.2f}% | '
            f'미래 {future_error:.2f}% | '
            f'{judgment} | '
            f'거래상승률 {rise_rate:+.2f}% | '
            f'예상변동 {expected_rate:+.2f}% | '
            f'가격변동 {price_variation:+.2f}% | '
            f'거래 {previous_count}→{recent_count}건 | '
            f'거래비 {volume_ratio:.2f}배 | '
            f'신뢰도 {r.get("trend_confidence", "미확인")}'
        )

    print("-" * 110)

    print(
        f"보합장 테스트 : "
        f"{len(flat_results)}건"
    )

    print(
        f"개선 / 악화 / 동일 : "
        f"{flat_improved} / "
        f"{flat_worsened} / "
        f"{flat_same}"
    )

    if flat_results:

        flat_current_mae = (
            sum(
                abs(
                    float(
                        r.get(
                            "current_error",
                            0
                        ) or 0
                    )
                )
                for r in flat_results
            )
            / len(flat_results)
        )

        flat_future_mae = (
            sum(
                abs(
                    float(
                        r.get(
                            "future_error",
                            0
                        ) or 0
                    )
                )
                for r in flat_results
            )
            / len(flat_results)
        )

        print(
            f"보합장 현재가격 MAE : "
            f"{flat_current_mae:.2f}%"
        )

        print(
            f"보합장 미래예측 MAE : "
            f"{flat_future_mae:.2f}%"
        )

        print(
            f"보합장 개선효과 : "
            f"{flat_current_mae - flat_future_mae:+.2f}%p"
        )

    print("=" * 110)

    # =========================================================
    # ✅ 미래예측 엔진 최종 통합 안정성 검증
    # =========================================================

    print()
    print("=" * 110)
    print("🧪 미래예측 엔진 최종 통합 안정성 검증")
    print("=" * 110)

    valid_results = []

    for r in results:

        current_error = abs(
            float(r.get("current_error", 0) or 0)
        )

        future_error = abs(
            float(r.get("future_error", 0) or 0)
        )

        # 오류값이 정상적으로 존재하는 결과만 사용
        if (
            current_error >= 0
            and future_error >= 0
        ):
            valid_results.append(r)

    total_count = len(valid_results)

    if total_count == 0:

        print("검증 가능한 데이터가 없습니다.")

    else:

        # -----------------------------------------------------
        # 1. 전체 MAE
        # -----------------------------------------------------

        current_mae = (
            sum(
                abs(float(r.get("current_error", 0) or 0))
                for r in valid_results
            )
            / total_count
        )

        future_mae = (
            sum(
                abs(float(r.get("future_error", 0) or 0))
                for r in valid_results
            )
            / total_count
        )

        # -----------------------------------------------------
        # 2. 개선 / 악화 / 동일
        # -----------------------------------------------------

        improved = 0
        worsened = 0
        same = 0

        # -----------------------------------------------------
        # 3. 큰 오차 / 심각한 오차
        # -----------------------------------------------------

        error_10_count = 0
        error_15_count = 0
        error_20_count = 0

        # -----------------------------------------------------
        # 4. 방향 적중률
        #
        # current_price → actual_future_price 실제 방향
        # expected_rate → 예측 방향
        #
        # ±1% 이하는 보합으로 판정
        # -----------------------------------------------------

        direction_total = 0
        direction_correct = 0

        phase_stats = {
            "상승": {
                "count": 0,
                "current_sum": 0.0,
                "future_sum": 0.0
            },
            "보합": {
                "count": 0,
                "current_sum": 0.0,
                "future_sum": 0.0
            },
            "하락": {
                "count": 0,
                "current_sum": 0.0,
                "future_sum": 0.0
            }
        }

        for r in valid_results:

            current_error = abs(
                float(r.get("current_error", 0) or 0)
            )

            future_error = abs(
                float(r.get("future_error", 0) or 0)
            )

            # ---------------------------------------------
            # 개선 / 악화
            # ---------------------------------------------

            diff = future_error - current_error

            if diff < -0.001:
                improved += 1

            elif diff > 0.001:
                worsened += 1

            else:
                same += 1

            # ---------------------------------------------
            # 대형 오차
            # ---------------------------------------------

            if future_error >= 10:
                error_10_count += 1

            if future_error >= 15:
                error_15_count += 1

            if future_error >= 20:
                error_20_count += 1

            # ---------------------------------------------
            # 시장국면별 MAE
            # ---------------------------------------------

            phase = r.get(
                "market_phase",
                "미확인"
            )

            if phase in phase_stats:

                phase_stats[phase]["count"] += 1

                phase_stats[phase]["current_sum"] += (
                    current_error
                )

                phase_stats[phase]["future_sum"] += (
                    future_error
                )

            # ---------------------------------------------
            # 방향 적중률
            # ---------------------------------------------

            current_price = float(
                r.get("current_price", 0) or 0
            )

            actual_future_price = float(
                r.get("actual_future_price", 0) or 0
            )

            expected_rate = float(
                r.get("expected_rate", 0) or 0
            )

            if (
                current_price > 0
                and actual_future_price > 0
            ):

                actual_change_rate = (
                    (
                        actual_future_price
                        - current_price
                    )
                    / current_price
                    * 100
                )

                # 실제 방향
                if actual_change_rate > 1:
                    actual_direction = "상승"

                elif actual_change_rate < -1:
                    actual_direction = "하락"

                else:
                    actual_direction = "보합"

                # 예측 방향
                if expected_rate > 1:
                    predicted_direction = "상승"

                elif expected_rate < -1:
                    predicted_direction = "하락"

                else:
                    predicted_direction = "보합"

                direction_total += 1

                if (
                    actual_direction
                    == predicted_direction
                ):
                    direction_correct += 1

        # -----------------------------------------------------
        # 결과 출력
        # -----------------------------------------------------

        print(
            f"전체 유효 테스트 : "
            f"{total_count}건"
        )

        print()

        print(
            f"현재가격 MAE : "
            f"{current_mae:.2f}%"
        )

        print(
            f"미래예측 MAE : "
            f"{future_mae:.2f}%"
        )

        print(
            f"미래예측 개선효과 : "
            f"{current_mae - future_mae:+.2f}%p"
        )

        print()

        print(
            f"개선 / 악화 / 동일 : "
            f"{improved} / "
            f"{worsened} / "
            f"{same}"
        )

        print()

        print(
            f"10% 이상 오차 : "
            f"{error_10_count}건 "
            f"({error_10_count / total_count * 100:.1f}%)"
        )

        print(
            f"15% 이상 오차 : "
            f"{error_15_count}건 "
            f"({error_15_count / total_count * 100:.1f}%)"
        )

        print(
            f"20% 이상 오차 : "
            f"{error_20_count}건 "
            f"({error_20_count / total_count * 100:.1f}%)"
        )

        print()

        # -----------------------------------------------------
        # 방향 적중률
        # -----------------------------------------------------

        if direction_total > 0:

            direction_accuracy = (
                direction_correct
                / direction_total
                * 100
            )

            print(
                f"6개월 방향 적중 : "
                f"{direction_correct}/"
                f"{direction_total}건"
            )

            print(
                f"6개월 방향 적중률 : "
                f"{direction_accuracy:.1f}%"
            )

        else:

            print(
                "6개월 방향 적중률 : "
                "계산 불가"
            )

        # -----------------------------------------------------
        # 시장국면별 결과
        # -----------------------------------------------------

        print()
        print("-" * 110)
        print("시장국면별 안정성")
        print("-" * 110)

        for phase in [
            "상승",
            "보합",
            "하락"
        ]:

            stat = phase_stats[phase]

            count = stat["count"]

            if count == 0:
                continue

            phase_current_mae = (
                stat["current_sum"]
                / count
            )

            phase_future_mae = (
                stat["future_sum"]
                / count
            )

            phase_effect = (
                phase_current_mae
                - phase_future_mae
            )

            print(
                f"[{phase}] "
                f"{count}건 | "
                f"현재 {phase_current_mae:.2f}% | "
                f"미래 {phase_future_mae:.2f}% | "
                f"효과 {phase_effect:+.2f}%p"
            )

    print("=" * 110)

    # =========================================================
    # ✅ 15% 이상 대형오차 상세 분석
    # =========================================================

    print()
    print("=" * 125)
    print("🚨 미래예측 15% 이상 대형오차 상세 분석")
    print("=" * 125)

    large_error_results = []

    for r in results:

        current_error = abs(
            float(r.get("current_error", 0) or 0)
        )

        future_error = abs(
            float(r.get("future_error", 0) or 0)
        )

        # 미래예측 오차 15% 이상만 추출
        if future_error < 15:
            continue

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        price_variation = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        previous_count = int(
            r.get(
                "previous_3m_count",
                0
            ) or 0
        )

        recent_count = int(
            r.get(
                "recent_3m_count",
                0
            ) or 0
        )

        # ---------------------------------------------
        # 실제 6개월 가격변동률
        # ---------------------------------------------

        if (
            current_price > 0
            and actual_future_price > 0
        ):

            actual_change_rate = (
                (
                    actual_future_price
                    - current_price
                )
                / current_price
                * 100
            )

        else:
            actual_change_rate = 0.0

        # ---------------------------------------------
        # 미래예측이 현재가격보다 얼마나
        # 개선/악화시켰는지
        # ---------------------------------------------

        effect = (
            current_error
            - future_error
        )

        if effect > 0.001:
            effect_text = (
                f"✅ +{effect:.2f}%p"
            )

        elif effect < -0.001:
            effect_text = (
                f"⚠️ {effect:.2f}%p"
            )

        else:
            effect_text = "➖ 0.00%p"

        large_error_results.append({
            "future_error": future_error,
            "text": (
                f'{r.get("apt_name", "")} | '
                f'{r.get("analysis_date", "")} | '
                f'국면 {r.get("market_phase", "미확인")} | '
                f'현재오차 {current_error:.2f}% | '
                f'미래오차 {future_error:.2f}% | '
                f'예측효과 {effect_text} | '
                f'예상 {expected_rate:+.2f}% | '
                f'실제변동 {actual_change_rate:+.2f}% | '
                f'거래상승률 {rise_rate:+.2f}% | '
                f'가격변동 {price_variation:+.2f}% | '
                f'거래 {previous_count}→{recent_count}건 | '
                f'신뢰도 {r.get("trend_confidence", "미확인")}'
            )
        })

    # 미래오차가 큰 순서대로 정렬
    large_error_results.sort(
        key=lambda x: x["future_error"],
        reverse=True
    )

    for item in large_error_results:
        print(item["text"])

    print("-" * 125)

    print(
        f"15% 이상 대형오차 : "
        f"{len(large_error_results)}건"
    )

    # ---------------------------------------------
    # 시장국면별 대형오차 건수
    # ---------------------------------------------

    for phase in [
        "상승",
        "보합",
        "하락"
    ]:

        phase_count = sum(
            1
            for r in results
            if (
                r.get("market_phase") == phase
                and abs(
                    float(
                        r.get("future_error", 0) or 0
                    )
                ) >= 15
            )
        )

        print(
            f"[{phase}] 15% 이상 : "
            f"{phase_count}건"
        )

    print("=" * 125)

    # =========================================================
    # ✅ 15% 이상 대형오차 기준가격 4자 비교
    #
    # 비교 대상
    # 1. 현재 기준가격
    # 2. TYPE 대표가격
    # 3. 최근3개월 평균가격
    # 4. 실제 6개월 후 가격
    #
    # 목적:
    # 미래예측률 문제가 아니라
    # 기준가격 자체의 오차인지 확인
    # =========================================================

    print()
    print("=" * 135)
    print("🔎 15% 이상 대형오차 기준가격 4자 비교")
    print("=" * 135)

    base_compare_results = []

    current_error_sum = 0.0
    type_error_sum = 0.0
    recent_error_sum = 0.0

    current_win_count = 0
    type_win_count = 0
    recent_win_count = 0

    valid_compare_count = 0

    for r in results:

        future_error = abs(
            float(
                r.get("future_error", 0) or 0
            )
        )

        # 미래예측 오차 15% 이상만 분석
        if future_error < 15:
            continue

        actual_price = float(
            r.get(
                "actual_future_price",
                0
            ) or 0
        )

        current_price = float(
            r.get(
                "current_price",
                0
            ) or 0
        )

        type_price = float(
            r.get(
                "type_reference_price",
                0
            ) or 0
        )

        recent_price = float(
            r.get(
                "recent_3m_avg",
                0
            ) or 0
        )

        if actual_price <= 0:
            continue

        # 세 가격이 모두 있어야
        # 동일 조건으로 비교
        if (
            current_price <= 0
            or type_price <= 0
            or recent_price <= 0
        ):
            continue

        # ---------------------------------------------
        # 실제 미래가격 대비 각각의 오차율
        # ---------------------------------------------

        current_base_error = abs(
            (
                current_price
                - actual_price
            )
            / actual_price
            * 100
        )

        type_base_error = abs(
            (
                type_price
                - actual_price
            )
            / actual_price
            * 100
        )

        recent_base_error = abs(
            (
                recent_price
                - actual_price
            )
            / actual_price
            * 100
        )

        valid_compare_count += 1

        current_error_sum += current_base_error
        type_error_sum += type_base_error
        recent_error_sum += recent_base_error

        # ---------------------------------------------
        # 가장 실제 미래가격에 가까운 기준가격
        # ---------------------------------------------

        errors = {
            "현재기준": current_base_error,
            "TYPE": type_base_error,
            "최근3개월": recent_base_error
        }

        best_source = min(
            errors,
            key=errors.get
        )

        if best_source == "현재기준":
            current_win_count += 1

        elif best_source == "TYPE":
            type_win_count += 1

        elif best_source == "최근3개월":
            recent_win_count += 1

        base_compare_results.append({
            "future_error": future_error,
            "apt_name": r.get(
                "apt_name",
                ""
            ),
            "analysis_date": r.get(
                "analysis_date",
                ""
            ),
            "market_phase": r.get(
                "market_phase",
                "미확인"
            ),

            "actual_price": actual_price,
            "current_price": current_price,
            "type_price": type_price,
            "recent_price": recent_price,

            "current_error": current_base_error,
            "type_error": type_base_error,
            "recent_error": recent_base_error,

            "best_source": best_source
        })


    # 미래예측 오차 큰 순서
    base_compare_results.sort(
        key=lambda x: x["future_error"],
        reverse=True
    )


    for x in base_compare_results:

        print(
            f'{x["apt_name"]} | '
            f'{x["analysis_date"]} | '
            f'국면 {x["market_phase"]} | '
            f'실제 {x["actual_price"]:,.0f}만원 | '
            f'현재 {x["current_price"]:,.0f} '
            f'({x["current_error"]:.2f}%) | '
            f'TYPE {x["type_price"]:,.0f} '
            f'({x["type_error"]:.2f}%) | '
            f'최근3M {x["recent_price"]:,.0f} '
            f'({x["recent_error"]:.2f}%) | '
            f'🏆 {x["best_source"]}'
        )


    print("-" * 135)

    print(
        f"비교 가능 대형오차 : "
        f"{valid_compare_count}건"
    )

    if valid_compare_count > 0:

        current_base_mae = (
            current_error_sum
            / valid_compare_count
        )

        type_base_mae = (
            type_error_sum
            / valid_compare_count
        )

        recent_base_mae = (
            recent_error_sum
            / valid_compare_count
        )

        print()
        print(
            f"현재기준가격 MAE : "
            f"{current_base_mae:.2f}%"
        )

        print(
            f"TYPE대표가격 MAE : "
            f"{type_base_mae:.2f}%"
        )

        print(
            f"최근3개월평균 MAE : "
            f"{recent_base_mae:.2f}%"
        )

        print()

        print(
            f"실제 미래가격에 가장 가까운 횟수"
        )

        print(
            f"현재기준가격 : "
            f"{current_win_count}건"
        )

        print(
            f"TYPE대표가격 : "
            f"{type_win_count}건"
        )

        print(
            f"최근3개월평균 : "
            f"{recent_win_count}건"
        )

        print()

        # ---------------------------------------------
        # 전체 1위 가격원
        # ---------------------------------------------

        source_mae = {
            "현재기준가격": current_base_mae,
            "TYPE대표가격": type_base_mae,
            "최근3개월평균": recent_base_mae
        }

        best_overall_source = min(
            source_mae,
            key=source_mae.get
        )

        print(
            f"🏆 대형오차 구간 최저 MAE : "
            f"{best_overall_source} "
            f"({source_mae[best_overall_source]:.2f}%)"
        )

    print("=" * 135)

    # =========================================================
    # ✅ 하락장 거래량 보정 가상정책
    #
    # 최근 3개월 거래량에 따라
    # 기존 예상변동률의 적용 강도를 조절한다.
    #
    # 0~6건   → 100%
    # 7~10건  → 50%
    # 11건 이상 → 0%
    #
    # 실제 미래예측 엔진은 수정하지 않음
    # =========================================================

    down_market_results = [
        r
        for r in results
        if r.get("market_phase") == "하락"
    ]

    if down_market_results:

        virtual_errors = []

        print()
        print("=" * 70)
        print("📉 하락장 거래량 보정 가상정책")
        print("=" * 70)

        for r in down_market_results:

            current_price = float(
                r.get("current_price", 0) or 0
            )

            actual_price = float(
                r.get("actual_future_price", 0) or 0
            )

            expected_change_rate = float(
                r.get("expected_rate", 0) or 0
            )

            recent_count = int(
                r.get("recent_3m_count", 0) or 0
            )

            if (
                current_price <= 0
                or actual_price <= 0
            ):
                continue

            # -----------------------------------------
            # 거래량에 따른 하락 보정 강도
            # -----------------------------------------

            if recent_count <= 6:
                strength = 1.0

            elif recent_count <= 10:
                strength = 0.5

            else:
                strength = 0.0

            adjusted_change_rate = (
                expected_change_rate
                * strength
            )

            virtual_price = (
                current_price
                * (
                    1
                    + adjusted_change_rate / 100
                )
            )

            virtual_error = (
                (
                    virtual_price
                    - actual_price
                )
                / actual_price
                * 100
            )

            virtual_errors.append(
                abs(virtual_error)
            )

            print(
                f"{r.get('apt_name')} | "
                f"{r.get('analysis_date')} | "
                f"거래 {recent_count}건 | "
                f"기존 {expected_change_rate:+.2f}% | "
                f"강도 {strength * 100:.0f}% | "
                f"보정 {adjusted_change_rate:+.2f}% | "
                f"가상오차 {virtual_error:+.2f}%"
            )

        if virtual_errors:

            virtual_mae = (
                sum(virtual_errors)
                / len(virtual_errors)
            )

            current_down_mae = (
                sum(
                    abs(r["current_error"])
                    for r in down_market_results
                )
                / len(down_market_results)
            )

            future_down_mae = (
                sum(
                    abs(r["future_error"])
                    for r in down_market_results
                )
                / len(down_market_results)
            )

            print("-" * 70)

            print(
                f"현재가격 MAE : "
                f"{current_down_mae:.2f}%"
            )

            print(
                f"기존 미래예측 MAE : "
                f"{future_down_mae:.2f}%"
            )

            print(
                f"거래량 보정 MAE : "
                f"{virtual_mae:.2f}%"
            )

            print()

            diff_current = (
                virtual_mae
                - current_down_mae
            )

            diff_future = (
                virtual_mae
                - future_down_mae
            )

            if diff_current < 0:
                print(
                    f"✅ 현재가격 대비 "
                    f"{abs(diff_current):.2f}%p 개선"
                )
            else:
                print(
                    f"⚠️ 현재가격 대비 "
                    f"{diff_current:.2f}%p 악화"
                )

            if diff_future < 0:
                print(
                    f"✅ 기존 미래예측 대비 "
                    f"{abs(diff_future):.2f}%p 개선"
                )
            else:
                print(
                    f"⚠️ 기존 미래예측 대비 "
                    f"{diff_future:.2f}%p 악화"
                )

        print("=" * 70)

    print()
    print("=" * 100)
    print("📋 개별 백테스트 비교")
    print("=" * 100)

    for r in results:

        current_abs = abs(r["current_error"])
        future_abs = abs(r["future_error"])

        diff = future_abs - current_abs

        if diff < 0:
            judgment = f"✅ 개선 {abs(diff):.2f}%p"
        elif diff > 0:
            judgment = f"⚠️ 악화 {diff:.2f}%p"
        else:
            judgment = "➖ 동일"

        print(
            f'{r["apt_name"]} | '
            f'{r["analysis_date"]} | '
            f'현재 {current_abs:.2f}% | '
            f'미래 {future_abs:.2f}% | '
            f'{judgment} | '
            f'예상변동 {r["expected_rate"]:+.2f}% | '
            f'추세 {r["trend"]} | '
            f'신뢰도 {r["trend_confidence"]} | '
            f'이전3개월 {r["previous_3m_count"]}건 | '
            f'최근3개월 {r["recent_3m_count"]}건'
        )

    # =========================================================
    # ✅ 추세신뢰도별 미래예측 성능 검증
    # =========================================================

    confidence_groups = {}

    for r in results:

        confidence = str(
            r.get("trend_confidence", "미확인")
        ).replace(" ", "").strip()

        if confidence not in confidence_groups:
            confidence_groups[confidence] = []

        confidence_groups[confidence].append(r)


    print()
    print("=" * 70)
    print("📊 추세신뢰도별 백테스트 비교")
    print("=" * 70)

    for confidence, group in confidence_groups.items():

        count = len(group)

        current_group_mae = sum(
            abs(r["current_error"])
            for r in group
        ) / count

        future_group_mae = sum(
            abs(r["future_error"])
            for r in group
        ) / count

        improved = sum(
            1
            for r in group
            if abs(r["future_error"])
            < abs(r["current_error"])
        )

        worsened = sum(
            1
            for r in group
            if abs(r["future_error"])
            > abs(r["current_error"])
        )

        diff = future_group_mae - current_group_mae

        print()
        print(f"[{confidence}]")
        print(f"테스트건수 : {count}건")
        print(
            f"현재가격 MAE : "
            f"{current_group_mae:.2f}%"
        )
        print(
            f"미래예측 MAE : "
            f"{future_group_mae:.2f}%"
        )
        print(
            f"개선 / 악화 : "
            f"{improved} / {worsened}"
        )

        if diff < 0:
            print(
                f"✅ 미래예측 효과 : "
                f"{abs(diff):.2f}%p 개선"
            )
        elif diff > 0:
            print(
                f"⚠️ 미래예측 효과 : "
                f"{diff:.2f}%p 악화"
            )
        else:
            print("➖ 미래예측 효과 : 동일")

    print("=" * 70) 

    print()
    print("=" * 70)
    print("📊 BATCH BACKTEST 종합 결과")
    print("=" * 70)

    print(f"유효 테스트 : {len(results)}건")
    print(f"현재가격 MAE : {current_mae:.2f}%")
    print(f"미래예측 MAE : {future_mae:.2f}%")
    print(
        f"미래예측 개선 : "
        f"{improved_count}/{len(results)}건"
    )
    print(
        f"미래예측 악화 : "
        f"{worsened_count}/{len(results)}건"
    )
    print(
        f"동일 : "
        f"{same_count}/{len(results)}건"
    )
    print()
    print(
        f"가상정책 MAE "
        f"(낮은 신뢰도 보정 제외) : "
        f"{virtual_mae:.2f}%"
    )

    print()
    print(
        f"가상정책 MAE "
        f"(예상변동률 50% 적용) : "
        f"{half_rate_mae:.2f}%"
    )

    half_vs_current = (
        half_rate_mae - current_mae
    )

    half_vs_future = (
        half_rate_mae - future_mae
    )

    if half_vs_current < 0:
        print(
            f"✅ 50% 정책이 현재가격보다 "
            f"{abs(half_vs_current):.2f}%p 개선"
        )
    elif half_vs_current > 0:
        print(
            f"⚠️ 50% 정책이 현재가격보다 "
            f"{half_vs_current:.2f}%p 악화"
        )
    else:
        print(
            "➖ 50% 정책과 현재가격 정확도가 동일"
        )

    if half_vs_future < 0:
        print(
            f"✅ 50% 정책이 기존 미래예측보다 "
            f"{abs(half_vs_future):.2f}%p 개선"
        )
    elif half_vs_future > 0:
        print(
            f"⚠️ 50% 정책이 기존 미래예측보다 "
            f"{half_vs_future:.2f}%p 악화"
        )
    else:
        print(
            "➖ 50% 정책과 기존 미래예측 정확도가 동일"
        )

    virtual_diff = virtual_mae - current_mae

    if virtual_diff < 0:
        print(
            f"✅ 가상정책이 현재가격 대비 "
            f"{abs(virtual_diff):.2f}%p 개선"
        )

    elif virtual_diff > 0:
        print(
            f"⚠️ 가상정책이 현재가격 대비 "
            f"{virtual_diff:.2f}%p 악화"
        )

    else:
        print(
            "➖ 가상정책과 현재가격 정확도가 동일"
        )

    future_vs_virtual = (
        virtual_mae - future_mae
    )

    if future_vs_virtual < 0:
        print(
            f"✅ 가상정책이 기존 미래예측보다 "
            f"{abs(future_vs_virtual):.2f}%p 개선"
        )

    elif future_vs_virtual > 0:
        print(
            f"⚠️ 가상정책이 기존 미래예측보다 "
            f"{future_vs_virtual:.2f}%p 악화"
        )

    else:
        print(
            "➖ 가상정책과 기존 미래예측 정확도가 동일"
        )

    mae_diff = future_mae - current_mae

    if mae_diff < 0:
        print(
            f"✅ 미래예측 엔진이 평균 "
            f"{abs(mae_diff):.2f}%p 개선"
        )
    elif mae_diff > 0:
        print(
            f"⚠️ 미래예측 엔진이 평균 "
            f"{mae_diff:.2f}%p 악화"
        )
    else:
        print("➖ 현재가격과 미래예측 정확도가 동일")

    print("=" * 70)

    print()
    print("=" * 70)
    print("📊 미래예측 보정강도별 MAE 비교")
    print("=" * 70)

    for strength, mae in strength_results.items():

        percent = round(
            strength * 100
        )

        diff = mae - current_mae

        if diff < 0:
            status = (
                f"✅ 현재가격 대비 "
                f"{abs(diff):.2f}%p 개선"
            )

        elif diff > 0:
            status = (
                f"⚠️ 현재가격 대비 "
                f"{diff:.2f}%p 악화"
            )

        else:
            status = "➖ 현재가격과 동일"

        print(
            f"보정강도 {percent:>3}%"
            f" | MAE {mae:.2f}%"
            f" | {status}"
        )

    best_strength = min(
        strength_results,
        key=strength_results.get
    )

    best_mae = strength_results[
        best_strength
    ]

    print("-" * 70)

    print(
        f"🏆 최적 보정강도 후보 : "
        f"{round(best_strength * 100)}%"
    )

    print(
        f"🏆 최적 MAE : "
        f"{best_mae:.2f}%"
    )

    print("=" * 70)

    # =========================================================
    # ✅ 하락장 거래량 경계값 자동 탐색
    #
    # low_limit 이하      → 기존 하락예측 100%
    # low_limit 초과
    # ~ high_limit 이하   → 하락예측 50%
    # high_limit 초과     → 하락예측 0%
    #
    # 실제 엔진은 수정하지 않음
    # =========================================================

    down_results = [
        r
        for r in results
        if r.get("market_phase") == "하락"
    ]

    policy_results = []

    if down_results:

        for low_limit in range(3, 9):

            for high_limit in range(
                low_limit + 1,
                16
            ):

                errors = []

                for r in down_results:

                    current_price = float(
                        r.get(
                            "current_price",
                            0
                        ) or 0
                    )

                    actual_price = float(
                        r.get(
                            "actual_future_price",
                            0
                        ) or 0
                    )

                    expected_rate = float(
                        r.get(
                            "expected_rate",
                            0
                        ) or 0
                    )

                    recent_count = int(
                        r.get(
                            "recent_3m_count",
                            0
                        ) or 0
                    )

                    if (
                        current_price <= 0
                        or actual_price <= 0
                    ):
                        continue

                    # ✅ 거래량별 보정 강도
                    if recent_count <= low_limit:

                        strength = 1.0

                    elif recent_count <= high_limit:

                        strength = 0.5

                    else:

                        strength = 0.0

                    adjusted_rate = (
                        expected_rate
                        * strength
                    )

                    predicted_price = (
                        current_price
                        * (
                            1
                            + adjusted_rate / 100
                        )
                    )

                    error = abs(
                        (
                            predicted_price
                            - actual_price
                        )
                        / actual_price
                        * 100
                    )

                    errors.append(
                        error
                    )

                if not errors:
                    continue

                mae = (
                    sum(errors)
                    / len(errors)
                )

                policy_results.append({
                    "low_limit": low_limit,
                    "high_limit": high_limit,
                    "mae": mae
                })

        # ✅ MAE가 낮은 순서
        policy_results.sort(
            key=lambda x: x["mae"]
        )

        print()
        print("=" * 70)
        print("📉 하락장 거래량 경계값 자동 탐색")
        print("=" * 70)

        # 상위 10개만 출력
        for rank, policy in enumerate(
            policy_results[:10],
            start=1
        ):

            print(
                f"{rank}위 | "
                f"100% 적용 <= "
                f"{policy['low_limit']}건 | "
                f"50% 적용 <= "
                f"{policy['high_limit']}건 | "
                f"그 이상 0% | "
                f"MAE {policy['mae']:.2f}%"
            )

        if policy_results:

            best = policy_results[0]

            print("-" * 70)

            print(
                f"🏆 최적 후보 : "
                f"{best['low_limit']}건 이하 100% / "
                f"{best['high_limit']}건 이하 50% / "
                f"그 이상 0%"
            )

            print(
                f"🏆 최적 MAE : "
                f"{best['mae']:.2f}%"
            )

        print("=" * 70)

    # =========================================================
    # ✅ 하락장 전환신호 가상정책
    #
    # 조건:
    # 1) 최근3개월 거래량 / 이전3개월 거래량 >= 2배
    # 2) 기존 예상하락률 <= -2%
    #
    # 두 조건이 모두 맞으면
    # → 하락보정 0%
    #
    # 그 외
    # → 기존 예상변동률 100% 적용
    #
    # 실제 엔진은 수정하지 않음
    # =========================================================

    transition_errors = []

    down_results = [
        r
        for r in results
        if r.get("market_phase") == "하락"
    ]

    print()
    print("=" * 70)
    print("📉 하락장 전환신호 가상정책")
    print("=" * 70)

    for r in down_results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        if (
            current_price <= 0
            or actual_price <= 0
        ):
            continue

        # -----------------------------------------
        # 거래량 회복률
        # -----------------------------------------

        if previous_count > 0:
            volume_recovery_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_recovery_ratio = 0

        # -----------------------------------------
        # 저점 접근 / 반등 전환 신호
        # -----------------------------------------

        if (
            volume_recovery_ratio >= 2.0
            and expected_rate <= -2.0
        ):
            strength = 0.0
            signal = "전환신호"

        else:
            strength = 1.0
            signal = "하락지속"

        adjusted_rate = (
            expected_rate
            * strength
        )

        virtual_price = (
            current_price
            * (
                1
                + adjusted_rate / 100
            )
        )

        virtual_error = (
            (
                virtual_price
                - actual_price
            )
            / actual_price
            * 100
        )

        transition_errors.append(
            abs(virtual_error)
        )

        print(
            f"{r.get('apt_name')} | "
            f"{r.get('analysis_date')} | "
            f"{previous_count}→{recent_count}건 | "
            f"회복률 {volume_recovery_ratio:.2f}배 | "
            f"기존 {expected_rate:+.2f}% | "
            f"{signal} | "
            f"보정 {adjusted_rate:+.2f}% | "
            f"가상오차 {virtual_error:+.2f}%"
        )

    if transition_errors:

        transition_mae = (
            sum(transition_errors)
            / len(transition_errors)
        )

        current_down_mae = (
            sum(
                abs(r["current_error"])
                for r in down_results
            )
            / len(down_results)
        )

        future_down_mae = (
            sum(
                abs(r["future_error"])
                for r in down_results
            )
            / len(down_results)
        )

        print("-" * 70)

        print(
            f"현재가격 MAE : "
            f"{current_down_mae:.2f}%"
        )

        print(
            f"기존 미래예측 MAE : "
            f"{future_down_mae:.2f}%"
        )

        print(
            f"전환신호 정책 MAE : "
            f"{transition_mae:.2f}%"
        )

        diff_current = (
            transition_mae
            - current_down_mae
        )

        diff_future = (
            transition_mae
            - future_down_mae
        )

        if diff_current < 0:
            print(
                f"✅ 현재가격 대비 "
                f"{abs(diff_current):.2f}%p 개선"
            )
        else:
            print(
                f"⚠️ 현재가격 대비 "
                f"{diff_current:.2f}%p 악화"
            )

        if diff_future < 0:
            print(
                f"✅ 기존 미래예측 대비 "
                f"{abs(diff_future):.2f}%p 개선"
            )
        else:
            print(
                f"⚠️ 기존 미래예측 대비 "
                f"{diff_future:.2f}%p 악화"
            )

    print("=" * 70)

    # =========================================================
    # ✅ 하락장 전환신호 임계값 자동 탐색
    #
    # 거래량 회복률 후보:
    # 1.5 / 2.0 / 2.5 / 3.0배
    #
    # 예상하락률 후보:
    # -1.5 / -2.0 / -2.5 / -3.0%
    #
    # 두 조건을 동시에 만족하면
    # 하락보정 0%
    #
    # 실제 엔진은 수정하지 않음
    # =========================================================

    recovery_ratio_candidates = [
        1.5,
        2.0,
        2.5,
        3.0
    ]

    decline_rate_candidates = [
        -1.5,
        -2.0,
        -2.5,
        -3.0
    ]

    transition_policy_results = []

    down_results = [
        r
        for r in results
        if r.get("market_phase") == "하락"
    ]

    for recovery_threshold in recovery_ratio_candidates:

        for decline_threshold in decline_rate_candidates:

            errors = []
            signal_count = 0

            for r in down_results:

                current_price = float(
                    r.get("current_price", 0) or 0
                )

                actual_price = float(
                    r.get("actual_future_price", 0) or 0
                )

                expected_rate = float(
                    r.get("expected_rate", 0) or 0
                )

                recent_count = int(
                    r.get("recent_3m_count", 0) or 0
                )

                previous_count = int(
                    r.get("previous_3m_count", 0) or 0
                )

                if (
                    current_price <= 0
                    or actual_price <= 0
                ):
                    continue

                if previous_count > 0:
                    recovery_ratio = (
                        recent_count
                        / previous_count
                    )
                else:
                    recovery_ratio = 0

                # ✅ 전환신호 판정
                if (
                    recovery_ratio
                    >= recovery_threshold
                    and expected_rate
                    <= decline_threshold
                ):
                    strength = 0.0
                    signal_count += 1

                else:
                    strength = 1.0

                adjusted_rate = (
                    expected_rate
                    * strength
                )

                predicted_price = (
                    current_price
                    * (
                        1
                        + adjusted_rate / 100
                    )
                )

                error = abs(
                    (
                        predicted_price
                        - actual_price
                    )
                    / actual_price
                    * 100
                )

                errors.append(error)

            if not errors:
                continue

            mae = (
                sum(errors)
                / len(errors)
            )

            transition_policy_results.append({
                "recovery_threshold": recovery_threshold,
                "decline_threshold": decline_threshold,
                "signal_count": signal_count,
                "mae": mae
            })

    # ✅ MAE 낮은 순서
    transition_policy_results.sort(
        key=lambda x: x["mae"]
    )

    print()
    print("=" * 78)
    print("📉 하락장 전환신호 임계값 자동 탐색")
    print("=" * 78)

    for rank, policy in enumerate(
        transition_policy_results[:10],
        start=1
    ):

        print(
            f"{rank}위 | "
            f"회복률 >= "
            f"{policy['recovery_threshold']:.1f}배 | "
            f"예상하락률 <= "
            f"{policy['decline_threshold']:.1f}% | "
            f"전환신호 {policy['signal_count']}건 | "
            f"MAE {policy['mae']:.2f}%"
        )

    if transition_policy_results:

        best = transition_policy_results[0]

        print("-" * 78)

        print(
            f"🏆 최적 회복률 기준 : "
            f"{best['recovery_threshold']:.1f}배 이상"
        )

        print(
            f"🏆 최적 예상하락률 기준 : "
            f"{best['decline_threshold']:.1f}% 이하"
        )

        print(
            f"🏆 전환신호 발생 : "
            f"{best['signal_count']}건"
        )

        print(
            f"🏆 최적 MAE : "
            f"{best['mae']:.2f}%"
        )

    print("=" * 78)

    # =========================================================
    # ✅ 하락장 전환신호 최소 거래건수 자동 검증
    # 고정 조건:
    #   회복률 >= 2.0배
    #   예상하락률 <= -2.0%
    #
    # 추가 검증:
    #   이전3개월 / 최근3개월 최소 거래건수
    # =========================================================

    print()
    print("=" * 78)
    print("📉 하락장 전환신호 최소 거래건수 자동 검증")
    print("=" * 78)

    min_count_results = []

    # 이전3개월 최소건수 / 최근3개월 최소건수
    for previous_min in [1, 2, 3, 4]:

        for recent_min in [1, 2, 3, 4]:

            policy_errors = []
            signal_count = 0

            for r in results:

                # 하락장만 검증
                rise_rate = float(
                    r.get("rise_rate", 0) or 0
                )

                if rise_rate >= -1:
                    continue

                previous_count = int(
                    r.get("previous_3m_count", 0) or 0
                )

                recent_count = int(
                    r.get("recent_3m_count", 0) or 0
                )

                expected_rate = float(
                    r.get("expected_rate", 0) or 0
                )

                current_price = float(
                    r.get("current_price", 0) or 0
                )

                actual_price = float(
                    r.get("actual_future_price", 0) or 0
                )

                if (
                    current_price <= 0
                    or actual_price <= 0
                ):
                    continue

                # 이전 거래가 없으면
                # 회복률 계산 대상에서 제외
                if previous_count <= 0:
                    recovery_ratio = 0.0
                else:
                    recovery_ratio = (
                        recent_count
                        / previous_count
                    )

                # -----------------------------------------
                # 전환신호 판정
                # -----------------------------------------
                transition_signal = (
                    previous_count >= previous_min
                    and recent_count >= recent_min
                    and recovery_ratio >= 2.0
                    and expected_rate <= -2.0
                )

                if transition_signal:

                    # 하락보정 중단
                    adjusted_rate = 0.0
                    signal_count += 1

                else:

                    # 기존 미래예측 유지
                    adjusted_rate = expected_rate

                virtual_price = (
                    current_price
                    * (1 + adjusted_rate / 100)
                )

                virtual_error = (
                    (virtual_price - actual_price)
                    / actual_price
                    * 100
                )

                policy_errors.append(
                    abs(virtual_error)
                )

            if not policy_errors:
                continue

            policy_mae = (
                sum(policy_errors)
                / len(policy_errors)
            )

            min_count_results.append({
                "previous_min": previous_min,
                "recent_min": recent_min,
                "signal_count": signal_count,
                "mae": policy_mae
            })

    # MAE가 낮은 순서
    min_count_results.sort(
        key=lambda x: x["mae"]
    )

    for rank, item in enumerate(
        min_count_results[:10],
        start=1
    ):

        print(
            f'{rank}위 | '
            f'이전 >= {item["previous_min"]}건 | '
            f'최근 >= {item["recent_min"]}건 | '
            f'전환신호 {item["signal_count"]}건 | '
            f'MAE {item["mae"]:.2f}%'
        )

    print("-" * 78)

    if min_count_results:

        best = min_count_results[0]

        print(
            f'🏆 최적 후보 : '
            f'이전3개월 >= '
            f'{best["previous_min"]}건 / '
            f'최근3개월 >= '
            f'{best["recent_min"]}건'
        )

        print(
            f'🏆 전환신호 건수 : '
            f'{best["signal_count"]}건'
        )

        print(
            f'🏆 최적 MAE : '
            f'{best["mae"]:.2f}%'
        )

    print("=" * 78)

    # =========================================================
    # ✅ 상승장 거래량 둔화 임계값 자동 탐색
    #
    # 조건:
    # 1) 시장국면 = 상승
    # 2) 최근3개월 / 이전3개월 거래량 유지율이
    #    특정 기준 이하
    # 3) 기존 예상상승률이 특정 기준 이상
    #
    # 조건 충족 시 기존 예상상승률을
    # 75% / 50% / 25% / 0%로 축소해 비교
    #
    # 실제 엔진은 수정하지 않음
    # =========================================================

    print()
    print("=" * 86)
    print("📈 상승장 거래량 둔화 임계값 자동 탐색")
    print("=" * 86)

    up_results = [
        r
        for r in results
        if r.get("market_phase") == "상승"
    ]

    retention_candidates = [
        0.3,
        0.4,
        0.5,
        0.6,
        0.7,
        0.8
    ]

    expected_rate_candidates = [
        1.0,
        2.0,
        3.0,
        4.0,
        5.0
    ]

    strength_candidates = [
        0.75,
        0.50,
        0.25,
        0.00
    ]

    up_policy_results = []

    for retention_threshold in retention_candidates:

        for rate_threshold in expected_rate_candidates:

            for strength in strength_candidates:

                errors = []
                signal_count = 0

                for r in up_results:

                    current_price = float(
                        r.get("current_price", 0) or 0
                    )

                    actual_price = float(
                        r.get(
                            "actual_future_price",
                            0
                        ) or 0
                    )

                    expected_rate = float(
                        r.get("expected_rate", 0) or 0
                    )

                    previous_count = int(
                        r.get(
                            "previous_3m_count",
                            0
                        ) or 0
                    )

                    recent_count = int(
                        r.get(
                            "recent_3m_count",
                            0
                        ) or 0
                    )

                    if (
                        current_price <= 0
                        or actual_price <= 0
                    ):
                        continue

                    # 이전 거래가 없으면
                    # 거래량 유지율 신호 사용 안 함
                    if previous_count > 0:
                        retention_ratio = (
                            recent_count
                            / previous_count
                        )
                    else:
                        retention_ratio = 1.0

                    # -----------------------------------------
                    # 상승장 둔화 신호
                    # -----------------------------------------
                    slowdown_signal = (
                        retention_ratio
                        <= retention_threshold
                        and expected_rate
                        >= rate_threshold
                    )

                    if slowdown_signal:

                        adjusted_rate = (
                            expected_rate
                            * strength
                        )

                        signal_count += 1

                    else:
                        adjusted_rate = expected_rate

                    predicted_price = (
                        current_price
                        * (
                            1
                            + adjusted_rate / 100
                        )
                    )

                    error = abs(
                        (
                            predicted_price
                            - actual_price
                        )
                        / actual_price
                        * 100
                    )

                    errors.append(error)

                if not errors:
                    continue

                mae = (
                    sum(errors)
                    / len(errors)
                )

                up_policy_results.append({
                    "retention_threshold":
                        retention_threshold,

                    "rate_threshold":
                        rate_threshold,

                    "strength":
                        strength,

                    "signal_count":
                        signal_count,

                    "mae":
                        mae
                })

    # ✅ MAE 낮은 순서
    up_policy_results.sort(
        key=lambda x: x["mae"]
    )

    for rank, policy in enumerate(
        up_policy_results[:15],
        start=1
    ):

        print(
            f"{rank}위 | "
            f"거래량 유지율 <= "
            f"{policy['retention_threshold']:.1f}배 | "
            f"예상상승률 >= "
            f"+{policy['rate_threshold']:.1f}% | "
            f"상승보정 "
            f"{policy['strength'] * 100:.0f}% 적용 | "
            f"신호 {policy['signal_count']}건 | "
            f"MAE {policy['mae']:.2f}%"
        )

    print("-" * 86)

    if up_policy_results:

        best = up_policy_results[0]

        print(
            f"🏆 최적 거래량 유지율 기준 : "
            f"{best['retention_threshold']:.1f}배 이하"
        )

        print(
            f"🏆 최적 예상상승률 기준 : "
            f"+{best['rate_threshold']:.1f}% 이상"
        )

        print(
            f"🏆 최적 상승보정 강도 : "
            f"{best['strength'] * 100:.0f}%"
        )

        print(
            f"🏆 둔화신호 발생 : "
            f"{best['signal_count']}건"
        )

        print(
            f"🏆 최적 MAE : "
            f"{best['mae']:.2f}%"
        )

    print("=" * 86)

    # =========================================================
    # ✅ 상승장 거래량 둔화 V1 후보 상세 검증
    #
    # 현재 최적 후보:
    # 거래량 유지율 <= 0.7배
    # 예상상승률 >= +4.0%
    # 기존 상승예측의 50%만 적용
    # =========================================================

    print()
    print("=" * 100)
    print("📈 상승장 거래량 둔화 V1 후보 상세 검증")
    print("=" * 100)

    slowdown_cases = []

    for r in up_results:

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_price = float(
            r.get("actual_future_price", 0) or 0
        )

        if (
            previous_count <= 0
            or current_price <= 0
            or actual_price <= 0
        ):
            continue

        retention_ratio = (
            recent_count
            / previous_count
        )

        # ✅ 현재 1위 정책
        if (
            retention_ratio <= 0.7
            and expected_rate >= 4.0
        ):

            adjusted_rate = (
                expected_rate * 0.5
            )

            original_predicted_price = (
                current_price
                * (1 + expected_rate / 100)
            )

            adjusted_predicted_price = (
                current_price
                * (1 + adjusted_rate / 100)
            )

            original_error = abs(
                (
                    original_predicted_price
                    - actual_price
                )
                / actual_price
                * 100
            )

            adjusted_error = abs(
                (
                    adjusted_predicted_price
                    - actual_price
                )
                / actual_price
                * 100
            )

            improvement = (
                original_error
                - adjusted_error
            )

            slowdown_cases.append({
                "apt_name":
                    r.get("apt_name", ""),

                "analysis_date":
                    r.get("analysis_date", ""),

                "previous_count":
                    previous_count,

                "recent_count":
                    recent_count,

                "retention_ratio":
                    retention_ratio,

                "expected_rate":
                    expected_rate,

                "adjusted_rate":
                    adjusted_rate,

                "original_error":
                    original_error,

                "adjusted_error":
                    adjusted_error,

                "recent_price_variation_rate": float(
                    r.get(
                        "recent_price_variation_rate",
                        0
                    ) or 0
                ),

                "improvement":
                    improvement
            })

    improved_slowdown = 0
    worsened_slowdown = 0
    same_slowdown = 0

    for case in slowdown_cases:

        improvement = case["improvement"]

        if improvement > 0.001:
            judgment = (
                f"✅ 개선 {improvement:.2f}%p"
            )
            improved_slowdown += 1

        elif improvement < -0.001:
            judgment = (
                f"⚠️ 악화 {abs(improvement):.2f}%p"
            )
            worsened_slowdown += 1

        else:
            judgment = "➖ 동일"
            same_slowdown += 1

        print(
            f'{case["apt_name"]} | '
            f'{case["analysis_date"]} | '
            f'{case["previous_count"]}'
            f'→{case["recent_count"]}건 | '
            f'유지율 '
            f'{case["retention_ratio"]:.2f}배 | '
            f'가격변동 '
            f'{case["recent_price_variation_rate"]:+.2f}% | '
            f'기존 '
            f'+{case["expected_rate"]:.2f}% | '
            f'보정 '
            f'+{case["adjusted_rate"]:.2f}% | '
            f'기존오차 '
            f'{case["original_error"]:.2f}% | '
            f'보정오차 '
            f'{case["adjusted_error"]:.2f}% | '
            f'{judgment}'           
        )

    print("-" * 100)

    print(
        f"둔화신호 : "
        f"{len(slowdown_cases)}건"
    )

    print(
        f"개선 / 악화 / 동일 : "
        f"{improved_slowdown} / "
        f"{worsened_slowdown} / "
        f"{same_slowdown}"
    )

    if slowdown_cases:

        original_signal_mae = (
            sum(
                c["original_error"]
                for c in slowdown_cases
            )
            / len(slowdown_cases)
        )

        adjusted_signal_mae = (
            sum(
                c["adjusted_error"]
                for c in slowdown_cases
            )
            / len(slowdown_cases)
        )

        print(
            f"신호구간 기존 MAE : "
            f"{original_signal_mae:.2f}%"
        )

        print(
            f"신호구간 보정 MAE : "
            f"{adjusted_signal_mae:.2f}%"
        )

        print(
            f"신호구간 개선효과 : "
            f"{original_signal_mae - adjusted_signal_mae:.2f}%p"
        )

    print("=" * 100)

    # =========================================================
    # ✅ 상승장 거래량 둔화 + 가격모멘텀 임계값 자동 탐색
    # =========================================================

    print()
    print("=" * 86)
    print("📈 상승장 거래량 둔화 + 가격모멘텀 임계값 자동 탐색")
    print("=" * 86)

    momentum_test_results = []

    # 가격변동률 상한 후보
    momentum_limits = [
        2.0,
        3.0,
        4.0,
        4.5,
        5.0,
        6.0,
        7.0,
        8.0
    ]

    # 보정 강도 후보
    adjustment_factors = [
        0.25,
        0.50,
        0.75
    ]

    for momentum_limit in momentum_limits:

        for adjustment_factor in adjustment_factors:

            errors = []
            signal_count = 0

            for r in results:

                rise_rate = float(
                    r.get("rise_rate", 0) or 0
                )

                # 상승장만
                if rise_rate <= 1:
                    continue

                previous_count = int(
                    r.get("previous_3m_count", 0) or 0
                )

                recent_count = int(
                    r.get("recent_3m_count", 0) or 0
                )

                expected_rate = float(
                    r.get("expected_rate", 0) or 0
                )

                price_momentum = float(
                    r.get(
                        "recent_price_variation_rate",
                        0
                    ) or 0
                )

                current_price = float(
                    r.get("current_price", 0) or 0
                )

                actual_future_price = float(
                    r.get("actual_future_price", 0) or 0
                )

                if (
                    current_price <= 0
                    or actual_future_price <= 0
                ):
                    continue

                if previous_count > 0:
                    retention_ratio = (
                        recent_count / previous_count
                    )
                else:
                    retention_ratio = 999

                adjusted_rate = expected_rate

                # 기존 V1 조건
                # +
                # 가격 모멘텀이 일정 수준 이하일 때만 보정
                if (
                    retention_ratio <= 0.7
                    and expected_rate >= 4.0
                    and price_momentum <= momentum_limit
                ):
                    adjusted_rate = (
                        expected_rate
                        * adjustment_factor
                    )

                    signal_count += 1

                adjusted_price = (
                    current_price
                    * (1 + adjusted_rate / 100)
                )

                error = abs(
                    (
                        adjusted_price
                        - actual_future_price
                    )
                    / actual_future_price
                    * 100
                )

                errors.append(error)

            if not errors:
                continue

            mae = sum(errors) / len(errors)

            momentum_test_results.append({
                "momentum_limit": momentum_limit,
                "adjustment_factor": adjustment_factor,
                "signal_count": signal_count,
                "mae": mae
            })


    momentum_test_results.sort(
        key=lambda x: x["mae"]
    )

    for rank, item in enumerate(
        momentum_test_results[:15],
        start=1
    ):

        print(
            f'{rank}위 | '
            f'가격변동률 <= '
            f'{item["momentum_limit"]:+.1f}% | '
            f'상승보정 '
            f'{item["adjustment_factor"] * 100:.0f}% 적용 | '
            f'신호 {item["signal_count"]}건 | '
            f'MAE {item["mae"]:.2f}%'
        )


    if momentum_test_results:

        best = momentum_test_results[0]

        print("-" * 86)

        print(
            f'🏆 최적 가격변동률 상한 : '
            f'{best["momentum_limit"]:+.1f}%'
        )

        print(
            f'🏆 최적 상승보정 강도 : '
            f'{best["adjustment_factor"] * 100:.0f}%'
        )

        print(
            f'🏆 신호 발생 : '
            f'{best["signal_count"]}건'
        )

        print(
            f'🏆 최적 MAE : '
            f'{best["mae"]:.2f}%'
        )

    # =========================================================
    # ✅ 상승장 거래량 둔화 V2 상세 검증
    # 조건:
    # 거래량 유지율 <= 0.7
    # 예상상승률 >= +4.0%
    # 가격변동률 <= +6.0%
    # 예상상승률 50% 적용
    # =========================================================

    print()
    print("=" * 100)
    print("📈 상승장 거래량 둔화 V3 상세 검증")
    print("=" * 100)

    v2_cases = []

    for r in results:

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        # 상승장만
        if rise_rate <= 1:
            continue

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        price_momentum = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
            or previous_count <= 0
        ):
            continue

        retention_ratio = (
            recent_count / previous_count
        )

        # V2 신호
        momentum_gap = (
            expected_rate
            - price_momentum
        )

        if not (
            retention_ratio <= 0.7
            and expected_rate >= 4.0
            and price_momentum <= 6.0
            and momentum_gap >= 0.5
        ):
            continue

        adjusted_rate = (
            expected_rate * 0.5
        )

        original_price = (
            current_price
            * (1 + expected_rate / 100)
        )

        adjusted_price = (
            current_price
            * (1 + adjusted_rate / 100)
        )

        original_error = abs(
            (
                original_price
                - actual_future_price
            )
            / actual_future_price
            * 100
        )

        adjusted_error = abs(
            (
                adjusted_price
                - actual_future_price
            )
            / actual_future_price
            * 100
        )

        improvement = (
            original_error
            - adjusted_error
        )

        if improvement > 0.001:
            judgment = (
                f"✅ 개선 {improvement:.2f}%p"
            )

        elif improvement < -0.001:
            judgment = (
                f"⚠️ 악화 {abs(improvement):.2f}%p"
            )

        else:
            judgment = "➖ 동일"

        v2_cases.append({
            "apt_name": r["apt_name"],
            "analysis_date": r["analysis_date"],
            "previous_count": previous_count,
            "recent_count": recent_count,
            "retention_ratio": retention_ratio,
            "price_momentum": price_momentum,
            "expected_rate": expected_rate,
            "adjusted_rate": adjusted_rate,
            "original_error": original_error,
            "adjusted_error": adjusted_error,
            "improvement": improvement,
            "momentum_gap": momentum_gap,
            "judgment": judgment
        })


    for case in v2_cases:

        print(
            f'{case["apt_name"]} | '
            f'{case["analysis_date"]} | '
            f'{case["previous_count"]}'
            f'→{case["recent_count"]}건 | '
            f'유지율 {case["retention_ratio"]:.2f}배 | '
            f'가격변동 {case["price_momentum"]:+.2f}% | '
            f'예상-가격차 {case["momentum_gap"]:+.2f}%p | '
            f'기존 {case["expected_rate"]:+.2f}% | '
            f'보정 {case["adjusted_rate"]:+.2f}% | '
            f'기존오차 {case["original_error"]:.2f}% | '
            f'보정오차 {case["adjusted_error"]:.2f}% | '
            f'{case["judgment"]}'
        )


    improved = sum(
        1
        for x in v2_cases
        if x["improvement"] > 0.001
    )

    worsened = sum(
        1
        for x in v2_cases
        if x["improvement"] < -0.001
    )

    same = (
        len(v2_cases)
        - improved
        - worsened
    )

    print("-" * 100)

    print(
        f'V3 신호 : {len(v2_cases)}건'
    )

    print(
        f'개선 / 악화 / 동일 : '
        f'{improved} / {worsened} / {same}'
    )

    if v2_cases:

        original_mae = sum(
            x["original_error"]
            for x in v2_cases
        ) / len(v2_cases)

        adjusted_mae = sum(
            x["adjusted_error"]
            for x in v2_cases
        ) / len(v2_cases)

        print(
            f'신호구간 기존 MAE : '
            f'{original_mae:.2f}%'
        )

        print(
            f'신호구간 V3 MAE : '
            f'{adjusted_mae:.2f}%'
        )

        print(
            f'신호구간 개선효과 : '
            f'{original_mae - adjusted_mae:.2f}%p'
        )

    # =========================================================
    # ✅ 상승장 거래량 둔화 V3
    # 예상상승률 - 최근가격변동률 차이 임계값 자동 탐색
    # =========================================================

    print()
    print("=" * 90)
    print("📈 상승장 거래량 둔화 V3 예상-가격모멘텀 차이 자동 탐색")
    print("=" * 90)

    gap_test_results = []

    gap_limits = [
        0.0,
        0.5,
        1.0,
        1.5,
        2.0,
        2.5,
        3.0
    ]

    adjustment_factors = [
        0.25,
        0.50,
        0.75
    ]

    for gap_limit in gap_limits:

        for adjustment_factor in adjustment_factors:

            errors = []
            signal_count = 0

            for r in results:

                rise_rate = float(
                    r.get("rise_rate", 0) or 0
                )

                # 상승장만
                if rise_rate <= 1:
                    continue

                previous_count = int(
                    r.get("previous_3m_count", 0) or 0
                )

                recent_count = int(
                    r.get("recent_3m_count", 0) or 0
                )

                expected_rate = float(
                    r.get("expected_rate", 0) or 0
                )

                price_momentum = float(
                    r.get(
                        "recent_price_variation_rate",
                        0
                    ) or 0
                )

                current_price = float(
                    r.get("current_price", 0) or 0
                )

                actual_future_price = float(
                    r.get("actual_future_price", 0) or 0
                )

                if (
                    current_price <= 0
                    or actual_future_price <= 0
                ):
                    continue

                if previous_count > 0:
                    retention_ratio = (
                        recent_count / previous_count
                    )
                else:
                    retention_ratio = 999

                # 예상상승률과 최근 가격모멘텀 차이
                momentum_gap = (
                    expected_rate
                    - price_momentum
                )

                adjusted_rate = expected_rate

                # V3 조건
                if (
                    retention_ratio <= 0.7
                    and expected_rate >= 4.0
                    and price_momentum <= 6.0
                    and momentum_gap >= gap_limit
                ):

                    adjusted_rate = (
                        expected_rate
                        * adjustment_factor
                    )

                    signal_count += 1

                adjusted_price = (
                    current_price
                    * (1 + adjusted_rate / 100)
                )

                error = abs(
                    (
                        adjusted_price
                        - actual_future_price
                    )
                    / actual_future_price
                    * 100
                )

                errors.append(error)

            if not errors:
                continue

            mae = (
                sum(errors)
                / len(errors)
            )

            gap_test_results.append({
                "gap_limit": gap_limit,
                "adjustment_factor": adjustment_factor,
                "signal_count": signal_count,
                "mae": mae
            })


    gap_test_results.sort(
        key=lambda x: x["mae"]
    )

    for rank, item in enumerate(
        gap_test_results[:15],
        start=1
    ):

        print(
            f'{rank}위 | '
            f'예상-가격차 >= '
            f'{item["gap_limit"]:.1f}%p | '
            f'상승보정 '
            f'{item["adjustment_factor"] * 100:.0f}% | '
            f'신호 {item["signal_count"]}건 | '
            f'MAE {item["mae"]:.2f}%'
        )


    if gap_test_results:

        best = gap_test_results[0]

        print("-" * 90)

        print(
            f'🏆 최적 예상-가격차 기준 : '
            f'{best["gap_limit"]:.1f}%p 이상'
        )

        print(
            f'🏆 최적 상승보정 강도 : '
            f'{best["adjustment_factor"] * 100:.0f}%'
        )

        print(
            f'🏆 신호 발생 : '
            f'{best["signal_count"]}건'
        )

        print(
            f'🏆 최적 MAE : '
            f'{best["mae"]:.2f}%'
        )

    # =========================================================
    # ✅ 통합 가상정책 백테스트
    #
    # 하락장 V1
    # - trend == 하락
    # - 이전3개월 > 0
    # - 최근/이전 >= 2.0배
    # - expected_rate <= -2.0%
    # → expected_rate = 0%
    #
    # 상승장 V3
    # - trend == 상승
    # - 이전3개월 > 0
    # - 최근/이전 <= 0.7배
    # - expected_rate >= +4.0%
    # - 최근가격변동률 <= +6.0%
    # - expected_rate - 최근가격변동률 >= +0.5%p
    # → expected_rate *= 0.5
    #
    # 실제 엔진은 아직 수정하지 않음
    # =========================================================

    print()
    print("=" * 90)
    print("📊 하락장 V1 + 상승장 V3 통합 가상정책")
    print("=" * 90)

    integrated_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        trend = str(
            r.get("trend", "")
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        price_momentum = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        if (
            current_price <= 0
            or actual_price <= 0
        ):
            continue

        adjusted_rate = expected_rate
        policy = "기존"

        if previous_count > 0:

            volume_ratio = (
                recent_count
                / previous_count
            )

        else:

            volume_ratio = 0.0

        # -----------------------------------------
        # ✅ 하락장 V1
        # -----------------------------------------
        if (
            trend == "하락"
            and previous_count > 0
            and volume_ratio >= 2.0
            and expected_rate <= -2.0
        ):

            adjusted_rate = 0.0
            policy = "하락V1"

        # -----------------------------------------
        # ✅ 상승장 V3
        # -----------------------------------------
        elif (
            trend == "상승"
            and previous_count > 0
            and volume_ratio <= 0.7
            and expected_rate >= 4.0
            and price_momentum <= 6.0
            and (
                expected_rate
                - price_momentum
            ) >= 0.5
        ):

            adjusted_rate = (
                expected_rate
                * 0.5
            )

            policy = "상승V3"

        predicted_price = (
            current_price
            * (
                1
                + adjusted_rate / 100
            )
        )

        error = (
            (
                predicted_price
                - actual_price
            )
            / actual_price
            * 100
        )

        integrated_results.append({
            "market_phase":
                r.get("market_phase", "미확인"),

            "policy":
                policy,

            "error":
                error
        })


    # =========================================================
    # 전체 MAE
    # =========================================================

    if integrated_results:

        integrated_mae = (
            sum(
                abs(x["error"])
                for x in integrated_results
            )
            / len(integrated_results)
        )

        original_mae = (
            sum(
                abs(r["future_error"])
                for r in results
            )
            / len(results)
        )

        current_mae_all = (
            sum(
                abs(r["current_error"])
                for r in results
            )
            / len(results)
        )

        down_v1_count = sum(
            1
            for x in integrated_results
            if x["policy"] == "하락V1"
        )

        up_v3_count = sum(
            1
            for x in integrated_results
            if x["policy"] == "상승V3"
        )

        print(
            f"유효 테스트 : "
            f"{len(integrated_results)}건"
        )

        print(
            f"현재가격 MAE : "
            f"{current_mae_all:.2f}%"
        )

        print(
            f"기존 미래예측 MAE : "
            f"{original_mae:.2f}%"
        )

        print(
            f"통합정책 MAE : "
            f"{integrated_mae:.2f}%"
        )

        print(
            f"하락 V1 적용 : "
            f"{down_v1_count}건"
        )

        print(
            f"상승 V3 적용 : "
            f"{up_v3_count}건"
        )

        print()

        if integrated_mae < original_mae:

            print(
                f"✅ 기존 미래예측 대비 "
                f"{original_mae - integrated_mae:.2f}%p 개선"
            )

        else:

            print(
                f"⚠️ 기존 미래예측 대비 "
                f"{integrated_mae - original_mae:.2f}%p 악화"
            )

        if integrated_mae < current_mae_all:

            print(
                f"✅ 현재가격 대비 "
                f"{current_mae_all - integrated_mae:.2f}%p 개선"
            )

        else:

            print(
                f"⚠️ 현재가격 대비 "
                f"{integrated_mae - current_mae_all:.2f}%p 악화"
            )


        # =====================================================
        # 시장국면별 통합정책 MAE
        # =====================================================

        print()
        print("-" * 90)
        print("📊 통합정책 시장국면별 결과")
        print("-" * 90)

        for phase in [
            "상승",
            "보합",
            "하락"
        ]:

            group = [
                x
                for x in integrated_results
                if x["market_phase"] == phase
            ]

            if not group:

                print(
                    f"[{phase}] 테스트 없음"
                )

                continue

            phase_mae = (
                sum(
                    abs(x["error"])
                    for x in group
                )
                / len(group)
            )

            print(
                f"[{phase}] "
                f"{len(group)}건 | "
                f"통합정책 MAE "
                f"{phase_mae:.2f}%"
            )

    print("=" * 90)

    # ✅ 급락장 조기경보 V1 임계값 탐색
    analyze_crash_warning_v1(
        results
    )

    # ✅ 급락장 조기경보 V2
    analyze_crash_warning_v2(
        results
    )

    # ✅ 급락장 조기경보 V3
    analyze_crash_warning_v3(
        results
    )

    # ✅ 급락장 조기경보 V4
    # 하락 보정강도 자동 탐색
    analyze_crash_adjustment_v4(
        results
    )

    # ✅ 급락 추가보정 미사용기간 외부검증
    validate_crash_adjustment_out_of_sample(
        results
    )

    # =====================================================
    # 🚀 15% 이상 급등 사례 상세 분석
    # =====================================================
    analyze_surge_cases(
        results
    )

    # ✅ 급등 조기신호 V1 임계값 자동 탐색
    analyze_surge_warning_v1(
        results
    )

    # ✅ 급등 조기신호 V2 최근가격 모멘텀 탐색
    analyze_surge_warning_v2(
        results
    )

    # ✅ 급등 V3 거래량 유지율 구간분석
    analyze_surge_volume_buckets_v3(
        results
    )

    # ✅ 급등 V4 거래량 증가 임계값 자동 탐색
    analyze_surge_volume_threshold_v4(
        results
    )

    # ✅ 급등 V5 상승둔화 V3와 실제 급등 충돌 분석
    analyze_surge_slowdown_conflict_v5(
        results
    )

    # ✅ 급등 V6
    # 강한 상승모멘텀일 때 V3 둔화보정 예외 검증
    analyze_uptrend_slowdown_exception_v6(
        results
    )

    # ✅ 급등 V7 선행기간 분석
    analyze_surge_lead_time_v7(
        results
    )

    # ✅ 급등 V7.1 실제가격 경로 분석
    analyze_surge_price_path_v71(
        results
    )

    # ✅ 급등 V7.2 가격 기준 일치성 검증
    analyze_surge_price_consistency_v72(
        results
    )

    return {
        "count": len(results),
        "current_mae": round(current_mae, 2),
        "future_mae": round(future_mae, 2),
        "improved_count": improved_count,
        "worsened_count": worsened_count,
        "same_count": same_count,
        "results": results
    }

def build_monthly_backtest_cases(
    apartments,
    start_date="2025-07-06",
    end_date="2026-02-06"
):
    """
    여러 단지에 대해 월 단위 백테스트 케이스를 자동 생성한다.
    예:
    2025-07-06
    2025-08-06
    ...
    2026-02-06
    """

    from datetime import datetime

    start = datetime.strptime(
        start_date,
        "%Y-%m-%d"
    )

    end = datetime.strptime(
        end_date,
        "%Y-%m-%d"
    )

    cases = []

    current = start

    while current <= end:

        analysis_date = current.strftime(
            "%Y-%m-%d"
        )

        for apt in apartments:

            cases.append({
                "region": apt["region"],
                "apt_name": apt["apt_name"],
                "size": apt["size"],
                "analysis_date": analysis_date
            })

        # ✅ 다음 달로 이동
        if current.month == 12:
            current = current.replace(
                year=current.year + 1,
                month=1
            )
        else:
            current = current.replace(
                month=current.month + 1
            )

    return cases

def analyze_crash_warning_v1(results):
    """
    급락장 조기경보 V1 임계값 자동 탐색

    목적
    --------------------------------------------------
    미래예측 오차가 15% 이상 발생하는 대형오차를
    사전에 구분할 수 있는 조건을 탐색한다.

    현재 미래예측 엔진은 수정하지 않는다.
    results에 저장된 과거 데이터만 사용한다.
    """

    print()
    print("=" * 100)
    print("🚨 급락장 조기경보 V1 임계값 자동 탐색")
    print("=" * 100)

    valid_results = []

    for r in results:

        current_error = abs(
            float(r.get("current_error", 0) or 0)
        )

        future_error = abs(
            float(r.get("future_error", 0) or 0)
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        price_variation = float(
            r.get("recent_price_variation_rate", 0) or 0
        )

        trend = str(
            r.get("trend", "")
        )

        # 거래량 유지율
        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0

        valid_results.append({
            "apt_name": r.get("apt_name", ""),
            "analysis_date": r.get("analysis_date", ""),

            "current_error": current_error,
            "future_error": future_error,

            "rise_rate": rise_rate,

            "previous_count": previous_count,
            "recent_count": recent_count,

            "volume_ratio": volume_ratio,

            "price_variation": price_variation,

            "trend": trend
        })

    if not valid_results:
        print("검증 가능한 데이터가 없습니다.")
        return

    # --------------------------------------------------
    # 대형오차 정의
    # --------------------------------------------------

    large_error_threshold = 15.0

    large_errors = [
        r
        for r in valid_results
        if r["future_error"] >= large_error_threshold
    ]

    print(
        f"전체 테스트 : {len(valid_results)}건"
    )

    print(
        f"15% 이상 대형오차 : {len(large_errors)}건"
    )

    print()

    # --------------------------------------------------
    # 자동 탐색 후보
    # --------------------------------------------------

    rise_thresholds = [
        -1.0,
        -2.0,
        -3.0,
        -4.0,
        -5.0,
        -7.0,
        -10.0
    ]

    volume_thresholds = [
        0.3,
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
        1.0
    ]

    candidates = []

    for rise_threshold in rise_thresholds:

        for volume_threshold in volume_thresholds:

            signal_results = []

            for r in valid_results:

                # 이전 거래가 없는 경우 제외
                if r["previous_count"] <= 0:
                    continue

                signal = (
                    r["rise_rate"] <= rise_threshold
                    and
                    r["volume_ratio"] <= volume_threshold
                )

                if signal:
                    signal_results.append(r)

            if not signal_results:
                continue

            # ------------------------------------------
            # 신호가 잡은 대형오차
            # ------------------------------------------

            captured_large_errors = [
                r
                for r in signal_results
                if r["future_error"] >= large_error_threshold
            ]

            # ------------------------------------------
            # 정상구간 오탐
            # ------------------------------------------

            false_signals = [
                r
                for r in signal_results
                if r["future_error"] < large_error_threshold
            ]

            signal_count = len(
                signal_results
            )

            captured_count = len(
                captured_large_errors
            )

            false_count = len(
                false_signals
            )

            # ------------------------------------------
            # 대형오차 포착률
            # ------------------------------------------

            if large_errors:
                capture_rate = (
                    captured_count
                    / len(large_errors)
                    * 100
                )
            else:
                capture_rate = 0

            # ------------------------------------------
            # 신호 정확도
            #
            # 신호를 발생시켰을 때 실제 대형오차였던 비율
            # ------------------------------------------

            if signal_count > 0:
                precision = (
                    captured_count
                    / signal_count
                    * 100
                )
            else:
                precision = 0

            # ------------------------------------------
            # 임시 종합점수
            #
            # 포착률과 정확도를 동일 비중으로 평가
            # ------------------------------------------

            score = (
                capture_rate
                + precision
            ) / 2

            candidates.append({
                "rise_threshold": rise_threshold,
                "volume_threshold": volume_threshold,

                "signal_count": signal_count,
                "captured_count": captured_count,
                "false_count": false_count,

                "capture_rate": capture_rate,
                "precision": precision,

                "score": score
            })

    if not candidates:
        print("조건에 해당하는 후보가 없습니다.")
        return

    # 종합점수 우선
    # 동점이면 포착 건수가 많은 조건 우선

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["captured_count"],
            -x["false_count"]
        ),
        reverse=True
    )

    print(
        "순위 | 거래상승률 기준 | 거래량 유지율 기준 | "
        "신호 | 대형오차 포착 | 오탐 | 포착률 | 정확도 | 점수"
    )

    print("-" * 100)

    for rank, c in enumerate(
        candidates[:15],
        start=1
    ):

        print(
            f"{rank}위 | "
            f"상승률 <= {c['rise_threshold']:+.1f}% | "
            f"거래비 <= {c['volume_threshold']:.1f}배 | "
            f"신호 {c['signal_count']}건 | "
            f"포착 {c['captured_count']}건 | "
            f"오탐 {c['false_count']}건 | "
            f"포착률 {c['capture_rate']:.1f}% | "
            f"정확도 {c['precision']:.1f}% | "
            f"점수 {c['score']:.1f}"
        )

    best = candidates[0]

    print("-" * 100)

    print(
        f"🏆 최적 거래상승률 기준 : "
        f"{best['rise_threshold']:+.1f}% 이하"
    )

    print(
        f"🏆 최적 거래량 유지율 기준 : "
        f"{best['volume_threshold']:.1f}배 이하"
    )

    print(
        f"🏆 신호 발생 : "
        f"{best['signal_count']}건"
    )

    print(
        f"🏆 대형오차 포착 : "
        f"{best['captured_count']}건"
    )

    print(
        f"🏆 정상구간 오탐 : "
        f"{best['false_count']}건"
    )

    print(
        f"🏆 대형오차 포착률 : "
        f"{best['capture_rate']:.1f}%"
    )

    print(
        f"🏆 신호 정확도 : "
        f"{best['precision']:.1f}%"
    )

    print(
        f"🏆 종합점수 : "
        f"{best['score']:.1f}"
    )

    print("=" * 100)

def analyze_crash_warning_v2(results):

    print()
    print("=" * 110)
    print("🚨 급락장 조기경보 V2 임계값 자동 탐색")
    print("=" * 110)

    valid_results = []

    for r in results:

        future_error = abs(
            float(r.get("future_error", 0) or 0)
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        price_variation = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        valid_results.append({
            "future_error": future_error,
            "rise_rate": rise_rate,
            "previous_count": previous_count,
            "recent_count": recent_count,
            "volume_ratio": volume_ratio,
            "price_variation": price_variation
        })

    if not valid_results:
        print("검증 가능한 데이터가 없습니다.")
        return

    large_error_threshold = 15.0

    large_errors = [
        r
        for r in valid_results
        if r["future_error"] >= large_error_threshold
    ]

    print(
        f"전체 테스트 : "
        f"{len(valid_results)}건"
    )

    print(
        f"15% 이상 대형오차 : "
        f"{len(large_errors)}건"
    )

    print()

    rise_thresholds = [
        -1.0,
        -2.0,
        -3.0,
        -4.0,
        -5.0
    ]

    volume_thresholds = [
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
        1.0
    ]

    price_variation_thresholds = [
        0.0,
        0.5,
        1.0,
        1.5,
        2.0,
        2.5,
        3.0
    ]

    candidates = []

    for rise_threshold in rise_thresholds:

        for volume_threshold in volume_thresholds:

            for price_threshold in price_variation_thresholds:

                signal_results = []

                for r in valid_results:

                    if r["previous_count"] <= 0:
                        continue

                    signal = (
                        r["rise_rate"]
                        <= rise_threshold
                        and
                        r["volume_ratio"]
                        <= volume_threshold
                        and
                        r["price_variation"]
                        >= price_threshold
                    )

                    if signal:
                        signal_results.append(r)

                if not signal_results:
                    continue

                captured = [
                    r
                    for r in signal_results
                    if r["future_error"]
                    >= large_error_threshold
                ]

                false_signals = [
                    r
                    for r in signal_results
                    if r["future_error"]
                    < large_error_threshold
                ]

                signal_count = len(
                    signal_results
                )

                captured_count = len(
                    captured
                )

                false_count = len(
                    false_signals
                )

                if large_errors:
                    capture_rate = (
                        captured_count
                        / len(large_errors)
                        * 100
                    )
                else:
                    capture_rate = 0

                if signal_count > 0:
                    precision = (
                        captured_count
                        / signal_count
                        * 100
                    )
                else:
                    precision = 0

                # F1 형태 점수
                if (
                    capture_rate > 0
                    and precision > 0
                ):
                    score = (
                        2
                        * capture_rate
                        * precision
                        / (
                            capture_rate
                            + precision
                        )
                    )
                else:
                    score = 0

                candidates.append({
                    "rise_threshold":
                        rise_threshold,

                    "volume_threshold":
                        volume_threshold,

                    "price_threshold":
                        price_threshold,

                    "signal_count":
                        signal_count,

                    "captured_count":
                        captured_count,

                    "false_count":
                        false_count,

                    "capture_rate":
                        capture_rate,

                    "precision":
                        precision,

                    "score":
                        score
                })

    if not candidates:
        print("조건에 해당하는 후보가 없습니다.")
        return

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["precision"],
            x["captured_count"],
            -x["false_count"]
        ),
        reverse=True
    )

    print(
        "순위 | 상승률 기준 | 거래량 유지율 | "
        "가격변동률 기준 | 신호 | 포착 | 오탐 | "
        "포착률 | 정확도 | 점수"
    )

    print("-" * 110)

    for rank, c in enumerate(
        candidates[:20],
        start=1
    ):

        print(
            f"{rank}위 | "
            f"상승률 <= "
            f"{c['rise_threshold']:+.1f}% | "
            f"거래비 <= "
            f"{c['volume_threshold']:.1f}배 | "
            f"가격변동 >= "
            f"{c['price_threshold']:+.1f}% | "
            f"신호 {c['signal_count']}건 | "
            f"포착 {c['captured_count']}건 | "
            f"오탐 {c['false_count']}건 | "
            f"포착률 {c['capture_rate']:.1f}% | "
            f"정확도 {c['precision']:.1f}% | "
            f"점수 {c['score']:.1f}"
        )

    best = candidates[0]

    print("-" * 110)

    print(
        f"🏆 최적 거래상승률 기준 : "
        f"{best['rise_threshold']:+.1f}% 이하"
    )

    print(
        f"🏆 최적 거래량 유지율 기준 : "
        f"{best['volume_threshold']:.1f}배 이하"
    )

    print(
        f"🏆 최적 최근가격변동률 기준 : "
        f"{best['price_threshold']:+.1f}% 이상"
    )

    print(
        f"🏆 신호 발생 : "
        f"{best['signal_count']}건"
    )

    print(
        f"🏆 대형오차 포착 : "
        f"{best['captured_count']}건"
    )

    print(
        f"🏆 정상구간 오탐 : "
        f"{best['false_count']}건"
    )

    print(
        f"🏆 대형오차 포착률 : "
        f"{best['capture_rate']:.1f}%"
    )

    print(
        f"🏆 신호 정확도 : "
        f"{best['precision']:.1f}%"
    )

    print(
        f"🏆 F1 점수 : "
        f"{best['score']:.1f}"
    )

    print("=" * 110)

def analyze_crash_warning_v3(results):
    """
    급락장 조기경보 V3

    V1 조건:
    - 거래상승률 하락
    - 거래량 감소

    V3 추가 조건:
    - TYPE 대비 최근가격변화율 하락

    목적:
    대형오차 포착률을 유지하면서
    정상구간 오탐을 줄일 수 있는지 검증
    """

    print()
    print("=" * 115)
    print("🚨 급락장 조기경보 V3 TYPE-최근가격 괴리 임계값 자동 탐색")
    print("=" * 115)

    valid_results = []

    for r in results:

        future_error = abs(
            float(
                r.get(
                    "future_error",
                    0
                ) or 0
            )
        )

        rise_rate = float(
            r.get(
                "rise_rate",
                0
            ) or 0
        )

        previous_count = int(
            r.get(
                "previous_3m_count",
                0
            ) or 0
        )

        recent_count = int(
            r.get(
                "recent_3m_count",
                0
            ) or 0
        )

        type_recent_gap_rate = float(
            r.get(
                "type_recent_gap_rate",
                0
            ) or 0
        )

        if previous_count > 0:

            volume_ratio = (
                recent_count
                / previous_count
            )

        else:

            volume_ratio = 0.0

        valid_results.append({

            "apt_name":
                r.get("apt_name", ""),

            "analysis_date":
                r.get("analysis_date", ""),

            "future_error":
                future_error,

            "rise_rate":
                rise_rate,

            "previous_count":
                previous_count,

            "recent_count":
                recent_count,

            "volume_ratio":
                volume_ratio,

            "type_recent_gap_rate":
                type_recent_gap_rate
        })

    if not valid_results:

        print(
            "검증 가능한 데이터가 없습니다."
        )

        return

    # =====================================================
    # 15% 이상 미래예측 오차를 대형오차로 정의
    # =====================================================

    large_error_threshold = 15.0

    large_errors = [

        r

        for r in valid_results

        if (
            r["future_error"]
            >= large_error_threshold
        )
    ]

    print(
        f"전체 테스트 : "
        f"{len(valid_results)}건"
    )

    print(
        f"15% 이상 대형오차 : "
        f"{len(large_errors)}건"
    )

    print()

    # =====================================================
    # 자동 탐색 범위
    # =====================================================

    rise_thresholds = [
        -1.0,
        -2.0,
        -3.0,
        -4.0,
        -5.0,
        -7.0,
        -10.0
    ]

    volume_thresholds = [
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
        1.0
    ]

    # TYPE 대비 최근가격변화율
    #
    # 예:
    # -5% = 최근가격이 TYPE보다 5% 낮음
    # -10% = 최근가격이 TYPE보다 10% 낮음

    gap_thresholds = [
        0.0,
        -1.0,
        -2.0,
        -3.0,
        -4.0,
        -5.0,
        -7.0,
        -10.0,
        -15.0
    ]

    candidates = []

    # =====================================================
    # 모든 조합 탐색
    # =====================================================

    for rise_threshold in rise_thresholds:

        for volume_threshold in volume_thresholds:

            for gap_threshold in gap_thresholds:

                signal_results = []

                for r in valid_results:

                    if (
                        r["previous_count"]
                        <= 0
                    ):
                        continue

                    signal = (

                        r["rise_rate"]
                        <= rise_threshold

                        and

                        r["volume_ratio"]
                        <= volume_threshold

                        and

                        r["type_recent_gap_rate"]
                        <= gap_threshold
                    )

                    if signal:

                        signal_results.append(
                            r
                        )

                if not signal_results:
                    continue

                captured = [

                    r

                    for r in signal_results

                    if (
                        r["future_error"]
                        >= large_error_threshold
                    )
                ]

                false_signals = [

                    r

                    for r in signal_results

                    if (
                        r["future_error"]
                        < large_error_threshold
                    )
                ]

                signal_count = len(
                    signal_results
                )

                captured_count = len(
                    captured
                )

                false_count = len(
                    false_signals
                )

                # =========================================
                # 포착률
                # =========================================

                if large_errors:

                    capture_rate = (
                        captured_count
                        / len(large_errors)
                        * 100
                    )

                else:

                    capture_rate = 0

                # =========================================
                # 정확도
                # =========================================

                if signal_count > 0:

                    precision = (
                        captured_count
                        / signal_count
                        * 100
                    )

                else:

                    precision = 0

                # =========================================
                # F1 점수
                # =========================================

                if (
                    capture_rate > 0
                    and
                    precision > 0
                ):

                    score = (

                        2
                        * capture_rate
                        * precision

                        / (
                            capture_rate
                            + precision
                        )
                    )

                else:

                    score = 0

                candidates.append({

                    "rise_threshold":
                        rise_threshold,

                    "volume_threshold":
                        volume_threshold,

                    "gap_threshold":
                        gap_threshold,

                    "signal_count":
                        signal_count,

                    "captured_count":
                        captured_count,

                    "false_count":
                        false_count,

                    "capture_rate":
                        capture_rate,

                    "precision":
                        precision,

                    "score":
                        score
                })

    if not candidates:

        print(
            "조건에 해당하는 후보가 없습니다."
        )

        return

    # =====================================================
    # 순위 정렬
    # =====================================================

    candidates.sort(

        key=lambda x: (

            x["score"],
            x["precision"],
            x["captured_count"],
            -x["false_count"]
        ),

        reverse=True
    )

    print(
        "순위 | 거래상승률 | 거래량 유지율 | "
        "TYPE대비최근가격 | 신호 | 포착 | 오탐 | "
        "포착률 | 정확도 | F1"
    )

    print("-" * 115)

    # =====================================================
    # 상위 20개 출력
    # =====================================================

    for rank, c in enumerate(
        candidates[:20],
        start=1
    ):

        print(

            f"{rank}위 | "

            f"상승률 <= "
            f"{c['rise_threshold']:+.1f}% | "

            f"거래비 <= "
            f"{c['volume_threshold']:.1f}배 | "

            f"TYPE괴리 <= "
            f"{c['gap_threshold']:+.1f}% | "

            f"신호 "
            f"{c['signal_count']}건 | "

            f"포착 "
            f"{c['captured_count']}건 | "

            f"오탐 "
            f"{c['false_count']}건 | "

            f"포착률 "
            f"{c['capture_rate']:.1f}% | "

            f"정확도 "
            f"{c['precision']:.1f}% | "

            f"F1 "
            f"{c['score']:.1f}"
        )

    # =====================================================
    # 최적 후보
    # =====================================================

    best = candidates[0]

    print("-" * 115)

    print(
        f"🏆 최적 거래상승률 기준 : "
        f"{best['rise_threshold']:+.1f}% 이하"
    )

    print(
        f"🏆 최적 거래량 유지율 기준 : "
        f"{best['volume_threshold']:.1f}배 이하"
    )

    print(
        f"🏆 최적 TYPE대비최근가격변화율 : "
        f"{best['gap_threshold']:+.1f}% 이하"
    )

    print(
        f"🏆 신호 발생 : "
        f"{best['signal_count']}건"
    )

    print(
        f"🏆 대형오차 포착 : "
        f"{best['captured_count']}건"
    )

    print(
        f"🏆 정상구간 오탐 : "
        f"{best['false_count']}건"
    )

    print(
        f"🏆 대형오차 포착률 : "
        f"{best['capture_rate']:.1f}%"
    )

    print(
        f"🏆 신호 정확도 : "
        f"{best['precision']:.1f}%"
    )

    print(
        f"🏆 F1 점수 : "
        f"{best['score']:.1f}"
    )

    print("=" * 115)    

def analyze_crash_adjustment_v4(results):
    """
    급락장 조기경보 V4

    V1 신호를 고정한 상태에서
    추가 하락 보정률을 자동 탐색한다.

    실제 future_prediction()은 수정하지 않는다.
    """

    print()
    print("=" * 110)
    print("🚨 급락장 조기경보 V4 하락 보정강도 자동 탐색")
    print("=" * 110)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_price = float(
            r.get(
                "actual_future_price",
                0
            ) or 0
        )

        expected_rate = float(
            r.get(
                "expected_rate",
                0
            ) or 0
        )

        rise_rate = float(
            r.get(
                "rise_rate",
                0
            ) or 0
        )

        previous_count = int(
            r.get(
                "previous_3m_count",
                0
            ) or 0
        )

        recent_count = int(
            r.get(
                "recent_3m_count",
                0
            ) or 0
        )

        if (
            current_price <= 0
            or actual_price <= 0
        ):
            continue

        if previous_count > 0:

            volume_ratio = (
                recent_count
                / previous_count
            )

        else:

            volume_ratio = 0.0

        valid_results.append({
            "apt_name":
                r.get("apt_name", ""),

            "analysis_date":
                r.get("analysis_date", ""),

            "current_price":
                current_price,

            "actual_price":
                actual_price,

            "expected_rate":
                expected_rate,

            "rise_rate":
                rise_rate,

            "previous_count":
                previous_count,

            "recent_count":
                recent_count,

            "volume_ratio":
                volume_ratio
        })

    if not valid_results:

        print(
            "검증 가능한 데이터가 없습니다."
        )

        return

    # =====================================================
    # V1 급락 경보 조건 고정
    # =====================================================

    warning_results = [

        r

        for r in valid_results

        if (
            r["previous_count"] > 0
            and r["rise_rate"] <= -1.0
            and r["volume_ratio"] <= 0.8
        )
    ]

    print(
        f"전체 테스트 : "
        f"{len(valid_results)}건"
    )

    print(
        f"V1 급락경보 발생 : "
        f"{len(warning_results)}건"
    )

    print()

    # =====================================================
    # 기존 실제 엔진 전체 MAE
    # =====================================================

    original_errors = []

    for r in valid_results:

        original_predicted_price = (
            r["current_price"]
            * (
                1
                + r["expected_rate"] / 100
            )
        )

        original_error = abs(
            (
                original_predicted_price
                - r["actual_price"]
            )
            / r["actual_price"]
            * 100
        )

        original_errors.append(
            original_error
        )

    original_mae = (
        sum(original_errors)
        / len(original_errors)
    )

    # =====================================================
    # 추가 하락 보정 후보
    #
    # -1.0 = 기존 expected_rate에서
    #        추가로 1%p 하향
    # =====================================================

    adjustment_candidates = [
        0.0,
        -2.0,
        -4.0,
        -6.0,
        -8.0,
        -10.0,
        -12.0,
        -14.0,
        -16.0,
        -18.0,
        -20.0,
        -25.0,
        -30.0
    ]

    adjustment_results = []

    for adjustment in adjustment_candidates:

        all_errors = []

        signal_original_errors = []
        signal_adjusted_errors = []

        improved_count = 0
        worsened_count = 0
        same_count = 0

        for r in valid_results:

            original_rate = (
                r["expected_rate"]
            )

            adjusted_rate = (
                original_rate
            )

            warning_signal = (
                r["previous_count"] > 0
                and r["rise_rate"] <= -1.0
                and r["volume_ratio"] <= 0.8
            )

            if warning_signal:

                adjusted_rate = (
                    original_rate
                    + adjustment
                )

            # -----------------------------------------
            # 기존 예상가격
            # -----------------------------------------

            original_price = (
                r["current_price"]
                * (
                    1
                    + original_rate / 100
                )
            )

            # -----------------------------------------
            # 가상 보정 예상가격
            # -----------------------------------------

            adjusted_price = (
                r["current_price"]
                * (
                    1
                    + adjusted_rate / 100
                )
            )

            original_error = abs(
                (
                    original_price
                    - r["actual_price"]
                )
                / r["actual_price"]
                * 100
            )

            adjusted_error = abs(
                (
                    adjusted_price
                    - r["actual_price"]
                )
                / r["actual_price"]
                * 100
            )

            all_errors.append(
                adjusted_error
            )

            if warning_signal:

                signal_original_errors.append(
                    original_error
                )

                signal_adjusted_errors.append(
                    adjusted_error
                )

                diff = (
                    original_error
                    - adjusted_error
                )

                if diff > 0.001:
                    improved_count += 1

                elif diff < -0.001:
                    worsened_count += 1

                else:
                    same_count += 1

        if not all_errors:
            continue

        total_mae = (
            sum(all_errors)
            / len(all_errors)
        )

        if signal_adjusted_errors:

            signal_original_mae = (
                sum(signal_original_errors)
                / len(signal_original_errors)
            )

            signal_adjusted_mae = (
                sum(signal_adjusted_errors)
                / len(signal_adjusted_errors)
            )

        else:

            signal_original_mae = 0.0
            signal_adjusted_mae = 0.0

        adjustment_results.append({
            "adjustment":
                adjustment,

            "total_mae":
                total_mae,

            "signal_original_mae":
                signal_original_mae,

            "signal_adjusted_mae":
                signal_adjusted_mae,

            "improved_count":
                improved_count,

            "worsened_count":
                worsened_count,

            "same_count":
                same_count
        })

    # =====================================================
    # 전체 MAE가 가장 낮은 순서
    # =====================================================

    adjustment_results.sort(
        key=lambda x: x["total_mae"]
    )

    print(
        "순위 | 추가하락보정 | 전체 MAE | "
        "신호구간 기존 MAE | 신호구간 보정 MAE | "
        "개선 / 악화 / 동일"
    )

    print("-" * 110)

    for rank, item in enumerate(
        adjustment_results,
        start=1
    ):

        print(
            f"{rank}위 | "
            f"{item['adjustment']:+.1f}%p | "
            f"전체 {item['total_mae']:.2f}% | "
            f"신호기존 "
            f"{item['signal_original_mae']:.2f}% | "
            f"신호보정 "
            f"{item['signal_adjusted_mae']:.2f}% | "
            f"{item['improved_count']} / "
            f"{item['worsened_count']} / "
            f"{item['same_count']}"
        )

    if adjustment_results:

        best = (
            adjustment_results[0]
        )

        print("-" * 110)

        print(
            f"현재 실제엔진 전체 MAE : "
            f"{original_mae:.2f}%"
        )

        print(
            f"🏆 최적 추가 하락보정 : "
            f"{best['adjustment']:+.1f}%p"
        )

        print(
            f"🏆 보정 후 전체 MAE : "
            f"{best['total_mae']:.2f}%"
        )

        print(
            f"🏆 전체 MAE 개선효과 : "
            f"{original_mae - best['total_mae']:+.2f}%p"
        )

        print(
            f"🏆 신호구간 기존 MAE : "
            f"{best['signal_original_mae']:.2f}%"
        )

        print(
            f"🏆 신호구간 보정 MAE : "
            f"{best['signal_adjusted_mae']:.2f}%"
        )

        print(
            f"🏆 신호구간 개선효과 : "
            f"{best['signal_original_mae'] - best['signal_adjusted_mae']:+.2f}%p"
        )

        print(
            f"🏆 신호구간 개선 / 악화 / 동일 : "
            f"{best['improved_count']} / "
            f"{best['worsened_count']} / "
            f"{best['same_count']}"
        )

    print("=" * 110)

def validate_crash_adjustment_out_of_sample(results):
    """
    급락장 추가보정 외부검증

    학습/탐색 구간에서 선택한 후보만 검증:
    0%p / -12%p / -14%p

    새로운 최적값 탐색 금지
    """

    print()
    print("=" * 110)
    print("🧪 급락장 추가보정 미사용기간 외부검증")
    print("=" * 110)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if (
            current_price <= 0
            or actual_price <= 0
        ):
            continue

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        valid_results.append({
            "current_price": current_price,
            "actual_price": actual_price,
            "expected_rate": expected_rate,
            "rise_rate": rise_rate,
            "previous_count": previous_count,
            "recent_count": recent_count,
            "volume_ratio": volume_ratio
        })


    if not valid_results:
        print("검증 가능한 데이터가 없습니다.")
        return


    # 탐색구간에서 확정한 후보만 사용
    fixed_candidates = [
        0.0,
        -12.0,
        -14.0
    ]

    print(
        f"전체 외부검증 테스트 : "
        f"{len(valid_results)}건"
    )

    warning_count = sum(
        1
        for r in valid_results
        if (
            r["previous_count"] > 0
            and r["rise_rate"] <= -1.0
            and r["volume_ratio"] <= 0.8
        )
    )

    print(
        f"V1 급락경보 발생 : "
        f"{warning_count}건"
    )

    print("-" * 110)

    validation_results = []

    for adjustment in fixed_candidates:

        errors = []

        signal_original_errors = []
        signal_adjusted_errors = []

        improved = 0
        worsened = 0
        same = 0

        for r in valid_results:

            original_rate = r["expected_rate"]
            adjusted_rate = original_rate

            warning_signal = (
                r["previous_count"] > 0
                and r["rise_rate"] <= -1.0
                and r["volume_ratio"] <= 0.8
            )

            if warning_signal:
                adjusted_rate = (
                    original_rate
                    + adjustment
                )

            original_price = (
                r["current_price"]
                * (1 + original_rate / 100)
            )

            adjusted_price = (
                r["current_price"]
                * (1 + adjusted_rate / 100)
            )

            original_error = abs(
                (
                    original_price
                    - r["actual_price"]
                )
                / r["actual_price"]
                * 100
            )

            adjusted_error = abs(
                (
                    adjusted_price
                    - r["actual_price"]
                )
                / r["actual_price"]
                * 100
            )

            errors.append(
                adjusted_error
            )

            if warning_signal:

                signal_original_errors.append(
                    original_error
                )

                signal_adjusted_errors.append(
                    adjusted_error
                )

                diff = (
                    original_error
                    - adjusted_error
                )

                if diff > 0.001:
                    improved += 1
                elif diff < -0.001:
                    worsened += 1
                else:
                    same += 1

        total_mae = (
            sum(errors)
            / len(errors)
        )

        if signal_adjusted_errors:

            signal_original_mae = (
                sum(signal_original_errors)
                / len(signal_original_errors)
            )

            signal_adjusted_mae = (
                sum(signal_adjusted_errors)
                / len(signal_adjusted_errors)
            )

        else:

            signal_original_mae = 0.0
            signal_adjusted_mae = 0.0

        validation_results.append({
            "adjustment": adjustment,
            "total_mae": total_mae,
            "signal_original_mae": signal_original_mae,
            "signal_adjusted_mae": signal_adjusted_mae,
            "improved": improved,
            "worsened": worsened,
            "same": same
        })


    for item in validation_results:

        print(
            f'추가보정 {item["adjustment"]:+.1f}%p | '
            f'전체 MAE {item["total_mae"]:.2f}% | '
            f'신호기존 {item["signal_original_mae"]:.2f}% | '
            f'신호보정 {item["signal_adjusted_mae"]:.2f}% | '
            f'개선/악화/동일 '
            f'{item["improved"]}/'
            f'{item["worsened"]}/'
            f'{item["same"]}'
        )

    print("=" * 110)

def analyze_surge_cases(results):

    print()
    print("=" * 125)
    print("🚀 15% 이상 급등 사례 상세 분석")
    print("=" * 125)

    surge_cases = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (actual_future_price - current_price)
            / current_price
            * 100
        )

        if actual_change_rate < 15:
            continue

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if previous_count > 0:
            volume_ratio = (
                recent_count / previous_count
            )
        else:
            volume_ratio = 0

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        price_variation = float(
            r.get("recent_price_variation", 0) or 0
        )

        surge_cases.append({
            "apt_name": r.get("apt_name", ""),
            "analysis_date": r.get("analysis_date", ""),
            "actual_change_rate": actual_change_rate,
            "expected_rate": expected_rate,
            "rise_rate": rise_rate,
            "price_variation": price_variation,
            "previous_count": previous_count,
            "recent_count": recent_count,
            "volume_ratio": volume_ratio,
            "trend_confidence": r.get(
                "trend_confidence",
                "미확인"
            )
        })

    surge_cases.sort(
        key=lambda x: x["actual_change_rate"],
        reverse=True
    )

    for x in surge_cases:

        print(
            f'{x["apt_name"]} | '
            f'{x["analysis_date"]} | '
            f'실제변동 {x["actual_change_rate"]:+.2f}% | '
            f'예상 {x["expected_rate"]:+.2f}% | '
            f'거래상승률 {x["rise_rate"]:+.2f}% | '
            f'가격변동 {x["price_variation"]:+.2f}% | '
            f'거래 {x["previous_count"]}→{x["recent_count"]}건 | '
            f'거래비 {x["volume_ratio"]:.2f}배 | '
            f'신뢰도 {x["trend_confidence"]}'
        )

    print("-" * 125)
    print(
        f"15% 이상 급등 사례 : "
        f"{len(surge_cases)}건"
    )

def analyze_surge_warning_v1(results):

    print()
    print("=" * 110)
    print("🚀 급등 조기신호 V1 임계값 자동 탐색")
    print("=" * 110)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (
                actual_future_price
                - current_price
            )
            / current_price
            * 100
        )

        rate_gap = (
            rise_rate
            - expected_rate
        )

        valid_results.append({
            "actual_change_rate":
                actual_change_rate,

            "rise_rate":
                rise_rate,

            "expected_rate":
                expected_rate,

            "rate_gap":
                rate_gap
        })

    if not valid_results:
        print("검증 가능한 데이터가 없습니다.")
        return

    # =====================================================
    # 실제 6개월 +15% 이상 상승을 급등으로 정의
    # =====================================================

    surge_threshold = 15.0

    surge_results = [
        r
        for r in valid_results
        if (
            r["actual_change_rate"]
            >= surge_threshold
        )
    ]

    print(
        f"전체 테스트 : "
        f"{len(valid_results)}건"
    )

    print(
        f"15% 이상 급등 : "
        f"{len(surge_results)}건"
    )

    print()

    # =====================================================
    # 자동 탐색 후보
    # =====================================================

    rise_thresholds = [
        2.0,
        3.0,
        4.0,
        5.0,
        6.0,
        7.0,
        8.0,
        10.0,
        12.0,
        15.0
    ]

    gap_thresholds = [
        0.0,
        1.0,
        2.0,
        3.0,
        4.0,
        5.0,
        6.0,
        8.0,
        10.0
    ]

    candidates = []

    for rise_threshold in rise_thresholds:

        for gap_threshold in gap_thresholds:

            signal_results = [
                r
                for r in valid_results
                if (
                    r["rise_rate"]
                    >= rise_threshold
                    and
                    r["rate_gap"]
                    >= gap_threshold
                )
            ]

            if not signal_results:
                continue

            captured = [
                r
                for r in signal_results
                if (
                    r["actual_change_rate"]
                    >= surge_threshold
                )
            ]

            false_signals = [
                r
                for r in signal_results
                if (
                    r["actual_change_rate"]
                    < surge_threshold
                )
            ]

            signal_count = len(
                signal_results
            )

            captured_count = len(
                captured
            )

            false_count = len(
                false_signals
            )

            if surge_results:

                capture_rate = (
                    captured_count
                    / len(surge_results)
                    * 100
                )

            else:

                capture_rate = 0

            if signal_count > 0:

                precision = (
                    captured_count
                    / signal_count
                    * 100
                )

            else:

                precision = 0

            if (
                capture_rate > 0
                and precision > 0
            ):

                f1 = (
                    2
                    * capture_rate
                    * precision
                    / (
                        capture_rate
                        + precision
                    )
                )

            else:

                f1 = 0

            candidates.append({
                "rise_threshold":
                    rise_threshold,

                "gap_threshold":
                    gap_threshold,

                "signal_count":
                    signal_count,

                "captured_count":
                    captured_count,

                "false_count":
                    false_count,

                "capture_rate":
                    capture_rate,

                "precision":
                    precision,

                "f1":
                    f1
            })

    if not candidates:
        print("조건에 해당하는 후보가 없습니다.")
        return

    candidates.sort(
        key=lambda x: (
            x["f1"],
            x["precision"],
            x["captured_count"],
            -x["false_count"]
        ),
        reverse=True
    )

    print(
        "순위 | 거래상승률 기준 | "
        "상승률-예상률 차이 | "
        "신호 | 급등포착 | 오탐 | "
        "포착률 | 정확도 | F1"
    )

    print("-" * 110)

    for rank, c in enumerate(
        candidates[:20],
        start=1
    ):

        print(
            f"{rank}위 | "
            f"거래상승률 >= "
            f"+{c['rise_threshold']:.1f}% | "
            f"격차 >= "
            f"+{c['gap_threshold']:.1f}%p | "
            f"신호 {c['signal_count']}건 | "
            f"포착 {c['captured_count']}건 | "
            f"오탐 {c['false_count']}건 | "
            f"포착률 {c['capture_rate']:.1f}% | "
            f"정확도 {c['precision']:.1f}% | "
            f"F1 {c['f1']:.1f}"
        )

    best = candidates[0]

    print("-" * 110)

    print(
        f"🏆 최적 거래상승률 기준 : "
        f"+{best['rise_threshold']:.1f}% 이상"
    )

    print(
        f"🏆 최적 상승률-예상률 격차 : "
        f"+{best['gap_threshold']:.1f}%p 이상"
    )

    print(
        f"🏆 신호 발생 : "
        f"{best['signal_count']}건"
    )

    print(
        f"🏆 급등 포착 : "
        f"{best['captured_count']}건"
    )

    print(
        f"🏆 정상구간 오탐 : "
        f"{best['false_count']}건"
    )

    print(
        f"🏆 급등 포착률 : "
        f"{best['capture_rate']:.1f}%"
    )

    print(
        f"🏆 신호 정확도 : "
        f"{best['precision']:.1f}%"
    )

    print(
        f"🏆 F1 점수 : "
        f"{best['f1']:.1f}"
    )

    print("=" * 110)

def analyze_surge_warning_v2(results):

    print()
    print("=" * 120)
    print("🚀 급등 조기신호 V2 최근가격 모멘텀 임계값 자동 탐색")
    print("=" * 120)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        price_variation = float(
            r.get(
                "recent_price_variation_rate",
                0
            ) or 0
        )

        actual_change_rate = (
            (actual_future_price - current_price)
            / current_price
            * 100
        )

        rate_gap = (
            rise_rate
            - expected_rate
        )

        valid_results.append({
            "actual_change_rate": actual_change_rate,
            "rise_rate": rise_rate,
            "expected_rate": expected_rate,
            "rate_gap": rate_gap,
            "price_variation": price_variation
        })

    if not valid_results:
        print("검증 가능한 데이터가 없습니다.")
        return

    surge_threshold = 15.0

    surge_results = [
        r
        for r in valid_results
        if r["actual_change_rate"] >= surge_threshold
    ]

    print(
        f"전체 테스트 : {len(valid_results)}건"
    )

    print(
        f"15% 이상 급등 : {len(surge_results)}건"
    )

    print()

    # =====================================================
    # V1 조건은 고정
    # 거래상승률 >= +4%
    # 거래상승률 - 예상상승률 >= +2%p
    #
    # V2에서는 최근가격변동률 임계값만 탐색
    # =====================================================

    price_thresholds = [
        -5.0,
        -3.0,
        -2.0,
        -1.0,
        0.0,
        1.0,
        2.0,
        3.0,
        4.0,
        5.0,
        6.0,
        8.0,
        10.0
    ]

    candidates = []

    for price_threshold in price_thresholds:

        signal_results = [
            r
            for r in valid_results
            if (
                r["rise_rate"] >= 4.0
                and r["rate_gap"] >= 2.0
                and r["price_variation"] >= price_threshold
            )
        ]

        if not signal_results:
            continue

        captured_count = sum(
            1
            for r in signal_results
            if r["actual_change_rate"] >= surge_threshold
        )

        signal_count = len(signal_results)

        false_count = (
            signal_count
            - captured_count
        )

        capture_rate = (
            captured_count
            / len(surge_results)
            * 100
            if surge_results
            else 0
        )

        precision = (
            captured_count
            / signal_count
            * 100
            if signal_count
            else 0
        )

        if capture_rate + precision > 0:
            f1 = (
                2
                * capture_rate
                * precision
                / (capture_rate + precision)
            )
        else:
            f1 = 0

        candidates.append({
            "price_threshold": price_threshold,
            "signal_count": signal_count,
            "captured_count": captured_count,
            "false_count": false_count,
            "capture_rate": capture_rate,
            "precision": precision,
            "f1": f1
        })

    candidates.sort(
        key=lambda x: (
            x["f1"],
            x["precision"],
            x["captured_count"]
        ),
        reverse=True
    )

    print(
        "순위 | 최근가격변동률 기준 | "
        "신호 | 급등포착 | 오탐 | "
        "포착률 | 정확도 | F1"
    )

    print("-" * 120)

    for rank, c in enumerate(
        candidates,
        start=1
    ):

        print(
            f"{rank}위 | "
            f"가격변동 >= {c['price_threshold']:+.1f}% | "
            f"신호 {c['signal_count']}건 | "
            f"포착 {c['captured_count']}건 | "
            f"오탐 {c['false_count']}건 | "
            f"포착률 {c['capture_rate']:.1f}% | "
            f"정확도 {c['precision']:.1f}% | "
            f"F1 {c['f1']:.1f}"
        )

    if not candidates:
        print("조건에 해당하는 후보가 없습니다.")
        return

    best = candidates[0]

    print("-" * 120)

    print(
        f"🏆 V1 고정조건 : "
        f"거래상승률 +4.0% 이상 / "
        f"상승률-예상률 격차 +2.0%p 이상"
    )

    print(
        f"🏆 최적 최근가격변동률 기준 : "
        f"{best['price_threshold']:+.1f}% 이상"
    )

    print(
        f"🏆 신호 발생 : "
        f"{best['signal_count']}건"
    )

    print(
        f"🏆 급등 포착 : "
        f"{best['captured_count']}건"
    )

    print(
        f"🏆 정상구간 오탐 : "
        f"{best['false_count']}건"
    )

    print(
        f"🏆 급등 포착률 : "
        f"{best['capture_rate']:.1f}%"
    )

    print(
        f"🏆 신호 정확도 : "
        f"{best['precision']:.1f}%"
    )

    print(
        f"🏆 F1 점수 : "
        f"{best['f1']:.1f}"
    )

    print("=" * 120)

def analyze_surge_volume_buckets_v3(results):

    print()
    print("=" * 115)
    print("🚀 급등 V3 거래량 유지율 구간분석")
    print("=" * 115)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (
                actual_future_price
                - current_price
            )
            / current_price
            * 100
        )

        rate_gap = (
            rise_rate
            - expected_rate
        )

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        # V1 신호만 분석
        if not (
            rise_rate >= 4.0
            and rate_gap >= 2.0
        ):
            continue

        valid_results.append({
            "actual_change_rate": actual_change_rate,
            "volume_ratio": volume_ratio
        })

    if not valid_results:
        print("분석 가능한 V1 신호가 없습니다.")
        return

    # =====================================================
    # 거래량 유지율 구간
    # =====================================================

    buckets = [
        {
            "name": "0.0 ~ 0.3배",
            "min": 0.0,
            "max": 0.3
        },
        {
            "name": "0.3 ~ 0.5배",
            "min": 0.3,
            "max": 0.5
        },
        {
            "name": "0.5 ~ 0.8배",
            "min": 0.5,
            "max": 0.8
        },
        {
            "name": "0.8 ~ 1.2배",
            "min": 0.8,
            "max": 1.2
        },
        {
            "name": "1.2 ~ 2.0배",
            "min": 1.2,
            "max": 2.0
        },
        {
            "name": "2.0배 이상",
            "min": 2.0,
            "max": None
        }
    ]

    print(
        f"V1 전체 신호 : "
        f"{len(valid_results)}건"
    )

    print()
    print(
        "거래비 구간 | 신호 | 급등 | 오탐 | 급등확률"
    )
    print("-" * 115)

    total_surge = 0
    total_false = 0

    for bucket in buckets:

        group = []

        for r in valid_results:

            ratio = r["volume_ratio"]

            if bucket["max"] is None:

                matched = (
                    ratio >= bucket["min"]
                )

            else:

                matched = (
                    ratio >= bucket["min"]
                    and
                    ratio < bucket["max"]
                )

            if matched:
                group.append(r)

        signal_count = len(group)

        surge_count = sum(
            1
            for r in group
            if r["actual_change_rate"] >= 15.0
        )

        false_count = (
            signal_count
            - surge_count
        )

        if signal_count > 0:
            surge_probability = (
                surge_count
                / signal_count
                * 100
            )
        else:
            surge_probability = 0

        total_surge += surge_count
        total_false += false_count

        print(
            f'{bucket["name"]:<12} | '
            f'{signal_count:>3}건 | '
            f'{surge_count:>3}건 | '
            f'{false_count:>3}건 | '
            f'{surge_probability:>5.1f}%'
        )

    print("-" * 115)

    print(
        f"V1 급등 포착 합계 : "
        f"{total_surge}건"
    )

    print(
        f"V1 오탐 합계 : "
        f"{total_false}건"
    )

    print("=" * 115)

def analyze_surge_volume_threshold_v4(results):

    print()
    print("=" * 115)
    print("🚀 급등 V4 거래량 증가 임계값 자동 탐색")
    print("=" * 115)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
            or previous_count <= 0
        ):
            continue

        actual_change_rate = (
            (actual_future_price - current_price)
            / current_price
            * 100
        )

        rate_gap = (
            rise_rate
            - expected_rate
        )

        # 급등 V1 조건
        if not (
            rise_rate >= 4.0
            and rate_gap >= 2.0
        ):
            continue

        volume_ratio = (
            recent_count
            / previous_count
        )

        valid_results.append({
            "actual_change_rate": actual_change_rate,
            "volume_ratio": volume_ratio
        })

    surge_total = sum(
        1
        for r in valid_results
        if r["actual_change_rate"] >= 15.0
    )

    thresholds = [
        0.8,
        1.0,
        1.2,
        1.5,
        2.0,
        2.5,
        3.0
    ]

    search_results = []

    for threshold in thresholds:

        signals = [
            r
            for r in valid_results
            if r["volume_ratio"] >= threshold
        ]

        signal_count = len(signals)

        caught = sum(
            1
            for r in signals
            if r["actual_change_rate"] >= 15.0
        )

        false_positive = (
            signal_count
            - caught
        )

        recall = (
            caught / surge_total * 100
            if surge_total > 0
            else 0
        )

        precision = (
            caught / signal_count * 100
            if signal_count > 0
            else 0
        )

        if precision + recall > 0:
            f1 = (
                2
                * precision
                * recall
                / (precision + recall)
            )
        else:
            f1 = 0

        search_results.append({
            "threshold": threshold,
            "signal_count": signal_count,
            "caught": caught,
            "false_positive": false_positive,
            "recall": recall,
            "precision": precision,
            "f1": f1
        })

    search_results.sort(
        key=lambda x: x["f1"],
        reverse=True
    )

    print(
        f"V1 분석 가능 신호 : "
        f"{len(valid_results)}건"
    )

    print(
        f"실제 15% 이상 급등 : "
        f"{surge_total}건"
    )

    print()
    print(
        "순위 | 거래량 증가 기준 | 신호 | 급등포착 | "
        "오탐 | 포착률 | 정확도 | F1"
    )

    print("-" * 115)

    for rank, r in enumerate(
        search_results,
        start=1
    ):

        print(
            f"{rank}위 | "
            f"거래비 >= {r['threshold']:.1f}배 | "
            f"신호 {r['signal_count']}건 | "
            f"포착 {r['caught']}건 | "
            f"오탐 {r['false_positive']}건 | "
            f"포착률 {r['recall']:.1f}% | "
            f"정확도 {r['precision']:.1f}% | "
            f"F1 {r['f1']:.1f}"
        )

    print("-" * 115)

    if search_results:

        best = search_results[0]

        print(
            f"🏆 최적 거래량 증가 기준 : "
            f"{best['threshold']:.1f}배 이상"
        )

        print(
            f"🏆 신호 발생 : "
            f"{best['signal_count']}건"
        )

        print(
            f"🏆 급등 포착 : "
            f"{best['caught']}건"
        )

        print(
            f"🏆 정상구간 오탐 : "
            f"{best['false_positive']}건"
        )

        print(
            f"🏆 급등 포착률 : "
            f"{best['recall']:.1f}%"
        )

        print(
            f"🏆 신호 정확도 : "
            f"{best['precision']:.1f}%"
        )

        print(
            f"🏆 F1 점수 : "
            f"{best['f1']:.1f}"
        )

def analyze_surge_slowdown_conflict_v5(results):

    print()
    print("=" * 125)
    print("🚀 급등 V5 상승둔화 V3 충돌 분석")
    print("=" * 125)

    conflicts = []
    slowdown_total = 0
    actual_surge_total = 0

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        price_variation = float(
            r.get("recent_price_variation", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (actual_future_price - current_price)
            / current_price
            * 100
        )

        if actual_change_rate >= 15.0:
            actual_surge_total += 1

        if previous_count > 0:

            volume_ratio = (
                recent_count
                / previous_count
            )

        else:

            volume_ratio = 0.0

        # =====================================================
        # 상승둔화 V3 조건 재현
        #
        # 주의:
        # results의 expected_rate는 이미 실제 엔진에서
        # 50% 보정된 값일 수 있으므로
        # 여기서는 보정 전 예상률을 역산합니다.
        # =====================================================

        expected_before_slowdown = expected_rate

        if (
            r.get("uptrend_slowdown_signal", False)
        ):
            expected_before_slowdown = (
                expected_rate * 2
            )

        momentum_gap = (
            expected_before_slowdown
            - price_variation
        )

        # =====================================================
        # ✅ 실제 미래예측 엔진에서 발생한
        #    상승둔화 V3 신호만 사용
        # =====================================================

        slowdown_signal = bool(
            r.get(
                "uptrend_slowdown_signal",
                False
            )
        )

        if not slowdown_signal:
            continue

        slowdown_total += 1

        # 실제 6개월 후 +15% 이상 상승한 경우만 충돌
        if actual_change_rate < 15.0:
            continue

        conflicts.append({
            "region": r.get("region", ""),
            "apt_name": r.get("apt_name", ""),
            "analysis_date": r.get("analysis_date", ""),
            "actual_change_rate": actual_change_rate,
            "rise_rate": rise_rate,
            "expected_before": expected_before_slowdown,
            "expected_after": expected_rate,
            "price_variation": price_variation,
            "previous_count": previous_count,
            "recent_count": recent_count,
            "volume_ratio": volume_ratio,
            "trend_confidence": r.get(
                "trend_confidence",
                "미확인"
            )
        })

    conflicts.sort(
        key=lambda x: x["actual_change_rate"],
        reverse=True
    )

    print(
        f"전체 실제 15% 이상 급등 : "
        f"{actual_surge_total}건"
    )

    print(
        f"상승둔화 V3 신호 : "
        f"{slowdown_total}건"
    )

    print(
        f"상승둔화 V3 + 실제 급등 충돌 : "
        f"{len(conflicts)}건"
    )

    print()

    if slowdown_total > 0:

        conflict_rate = (
            len(conflicts)
            / slowdown_total
            * 100
        )

        print(
            f"둔화신호 중 실제 급등 비율 : "
            f"{conflict_rate:.1f}%"
        )

    if actual_surge_total > 0:

        missed_surge_rate = (
            len(conflicts)
            / actual_surge_total
            * 100
        )

        print(
            f"전체 급등 중 둔화보정 충돌 비율 : "
            f"{missed_surge_rate:.1f}%"
        )

    print()
    print("-" * 125)

    print(
        "단지 | 기준일 | 실제변동 | 거래상승률 | "
        "보정전예상 | 보정후예상 | 가격변동 | "
        "거래량 | 거래비 | 신뢰도"
    )

    print("-" * 125)

    for x in conflicts:

        print(
            f"{x['apt_name']} | "
            f"{x['analysis_date']} | "
            f"실제 {x['actual_change_rate']:+.2f}% | "
            f"거래상승률 {x['rise_rate']:+.2f}% | "
            f"보정전 {x['expected_before']:+.2f}% | "
            f"보정후 {x['expected_after']:+.2f}% | "
            f"가격변동 {x['price_variation']:+.2f}% | "
            f"{x['previous_count']}→{x['recent_count']}건 | "
            f"{x['volume_ratio']:.2f}배 | "
            f"{x['trend_confidence']}"
        )

    print("-" * 125)

    if conflicts:

        print(
            "⚠️ 상승둔화 V3가 실제 15% 이상 급등과 "
            f"{len(conflicts)}건 충돌"
        )

    else:

        print(
            "✅ 상승둔화 V3와 15% 이상 실제 급등의 "
            "충돌 없음"
        )

def analyze_uptrend_slowdown_exception_v6(results):

    print()
    print("=" * 120)
    print("🚀 상승둔화 V3 예외조건 V6 임계값 자동 탐색")
    print("=" * 120)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        slowdown_signal = bool(
            r.get(
                "uptrend_slowdown_signal",
                False
            )
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (
                actual_future_price
                - current_price
            )
            / current_price
            * 100
        )

        valid_results.append({
            "current_price": current_price,
            "actual_future_price": actual_future_price,
            "expected_rate": expected_rate,
            "rise_rate": rise_rate,
            "slowdown_signal": slowdown_signal,
            "actual_change_rate": actual_change_rate,
            "market_phase": r.get(
                "market_phase",
                "미확인"
            )
        })

    if not valid_results:

        print("검증 가능한 데이터가 없습니다.")
        return

    # =====================================================
    # 현재 실제 엔진 MAE
    # =====================================================

    original_errors = []

    for r in valid_results:

        predicted_price = (
            r["current_price"]
            * (
                1
                + r["expected_rate"] / 100
            )
        )

        error = abs(
            (
                predicted_price
                - r["actual_future_price"]
            )
            / r["actual_future_price"]
            * 100
        )

        original_errors.append(
            error
        )

    original_mae = (
        sum(original_errors)
        / len(original_errors)
    )

    # =====================================================
    # 거래상승률 예외 임계값 후보
    # =====================================================

    thresholds = [
        8.0,
        9.0,
        10.0,
        11.0,
        12.0,
        13.0,
        14.0,
        15.0,
        16.0
    ]

    search_results = []

    for threshold in thresholds:

        all_errors = []

        signal_original_errors = []
        signal_exception_errors = []

        exception_count = 0

        surge_rescued = 0
        non_surge_exception = 0

        improved = 0
        worsened = 0
        same = 0

        for r in valid_results:

            adjusted_rate = (
                r["expected_rate"]
            )

            # =============================================
            # 현재 V3 신호가 실제 발생했고,
            # 거래상승률이 매우 강하면
            # 50% 둔화보정을 가상으로 해제
            #
            # 현재 expected_rate는 이미 50% 적용값이므로
            # 원래 값으로 복원하기 위해 ×2
            # =============================================

            exception_signal = (
                r["slowdown_signal"]
                and
                r["rise_rate"] >= threshold
            )

            if exception_signal:

                adjusted_rate = (
                    r["expected_rate"]
                    * 2
                )

                exception_count += 1

                if (
                    r["actual_change_rate"]
                    >= 15.0
                ):
                    surge_rescued += 1

                else:
                    non_surge_exception += 1

            # ---------------------------------------------
            # 기존 실제 엔진 예상가격
            # ---------------------------------------------

            original_price = (
                r["current_price"]
                * (
                    1
                    + r["expected_rate"] / 100
                )
            )

            # ---------------------------------------------
            # V6 가상 예외 적용 예상가격
            # ---------------------------------------------

            adjusted_price = (
                r["current_price"]
                * (
                    1
                    + adjusted_rate / 100
                )
            )

            original_error = abs(
                (
                    original_price
                    - r["actual_future_price"]
                )
                / r["actual_future_price"]
                * 100
            )

            adjusted_error = abs(
                (
                    adjusted_price
                    - r["actual_future_price"]
                )
                / r["actual_future_price"]
                * 100
            )

            all_errors.append(
                adjusted_error
            )

            if exception_signal:

                signal_original_errors.append(
                    original_error
                )

                signal_exception_errors.append(
                    adjusted_error
                )

                diff = (
                    original_error
                    - adjusted_error
                )

                if diff > 0.001:
                    improved += 1

                elif diff < -0.001:
                    worsened += 1

                else:
                    same += 1

        total_mae = (
            sum(all_errors)
            / len(all_errors)
        )

        if signal_exception_errors:

            signal_original_mae = (
                sum(signal_original_errors)
                / len(signal_original_errors)
            )

            signal_exception_mae = (
                sum(signal_exception_errors)
                / len(signal_exception_errors)
            )

        else:

            signal_original_mae = 0.0
            signal_exception_mae = 0.0

        search_results.append({
            "threshold": threshold,
            "total_mae": total_mae,

            "exception_count": exception_count,

            "surge_rescued": surge_rescued,

            "non_surge_exception":
                non_surge_exception,

            "signal_original_mae":
                signal_original_mae,

            "signal_exception_mae":
                signal_exception_mae,

            "improved": improved,
            "worsened": worsened,
            "same": same
        })

    # =====================================================
    # 전체 MAE 낮은 순서
    # =====================================================

    search_results.sort(
        key=lambda x: (
            x["total_mae"],
            -x["surge_rescued"],
            x["non_surge_exception"]
        )
    )

    print(
        f"전체 테스트 : "
        f"{len(valid_results)}건"
    )

    slowdown_count = sum(
        1
        for r in valid_results
        if r["slowdown_signal"]
    )

    print(
        f"실제 상승둔화 V3 신호 : "
        f"{slowdown_count}건"
    )

    print(
        f"현재 실제엔진 MAE : "
        f"{original_mae:.2f}%"
    )

    print()

    print(
        "순위 | 거래상승률 예외기준 | "
        "예외적용 | 급등구제 | 비급등예외 | "
        "전체 MAE | 신호기존 MAE | 신호예외 MAE | "
        "개선/악화/동일"
    )

    print("-" * 120)

    for rank, x in enumerate(
        search_results,
        start=1
    ):

        print(
            f"{rank}위 | "
            f"거래상승률 >= "
            f"+{x['threshold']:.1f}% | "
            f"예외 {x['exception_count']}건 | "
            f"급등구제 {x['surge_rescued']}건 | "
            f"비급등 {x['non_surge_exception']}건 | "
            f"전체 {x['total_mae']:.2f}% | "
            f"신호기존 "
            f"{x['signal_original_mae']:.2f}% | "
            f"신호예외 "
            f"{x['signal_exception_mae']:.2f}% | "
            f"{x['improved']}/"
            f"{x['worsened']}/"
            f"{x['same']}"
        )

    if search_results:

        best = (
            search_results[0]
        )

        print("-" * 120)

        print(
            f"🏆 최적 거래상승률 예외 기준 : "
            f"+{best['threshold']:.1f}% 이상"
        )

        print(
            f"🏆 예외 적용 : "
            f"{best['exception_count']}건"
        )

        print(
            f"🏆 실제 급등 구제 : "
            f"{best['surge_rescued']}건"
        )

        print(
            f"🏆 비급등 예외 적용 : "
            f"{best['non_surge_exception']}건"
        )

        print(
            f"🏆 현재 엔진 MAE : "
            f"{original_mae:.2f}%"
        )

        print(
            f"🏆 V6 가상정책 MAE : "
            f"{best['total_mae']:.2f}%"
        )

        print(
            f"🏆 전체 개선효과 : "
            f"{original_mae - best['total_mae']:+.2f}%p"
        )

        print(
            f"🏆 예외구간 기존 MAE : "
            f"{best['signal_original_mae']:.2f}%"
        )

        print(
            f"🏆 예외구간 V6 MAE : "
            f"{best['signal_exception_mae']:.2f}%"
        )

        print(
            f"🏆 예외구간 개선 / 악화 / 동일 : "
            f"{best['improved']} / "
            f"{best['worsened']} / "
            f"{best['same']}"
        )

    print("=" * 120)   

def analyze_surge_lead_time_v7(results):

    print()
    print("=" * 125)
    print("🚀 급등 조기신호 V7 선행기간 분석")
    print("=" * 125)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        actual_future_price = float(
            r.get("actual_future_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if (
            current_price <= 0
            or actual_future_price <= 0
        ):
            continue

        actual_change_rate = (
            (
                actual_future_price
                - current_price
            )
            / current_price
            * 100
        )

        rate_gap = (
            rise_rate
            - expected_rate
        )

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        # =====================================================
        # 급등 V1 관심신호
        # =====================================================

        surge_signal = (
            rise_rate >= 4.0
            and rate_gap >= 2.0
        )

        # =====================================================
        # 강한 급등신호
        # =====================================================

        strong_surge_signal = (
            surge_signal
            and previous_count > 0
            and volume_ratio >= 2.0
        )

        valid_results.append({
            "apt_name": r.get(
                "apt_name",
                ""
            ),

            "analysis_date": r.get(
                "analysis_date",
                ""
            ),

            "actual_change_rate":
                actual_change_rate,

            "rise_rate":
                rise_rate,

            "expected_rate":
                expected_rate,

            "rate_gap":
                rate_gap,

            "volume_ratio":
                volume_ratio,

            "surge_signal":
                surge_signal,

            "strong_surge_signal":
                strong_surge_signal
        })

    if not valid_results:

        print(
            "검증 가능한 데이터가 없습니다."
        )

        return

    # =====================================================
    # 날짜순 정렬
    # =====================================================

    valid_results.sort(
        key=lambda x: (
            x["apt_name"],
            str(x["analysis_date"])
        )
    )

    # =====================================================
    # 단지별 분리
    # =====================================================

    apt_groups = {}

    for r in valid_results:

        apt_name = r["apt_name"]

        if apt_name not in apt_groups:
            apt_groups[apt_name] = []

        apt_groups[apt_name].append(r)

    # =====================================================
    # 선행개월 집계
    #
    # 기준:
    # 신호 발생 후 1~6개월 내
    # 실제 +15% 이상 급등 여부 확인
    # =====================================================

    lead_months = [
        1,
        2,
        3,
        4,
        5,
        6
    ]

    summary = {
        month: {
            "signal_count": 0,
            "caught": 0,
            "strong_signal_count": 0,
            "strong_caught": 0
        }
        for month in lead_months
    }

    detail_rows = []

    for apt_name, rows in apt_groups.items():

        for i, row in enumerate(rows):

            if not row["surge_signal"]:
                continue

            for lead in lead_months:

                target_index = (
                    i + lead
                )

                if target_index >= len(rows):
                    continue

                future_row = (
                    rows[target_index]
                )

                summary[lead][
                    "signal_count"
                ] += 1

                if (
                    future_row[
                        "actual_change_rate"
                    ] >= 15.0
                ):
                    summary[lead][
                        "caught"
                    ] += 1

                    detail_rows.append({
                        "apt_name":
                            apt_name,

                        "signal_date":
                            row[
                                "analysis_date"
                            ],

                        "lead":
                            lead,

                        "target_date":
                            future_row[
                                "analysis_date"
                            ],

                        "rise_rate":
                            row[
                                "rise_rate"
                            ],

                        "expected_rate":
                            row[
                                "expected_rate"
                            ],

                        "volume_ratio":
                            row[
                                "volume_ratio"
                            ],

                        "future_change":
                            future_row[
                                "actual_change_rate"
                            ],

                        "strong":
                            row[
                                "strong_surge_signal"
                            ]
                    })

                if (
                    row[
                        "strong_surge_signal"
                    ]
                ):

                    summary[lead][
                        "strong_signal_count"
                    ] += 1

                    if (
                        future_row[
                            "actual_change_rate"
                        ] >= 15.0
                    ):
                        summary[lead][
                            "strong_caught"
                        ] += 1

    # =====================================================
    # 출력
    # =====================================================

    print(
        "선행기간 | 일반신호 | 급등포착 | 적중률 | "
        "강한신호 | 급등포착 | 강한신호 적중률"
    )

    print("-" * 125)

    for lead in lead_months:

        s = summary[lead]

        signal_count = (
            s["signal_count"]
        )

        caught = (
            s["caught"]
        )

        strong_count = (
            s["strong_signal_count"]
        )

        strong_caught = (
            s["strong_caught"]
        )

        hit_rate = (
            caught
            / signal_count
            * 100
            if signal_count > 0
            else 0
        )

        strong_hit_rate = (
            strong_caught
            / strong_count
            * 100
            if strong_count > 0
            else 0
        )

        print(
            f"{lead}개월 | "
            f"{signal_count:>3}건 | "
            f"{caught:>3}건 | "
            f"{hit_rate:>5.1f}% | "
            f"{strong_count:>3}건 | "
            f"{strong_caught:>3}건 | "
            f"{strong_hit_rate:>5.1f}%"
        )

    print("-" * 125)

    # =====================================================
    # 실제 포착 사례 일부 출력
    # =====================================================

    detail_rows.sort(
        key=lambda x: (
            x["lead"],
            -x["future_change"]
        )
    )

    print()
    print(
        "📋 급등신호 → 실제 급등 사례"
    )

    print("-" * 125)

    for x in detail_rows[:40]:

        strength = (
            "🔥 강한신호"
            if x["strong"]
            else "관심신호"
        )

        print(
            f'{x["apt_name"]} | '
            f'신호 {x["signal_date"]} | '
            f'{x["lead"]}개월 후 | '
            f'목표 {x["target_date"]} | '
            f'거래상승률 '
            f'{x["rise_rate"]:+.2f}% | '
            f'예상 '
            f'{x["expected_rate"]:+.2f}% | '
            f'거래비 '
            f'{x["volume_ratio"]:.2f}배 | '
            f'실제 '
            f'{x["future_change"]:+.2f}% | '
            f'{strength}'
        )

    print("=" * 125)   

def analyze_surge_price_path_v71(results):

    print()
    print("=" * 130)
    print("🚀 급등 조기신호 V7.1 실제가격 경로 분석")
    print("=" * 130)

    valid_results = []

    for r in results:

        current_price = float(
            r.get("current_price", 0) or 0
        )

        rise_rate = float(
            r.get("rise_rate", 0) or 0
        )

        expected_rate = float(
            r.get("expected_rate", 0) or 0
        )

        previous_count = int(
            r.get("previous_3m_count", 0) or 0
        )

        recent_count = int(
            r.get("recent_3m_count", 0) or 0
        )

        if current_price <= 0:
            continue

        if previous_count > 0:
            volume_ratio = (
                recent_count
                / previous_count
            )
        else:
            volume_ratio = 0.0

        rate_gap = (
            rise_rate
            - expected_rate
        )

        surge_signal = (
            rise_rate >= 4.0
            and rate_gap >= 2.0
        )

        strong_surge_signal = (
            surge_signal
            and previous_count > 0
            and volume_ratio >= 2.0
        )

        valid_results.append({
            "apt_name": r.get(
                "apt_name",
                ""
            ),

            "analysis_date": r.get(
                "analysis_date",
                ""
            ),

            "current_price":
                current_price,

            "rise_rate":
                rise_rate,

            "expected_rate":
                expected_rate,

            "rate_gap":
                rate_gap,

            "volume_ratio":
                volume_ratio,

            "surge_signal":
                surge_signal,

            "strong_surge_signal":
                strong_surge_signal
        })

    if not valid_results:

        print(
            "검증 가능한 데이터가 없습니다."
        )

        return

    # =====================================================
    # 단지별 날짜순 정렬
    # =====================================================

    valid_results.sort(
        key=lambda x: (
            x["apt_name"],
            str(x["analysis_date"])
        )
    )

    apt_groups = {}

    for r in valid_results:

        apt_name = r["apt_name"]

        if apt_name not in apt_groups:
            apt_groups[apt_name] = []

        apt_groups[apt_name].append(r)

    # =====================================================
    # 1~6개월 실제 가격변동 경로 집계
    # =====================================================

    lead_months = [
        1,
        2,
        3,
        4,
        5,
        6
    ]

    summary = {
        lead: {
            "signal_count": 0,
            "sum_change": 0.0,
            "rise_5": 0,
            "rise_10": 0,
            "rise_15": 0,

            "strong_count": 0,
            "strong_sum_change": 0.0,
            "strong_rise_5": 0,
            "strong_rise_10": 0,
            "strong_rise_15": 0
        }
        for lead in lead_months
    }

    detail_rows = []

    for apt_name, rows in apt_groups.items():

        for i, row in enumerate(rows):

            if not row["surge_signal"]:
                continue

            base_price = (
                row["current_price"]
            )

            path_changes = []

            for lead in lead_months:

                target_index = (
                    i + lead
                )

                if target_index >= len(rows):
                    continue

                target_row = (
                    rows[target_index]
                )

                target_price = (
                    target_row[
                        "current_price"
                    ]
                )

                if (
                    base_price <= 0
                    or target_price <= 0
                ):
                    continue

                actual_change = (
                    (
                        target_price
                        - base_price
                    )
                    / base_price
                    * 100
                )

                path_changes.append(
                    (
                        lead,
                        actual_change
                    )
                )

                s = summary[lead]

                s["signal_count"] += 1
                s["sum_change"] += (
                    actual_change
                )

                if actual_change >= 5:
                    s["rise_5"] += 1

                if actual_change >= 10:
                    s["rise_10"] += 1

                if actual_change >= 15:
                    s["rise_15"] += 1

                if row[
                    "strong_surge_signal"
                ]:

                    s["strong_count"] += 1

                    s[
                        "strong_sum_change"
                    ] += actual_change

                    if actual_change >= 5:
                        s[
                            "strong_rise_5"
                        ] += 1

                    if actual_change >= 10:
                        s[
                            "strong_rise_10"
                        ] += 1

                    if actual_change >= 15:
                        s[
                            "strong_rise_15"
                        ] += 1

            # =================================================
            # 신호 1건별 1~6개월 최대 상승폭
            # =================================================

            if path_changes:

                max_lead, max_change = max(
                    path_changes,
                    key=lambda x: x[1]
                )

                detail_rows.append({
                    "apt_name":
                        apt_name,

                    "signal_date":
                        row[
                            "analysis_date"
                        ],

                    "rise_rate":
                        row[
                            "rise_rate"
                        ],

                    "expected_rate":
                        row[
                            "expected_rate"
                        ],

                    "volume_ratio":
                        row[
                            "volume_ratio"
                        ],

                    "max_lead":
                        max_lead,

                    "max_change":
                        max_change,

                    "strong":
                        row[
                            "strong_surge_signal"
                        ],

                    "path":
                        path_changes
                })

    # =====================================================
    # 요약 출력
    # =====================================================

    print(
        "선행기간 | 일반신호 | 평균상승률 | "
        "+5% | +10% | +15% | "
        "강한신호 | 평균상승률 | "
        "+5% | +10% | +15%"
    )

    print("-" * 130)

    for lead in lead_months:

        s = summary[lead]

        count = (
            s["signal_count"]
        )

        strong_count = (
            s["strong_count"]
        )

        avg_change = (
            s["sum_change"]
            / count
            if count > 0
            else 0
        )

        strong_avg = (
            s["strong_sum_change"]
            / strong_count
            if strong_count > 0
            else 0
        )

        print(
            f"{lead}개월 | "
            f"{count:>3}건 | "
            f"{avg_change:+6.2f}% | "
            f"{s['rise_5']:>3} | "
            f"{s['rise_10']:>3} | "
            f"{s['rise_15']:>3} | "
            f"{strong_count:>3}건 | "
            f"{strong_avg:+6.2f}% | "
            f"{s['strong_rise_5']:>3} | "
            f"{s['strong_rise_10']:>3} | "
            f"{s['strong_rise_15']:>3}"
        )

    print("-" * 130)

    # =====================================================
    # 6개월 내 최대상승률 기준 통계
    # =====================================================

    if detail_rows:

        max_5 = sum(
            1
            for x in detail_rows
            if x["max_change"] >= 5
        )

        max_10 = sum(
            1
            for x in detail_rows
            if x["max_change"] >= 10
        )

        max_15 = sum(
            1
            for x in detail_rows
            if x["max_change"] >= 15
        )

        total = len(
            detail_rows
        )

        print(
            f"신호 발생 후 6개월 내 "
            f"최대 +5% 이상 : "
            f"{max_5}/{total}건 "
            f"({max_5 / total * 100:.1f}%)"
        )

        print(
            f"신호 발생 후 6개월 내 "
            f"최대 +10% 이상 : "
            f"{max_10}/{total}건 "
            f"({max_10 / total * 100:.1f}%)"
        )

        print(
            f"신호 발생 후 6개월 내 "
            f"최대 +15% 이상 : "
            f"{max_15}/{total}건 "
            f"({max_15 / total * 100:.1f}%)"
        )

    print()
    print("-" * 130)
    print("📋 신호별 실제 가격경로")
    print("-" * 130)

    detail_rows.sort(
        key=lambda x: (
            -x["max_change"]
        )
    )

    for x in detail_rows[:30]:

        path_text = " | ".join(
            f"{lead}M {change:+.1f}%"
            for lead, change in x["path"]
        )

        strength = (
            "🔥 강한신호"
            if x["strong"]
            else "관심신호"
        )

        print(
            f'{x["apt_name"]} | '
            f'{x["signal_date"]} | '
            f'거래상승률 '
            f'{x["rise_rate"]:+.2f}% | '
            f'예상 '
            f'{x["expected_rate"]:+.2f}% | '
            f'거래비 '
            f'{x["volume_ratio"]:.2f}배 | '
            f'최대 '
            f'{x["max_change"]:+.2f}% '
            f'({x["max_lead"]}개월) | '
            f'{strength} | '
            f'{path_text}'
        )

    print("=" * 130)

def analyze_surge_price_consistency_v72(results):

    print()
    print("=" * 120)
    print("🔍 급등 V7.2 가격 기준 일치성 검증")
    print("=" * 120)

    # 대표 검증 사례
    target_apt = "잠실엘스"
    target_date = "2025-05-06"

    target = None

    # =====================================================
    # ① 기준일 데이터 찾기
    # =====================================================

    for r in results:

        apt_name = str(
            r.get("apt_name", "")
        )

        analysis_date = str(
            r.get("analysis_date", "")
        )

        if (
            apt_name == target_apt
            and analysis_date == target_date
        ):
            target = r
            break

    if target is None:

        print(
            f"⚠️ 대상 데이터를 찾지 못했습니다: "
            f"{target_apt} | {target_date}"
        )

        return

    # =====================================================
    # ② 기준일 데이터
    # =====================================================

    current_price = float(
        target.get(
            "current_price",
            0
        ) or 0
    )

    actual_future_price = float(
        target.get(
            "actual_future_price",
            0
        ) or 0
    )

    actual_change_rate = float(
        target.get(
            "actual_change_rate",
            0
        ) or 0
    )

    expected_rate = float(
        target.get(
            "expected_rate",
            0
        ) or 0
    )

    # =====================================================
    # ③ results에서 6개월 뒤 행 찾기
    #
    # 현재 테스트가 매월 6일 기준이므로
    # 2025-05-06 → 2025-11-06
    # =====================================================

    from datetime import datetime

    try:

        base_date = datetime.strptime(
            target_date,
            "%Y-%m-%d"
        )

        target_month_index = (
            base_date.year * 12
            + base_date.month
            + 6
        )

        future_year = (
            (target_month_index - 1)
            // 12
        )

        future_month = (
            (target_month_index - 1)
            % 12
            + 1
        )

        future_date = (
            f"{future_year:04d}-"
            f"{future_month:02d}-"
            f"{base_date.day:02d}"
        )

    except Exception as e:

        print(
            "⚠️ 날짜 계산 오류:",
            e
        )

        return

    future_row = None

    for r in results:

        if (
            str(
                r.get(
                    "apt_name",
                    ""
                )
            ) == target_apt

            and

            str(
                r.get(
                    "analysis_date",
                    ""
                )
            ) == future_date
        ):

            future_row = r
            break

    # =====================================================
    # ④ 6개월 뒤 current_price
    # =====================================================

    future_current_price = 0.0

    if future_row:

        future_current_price = float(
            future_row.get(
                "current_price",
                0
            ) or 0
        )

    # =====================================================
    # ⑤ current_price끼리 직접 변동률
    # =====================================================

    current_price_path_rate = None

    if (
        current_price > 0
        and future_current_price > 0
    ):

        current_price_path_rate = (
            (
                future_current_price
                - current_price
            )
            / current_price
            * 100
        )

    # =====================================================
    # ⑥ actual_change_rate 역산
    #
    # actual_future_price가 존재한다면
    # actual_change_rate가 current_price 기준인지 확인
    # =====================================================

    recalculated_actual_rate = None

    if (
        current_price > 0
        and actual_future_price > 0
    ):

        recalculated_actual_rate = (
            (
                actual_future_price
                - current_price
            )
            / current_price
            * 100
        )

    # =====================================================
    # 출력
    # =====================================================

    print(
        f"단지명                : "
        f"{target_apt}"
    )

    print(
        f"분석기준일            : "
        f"{target_date}"
    )

    print(
        f"6개월 뒤 비교일       : "
        f"{future_date}"
    )

    print("-" * 120)

    print(
        f"① 기준일 current_price        : "
        f"{current_price:,.0f}만원"
    )

    print(
        f"② actual_future_price         : "
        f"{actual_future_price:,.0f}만원"
    )

    print(
        f"③ 저장 actual_change_rate     : "
        f"{actual_change_rate:+.2f}%"
    )

    print(
        f"   저장 expected_rate          : "
        f"{expected_rate:+.2f}%"
    )

    print("-" * 120)

    if future_row:

        print(
            f"④ {future_date} current_price : "
            f"{future_current_price:,.0f}만원"
        )

    else:

        print(
            f"④ {future_date} current_price : "
            f"데이터 없음"
        )

    if current_price_path_rate is not None:

        print(
            f"⑤ current_price 직접변동률     : "
            f"{current_price_path_rate:+.2f}%"
        )

    else:

        print(
            "⑤ current_price 직접변동률     : "
            "계산 불가"
        )

    print("-" * 120)

    if recalculated_actual_rate is not None:

        print(
            f"⑥ current→actual_future 재계산 : "
            f"{recalculated_actual_rate:+.2f}%"
        )

        difference = (
            actual_change_rate
            - recalculated_actual_rate
        )

        print(
            f"⑦ 저장률-재계산률 차이         : "
            f"{difference:+.4f}%p"
        )

    else:

        print(
            "⑥ current→actual_future 재계산 : "
            "계산 불가"
        )

    print("-" * 120)

    # =====================================================
    # 자동 판정
    # =====================================================

    if recalculated_actual_rate is not None:

        difference = abs(
            actual_change_rate
            - recalculated_actual_rate
        )

        if difference <= 0.1:

            print(
                "✅ actual_change_rate는 "
                "current_price → actual_future_price "
                "기준으로 계산된 것으로 보입니다."
            )

        else:

            print(
                "⚠️ actual_change_rate는 "
                "current_price → actual_future_price "
                "단순 계산과 일치하지 않습니다."
            )

    if (
        current_price_path_rate is not None
        and actual_change_rate is not None
    ):

        path_gap = (
            actual_change_rate
            - current_price_path_rate
        )

        print(
            f"📌 기존 실제변동률과 "
            f"V7.1 가격경로 차이 : "
            f"{path_gap:+.2f}%p"
        )

    print("=" * 120)

def run_backtest_batch(
    region,
    analysis_date,
    candidates
):
    """
    여러 단지/면적을 한꺼번에 백테스트하고
    현재가격 엔진 vs 6개월 미래예측 엔진의
    평균 정확도를 비교한다.
    """
    print(
        "🔥 실제 실행 run_backtest_batch 위치 :",
        run_backtest_batch.__code__.co_firstlineno
    )
    print()
    print("========== BATCH 입력 후보 확인 ==========")
    print(f"입력 후보수 : {len(candidates)}개")

    for i, c in enumerate(candidates, 1):
        print(
            f"[{i}] "
            f"{c.get('apt_name')} | "
            f"{c.get('size')}㎡"
        )

    print("========================================")

    print()
    print("============================================================")
    print("              BATCH PRICE ENGINE BACKTEST")
    print("============================================================")

    print(f"지역 : {region}")
    print(f"분석기준일 : {analysis_date}")
    print(f"테스트 후보수 : {len(candidates)}개")
    print()

    batch_results = []

    for index, candidate in enumerate(
        candidates,
        start=1
    ):

        apt_name = candidate["apt_name"]
        size = candidate["size"]

        print()
        print("############################################################")
        print(
            f"[{index}/{len(candidates)}] "
            f"{apt_name} | {size}㎡"
        )
        print("############################################################")

        try:

            result = run_backtest(
                region=region,
                apt_name=apt_name,
                size=size,
                analysis_date=analysis_date
            )

            summary = result.get(
                "백테스트요약",
                {}
            )

            trend_confidence = summary.get(
                "추세신뢰도",
                "미확인"
            )

            current_abs_error = float(
                summary.get(
                    "현재가격절대오차율",
                    0
                ) or 0
            )

            future_abs_error = float(
                summary.get(
                    "6개월예측절대오차율",
                    0
                ) or 0
            )

            future_trade_count = int(
                summary.get(
                    "미래거래건수",
                    0
                ) or 0
            )

            # 미래 실제거래가 없는 테스트는 제외
            if future_trade_count <= 0:
                print(
                    "⚠️ 미래 거래 없음 → "
                    "집계에서 제외"
                )
                continue

            improvement = round(
                current_abs_error
                - future_abs_error,
                2
            )

            current_price = summary.get(
                "현재기준가격",
                0
            )

            predicted_price = summary.get(
                "6개월예상중심가",
                0
            )

            actual_price = summary.get(
                "미래실제절사평균",
                0
            )


            # =================================================
            # ✅ 유효 가격 검증
            # 0원 또는 음수 가격은 백테스트 집계에서 제외
            # =================================================

            if (
                not current_price
                or not predicted_price
                or not actual_price
                or current_price <= 0
                or predicted_price <= 0
                or actual_price <= 0
            ):
                print(
                    f"⚠️ 백테스트 제외 : "
                    f"{apt_name} | {size}㎡ "
                    f"(유효 가격 없음)"
                )
                continue


            batch_results.append({
                "apt_name": apt_name,
                "size": size,

                "current_price": current_price,
                "predicted_price": predicted_price,
                "actual_price": actual_price,

                "current_error": current_abs_error,
                "future_error": future_abs_error,

                "improvement": improvement,

                "future_trade_count": (
                    future_trade_count
                ),
                "trend_confidence": trend_confidence
            })
            print(
                f"🔥 append 완료 : "
                f"{index}/{len(candidates)} | "
                f"{apt_name} | "
                f"batch_results={len(batch_results)}개"
            )
        except Exception as e:

            print(
                f"❌ 백테스트 실패 : "
                f"{apt_name} | {size}㎡ | {e}"
            )

            continue


    # =================================================
    # ✅ 개별 결과 요약
    # =================================================

    print()
    print()
    print("============================================================")
    print("                  일괄 백테스트 결과")
    print("============================================================")

    if not batch_results:

        print("유효한 백테스트 결과가 없습니다.")
        print("============================================================")

        return []


    for index, row in enumerate(
        batch_results,
        start=1
    ):

        print()
        print(
            f"[{index}] "
            f"{row['apt_name']} | "
            f"{row['size']}㎡"
        )

        print(
            f"    현재 기준가격 : "
            f"{row['current_price']:,}만원"
        )

        print(
            f"    6개월 예상가 : "
            f"{row['predicted_price']:,}만원"
        )

        print(
            f"    미래 실제가격 : "
            f"{row['actual_price']:,}만원"
        )

        print(
            f"    현재가격 절대오차 : "
            f"{row['current_error']:.2f}%"
        )

        print(
            f"    미래예측 절대오차 : "
            f"{row['future_error']:.2f}%"
        )

        print(
            f"    개선폭 : "
            f"{row['improvement']:+.2f}%p"
        )

        print(
            f"    추세신뢰도 : "
            f"{row['trend_confidence']}"
        )

        if row["improvement"] > 0:
            verdict = "개선"

        elif row["improvement"] < 0:
            verdict = "악화"

        else:
            verdict = "동일"

        print(
            f"    결과 : {verdict}"
        )

        print()
        print(
            "🔥 전체 통계 직전 batch_results :",
            len(batch_results)
        )
    # =================================================
    # ✅ 전체 통계
    # =================================================

    current_errors = [
        row["current_error"]
        for row in batch_results
    ]

    future_errors = [
        row["future_error"]
        for row in batch_results
    ]

    avg_current_error = round(
        sum(current_errors)
        / len(current_errors),
        2
    )

    avg_future_error = round(
        sum(future_errors)
        / len(future_errors),
        2
    )

    total_improvement = round(
        avg_current_error
        - avg_future_error,
        2
    )

    improved_count = sum(
        1
        for row in batch_results
        if row["improvement"] > 0
    )

    worsened_count = sum(
        1
        for row in batch_results
        if row["improvement"] < 0
    )

    same_count = (
        len(batch_results)
        - improved_count
        - worsened_count
    )

    print()
    print("============================================================")
    print("                    전체 성능 요약")
    print("============================================================")

    print(
        f"유효 테스트 : "
        f"{len(batch_results)}개"
    )

    print(
        f"현재가격 평균 절대오차 : "
        f"{avg_current_error:.2f}%"
    )

    print(
        f"미래예측 평균 절대오차 : "
        f"{avg_future_error:.2f}%"
    )

    print(
        f"평균 개선폭 : "
        f"{total_improvement:+.2f}%p"
    )

    print(
        f"개선된 사례 : "
        f"{improved_count}개"
    )

    print(
        f"악화된 사례 : "
        f"{worsened_count}개"
    )

    print(
        f"동일한 사례 : "
        f"{same_count}개"
    )


    if avg_future_error < avg_current_error:

        print()
        print(
            "✅ 미래예측 엔진이 전체적으로 "
            "현재가격 기준보다 개선됨"
        )

    elif avg_future_error > avg_current_error:

        print()
        print(
            "⚠️ 미래예측 엔진이 전체적으로 "
            "현재가격 기준보다 악화됨"
        )

    else:

        print()
        print(
            "➖ 두 방식의 평균 정확도가 동일함"
        )

    print("============================================================")

    return batch_results

# =========================================================
# ✅ 미래예측 엔진 V1 기준 성능
#
# 검증일 : 2026-01-01
# 지역 :
#   - 경기도 의왕시
#   - 서울특별시 강동구
#   - 경기도 수원시 영통구
#
# 전체 표본 : 50
#
# 현재가격 MAE : 8.02%
# 미래예측 MAE : 7.13%
# 평균 개선폭 : +0.89%p
#
# 상승군 :
#   현재 8.75% → 미래 7.33%
#   개선 +1.42%p
#
# 하락군 :
#   현재 6.93% → 미래 6.83%
#   개선 +0.10%p
#
# 추세 정책 :
#   상승 = 100% 반영
#   하락 = 50% 반영
#
# ※ 향후 엔진 수정 시 반드시 이 성능과 비교
# =========================================================

def run_backtest_multi_region(
    regions,
    analysis_date,
    rise_limit=10,
    decline_limit=10,
    min_rise_rate=3.0,
    min_decline_rate=-3.0
):
    """
    여러 지역의 상승/하락 후보를 자동 검색한 뒤
    run_backtest_batch()로 일괄 검증하고
    전체 성능을 통합 집계한다.
    """

    print()
    print("============================================================")
    print("              미래예측 엔진 종합 백테스트")
    print("============================================================")
    print(f"분석기준일 : {analysis_date}")
    print(f"지역수 : {len(regions)}개")
    print()

    all_results = []

    rise_results = []
    decline_results = []


    for region in regions:

        print()
        print("############################################################")
        print(f"지역 : {region}")
        print("############################################################")


        # =================================================
        # ✅ 상승 후보 검색
        # =================================================

        print()
        print("========== 상승 후보 검증 ==========")

        rise_rows = find_rising_backtest_candidates(
            region=region,
            analysis_date=analysis_date,
            min_rise_rate=min_rise_rate,
            limit=rise_limit
        )

        rise_candidates = [
            {
                "apt_name": row[0],
                "size": float(row[1])
            }
            for row in rise_rows
        ]


        if rise_candidates:

            region_rise_results = run_backtest_batch(
                region=region,
                analysis_date=analysis_date,
                candidates=rise_candidates
            )

            for row in region_rise_results:

                row["region"] = region
                row["trend_type"] = "상승"

            rise_results.extend(
                region_rise_results
            )

            all_results.extend(
                region_rise_results
            )

        else:

            print(
                f"⚠️ {region} 상승 후보 없음"
            )


        # =================================================
        # ✅ 하락 후보 검색
        # =================================================

        print()
        print("========== 하락 후보 검증 ==========")

        decline_rows = find_backtest_candidates(
            region=region,
            analysis_date=analysis_date,
            min_decline_rate=min_decline_rate,
            limit=decline_limit
        )

        decline_candidates = [
            {
                "apt_name": row[0],
                "size": float(row[1])
            }
            for row in decline_rows
        ]


        if decline_candidates:

            region_decline_results = run_backtest_batch(
                region=region,
                analysis_date=analysis_date,
                candidates=decline_candidates
            )

            for row in region_decline_results:

                row["region"] = region
                row["trend_type"] = "하락"

            decline_results.extend(
                region_decline_results
            )

            all_results.extend(
                region_decline_results
            )

        else:

            print(
                f"⚠️ {region} 하락 후보 없음"
            )


    # =================================================
    # ✅ 그룹 통계 함수
    # =================================================

    def calculate_summary(results):

        if not results:

            return {
                "count": 0,
                "current_mae": 0,
                "future_mae": 0,
                "improvement": 0,
                "improved_count": 0,
                "worsened_count": 0,
                "same_count": 0
            }


        current_errors = [
            row["current_error"]
            for row in results
        ]

        future_errors = [
            row["future_error"]
            for row in results
        ]


        current_mae = round(
            sum(current_errors)
            / len(current_errors),
            2
        )

        future_mae = round(
            sum(future_errors)
            / len(future_errors),
            2
        )

        improvement = round(
            current_mae
            - future_mae,
            2
        )


        improved_count = sum(
            1
            for row in results
            if row["improvement"] > 0
        )

        worsened_count = sum(
            1
            for row in results
            if row["improvement"] < 0
        )

        same_count = (
            len(results)
            - improved_count
            - worsened_count
        )


        return {
            "count": len(results),
            "current_mae": current_mae,
            "future_mae": future_mae,
            "improvement": improvement,
            "improved_count": improved_count,
            "worsened_count": worsened_count,
            "same_count": same_count
        }


    # =================================================
    # ✅ 상승 / 하락 / 전체 통계
    # =================================================

    rise_summary = calculate_summary(
        rise_results
    )

    decline_summary = calculate_summary(
        decline_results
    )

    total_summary = calculate_summary(
        all_results
    )


    # =================================================
    # ✅ 종합 결과 출력
    # =================================================

    print()
    print()
    print("============================================================")
    print("              미래예측 엔진 종합 검증")
    print("============================================================")

    print(
        f"지역수 : "
        f"{len(regions)}개"
    )

    print(
        f"전체 유효 테스트 : "
        f"{total_summary['count']}개"
    )


    print()
    print("[상승군]")

    print(
        f"표본 : "
        f"{rise_summary['count']}개"
    )

    print(
        f"현재가격 MAE : "
        f"{rise_summary['current_mae']:.2f}%"
    )

    print(
        f"미래예측 MAE : "
        f"{rise_summary['future_mae']:.2f}%"
    )

    print(
        f"평균 개선폭 : "
        f"{rise_summary['improvement']:+.2f}%p"
    )

    print(
        f"개선 : "
        f"{rise_summary['improved_count']}개"
    )

    print(
        f"악화 : "
        f"{rise_summary['worsened_count']}개"
    )


    print()
    print("[하락군]")

    print(
        f"표본 : "
        f"{decline_summary['count']}개"
    )

    print(
        f"현재가격 MAE : "
        f"{decline_summary['current_mae']:.2f}%"
    )

    print(
        f"미래예측 MAE : "
        f"{decline_summary['future_mae']:.2f}%"
    )

    print(
        f"평균 개선폭 : "
        f"{decline_summary['improvement']:+.2f}%p"
    )

    print(
        f"개선 : "
        f"{decline_summary['improved_count']}개"
    )

    print(
        f"악화 : "
        f"{decline_summary['worsened_count']}개"
    )


    print()
    print("[전체]")

    print(
        f"현재가격 MAE : "
        f"{total_summary['current_mae']:.2f}%"
    )

    print(
        f"미래예측 MAE : "
        f"{total_summary['future_mae']:.2f}%"
    )

    print(
        f"평균 개선폭 : "
        f"{total_summary['improvement']:+.2f}%p"
    )

    print(
        f"개선 : "
        f"{total_summary['improved_count']}개"
    )

    print(
        f"악화 : "
        f"{total_summary['worsened_count']}개"
    )

    print(
        f"동일 : "
        f"{total_summary['same_count']}개"
    )


    # =================================================
    # ✅ 최종 판정
    # =================================================

    print()

    if (
        total_summary["count"] >= 20
        and total_summary["future_mae"]
        < total_summary["current_mae"]
    ):

        print(
            "✅ 종합 판정 : "
            "미래예측 엔진 V1 성능 개선 확인"
        )

    elif (
        total_summary["future_mae"]
        < total_summary["current_mae"]
    ):

        print(
            "⚠️ 종합 판정 : "
            "성능은 개선됐지만 표본 추가 검증 필요"
        )

    else:

        print(
            "❌ 종합 판정 : "
            "현재가격 기준보다 개선되지 않음"
        )

    print("============================================================")


    return {
        "상승": rise_summary,
        "하락": decline_summary,
        "전체": total_summary,
        "results": all_results
    }

def get_backtest_sale_trades(
    region,
    apt_name,
    size
):
    """
    백테스트 전용 과거 매매 거래 조회
    운영 Supabase가 아니라
    backtest_history.db를 사용한다.
    """

    conn = sqlite3.connect(
        BACKTEST_DB_PATH
    )

    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    try:

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
                source_month,
                apt_dong
            FROM backtest_sale_trades
            WHERE TRIM(region || ' ' || sigungu) = ?
              AND apt_name = ?
              AND ROUND(size, 4) = ROUND(?, 4)
            ORDER BY contract_date DESC
        """, (
            region.strip(),
            apt_name.strip(),
            float(size)
        ))

        rows = cur.fetchall()

        result = []

        for row in rows:

            result.append({
                "region": row["region"],
                "sigungu": row["sigungu"],
                "dong": row["dong"],
                "apt_name": row["apt_name"],
                "size": row["size"],
                "date": row["contract_date"],
                "price": row["price"],
                "floor": row["floor"],
                "source_month": row["source_month"],
                "apt_dong": row["apt_dong"] or ""
            })

        print(
            "✅ 백테스트 과거거래 조회:",
            f"region=[{region}]",
            f"apt_name=[{apt_name}]",
            f"size=[{size}]",
            f"건수=[{len(result)}]"
        )

        return result

    finally:

        cur.close()
        conn.close()

@app.get("/admin", response_class=HTMLResponse)
def admin_page(pw: str = ""):

    if pw != ADMIN_PASSWORD:
        return """
        <html>
        <body>
            <h2>관리자 비밀번호가 필요합니다.</h2>
        </body>
        </html>
        """
    today_search = get_today_search_count()
    today_analysis = get_today_analysis_count()
    popular_apts = get_popular_apts()
    recent_analysis = get_recent_analysis()
    popular_regions = get_popular_regions()
    latest_collect_log = get_latest_collect_log()
    region_change_logs = get_region_change_logs(limit=20)

    if latest_collect_log:
        collect_status = latest_collect_log["status"]

        if collect_status == "success":
            collect_status_text = "성공 ✅"
        elif collect_status == "failed":
            collect_status_text = "실패 ❌"
        elif collect_status == "running":
            collect_status_text = "진행중 🔄"
        else:
            collect_status_text = collect_status
    else:
        collect_status_text = "-"

    if latest_collect_log:
        collect_started_at = latest_collect_log["started_at"].strftime("%Y-%m-%d %H:%M") if latest_collect_log["started_at"] else "-"
        collect_ended_at = latest_collect_log["ended_at"].strftime("%Y-%m-%d %H:%M") if latest_collect_log["ended_at"] else "진행 중"

        if latest_collect_log["started_at"] and latest_collect_log["ended_at"]:
            elapsed = latest_collect_log["ended_at"] - latest_collect_log["started_at"]
            total_seconds = int(elapsed.total_seconds())
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            collect_elapsed_time = f"{minutes}분 {seconds}초"
        else:
            collect_elapsed_time = "-"

        collect_last_region = (
            f'{latest_collect_log["last_sido"]} {latest_collect_log["last_sigungu"]}'
            if latest_collect_log["last_sido"] and latest_collect_log["last_sigungu"]
            else "-"
        )
        collect_error = latest_collect_log["error_message"] if latest_collect_log["error_message"] else "-"
    else:
        collect_started_at = "-"
        collect_ended_at = "-"
        collect_last_region = "-"
        collect_error = "-"

    return f"""
    <html>
    <head>
        <title>관리자 페이지</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f5f7fb;
                padding: 24px;
                color: #222;
            }}

            h1 {{
                background: #1f6feb;
                color: white;
                padding: 18px;
                border-radius: 12px;
                font-size: 24px;
            }}

            h2 {{
                margin-top: 28px;
                color: #1f6feb;
                font-size: 18px;
            }}
            
            .card {{
                background: white;
                padding: 16px;
                border-radius: 12px;
                margin-bottom: 16px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            }}

            li {{
                margin: 8px 0;
            }}
        </style>
        
    </head>
    <body>
        <h1>📊 얼마일까 관리자</h1>

        <div class="card">
            <h2>🤖 자동수집 현황</h2>

            <p><b>상태 :</b> {collect_status_text}</p>
            <p><b>시작 :</b> {collect_started_at}</p>
            <p><b>종료 :</b> {collect_ended_at}</p>
            <p><b>소요시간 :</b> {collect_elapsed_time}</p>

            <hr style="margin:16px 0; border:none; border-top:1px solid #e5e7eb;">

            <h3 style="margin:0 0 10px;">📦 거래 데이터</h3>

            <p><b>매매 거래 :</b>
                {f'{latest_collect_log["sale_trade_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <p><b>전월세 거래 :</b>
                {f'{latest_collect_log["rent_trade_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <p><b>분양권 거래 :</b>
                {f'{latest_collect_log["presale_trade_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <hr style="margin:16px 0; border:none; border-top:1px solid #e5e7eb;">

            <h3 style="margin:0 0 10px;">🔎 검색 목록</h3>

            <p><b>매매 목록 :</b>
                {f'{latest_collect_log["sale_list_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <p><b>전월세 목록 :</b>
                {f'{latest_collect_log["rent_list_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <p><b>분양권 목록 :</b>
                {f'{latest_collect_log["presale_list_count"]:,}' if latest_collect_log else '0'}건
            </p>

            <hr style="margin:16px 0; border:none; border-top:1px solid #e5e7eb;">

            <p><b>완료 지역 :</b>
                {latest_collect_log["success_count"] if latest_collect_log else 0}
            </p>

            <p><b>실패 건수 :</b>
                {latest_collect_log["fail_count"] if latest_collect_log else 0}
            </p>

            <p><b>마지막 지역 :</b> {collect_last_region}</p>
            <p><b>오류 :</b> {collect_error}</p>

        </div>

        <div class="card">
            <h2>오늘 현황</h2>
            <p>오늘 조회수 : {today_search}</p>
            <p>오늘 분석수 : {today_analysis}</p>
        </div>

        <div class="card">
            <h2>인기 단지 TOP 5</h2>
            <ul>
                {"".join([f"<li>{apt['아파트']} ({apt['조회수']})</li>" for apt in popular_apts])}
            </ul>
        </div>

        <div class="card">
            <h2>인기 지역 TOP 5</h2>
            <ul>
                {"".join([f"<li>{region['지역']} ({region['조회수']})</li>" for region in popular_regions])}
            </ul>
        </div>

        <div class="card">
            <h2>최근 분석 TOP 10</h2>
            <ul>
                {"".join([
                    f"<li>{item['아파트']} {item['평형']}㎡ / 입력가: {item['입력가'] if item['입력가'] is not None else '미입력'} / AI추천가: {item['AI추천가']} / {item['판단']}</li>"
                    for item in recent_analysis
                ])}
            </ul>
        </div>

        <!-- ✅ 행정구역 변경 이력 -->
        <div class="card">
            <h2>🗂 행정구역 변경 이력</h2>

            {
                (
                    "<ul>"
                    +
                    "".join([
                        f"<li>"
                        f"[{row['change_type']}] "
                        f"{row['lawd_cd']} | "
                        f"{(row['old_sido'] or '-')} {(row['old_sigungu'] or '')}"
                        f" → "
                        f"{(row['new_sido'] or '-')} {(row['new_sigungu'] or '')}"
                        f" ({row['detected_at'].strftime('%Y-%m-%d %H:%M')})"
                        f"</li>"
                        for row in region_change_logs
                    ])
                    +
                    "</ul>"
                )
                if region_change_logs
                else "<p>현재 감지된 행정구역 변경사항이 없습니다.</p>"
            }

        </div>
    </body>
    </html>
    """
if __name__ == "__main__":

    apartments = [
        {
            "region": "서울특별시 강동구",
            "apt_name": "고덕그라시움",
            "size": 59.785
        },
        {
            "region": "서울특별시 송파구",
            "apt_name": "헬리오시티",
            "size": 84.99
        },
        {
            "region": "서울특별시 송파구",
            "apt_name": "잠실엘스",
            "size": 84.8
        },
        {
            "region": "경기도 화성시 동탄구",
            "apt_name": "동탄역시범예미지아파트",
            "size": 84.8
        },
        {
            "region": "경기도 수원시 영통구",
            "apt_name": "힐스테이트영통",
            "size": 84.8897
        },
        {
            "region": "경기도 성남시 분당구",
            "apt_name": "파크뷰",
            "size": 84.99
        }
    ]

    test_cases = build_monthly_backtest_cases(
        apartments,
        start_date="2024-07-06",
        end_date="2026-01-06"
    )

    print(
        f"🔥 상승장 V3 실엔진 재검증 케이스 = "
        f"{len(test_cases)}건"
    )

    run_batch_backtest(test_cases)

# =========================================================
# ✅ 백테스트 수동 실행 영역
#
# 미래예측 엔진 V1 검증 완료 후 비활성화
# 필요할 때 아래 코드를 임시 활성화하여 사용
# =========================================================

# if __name__ == "__main__":

# =========================================================
# =========================================================
#          미래예측 V1 검증 / 지역통계 기반 함수
# =========================================================
#
# [현재 용도]
# - 미래예측 엔진 백테스트
# - 엔진 수정 시 회귀검증
#
# [향후 재활용]
# - 지역별 상승 단지 검색
# - 지역별 하락 단지 검색
# - 지역 시장 방향 통계
#
# ⚠️ 실서비스 일반 분석에서는 직접 호출하지 않음
# ⚠️ 삭제 금지 - 향후 지역통계 기능에 재사용 예정
#
# =========================================================
#
#     run_backtest_multi_region(
#         regions=[
#             "경기도 의왕시",
#             "서울특별시 강동구",
#             "경기도 수원시 영통구",
#             "서울특별시 강남구"
#         ],
#         analysis_date="2026-01-01",
#         rise_limit=10,
#         decline_limit=10,
#         min_rise_rate=3.0,
#         min_decline_rate=-3.0
#     )
# =========================================================
# ✅ 미래예측 엔진 V1 기준 성능
#
# 분석기준일 : 2026-01-01
#
# 3개 지역 종합
# - 의왕시
# - 서울 강동구
# - 수원 영통구
#
# 전체 유효 테스트 : 50개
# 현재가격 MAE : 8.02%
# 미래예측 MAE : 7.13%
# 평균 개선폭 : +0.89%p
#
# 상승군
# - 30개
# - 현재 MAE : 8.75%
# - 미래 MAE : 7.33%
# - 개선폭 : +1.42%p
#
# 하락군
# - 20개
# - 현재 MAE : 6.93%
# - 미래 MAE : 6.83%
# - 개선폭 : +0.10%p
#
# 추가 외부검증 : 서울 강남구
# - 12개
# - 현재 MAE : 11.32%
# - 미래 MAE : 8.52%
# - 개선폭 : +2.80%p
# - 개선 11 / 악화 1
#
# 추세 정책
# - 상승 추세 : 100% 반영
# - 하락 추세 : 50% 반영
#
# 향후 엔진 수정 시 반드시 본 V1과 비교
# =========================================================