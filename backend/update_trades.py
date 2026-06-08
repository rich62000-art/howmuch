import csv
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

from db import (
    insert_apt_sale_trade,
    insert_presale_trade,
    insert_apt_rent_trade,
    insert_region_code,
    create_tables,
    get_all_region_codes,
    clear_region_codes
)

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"

url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

presale_url = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"

rent_url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"

REGION_CODE_URL = "https://www.code.go.kr/etc/codeFullDown.do?codeseId=00002"

# ✅ 법정동 코드 파일 다운로드
def download_region_code_file():
    response = requests.get(REGION_CODE_URL)

    print("법정동 코드 다운로드 응답:", response.status_code)

    with open("region_codes.txt", "wb") as f:
        f.write(response.content)

    print("region_codes.txt 저장 완료")

def get_recent_months(count=12):
    months = []
    today = datetime.today()

    year = today.year
    month = today.month

    for _ in range(count):
        months.append(f"{year}{month:02d}")

        month -= 1
        if month == 0:
            month = 12
            year -= 1

    return months


def save_month_trades(lawd_cd, region, sigungu, deal_ymd):
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "numOfRows": 500,
        "pageNo": 1
    }

    response = requests.get(url, params=params)

    print(f"{sigungu} {deal_ymd} 응답:", response.status_code)

    root = ET.fromstring(response.text)
    items = root.findall(".//item")

    print(f"{sigungu} {deal_ymd} 거래 건수:", len(items))

    for item in items:
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()

        exclu_use_ar = item.findtext("excluUseAr", "0").strip()
        deal_amount = item.findtext("dealAmount", "0").replace(",", "").strip()

        deal_year = item.findtext("dealYear", "").strip()
        deal_month = item.findtext("dealMonth", "").strip().zfill(2)
        deal_day = item.findtext("dealDay", "").strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        trade = {
            "region": region,
            "sigungu": sigungu,
            "dong": dong,
            "apt_name": apt_name,
            "size": float(exclu_use_ar),
            "contract_date": f"{deal_year}-{deal_month}-{deal_day}",
            "price": int(deal_amount),
            "floor": int(floor),
            "source_month": deal_ymd
        }

        insert_apt_sale_trade(trade)


# ✅ 분양권 월별 수집
def save_month_presale_trades(
    lawd_cd,
    region,
    sigungu,
    deal_ymd
):
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "pageNo": 1,
        "numOfRows": 1000
    }

    response = requests.get(
        presale_url,
        params=params
    )

    print(
        f"{sigungu} 분양권 {deal_ymd} 응답:",
        response.status_code
    )

    if response.status_code != 200:
        return
    
    root = ET.fromstring(response.text)
    items = root.findall(".//item")

    print(f"{sigungu} 분양권 {deal_ymd} 거래 건수:", len(items))

    for item in items:
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()

        exclu_use_ar = item.findtext("excluUseAr", "0").strip()
        deal_amount = item.findtext("dealAmount", "0").replace(",", "").strip()

        deal_year = item.findtext("dealYear", "").strip()
        deal_month = item.findtext("dealMonth", "").strip().zfill(2)
        deal_day = item.findtext("dealDay", "").strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        trade = {
            "region": region,
            "sigungu": sigungu,
            "dong": dong,
            "apt_name": apt_name,
            "size": float(exclu_use_ar),
            "contract_date": f"{deal_year}-{deal_month}-{deal_day}",
            "price": int(deal_amount),
            "floor": int(floor),
            "source_month": deal_ymd
        }

        insert_presale_trade(trade)

# ✅ 아파트 전월세 월별 수집
def save_month_rent_trades(
    lawd_cd,
    region,
    sigungu,
    deal_ymd
):
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "pageNo": 1,
        "numOfRows": 1000
    }

    try:
        response = requests.get(
            rent_url,
            params=params,
            timeout=20
        )
    except requests.exceptions.RequestException as e:
        print(f"{sigungu} 전월세 {deal_ymd} 요청 실패:", e)
        return

    print(f"{sigungu} 전월세 {deal_ymd} 응답:", response.status_code)

    if response.status_code != 200:
        return
    
    root = ET.fromstring(response.text)
    items = root.findall(".//item")

    print(f"{sigungu} 전월세 {deal_ymd} 거래 건수:", len(items))

    for item in items:
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()

        exclu_use_ar = item.findtext("excluUseAr", "0").strip()
        deposit = item.findtext("deposit", "0").replace(",", "").strip()
        monthly_rent = item.findtext("monthlyRent", "0").replace(",", "").strip()

        deal_year = item.findtext("dealYear", "").strip()
        deal_month = item.findtext("dealMonth", "").strip().zfill(2)
        deal_day = item.findtext("dealDay", "").strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        trade = {
            "region": region,
            "sigungu": sigungu,
            "dong": dong,
            "apt_name": apt_name,
            "size": float(exclu_use_ar),
            "contract_date": f"{deal_year}-{deal_month}-{deal_day}",
            "deposit": int(deposit),
            "monthly_rent": int(monthly_rent),
            "floor": int(floor),
            "source_month": deal_ymd
        }

        insert_apt_rent_trade(trade)


