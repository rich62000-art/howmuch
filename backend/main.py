from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import json
import requests
session = requests.Session()
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
import time
import threading
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
with open("lawd_codes.json", "r", encoding="utf-8") as f:
    lawd_map = json.load(f)

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
            "apt_dong": "",
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
    rows = get_rent_trades(apt_name, size)
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
                    return cached_data
            del analysis_cache[cache_key]

        else:
            del analysis_cache[cache_key]

    # ✅ Supabase 분석 결과 캐시 확인
    # 같은 단지/면적/입력가 조건으로 1시간 이내 분석한 결과가 있으면
    # 거래 데이터를 다시 계산하지 않고 즉시 반환한다.
    db_cached_result = get_analysis_cache_from_db(cache_key)

    if isinstance(db_cached_result, dict) and db_cached_result:
        return db_cached_result
    
    # ✅ 지역 처리 시스템 V3
    # 행정구역 변경/별칭 지역을 DB 기준 지역명으로 보정
    region = normalize_region_for_db(region)
        
    LAWD_CD = find_lawd_cd(region)
    
    if not LAWD_CD:
        return {"결과": "지역 오류"}

    apt_name_norm = normalize(apt_name)

    trades = []
    if type == "presale":
        db_rows = get_presale_trades(apt_name, size)
        items = db_rows_to_items(db_rows)
    else:
        db_rows = get_apt_sale_trades(apt_name, size)
        items = db_rows_to_items(db_rows)

        if not items:
            items = []

    for item in items:
        name = item["apt_name"]

        if is_same_apartment_name(apt_name, name) and is_same_size(size, item["size"]):
            
            trades.append({
                "price": item["price"],
                "date": item["date"],
                "floor": item.get("floor"),
                "apt_dong": item.get("apt_dong"),
                "size": item["size"]
            })
        
        trades = []

        for item in items:
            name = item["apt_name"]

            if is_same_apartment_name(apt_name, name) and is_same_size(size, item["size"]):
                
                trades.append({
                    "price": item["price"],
                    "date": item["date"],
                    "floor": item.get("floor"),
                    "apt_dong": item.get("apt_dong"),
                    "size": item["size"]
                })


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
    trades.sort(key=lambda x: x["date"], reverse=True)

    trades = [
        t for t in trades
        if is_same_size(size, t.get("size"))
    ]


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

    import statistics

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
    result = {
        "아파트": apt_name,
        "평형": size,
        "거래건수": sum(int(v.get("count", 0)) for v in monthly_volume),
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

    return result


# 🔥 미래 예측 조회

@app.get("/future_prediction")
def future_prediction(
    region: str,
    apt_name: str,
    size: str = ""
):
    region = normalize_region_for_db(region)
    
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

        # ✅ 매칭된 실제 면적으로 거래 조회
        db_rows = get_apt_sale_trades(apt_name, matched_size)
        
        items = db_rows_to_items(db_rows)

        trades = []

        for item in items:
            item_size = str(item.get("size", "")).replace("㎡", "").strip()
            target_size = str(matched_size).replace("㎡", "").strip()

            if (
                is_same_apartment_name(apt_name, item["apt_name"])
                and item_size == target_size
                and item.get("dong", "") in [
                    row[2]
                    for row in db_rows
                    if row[0] == db_region and row[1] == db_sigungu
                ]
            ):

                trades.append({
                    "price": item["price"],
                    "date": item["date"],
                    "size": item["size"],
                    "dong": item.get("dong", "")
                })

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
        today = datetime.today()

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
        today = datetime.today()

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
        today = datetime.today()

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

        # ✅ 실제 전세 평균가 계산
        rent_size = str(size).replace("㎡", "").strip()

        rent_rows = get_rent_trades(apt_name, rent_size)
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

        
        if recent_avg > 0 and prev_avg > 0:
            rise_rate = round(((recent_avg - prev_avg) / prev_avg) * 100, 1)
        else:
            rise_rate = 0

        if rise_rate > 1:
            trend = "상승"
        elif rise_rate < -1:
            trend = "하락"
        else:
            trend = "보합"

        # ✅ 최근 12개월 월별 거래량 생성
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
        base_price = recent_avg

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

        trend_adjust = rise_rate * activity_factor
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

        expected_rate = (
            trend_adjust
            + jeonse_adjust
            + volume_adjust
            + total_trade_adjust
            + gap_adjust
        )

        expected_center = base_price * (1 + expected_rate / 100)
        expected_low = expected_center * (1 - volatility / 100)
        expected_high = expected_center * (1 + volatility / 100)

        expected_center = int(round(expected_center))
        expected_low = int(round(expected_low))
        expected_high = int(round(expected_high))

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

            "6개월예상하한가": expected_low,
            "6개월예상상한가": expected_high,
            "6개월예상중심가": expected_center,
            "6개월예상상승률": round(expected_rate, 2),

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

def get_latest_collect_log():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                status,
                started_at,
                ended_at,
                success_count,
                fail_count,
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
            "success_count": row[3],
            "fail_count": row[4],
            "last_sido": row[5],
            "last_sigungu": row[6],
            "last_lawd_cd": row[7],
            "error_message": row[8],
        }

    except Exception as e:
        print("자동수집 로그 조회 실패:", e)
        return None

    finally:
        cur.close()
        release_pg_connection(conn)

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

            <p><b>완료 지역 :</b> {latest_collect_log["success_count"] if latest_collect_log else 0}</p>

            <p><b>실패 건수:</b> {latest_collect_log["fail_count"] if latest_collect_log else 0}</p>

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

    </body>
    </html>
    """
