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

trade_cache = {}
analysis_cache = {}
dong_cache = {}
apt_cache = {}
areas_cache = {}
lawd_cache = {}

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"

DEBUG = False

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

    if region_norm in lawd_cache:
        return lawd_cache[region_norm]

    for name, code in lawd_map.items():
        if region_norm in normalize_region(name):
            lawd_cache[region_norm] = code
            return code

    return None

def warmup_region(region: str):
    print(f"🔥 백그라운드 워밍업 시작: {region}")
    try:
        fetch_trade_items(region, 12)
        print(f"✅ 백그라운드 워밍업 완료: {region}")
    except Exception as e:
        print("워밍업 실패:", e)


# 문자열 정규화
def normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())

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
    return int(float(user_size)) == int(float(data_size))

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
                print("분양권 응답 상태:", res.status_code)
                print("분양권 응답 앞부분:", res.text[:300])


                if not res.text.strip().startswith("<"):
                    print("분양권 XML 아님, 건너뜀:", month, res.text[:100])
                    continue
               
                root = ET.fromstring(res.content)

            except Exception as e:
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

        trade_cache[cache_key] = month_items
        all_items.extend(month_items)

    return all_items

# 🔥 분양권 거래 데이터 가져오기
def fetch_presale_items(region: str, months_count: int = 6):

    print("🔥 fetch_presale_items 실행됨:", region)

    LAWD_CD = find_lawd_cd(region)

    if not LAWD_CD:
        return []

    url = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"
    print("🔥 분양권 URL:", url)
    months = []

    today = datetime.today()

    for i in range(months_count):
        year = today.year
        month = today.month - i

        while month <= 0:
            month += 12
            year -= 1

        months.append(f"{year}{month:02d}")

    print("분양권 최종 조회 월 목록:", months)

    all_items = []

    for month in months:
        month_items = []
        cache_key = f"presale_{normalize_region(region)}_{month}"
        if cache_key in trade_cache:
            print(f"⚡ 분양권 캐시 사용: {month}")
            all_items.extend(trade_cache[cache_key])
            continue

        print("분양권 조회 월:", month)

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
                print("분양권 XML 아님, 임시 데이터 사용:", month)

                temp_items = [
                    {
                        "apt_name": "의왕푸르지오포레움1블럭",
                        "dong": "",
                        "apt_dong": "",
                        "size": 50,
                        "price": 0,
                        "date": "분양권",
                        "floor": None,
                        "name": "의왕푸르지오포레움1블럭",
                        "name_norm": normalize("의왕푸르지오포레움1블럭")
                    }
                ]

                trade_cache[cache_key] = temp_items
                return temp_items
            
            root = ET.fromstring(text)

        except Exception as e:
            print("분양권 요청/파싱 오류:", e)
            continue

        items = root.findall(".//item")

        print("분양권 item 수:", len(items))

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

                print("분양권 단지 추가:", apt_name, dong)

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
def get_dongs(region: str, type: str = "apt"):
    cache_key = f"{type}_{normalize_region(region)}"

    if cache_key in dong_cache:
        print("⚡ 동 캐시 사용")
        return dong_cache[cache_key]

    if type == "presale":
        items = fetch_presale_items(region, 10)
    else:
        items = fetch_trade_items(region, 6)

    dongs = set()

    for item in items:
        dong = item.get("dong", "")
        if dong:
            dongs.add(dong)

    result = {"동목록": sorted(list(dongs))}
    dong_cache[cache_key] = result
    return result

    # 🔥 백그라운드로 12개월 미리 준비
    # threading.Thread(target=warmup_region, args=(region,), daemon=True).start()

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
        if len(parts) < 2 and "세종" not in name:
            continue

        # 중복 비슷한 이름 제거
        if name_norm in seen:
            continue

        seen.add(name_norm)
        results.append(name)

    return {"검색결과": results[:20]}

# 🔥 아파트 검색 (핵심)
@app.get("/apts")
def search_apts(
    region: str,
    keyword: str = "",
    dong: str = "",
    type: str = "apt"
):


    print("🔥 /apts 호출됨")
    print("region:", region)
    print("dong:", dong)
    print("type:", type)

    print("검색 타입:", type)
    LAWD_CD = find_lawd_cd(region)

    if not LAWD_CD:
        return {"검색결과": []}

    list_months = 10 if type == "presale" else 12
    cache_key = f"{type}_{normalize_region(region)}_{dong or 'all'}_{list_months}m"

    if cache_key not in apt_cache:
        # 동별 자동완성은 실거래 데이터 기준으로 생성
        if type == "presale":
            print("🔥 분양권 함수 호출 직전")
            items = fetch_presale_items(region, list_months)

        else:
            items = fetch_trade_items(region, list_months)
            
        apt_list = []
        seen = set()

        for item in items:
            apt_name = item.get("apt_name", "")
            umd_name = item.get("dong", "")
            print("검색용 단지:", apt_name)

            if not apt_name:
                continue
            
            if dong and umd_name != dong:
                continue

            key = (apt_name, umd_name)

            if key in seen:
                continue

            seen.add(key)

            print("결과 추가:", apt_name)

            display_name = apt_name