# ✅ 전국 시군구 코드 기본 저장
def update_region_codes():
    clear_region_codes()

    region_list = [
        # 서울특별시
        ("서울특별시", "종로구", "11110"),
        ("서울특별시", "중구", "11140"),
        ("서울특별시", "용산구", "11170"),
        ("서울특별시", "성동구", "11200"),
        ("서울특별시", "광진구", "11215"),
        ("서울특별시", "동대문구", "11230"),
        ("서울특별시", "중랑구", "11260"),
        ("서울특별시", "성북구", "11290"),
        ("서울특별시", "강북구", "11305"),
        ("서울특별시", "도봉구", "11320"),
        ("서울특별시", "노원구", "11350"),
        ("서울특별시", "은평구", "11380"),
        ("서울특별시", "서대문구", "11410"),
        ("서울특별시", "마포구", "11440"),
        ("서울특별시", "양천구", "11470"),
        ("서울특별시", "강서구", "11500"),
        ("서울특별시", "구로구", "11530"),
        ("서울특별시", "금천구", "11545"),
        ("서울특별시", "영등포구", "11560"),
        ("서울특별시", "동작구", "11590"),
        ("서울특별시", "관악구", "11620"),
        ("서울특별시", "서초구", "11650"),
        ("서울특별시", "강남구", "11680"),
        ("서울특별시", "송파구", "11710"),
        ("서울특별시", "강동구", "11740"),
                # 부산광역시
        ("부산광역시", "중구", "26110"),
        ("부산광역시", "서구", "26140"),
        ("부산광역시", "동구", "26170"),
        ("부산광역시", "영도구", "26200"),
        ("부산광역시", "부산진구", "26230"),
        ("부산광역시", "동래구", "26260"),
        ("부산광역시", "남구", "26290"),
        ("부산광역시", "북구", "26320"),
        ("부산광역시", "해운대구", "26350"),
        ("부산광역시", "사하구", "26380"),
        ("부산광역시", "금정구", "26410"),
        ("부산광역시", "강서구", "26440"),
        ("부산광역시", "연제구", "26470"),
        ("부산광역시", "수영구", "26500"),
        ("부산광역시", "사상구", "26530"),
        ("부산광역시", "기장군", "26710"),
                # 대구광역시
        ("대구광역시", "중구", "27110"),
        ("대구광역시", "동구", "27140"),
        ("대구광역시", "서구", "27170"),
        ("대구광역시", "남구", "27200"),
        ("대구광역시", "북구", "27230"),
        ("대구광역시", "수성구", "27260"),
        ("대구광역시", "달서구", "27290"),
        ("대구광역시", "달성군", "27710"),
        ("대구광역시", "군위군", "27720"),
                # 인천광역시
        ("인천광역시", "중구", "28110"),
        ("인천광역시", "동구", "28140"),
        ("인천광역시", "미추홀구", "28177"),
        ("인천광역시", "연수구", "28185"),
        ("인천광역시", "남동구", "28200"),
        ("인천광역시", "부평구", "28237"),
        ("인천광역시", "계양구", "28245"),
        ("인천광역시", "서구", "28260"),
        ("인천광역시", "강화군", "28710"),
        ("인천광역시", "옹진군", "28720"),
                # 광주광역시
        ("광주광역시", "동구", "29110"),
        ("광주광역시", "서구", "29140"),
        ("광주광역시", "남구", "29155"),
        ("광주광역시", "북구", "29170"),
        ("광주광역시", "광산구", "29200"),

        # 대전광역시
        ("대전광역시", "동구", "30110"),
        ("대전광역시", "중구", "30140"),
        ("대전광역시", "서구", "30170"),
        ("대전광역시", "유성구", "30200"),
        ("대전광역시", "대덕구", "30230"),

        # 울산광역시
        ("울산광역시", "중구", "31110"),
        ("울산광역시", "남구", "31140"),
        ("울산광역시", "동구", "31170"),
        ("울산광역시", "북구", "31200"),
        ("울산광역시", "울주군", "31710"),

        # 세종특별자치시
        ("세종특별자치시", "세종시", "36110"),
                # 경기도
        ("경기도", "수원시 장안구", "41111"),
        ("경기도", "수원시 권선구", "41113"),
        ("경기도", "수원시 팔달구", "41115"),
        ("경기도", "수원시 영통구", "41117"),

        ("경기도", "성남시 수정구", "41131"),
        ("경기도", "성남시 중원구", "41133"),
        ("경기도", "성남시 분당구", "41135"),

        ("경기도", "의정부시", "41150"),
        ("경기도", "안양시 만안구", "41171"),
        ("경기도", "안양시 동안구", "41173"),

        ("경기도", "부천시", "41190"),
        ("경기도", "광명시", "41210"),
        ("경기도", "평택시", "41220"),
        ("경기도", "동두천시", "41250"),
        ("경기도", "안산시 상록구", "41271"),
        ("경기도", "안산시 단원구", "41273"),

        ("경기도", "고양시 덕양구", "41281"),
        ("경기도", "고양시 일산동구", "41285"),
        ("경기도", "고양시 일산서구", "41287"),

        ("경기도", "과천시", "41290"),
        ("경기도", "구리시", "41310"),
        ("경기도", "남양주시", "41360"),
        ("경기도", "오산시", "41370"),
        ("경기도", "시흥시", "41390"),
        ("경기도", "군포시", "41410"),
        ("경기도", "의왕시", "41430"),
        ("경기도", "하남시", "41450"),
        ("경기도", "용인시 처인구", "41461"),
        ("경기도", "용인시 기흥구", "41463"),
        ("경기도", "용인시 수지구", "41465"),
        ("경기도", "파주시", "41480"),
                ("경기도", "이천시", "41500"),
        ("경기도", "안성시", "41550"),
        ("경기도", "김포시", "41570"),
        ("경기도", "화성시", "41590"),
        ("경기도", "광주시", "41610"),
        ("경기도", "양주시", "41630"),
        ("경기도", "포천시", "41650"),
        ("경기도", "여주시", "41670"),
        ("경기도", "연천군", "41800"),
        ("경기도", "가평군", "41820"),
        ("경기도", "양평군", "41830"),

        # 강원특별자치도
        ("강원특별자치도", "춘천시", "51110"),
        ("강원특별자치도", "원주시", "51130"),
        ("강원특별자치도", "강릉시", "51150"),
        ("강원특별자치도", "동해시", "51170"),
        ("강원특별자치도", "태백시", "51190"),
        ("강원특별자치도", "속초시", "51210"),
        ("강원특별자치도", "삼척시", "51230"),
        ("강원특별자치도", "홍천군", "51720"),
        ("강원특별자치도", "횡성군", "51730"),
        ("강원특별자치도", "영월군", "51750"),
        ("강원특별자치도", "평창군", "51760"),
        ("강원특별자치도", "정선군", "51770"),
        ("강원특별자치도", "철원군", "51780"),
        ("강원특별자치도", "화천군", "51790"),
        ("강원특별자치도", "양구군", "51800"),
        ("강원특별자치도", "인제군", "51810"),
        ("강원특별자치도", "고성군", "51820"),
        ("강원특별자치도", "양양군", "51830"),
                # 충청북도
        ("충청북도", "청주시 상당구", "43111"),
        ("충청북도", "청주시 서원구", "43112"),
        ("충청북도", "청주시 흥덕구", "43113"),
        ("충청북도", "청주시 청원구", "43114"),
        ("충청북도", "충주시", "43130"),
        ("충청북도", "제천시", "43150"),
        ("충청북도", "보은군", "43720"),
        ("충청북도", "옥천군", "43730"),
        ("충청북도", "영동군", "43740"),
        ("충청북도", "증평군", "43745"),
        ("충청북도", "진천군", "43750"),
        ("충청북도", "괴산군", "43760"),
        ("충청북도", "음성군", "43770"),
        ("충청북도", "단양군", "43800"),

        # 충청남도
        ("충청남도", "천안시 동남구", "44131"),
        ("충청남도", "천안시 서북구", "44133"),
        ("충청남도", "공주시", "44150"),
        ("충청남도", "보령시", "44180"),
        ("충청남도", "아산시", "44200"),
        ("충청남도", "서산시", "44210"),
        ("충청남도", "논산시", "44230"),
        ("충청남도", "계룡시", "44250"),
        ("충청남도", "당진시", "44270"),
        ("충청남도", "금산군", "44710"),
        ("충청남도", "부여군", "44760"),
        ("충청남도", "서천군", "44770"),
        ("충청남도", "청양군", "44790"),
        ("충청남도", "홍성군", "44800"),
        ("충청남도", "예산군", "44810"),
        ("충청남도", "태안군", "44825"),
                # 전북특별자치도
        ("전북특별자치도", "전주시 완산구", "52111"),
        ("전북특별자치도", "전주시 덕진구", "52113"),
        ("전북특별자치도", "군산시", "52130"),
        ("전북특별자치도", "익산시", "52140"),
        ("전북특별자치도", "정읍시", "52180"),
        ("전북특별자치도", "남원시", "52190"),
        ("전북특별자치도", "김제시", "52210"),
        ("전북특별자치도", "완주군", "52710"),
        ("전북특별자치도", "진안군", "52720"),
        ("전북특별자치도", "무주군", "52730"),
        ("전북특별자치도", "장수군", "52740"),
        ("전북특별자치도", "임실군", "52750"),
        ("전북특별자치도", "순창군", "52770"),
        ("전북특별자치도", "고창군", "52790"),
        ("전북특별자치도", "부안군", "52800"),

        # 전라남도
        ("전라남도", "목포시", "46110"),
        ("전라남도", "여수시", "46130"),
        ("전라남도", "순천시", "46150"),
        ("전라남도", "나주시", "46170"),
        ("전라남도", "광양시", "46230"),
        ("전라남도", "담양군", "46710"),
        ("전라남도", "곡성군", "46720"),
        ("전라남도", "구례군", "46730"),
        ("전라남도", "고흥군", "46770"),
        ("전라남도", "보성군", "46780"),
        ("전라남도", "화순군", "46790"),
        ("전라남도", "장흥군", "46800"),
        ("전라남도", "강진군", "46810"),
        ("전라남도", "해남군", "46820"),
        ("전라남도", "영암군", "46830"),
        ("전라남도", "무안군", "46840"),
        ("전라남도", "함평군", "46860"),
        ("전라남도", "영광군", "46870"),
        ("전라남도", "장성군", "46880"),
        ("전라남도", "완도군", "46890"),
        ("전라남도", "진도군", "46900"),
        ("전라남도", "신안군", "46910"),
                # 경상북도
        ("경상북도", "포항시 남구", "47111"),
        ("경상북도", "포항시 북구", "47113"),
        ("경상북도", "경주시", "47130"),
        ("경상북도", "김천시", "47150"),
        ("경상북도", "안동시", "47170"),
        ("경상북도", "구미시", "47190"),
        ("경상북도", "영주시", "47210"),
        ("경상북도", "영천시", "47230"),
        ("경상북도", "상주시", "47250"),
        ("경상북도", "문경시", "47280"),
        ("경상북도", "경산시", "47290"),
        ("경상북도", "의성군", "47730"),
        ("경상북도", "청송군", "47750"),
        ("경상북도", "영양군", "47760"),
        ("경상북도", "영덕군", "47770"),
        ("경상북도", "청도군", "47820"),
        ("경상북도", "고령군", "47830"),
        ("경상북도", "성주군", "47840"),
        ("경상북도", "칠곡군", "47850"),
        ("경상북도", "예천군", "47900"),
        ("경상북도", "봉화군", "47920"),
        ("경상북도", "울진군", "47930"),
        ("경상북도", "울릉군", "47940"),

        # 경상남도
        ("경상남도", "창원시 의창구", "48121"),
        ("경상남도", "창원시 성산구", "48123"),
        ("경상남도", "창원시 마산합포구", "48125"),
        ("경상남도", "창원시 마산회원구", "48127"),
        ("경상남도", "창원시 진해구", "48129"),
        ("경상남도", "진주시", "48170"),
        ("경상남도", "통영시", "48220"),
        ("경상남도", "사천시", "48240"),
        ("경상남도", "김해시", "48250"),
        ("경상남도", "밀양시", "48270"),
        ("경상남도", "거제시", "48310"),
        ("경상남도", "양산시", "48330"),
        ("경상남도", "의령군", "48720"),
        ("경상남도", "함안군", "48730"),
        ("경상남도", "창녕군", "48740"),
        ("경상남도", "고성군", "48820"),
        ("경상남도", "남해군", "48840"),
        ("경상남도", "하동군", "48850"),
        ("경상남도", "산청군", "48860"),
        ("경상남도", "함양군", "48870"),
        ("경상남도", "거창군", "48880"),
        ("경상남도", "합천군", "48890"),

        # 제주특별자치도
        ("제주특별자치도", "제주시", "50110"),
        ("제주특별자치도", "서귀포시", "50130"),
    ]

    for sido, sigungu, lawd_cd in region_list:
        insert_region_code(sido, sigungu, lawd_cd)

    print("시군구 코드 저장 완료:", len(region_list))

