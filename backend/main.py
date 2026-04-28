from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import json
import requests
session = requests.Session()
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
import threading

from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.mount("/static", StaticFiles(directory="."), name="static")

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

apt_cache = {}
areas_cache = {}
dong_cache = {}

trade_cache = {}
analysis_cache = {} 

trade_cache = {}
analysis_cache = {}
dong_cache = {}
apt_cache = {}
areas_cache = {}

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"

def normalize_region(text: str) -> str:
    text = text.replace(" ", "")
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

def find_lawd_cd(region: str):
    region_norm = normalize_region(region)

    for name, code in lawd_map.items():
        if region_norm in normalize_region(name):
            return code

    return None

def warmup_region(region: str):
    print(f"🔥 워밍업 시작: {region}")
    try:
        fetch_trade_items(region, 6)
    except Exception as e:
        print("워밍업 실패:", e)


# 문자열 정규화
def normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())

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

def parse_area(text: str) -> int:
    return int(float(text)) if text and text.strip() else 0

def item_to_dict(item) -> dict:
    apt_name = item.findtext("aptNm", "").strip()
    dong = item.findtext("umdNm", "").strip()
    size = item.findtext("excluUseAr", "").strip()
    price = item.findtext("dealAmount", "").strip()
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

    for month in months:
        cache_key = f"trade_items_{normalize_region(region)}_{month}"

        if cache_key in trade_cache:
            print(f"⚡ 월 캐시 사용: {month}")
            all_items.extend(trade_cache[cache_key])
            continue

        month_items = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            params = {
                "serviceKey": SERVICE_KEY,
                "pageNo": str(page),
                "numOfRows": "100",
                "LAWD_CD": LAWD_CD,
                "DEAL_YMD": month
            }

            try:
                res = session.get(url, params=params, timeout=10)
                root = ET.fromstring(res.content)
            except Exception as e:
                print("요청/파싱 오류:", e)
                break

            if page == 1:
                total_count_text = root.findtext(".//totalCount", "0").strip()
                total_count = int(total_count_text) if total_count_text.isdigit() else 0
                total_pages = min(2, (total_count + 99) // 100) if total_count > 0 else 1

            items = root.findall(".//item")

            if not items:
                break

            # ✅ 여기만 유지 (핵심)
            month_items.extend([item_to_dict(item) for item in items])

            page += 1

        trade_cache[cache_key] = month_items
        all_items.extend(month_items)

    return all_items


# 🔥 아파트 가져오기 (필요할 때만)
def fetch_apts(LAWD_CD):
    print("🔥 fetch_apts 실행됨")

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

    months = get_recent_months(6)
    names = set()

    for month in months:
        page = 1

        while page <= 5:
            params = {
                "serviceKey": SERVICE_KEY,
                "pageNo": str(page),
                "numOfRows": "100",
                "LAWD_CD": LAWD_CD,
                "DEAL_YMD": month
            }

            try:
                res = requests.get(url, params=params, timeout=10)
                root = ET.fromstring(res.content)
            except Exception as e:
                print("아파트 조회 오류:", e)
                break

            items = root.findall(".//item")
            if not items:
                break

            for item in items:
                name = item.findtext("aptNm", "").strip()
                if name:
                    names.add(name)

            page += 1

        print(f"{month} 데이터 수집 완료, 현재 아파트 수: {len(names)}")

    return sorted(list(names))

@app.get("/dongs")
def get_dongs(region: str):
    cache_key = normalize_region(region)

    if cache_key in dong_cache:
        print("⚡ 동 캐시 사용")
        return dong_cache[cache_key]

    items = fetch_trade_items(region, 6)

    # 🔥 백그라운드로 12개월 미리 준비
    threading.Thread(target=warmup_region, args=(region,), daemon=True).start()

    dongs = set()

    for item in items:
        dong = item["dong"]
        if dong:
            dongs.add(dong)

    result = {"동목록": sorted(list(dongs))}
    dong_cache[cache_key] = result
    return result


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
        if len(parts) < 2:
            continue

        # 중복 비슷한 이름 제거
        if name_norm in seen:
            continue

        seen.add(name_norm)
        results.append(name)

    return {"검색결과": results[:20]}

# 🔥 아파트 검색 (핵심)
@app.get("/apts")
def search_apts(region: str, keyword: str = "", dong: str = ""):
    LAWD_CD = find_lawd_cd(region)

    if not LAWD_CD:
        return {"검색결과": []}

    cache_key = f"{normalize_region(region)}_{dong or 'all'}"

    if cache_key not in apt_cache:
        items = fetch_trade_items(region, 6)

        apt_list = []
        seen = set()

        for item in items:
            apt_name = item["apt_name"]
            umd_name = item["dong"]

            if not apt_name:
                continue

            key = (apt_name, umd_name)
            if key in seen:
                continue

            seen.add(key)
            apt_list.append({
                "name": apt_name,
                "dong": umd_name,
                "name_norm": normalize(apt_name)
            })

        apt_cache[cache_key] = apt_list

    keyword_norm = normalize(keyword)

    result = []
    for apt in apt_cache[cache_key]:
        name = apt["name"]
        apt_dong = apt["dong"]

        if dong and apt_dong != dong:
            continue

        if keyword_norm and keyword_norm not in apt["name_norm"]:
            continue

        result.append(apt)

    if not result:
        result = apt_cache[cache_key][:20]

    return {"검색결과": result[:50]}

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

            res = requests.get(url, params=params)

            try:
                root = ET.fromstring(res.content)
            except:
                break

            items = root.findall(".//item")
            if not items:
                break

            for item in items:
                name = item.findtext("aptNm", "").strip()

                if normalize(apt_name) in normalize(name):
                    size = item.findtext("excluUseAr", "").strip()
                    price = item.findtext("dealAmount", "").strip().replace(",", "")
                    year = item.findtext("dealYear", "").strip()
                    month2 = item.findtext("dealMonth", "").strip()
                    day = item.findtext("dealDay", "").strip()

                    if size and price:
                        trades.append({
                            "아파트": name,
                            "전용면적": int(float(size)),
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

            res = requests.get(url, params=params)

            try:
                root = ET.fromstring(res.content)
            except:
                break

            items = root.findall(".//item")
            if not items:
                break

            for item in items:
                name = item.findtext("aptNm", "").strip()

                if normalize(apt_name) in normalize(name):
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
    size: int, 
    user_price: int | None = None,
    direction: str | None = None,
    floor_level: str | None = None,
    interior: str | None = None
):
    cache_key = f"{normalize_region(region)}_{normalize(apt_name)}_{size}_{direction or 'none'}_{floor_level or 'none'}_{interior or 'none'}_{user_price or 'none'}"

    if cache_key in analysis_cache:
       print("⚡ 분석 캐시 사용")
       return analysis_cache[cache_key]
    # analysis_cache[cache_key] = result
    
    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return {"결과": "지역 오류"}

    apt_name_norm = normalize(apt_name)

    trades = []
    items = fetch_trade_items(region, 12)

    for item in items:
        name = item["apt_name"]

        if apt_name_norm in normalize(name) and item["size"] == size:
            
            trades.append({
                "price": item["price"],
                "date": item["date"],
                "floor": item.get("floor")
            })
        

    if len(trades) < 5:
        items = fetch_trade_items(region, 24)
        trades = []

        for item in items:
            name = item["apt_name"]

            if apt_name_norm in normalize(name) and item["size"] == size:
                
                trades.append({
                    "price": item["price"],
                    "date": item["date"],
                    "floor": item.get("floor")
                })


    if not trades:
        return {"결과": "데이터 없음"}

    # 최신순 정렬
    trades.sort(key=lambda x: x["date"], reverse=True)

    # 최근 거래 5건
    recent_trades = trades[:5]

    # 최근 3개월 거래 건수 / 활발도
    today = datetime.today()
    three_months_ago = today - timedelta(days=90)

    recent_3m_count = 0
    for t in trades:
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
            if d >= three_months_ago:
                recent_3m_count += 1
        except:
            continue

    if recent_3m_count >= 6:
        trade_activity = "(거래 활발)"
    elif recent_3m_count >= 3:
        trade_activity = "(거래 보통)"
    else:
        trade_activity = "(비활성)"

    prices = [t["price"] for t in trades]

    # 1. 평균가격
    avg_price = round(sum(prices) / len(prices))

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

    # 4. 추세 / 상승률 (최근 3개월 vs 이전 3개월)

    recent_prices = []
    past_prices = []

    today = datetime.today()
    three_months_ago = today - timedelta(days=90)
    six_months_ago = today - timedelta(days=180)

    for t in trades:
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
        except:
            continue

        if d >= three_months_ago:
            recent_prices.append(t["price"])
        elif six_months_ago <= d < three_months_ago:
            past_prices.append(t["price"])

    if recent_prices and past_prices:
        recent_avg = sum(recent_prices) / len(recent_prices)
        past_avg = sum(past_prices) / len(past_prices)

        diff_rate = ((recent_avg - past_avg) / past_avg) * 100

        if diff_rate >= 2:
            trend = "상승"
        elif diff_rate <= -2:
            trend = "하락"
        else:
            trend = "보합"

        change_rate = round(diff_rate, 2)
    else:
        if len(prices) >= 5:
            recent_avg = sum(prices[:5]) / len(prices[:5])
            past_avg = sum(prices[-5:]) / len(prices[-5:])

            diff_rate = ((recent_avg - past_avg) / past_avg) * 100

            if diff_rate >= 2:
                trend = "상승"
            elif diff_rate <= -2:
                trend = "하락"
            else:
                trend = "보합"

            change_rate = round(diff_rate, 2)
        else:
            trend = "보류"
            change_rate = 0

    # 5. 추천 매수가
    if trend == "하락":
        recommended_buy_price = round(weighted_avg_price * 0.95)
    elif trend == "상승":
        recommended_buy_price = round(weighted_avg_price * 0.98)
    elif trend == "보합":
        recommended_buy_price = round(weighted_avg_price * 0.97)
    else:
        recommended_buy_price = round(weighted_avg_price * 0.97)
    
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

    
    result = {
        "아파트": apt_name,
        "평형": size,
        "거래건수": len(prices),
        "평균가격": avg_price,
        "가중평균가격": weighted_avg_price,
        "최고가": high_price,
        "최저가": low_price,
        "추세": trend,
        "상승률(%)": change_rate,
        "추천매수가": recommended_buy_price,
        "보정추천가": adjusted_buy_price,
        "최근거래5건": recent_trades,
        "최근3개월거래건수": recent_3m_count,
        "거래활발도": trade_activity,
        "사용자입력가격": user_price,
        "가격판단": judgment,
        "한줄결론": conclusion,
        "실거래가기준안내": "이 앱의 분석은 국토교통부 실거래가 공개시스템에 등록된 실거래 신고 자료를 기준으로 합니다.",
        "참고": "동·층·향·내부상태·급매 여부는 반영되지 않은 참고용 분석입니다."
    }
    
    analysis_cache[cache_key] = result

    return result

# 🔥 평형 조회
@app.get("/sizes")
def get_sizes(region: str, apt_name: str):

    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return {"평형목록": []}

    cache_key = f"{normalize_region(region)}_{normalize(apt_name)}"

    if cache_key in areas_cache:
        print("⚡ 캐시 사용")
        return areas_cache[cache_key]


    apt_name_norm = normalize(apt_name)
    sizes = set()

    for months_count in [6, 12, 24]:
        items = fetch_trade_items(region, months_count)
        sizes = set()

        for item in items:
            name = item["apt_name"]

            if apt_name_norm in normalize(name):
                size = item["size"]
                if size:
                   sizes.add(size)

        if sizes:
            break

    result = {
        "아파트": apt_name,
        "평형목록": sorted(list(sizes))
    }

    areas_cache[cache_key] = result

    return result

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
