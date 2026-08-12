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
                
                type_db_rows = get_apt_sale_trades(
                    apt_name,
                    type_size,
                    region=region
                )

                type_items = db_rows_to_items(type_db_rows)

                

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
        
        db_rows = get_apt_sale_trades(apt_name, matched_size, region=region)
        
        items = db_rows_to_items(db_rows)

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
                and item.get("dong", "") in [
                    row[2]
                    for row in db_rows
                    if (
                        row[0] == db_region
                        and row[1] == db_sigungu
                    )
                ]
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
        if DEBUG_FUTURE_ENGINE:
            print(
                f"최종 expected_rate : "
                f"{expected_rate:+.2f}%"
            )

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

            print(
                f"최종 expected_rate : "
                f"{expected_rate:+.2f}%"
            )
            print("===================================")
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

    future_rows = get_apt_sale_trades(
        apt_name,
        matched_size,
        region=region
    )

    print(
        f"DB 전체 조회건수 : "
        f"{len(future_rows)}건"
    )

    future_trades = []

    for row in future_rows:

        try:
            raw_date = row[5]
            raw_price = row[6]
            raw_floor = row[7]

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