# ✅ DB에 저장된 모든 시군구 12개월 거래 수집
def update_all_regions_trades():
    months = get_recent_months(12)
    regions = get_all_region_codes()
    total = len(regions)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):
        print(f"[{index}/{total}] {sido} {sigungu} 수집 시작")
        for ym in months:
            save_month_trades(
                lawd_cd=lawd_cd,
                region=sido,
                sigungu=sigungu,
                deal_ymd=ym
            )

            time.sleep(0.2)

    print("전국 12개월 거래 저장 완료")

# ✅ 전국 수집 전 3개 지역 테스트
def update_test_regions_trades():
    months = get_recent_months(12)

    test_regions = [
        ("서울특별시", "강남구", "11680"),
        ("경기도", "의왕시", "41430"),
        ("부산광역시", "해운대구", "26350"),
    ]

    for sido, sigungu, lawd_cd in test_regions:
        for ym in months:
            save_month_trades(
                lawd_cd=lawd_cd,
                region=sido,
                sigungu=sigungu,
                deal_ymd=ym
            )

    print("3개 지역 12개월 테스트 저장 완료")

# ✅ 분양권 1개 지역 테스트
def update_test_presale_trades():
    months = get_recent_months(12)

    test_region = ("경기도", "의왕시", "41430")

    sido, sigungu, lawd_cd = test_region

    for ym in months:
        save_month_presale_trades(
            lawd_cd=lawd_cd,
            region=sido,
            sigungu=sigungu,
            deal_ymd=ym
        )

        time.sleep(0.2)

    print("분양권 1개 지역 테스트 저장 완료")