#            if type == "presale":
#                display_name = apt_name + " (분양권)"

            apt_list.append({
                "name": display_name,
                "real_name": apt_name,
                "dong": umd_name,
                "name_norm": normalize(apt_name)
            })



        apt_cache[cache_key] = apt_list

    keyword_norm = normalize(keyword)

    result = []

    for apt in apt_cache[cache_key]:
        if keyword_norm and keyword_norm not in apt["name_norm"]:
            continue

        result.append(apt)

    if not result and not keyword_norm:
        result = apt_cache[cache_key]

    if type == "presale" and not result:
        return {"검색결과": []}
    return {"검색결과": result[:200]}

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

                if is_same_apartment_name(apt_name, name):
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
    size: int, 
    user_price: int | None = None,
    direction: str | None = None,
    floor_level: str | None = None,
    interior: str | None = None,
    type: str = "apt"
):
    print("분석 타입:", type)


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
            "AI설명": "분양권 거래 데이터가 부족하여 예상 총 매입가를 기준으로 임시 분석했습니다.",
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
        


           
    cache_key = f"{type}_{normalize_region(region)}_{normalize(apt_name)}_{size}_{direction or 'none'}_{floor_level or 'none'}_{interior or 'none'}_{user_price or 'none'}"

    if cache_key in analysis_cache:
        if type == "presale":
            print("⚡ 분양권 분석 캐시 사용")
        else:
            print("⚡ 아파트 분석 캐시 사용")

        return analysis_cache[cache_key]
        
    LAWD_CD = find_lawd_cd(region)
    if not LAWD_CD:
        return {"결과": "지역 오류"}

    apt_name_norm = normalize(apt_name)

    trades = []
    if type == "presale":
        items = fetch_presale_items(region, 1)
    else:
        items = fetch_trade_items(region, 9)

    for item in items:
        name = item["apt_name"]

        if is_same_apartment_name(apt_name, name) and is_same_size(size, item["size"]):
            # print("매칭:", name, item["size"], item["date"], item["price"])
            # print("매칭됨:", apt_name, name, "선택평형:", size, "거래평형:", item["size"])
            trades.append({
                "price": item["price"],
                "date": item["date"],
                "floor": item.get("floor"),
                "apt_dong": item.get("apt_dong"),
                "size": item["size"]
            })
        

    if len(trades) < 5:
        if type == "presale":
            items = fetch_presale_items(region, 3)
        else:
            items = fetch_trade_items(region, 18)


        trades = []

        for item in items:
            name = item["apt_name"]

            if is_same_apartment_name(apt_name, name) and is_same_size(size, item["size"]):
                # print("매칭:", name, item["size"], item["date"], item["price"])
                # print("매칭됨:", apt_name, name, "선택평형:", size, "거래평형:", item["size"])
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
            analysis_cache[cache_key] = result
            return result
        

        return {"결과": "데이터 없음"}
    

    # 최신순 정렬
    trades.sort(key=lambda x: x["date"], reverse=True)

    trades = [
        t for t in trades
        if is_same_size(size, t.get("size"))
    ]

    # 최근 거래 5건
    recent_trades = trades[:5]

    recent_price = user_price or 0

    for t in recent_trades:
        if t.get("price"):
            recent_price = t["price"]
            break

    # 🔥 평균가 개선: 최근 거래 중심
    prices = [t["price"] for t in trades if t.get("price")]

    if not prices:
        result = presale_fallback()
        analysis_cache[cache_key] = result
        return result

    recent_5_prices = [t["price"] for t in recent_trades if t.get("price")]

    if len(recent_5_prices) >= 3:
        avg_price = round(sum(recent_5_prices) / len(recent_5_prices))
    elif prices:
        avg_price = round(sum(prices) / len(prices))
    else:
        avg_price = 0

    # 최근 3개월 거래 건수 / 활발도
    today = datetime.today()
    three_months_ago = today - timedelta(days=90)

    recent_prices = []
    past_prices = []

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

        if type == "presale" and len(recent_prices) >= 3 and len(past_prices) == 0:
            trend = "최근 거래 활발"
            change_rate = 0
            change_rate_text = "비교 기준 부족"
            trend_confidence = "참고"

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
        "상승률텍스트": change_rate_text,
        "추세신뢰도": trend_confidence,
        "추세해석": trend_comment,
        "AI설명": ai_comment,
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
    if type == "presale" or result.get("거래건수", 0) >= 3:
        analysis_cache[cache_key] = result

    return result

# 🔥 평형 조회
@app.get("/sizes")
def get_sizes(region: str, apt_name: str, type: str = "apt"):

    LAWD_CD = find_lawd_cd(region)



    if type == "presale":
        cache_key = f"presale_size_{normalize_region(region)}_{normalize(apt_name)}"

        if cache_key in areas_cache:
            print("⚡ 분양권 평형 캐시 사용")
            return areas_cache[cache_key]

        sizes = set()

        for months_count in [10]:
            items = fetch_presale_items(region, months_count)

            for item in items:
                name = item.get("apt_name", "")

                if is_same_apartment_name(apt_name, name):
                    size = item.get("size", 0)
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
    


    if not LAWD_CD:
        return {"평형목록": []}

    cache_key = f"{type}_{normalize_region(region)}_{normalize(apt_name)}"

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

            if is_same_apartment_name(apt_name, name):
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