# ✅ 전월세 1개 지역 테스트
def update_test_rent_trades():
    months = get_recent_months(12)

    test_region = ("경기도", "의왕시", "41430")

    sido, sigungu, lawd_cd = test_region

    for ym in months:
        save_month_rent_trades(
            lawd_cd=lawd_cd,
            region=sido,
            sigungu=sigungu,
            deal_ymd=ym
        )

        time.sleep(0.2)

    print("전월세 1개 지역 테스트 저장 완료")

# ✅ 전국 전월세 12개월 수집
def update_all_rent_trades():
    months = get_recent_months(12)
    regions = get_all_region_codes()

    total = len(regions)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):
        print(f"[{index}/{total}] {sido} {sigungu} 전월세 수집 시작")

        for ym in months:
            save_month_rent_trades(
                lawd_cd=lawd_cd,
                region=sido,
                sigungu=sigungu,
                deal_ymd=ym
            )

            time.sleep(0.2)

    print("전국 전월세 12개월 거래 저장 완료")

# ✅ 전국 분양권 12개월 수집
def update_all_presale_trades(start_index=1):
    months = get_recent_months(12)
    all_regions = get_all_region_codes()
    total = len(all_regions)

    regions = all_regions[start_index - 1:]

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=start_index):
        print(f"[{index}/{total}] {sido} {sigungu} 분양권 수집 시작")

        for ym in months:
            save_month_presale_trades(
                lawd_cd=lawd_cd,
                region=sido,
                sigungu=sigungu,
                deal_ymd=ym
            )

            time.sleep(0.2)

    print("전국 분양권 12개월 거래 저장 완료")

if __name__ == "__main__":
    create_tables()
    update_all_presale_trades(start_index=136)
    