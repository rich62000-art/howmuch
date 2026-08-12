import csv
import time
import requests
import zipfile
import io
import xml.etree.ElementTree as ET
import sys

DEBUG_COLLECTOR = False

from datetime import datetime
from db import (
    get_pg_connection,
    release_pg_connection,
    insert_apt_sale_trade,
    replace_apt_sale_trades_for_month,
    replace_apt_rent_trades_for_month,
    replace_presale_trades_for_month,
    insert_presale_trade,
    insert_apt_rent_trade,
    insert_region_code,
    create_tables,
    get_all_region_codes,
    rebuild_apt_sale_list,
    clear_region_codes
)

SERVICE_KEY = "59c26233a7edcacf04e5d2a957e2e4e4c4a7d9d76b5925d23460aab1557e542e"

url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

presale_url = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"

rent_url = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"

REGION_CODE_URL = "https://www.code.go.kr/etc/codeFullDown.do?codeseId=00002"

# ==========================================================
# ✅ 공공데이터 API 요청 공통 재시도 함수
#
# 목적
#   - 일시적인 연결 지연, 타임아웃, 서버 오류 때문에
#     전국 수집 전체가 즉시 중단되는 것을 방지
#
# 처리 방식
#   1. 요청 실패 시 최대 3회까지 재시도
#   2. 재시도할수록 대기 시간을 늘림
#   3. 3회 모두 실패하면 예외를 발생시켜 수집을 중단
#
# 주의
#   - 실패한 응답으로 기존 DB를 교체하지 않음
#   - collect_progress를 통해 재실행 시 해당 지역부터 재개
# ==========================================================
def request_with_retry(
    request_url,
    params,
    description,
    timeout=30,
    max_retries=3
):
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                request_url,
                params=params,
                timeout=timeout
            )

            # 정상 응답
            if response.status_code == 200:
                return response

            last_error = RuntimeError(
                f"{description} HTTP 오류: "
                f"{response.status_code}"
            )

        except requests.exceptions.RequestException as e:
            last_error = e

        if attempt < max_retries:
            wait_seconds = attempt * 2

            print(
                f"⚠️ {description} 요청 실패 "
                f"({attempt}/{max_retries}) "
                f"- {wait_seconds}초 후 재시도"
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"{description} 요청이 "
        f"{max_retries}회 모두 실패했습니다: {last_error}"
    )

# ==========================================================
# ✅ 공공데이터 XML 응답 공통 파싱
#
# 목적
#   - 매매·전월세·분양권 함수에 반복되는
#     XML 파싱과 totalCount 추출 코드를 공통화
#
# 반환값
#   - root        : XML 루트 객체
#   - page_items  : 현재 페이지의 거래 항목 목록
#   - total_count : API가 알려준 전체 거래 건수
#
# 안전장치
#   - XML 파싱 실패 시 예외 발생
#   - totalCount 누락 또는 숫자 변환 실패 시 예외 발생
# ==========================================================
def parse_trade_xml(response_text, description):
    try:
        root = ET.fromstring(response_text)
    except ET.ParseError as e:
        raise RuntimeError(
            f"{description} XML 파싱 실패: {e}"
        ) from e

    total_count_text = root.findtext(".//totalCount")

    if total_count_text is None:
        raise RuntimeError(
            f"{description} API 응답에 totalCount가 없습니다."
        )

    try:
        total_count = int(total_count_text)
    except (TypeError, ValueError) as e:
        raise RuntimeError(
            f"{description} totalCount 변환 실패: "
            f"{total_count_text}"
        ) from e

    page_items = root.findall(".//item")

    return root, page_items, total_count

# ✅ 법정동 코드 파일 다운로드
def download_region_code_file():
    response = requests.get(REGION_CODE_URL)
    if DEBUG_COLLECTOR:
        print("법정동 코드 다운로드 응답:", response.status_code)

    with open("region_codes.txt", "wb") as f:
        f.write(response.content)
    if DEBUG_COLLECTOR:
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


# ==========================================================
# ✅ 아파트 매매 월별 전체 페이지 수집 및 교체 저장
#
# 목적
#   - 거래량이 500건을 초과하는 지역·월도 전부 수집
#   - 모든 페이지를 정상 수집한 뒤 DB를 월 단위로 교체
#
# 처리 순서
#   1. 첫 페이지에서 totalCount 확인
#   2. 마지막 페이지까지 순차 수집
#   3. 전체 거래 항목 파싱 및 검증
#   4. 기존 지역·월 데이터를 최신 전체 응답으로 교체
#
# 안전장치
#   - API 요청 또는 XML 파싱 실패 시 DB를 건드리지 않음
#   - totalCount와 실제 수집 건수가 다르면 교체 중단
#   - 삭제와 저장은 하나의 트랜잭션으로 처리
# ==========================================================
def save_month_trades(
    lawd_cd,
    region,
    sigungu,
    deal_ymd
):
    page_size = 500
    page_no = 1

    all_items = []
    total_count = None

    # ======================================================
    # ① 매매 API 전체 페이지 수집
    # ======================================================
    while True:
        params = {
            "serviceKey": SERVICE_KEY,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "numOfRows": page_size,
            "pageNo": page_no
        }

        response = request_with_retry(
            request_url=url,
            params=params,
            description=(
                f"{sigungu} 매매 {deal_ymd} "
                f"{page_no}페이지"
            ),
            timeout=30,
            max_retries=3
        )
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 매매 {deal_ymd} "
                f"{page_no}페이지 응답:",
                response.status_code
            )

        description = (
            f"{sigungu} 매매 {deal_ymd} "
            f"{page_no}페이지"
        )

        root, page_items, parsed_total_count = parse_trade_xml(
            response.text,
            description
        )

        if total_count is None:
            total_count = parsed_total_count

        all_items.extend(page_items)
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 매매 {deal_ymd} 수집 진행: "
                f"{len(all_items)}/{total_count}건"
            )

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as e:
            raise RuntimeError(
                f"{sigungu} 매매 {deal_ymd} "
                f"{page_no}페이지 XML 파싱 실패: {e}"
            ) from e

        if total_count is None:
            total_count_text = root.findtext(".//totalCount")

            if total_count_text is None:
                raise RuntimeError(
                    f"{sigungu} 매매 {deal_ymd} "
                    "API 응답에 totalCount가 없습니다."
                )

            try:
                total_count = int(total_count_text)
            except (TypeError, ValueError) as e:
                raise RuntimeError(
                    f"{sigungu} 매매 {deal_ymd} "
                    f"totalCount 변환 실패: {total_count_text}"
                ) from e

        
        if len(all_items) >= total_count:
            break

        if not page_items:
            raise RuntimeError(
                f"{sigungu} 매매 {deal_ymd} "
                f"{page_no}페이지가 비어 있어 "
                f"전체 {total_count}건을 수집하지 못했습니다."
            )

        page_no += 1
        time.sleep(0.1)

    if len(all_items) != total_count:
        raise RuntimeError(
            f"{sigungu} 매매 {deal_ymd} 전체 건수 불일치: "
            f"API {total_count}건 / 수집 {len(all_items)}건"
        )
    if DEBUG_COLLECTOR:
        print(
            f"{sigungu} 매매 {deal_ymd} "
            f"전체 거래 건수: {total_count}"
        )

    # ======================================================
    # ② 전체 응답을 DB 저장 형식으로 변환
    # ======================================================
    trades = []

    xml_debug_printed = False

    for item in all_items:
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()
        apt_dong = item.findtext("aptDong", "").strip()
   
        exclu_use_ar = item.findtext(
            "excluUseAr",
            "0"
        ).strip()

        deal_amount = item.findtext(
            "dealAmount",
            "0"
        ).replace(",", "").strip()

        deal_year = item.findtext(
            "dealYear",
            ""
        ).strip()

        deal_month = item.findtext(
            "dealMonth",
            ""
        ).strip().zfill(2)

        deal_day = item.findtext(
            "dealDay",
            ""
        ).strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        if not apt_name or not deal_year or not deal_month or not deal_day:
            raise RuntimeError(
                f"{sigungu} 매매 {deal_ymd} "
                "필수값이 없는 거래 항목이 발견됐습니다."
            )

        try:
            trade = {
                "region": region,
                "sigungu": sigungu,
                "dong": dong,
                "apt_name": apt_name,
                 "apt_dong": apt_dong,
                "size": float(exclu_use_ar or 0),
                "contract_date": (
                    f"{deal_year}-{deal_month}-{deal_day}"
                ),
                "price": int(deal_amount or 0),
                "floor": int(floor or 0),
                "source_month": deal_ymd
            }
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"{sigungu} 매매 {deal_ymd} "
                f"거래 항목 변환 실패: {apt_name}"
            ) from e

        trades.append(trade)

    # ======================================================
    # ③ 전체 페이지 수집·검증 후 월 데이터 한 번만 교체
    # ======================================================
    replace_apt_sale_trades_for_month(
        region=region,
        sigungu=sigungu,
        source_month=deal_ymd,
        trades=trades
    )

# ==========================================================
# ✅ 분양권 월별 전체 페이지 수집 및 교체 저장
#
# 목적
#   - 거래량이 1,000건을 초과하는 지역·월도 전부 수집
#   - 모든 페이지를 정상 수집한 뒤 DB를 월 단위로 교체
#
# 처리 순서
#   1. 첫 페이지에서 totalCount 확인
#   2. 마지막 페이지까지 순차 수집
#   3. 전체 거래 항목 파싱 및 검증
#   4. 기존 지역·월 데이터를 최신 전체 응답으로 교체
#
# 안전장치
#   - API 요청 또는 XML 파싱 실패 시 DB를 건드리지 않음
#   - totalCount와 실제 수집 건수가 다르면 교체 중단
#   - 삭제와 저장은 하나의 트랜잭션으로 처리
# ==========================================================
def save_month_presale_trades(
    lawd_cd,
    region,
    sigungu,
    deal_ymd
):
    page_size = 1000
    page_no = 1

    all_items = []
    total_count = None

    # ======================================================
    # ① 분양권 API 전체 페이지 수집
    # ======================================================
    while True:
        params = {
            "serviceKey": SERVICE_KEY,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": page_no,
            "numOfRows": page_size
        }

        response = request_with_retry(
            request_url=presale_url,
            params=params,
            description=(
                f"{sigungu} 분양권 {deal_ymd} "
                f"{page_no}페이지"
            ),
            timeout=30,
            max_retries=3
        )
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 분양권 {deal_ymd} "
                f"{page_no}페이지 응답:",
                response.status_code
            )

        description = (
            f"{sigungu} 분양권 {deal_ymd} "
            f"{page_no}페이지"
        )

        root, page_items, parsed_total_count = parse_trade_xml(
            response.text,
            description
        )

        if total_count is None:
            total_count = parsed_total_count

        all_items.extend(page_items)
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 분양권 {deal_ymd} 수집 진행: "
                f"{len(all_items)}/{total_count}건"
            )

        if len(all_items) >= total_count:
            break

        if not page_items:
            raise RuntimeError(
                f"{sigungu} 분양권 {deal_ymd} "
                f"{page_no}페이지가 비어 있어 "
                f"전체 {total_count}건을 수집하지 못했습니다."
            )

        page_no += 1
        time.sleep(0.1)

    if len(all_items) != total_count:
        raise RuntimeError(
            f"{sigungu} 분양권 {deal_ymd} 전체 건수 불일치: "
            f"API {total_count}건 / 수집 {len(all_items)}건"
        )
    if DEBUG_COLLECTOR:
        print(
            f"{sigungu} 분양권 {deal_ymd} "
            f"전체 거래 건수: {total_count}"
        )

    # ======================================================
    # ② 전체 응답을 DB 저장 형식으로 변환
    # ======================================================
    trades = []

    for item in all_items:
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()

        exclu_use_ar = item.findtext(
            "excluUseAr",
            "0"
        ).strip()

        deal_amount = item.findtext(
            "dealAmount",
            "0"
        ).replace(",", "").strip()

        deal_year = item.findtext(
            "dealYear",
            ""
        ).strip()

        deal_month = item.findtext(
            "dealMonth",
            ""
        ).strip().zfill(2)

        deal_day = item.findtext(
            "dealDay",
            ""
        ).strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        if not apt_name or not deal_year or not deal_month or not deal_day:
            raise RuntimeError(
                f"{sigungu} 분양권 {deal_ymd} "
                "필수값이 없는 거래 항목이 발견됐습니다."
            )

        try:
            trade = {
                "region": region,
                "sigungu": sigungu,
                "dong": dong,
                "apt_name": apt_name,
                "size": float(exclu_use_ar or 0),
                "contract_date": (
                    f"{deal_year}-{deal_month}-{deal_day}"
                ),
                "price": int(deal_amount or 0),
                "floor": int(floor or 0),
                "source_month": deal_ymd
            }
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"{sigungu} 분양권 {deal_ymd} "
                f"거래 항목 변환 실패: {apt_name}"
            ) from e

        trades.append(trade)

    # ======================================================
    # ✅ 중복 거래 제거
    # ======================================================
    unique_trades = []
    seen = set()

    for trade in trades:
        key = (
            trade["region"],
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"],
            round(float(trade["size"]), 4),
            trade["contract_date"],
            trade["price"],
            trade["floor"]
        )

        if key in seen:
            continue

        seen.add(key)
        unique_trades.append(trade)

    trades = unique_trades

    # ======================================================
    # ✅ 중복 제거
    # ======================================================
    before_count = len(trades)

    unique_trades = []
    seen = set()

    for trade in trades:
        key = (
            trade["region"],
            trade["sigungu"],
            trade["dong"],
            trade["apt_name"],
            round(float(trade["size"]), 4),
            trade["contract_date"],
            trade["price"],
            trade["floor"]
        )

        if key in seen:
            continue

        seen.add(key)
        unique_trades.append(trade)

    trades = unique_trades

    after_count = len(trades)

    if before_count != after_count:
        print(
            f"⚠️ 분양권 중복 제거: "
            f"{before_count - after_count}건 제거 "
            f"({before_count} → {after_count})"
        )

    # ======================================================
    # ③ 전체 페이지 수집·검증 후 월 데이터 한 번만 교체
    # ======================================================
    replace_presale_trades_for_month(
        region=region,
        sigungu=sigungu,
        source_month=deal_ymd,
        trades=trades
    )

# ==========================================================
# ✅ 아파트 전월세 월별 전체 페이지 수집 및 교체 저장
#
# 목적
#   - 거래량이 1,000건을 초과하는 지역·월도 전부 수집
#   - 모든 페이지를 정상적으로 받은 뒤에만 DB 교체
#
# 처리 순서
#   1. 첫 페이지에서 totalCount 확인
#   2. 마지막 페이지까지 순차 수집
#   3. 전체 항목 파싱 및 검증
#   4. 기존 지역·월 데이터와 최신 전체 데이터를 트랜잭션으로 교체
#
# 안전장치
#   - 페이지 요청·XML 파싱 실패 시 DB를 교체하지 않음
#   - 전체 건수보다 적게 수집되면 오류로 중단
# ==========================================================
def save_month_rent_trades(
    lawd_cd,
    region,
    sigungu,
    deal_ymd
):
    page_size = 1000
    page_no = 1

    all_items = []
    total_count = None

    # ======================================================
    # ① API 전체 페이지 수집
    # ======================================================
    while True:
        params = {
            "serviceKey": SERVICE_KEY,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": page_no,
            "numOfRows": page_size
        }

        response = request_with_retry(
            request_url=rent_url,
            params=params,
            description=(
                f"{sigungu} 전월세 {deal_ymd} "
                f"{page_no}페이지"
            ),
            timeout=30,
            max_retries=3
        )
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 전월세 {deal_ymd} "
                f"{page_no}페이지 응답:",
                response.status_code
            )

        description = (
            f"{sigungu} 전월세 {deal_ymd} "
            f"{page_no}페이지"
        )

        root, page_items, parsed_total_count = parse_trade_xml(
            response.text,
            description
        )
        

        if total_count is None:
            total_count = parsed_total_count

        all_items.extend(page_items)
        if DEBUG_COLLECTOR:
            print(
                f"{sigungu} 전월세 {deal_ymd} 수집 진행: "
                f"{len(all_items)}/{total_count}건"
            )

        # 전체 데이터를 모두 받았으면 종료
        if len(all_items) >= total_count:
            break

        # 전체 건수는 남았는데 페이지가 비어 있으면 불완전 응답
        if not page_items:
            raise RuntimeError(
                f"{sigungu} 전월세 {deal_ymd} "
                f"{page_no}페이지가 비어 있어 "
                f"전체 {total_count}건을 수집하지 못했습니다."
            )

        page_no += 1
        time.sleep(0.1)

    # API 표시 건수와 실제 수집 건수가 다르면 DB를 건드리지 않음
    if len(all_items) != total_count:
        raise RuntimeError(
            f"{sigungu} 전월세 {deal_ymd} 전체 건수 불일치: "
            f"API {total_count}건 / 수집 {len(all_items)}건"
        )
    if DEBUG_COLLECTOR:
        print(
            f"{sigungu} 전월세 {deal_ymd} "
            f"전체 거래 건수: {total_count}"
        )

    # ======================================================
    # ② 전체 응답을 DB 저장 형식으로 변환
    # ======================================================
    trades = []
    if DEBUG_COLLECTOR:
        print(
            f"🔍 전월세 변환 시작: "
            f"{region} {sigungu} {deal_ymd} "
            f"/ all_items={len(all_items)}건"
        )

    for item in all_items:
        
        apt_name = item.findtext("aptNm", "").strip()
        dong = item.findtext("umdNm", "").strip()

        exclu_use_ar = item.findtext(
            "excluUseAr",
            "0"
        ).strip()

        deposit = item.findtext(
            "deposit",
            "0"
        ).replace(",", "").strip()

        monthly_rent = item.findtext(
            "monthlyRent",
            "0"
        ).replace(",", "").strip()

        deal_year = item.findtext(
            "dealYear",
            ""
        ).strip()

        deal_month = item.findtext(
            "dealMonth",
            ""
        ).strip().zfill(2)

        deal_day = item.findtext(
            "dealDay",
            ""
        ).strip().zfill(2)

        floor = item.findtext("floor", "0").strip()

        if not apt_name or not deal_year or not deal_month or not deal_day:
            raise RuntimeError(
                f"{sigungu} 전월세 {deal_ymd} "
                "필수값이 없는 거래 항목이 발견됐습니다."
            )

        try:
            trade = {
                "region": region,
                "sigungu": sigungu,
                "dong": dong,
                "apt_name": apt_name,
                "size": float(exclu_use_ar or 0),
                "contract_date": (
                    f"{deal_year}-{deal_month}-{deal_day}"
                ),
                "deposit": int(deposit or 0),
                "monthly_rent": int(monthly_rent or 0),
                "floor": int(floor or 0),
                "source_month": deal_ymd
            }
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"{sigungu} 전월세 {deal_ymd} "
                f"거래 항목 변환 실패: {apt_name}"
            ) from e

        trades.append(trade)

    # ======================================================
    # ③ 모든 페이지 수집·검증 완료 후 한 번만 DB 교체
    # ======================================================
    if DEBUG_COLLECTOR:
        print(
            f"🔍 전월세 변환 완료: "
            f"all_items={len(all_items)}건 "
            f"/ trades={len(trades)}건"
        )
    replace_apt_rent_trades_for_month(
        region=region,
        sigungu=sigungu,
        source_month=deal_ymd,
        trades=trades
    )


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

        ("경기도", "부천시 원미구", "41192"),
        ("경기도", "부천시 소사구", "41194"),
        ("경기도", "부천시 오정구", "41196"),
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
    if DEBUG_COLLECTOR:
        print("시군구 코드 저장 완료:", len(region_list))

def load_sale_12m_progress():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                last_region_index,
                last_step,
                status
            FROM collect_progress
            WHERE job_name = %s
        """, ("sale_12m",))

        row = cur.fetchone()

        if row is None:
            return {
                "last_region_index": 0,
                "last_month": "",
                "status": ""
            }

        return {
            "last_region_index": int(row[0] or 0),
            "last_month": row[1] or "",
            "status": row[2] or ""
        }

    finally:
        cur.close()
        release_pg_connection(conn)

def load_rent_12m_progress():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                last_region_index,
                last_step,
                status
            FROM collect_progress
            WHERE job_name = %s
        """, ("rent_12m",))

        row = cur.fetchone()

        if row is None:
            return {
                "last_region_index": 0,
                "last_month": "",
                "status": ""
            }

        return {
            "last_region_index": int(row[0] or 0),
            "last_month": row[1] or "",
            "status": row[2] or ""
        }

    finally:
        cur.close()
        release_pg_connection(conn)

def load_presale_12m_progress():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                last_region_index,
                last_step,
                status
            FROM collect_progress
            WHERE job_name = %s
        """, ("presale_12m",))

        row = cur.fetchone()

        if row is None:
            return {
                "last_region_index": 0,
                "last_month": "",
                "status": ""
            }

        return {
            "last_region_index": int(row[0] or 0),
            "last_month": row[1] or "",
            "status": row[2] or ""
        }

    finally:
        cur.close()
        release_pg_connection(conn)

def update_collect_job(
    job_type,
    title,
    save_month_func,
    load_progress_func
):
    progress = load_progress_func()

    if progress.get("status") == "completed":
        print("=" * 70)
        print(f"✅ 전국 최근 12개월 {title} 수집은 이미 완료된 상태입니다.")
        print("🔄 새로 전체 수집하려면 진행상태를 초기화해야 합니다.")
        print("=" * 70)
        return

    months = get_recent_months(12)
    regions = get_all_region_codes()
    total = len(regions)

    start_region_index = progress["last_region_index"]
    last_completed_month = progress["last_month"]

    success_count = 0
    fail_count = 0

    print("=" * 70)
    print(f"🚀 전국 최근 12개월 {title} 수집 시작")
    print(f"대상 지역 : {total}개")
    print(f"대상 월   : {months}")

    if start_region_index > 0:
        print(
            f"🔄 재개 위치 : "
            f"{start_region_index}번째 지역 / "
            f"마지막 완료 월 {last_completed_month}"
        )

    print("=" * 70)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):

        if index < start_region_index:
            continue

        print(
            f"[{index}/{total}] "
            f"{sido} {sigungu} 최근 12개월 {title} 수집 시작"
        )

        for ym in months:

            if index == start_region_index and last_completed_month:
                if ym == last_completed_month:
                    last_completed_month = ""
                continue

            try:
                save_month_func(
                    lawd_cd=lawd_cd,
                    region=sido,
                    sigungu=sigungu,
                    deal_ymd=ym
                )

                success_count += 1

                save_collect_progress(
                    job_type=job_type,
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="running",
                    last_error=None
                )

                print(
                    f"✅ [{index}/{total}] "
                    f"{sido} {sigungu} {ym} {title} 완료"
                )

            except Exception as e:
                fail_count += 1

                save_collect_progress(
                    job_type=job_type,
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="error",
                    last_error=str(e)
                )

                print("=" * 70)
                print(
                    f"❌ {title} 수집 실패: "
                    f"{sido} {sigungu} / {ym} / {e}"
                )
                print("⚠️ 현재 위치를 유지하고 수집을 중단합니다.")
                print("⚠️ 다시 실행하면 실패한 월부터 재시도합니다.")
                print(f"성공: {success_count}회")
                print(f"실패: {fail_count}회")
                print("=" * 70)

                return

            time.sleep(0.2)

    # 전국 모든 지역 정상 완료
    if regions:
        last_sido, last_sigungu, last_lawd_cd = regions[-1]

        save_collect_progress(
            job_type=job_type,
            index=total,
            sido=last_sido,
            sigungu=last_sigungu,
            lawd_cd=last_lawd_cd,
            month=months[-1] if months else "",
            success_count=success_count,
            fail_count=fail_count,
            status="completed",
            last_error=None
        )

    print_collect_summary(
        title,
        success_count,
        fail_count
    )

def save_collect_progress(
    job_type,
    index,
    sido,
    sigungu,
    lawd_cd,
    month,
    success_count=0,
    fail_count=0,
    status="running",
    last_error=None
):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO collect_progress (
                job_name,
                last_region_index,
                last_sido,
                last_sigungu,
                last_lawd_cd,
                last_step,
                status,
                success_count,
                fail_count,
                last_error,
                started_at,
                updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                COALESCE(
                    (
                        SELECT started_at
                        FROM collect_progress
                        WHERE job_name = %s
                    ),
                    NOW()
                ),
                NOW()
            )
            ON CONFLICT (job_name)
            DO UPDATE SET
                last_region_index = EXCLUDED.last_region_index,
                last_sido         = EXCLUDED.last_sido,
                last_sigungu       = EXCLUDED.last_sigungu,
                last_lawd_cd       = EXCLUDED.last_lawd_cd,
                last_step          = EXCLUDED.last_step,
                status             = EXCLUDED.status,
                success_count      = EXCLUDED.success_count,
                fail_count         = EXCLUDED.fail_count,
                last_error         = EXCLUDED.last_error,
                updated_at         = NOW(),
                completed_at      = CASE
                    WHEN EXCLUDED.status = 'completed'
                    THEN NOW()
                    ELSE collect_progress.completed_at
                END;
        """, (
            job_type,
            index,
            sido,
            sigungu,
            lawd_cd,
            month,
            status,
            success_count,
            fail_count,
            last_error,
            job_type
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        release_pg_connection(conn)

# ✅ 매일 실행용: 최근 2개월
def update_all_regions_trades_old():
    months = get_recent_months(2)
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

    print("✅ 전국 최근 2개월 매매 거래 저장 완료")

def update_all_regions_trades():
    return update_collect_job_recent(
        title="매매",
        save_month_func=save_month_trades
    )

def update_all_regions_rent_trades():
    return update_collect_job_recent(
        title="전월세",
        save_month_func=save_month_rent_trades
    )

def update_all_regions_presale_trades():
    return update_collect_job_recent(
        title="분양권",
        save_month_func=save_month_presale_trades
    )

def update_recent_all():
    print("\n" + "=" * 70)
    print("🚀 최근 2개월 전국 데이터 자동수집 시작")
    print("=" * 70)

    update_all_regions_trades()
    update_all_regions_rent_trades()
    update_all_regions_presale_trades()

    print("=" * 70)
    print("✅ 최근 2개월 전체 수집 완료")
    print("=" * 70)

def update_12m_all():
    print("\n" + "=" * 70)
    print("🚀 최근 12개월 전국 데이터 자동수집 시작")
    print("=" * 70)

    update_all_regions_trades_12m()
    update_all_regions_rent_trades_12m()
    update_all_presale_trades_12m()

    print("=" * 70)
    print("✅ 최근 12개월 전체 수집 완료")
    print("=" * 70)

# ==========================================================
# Legacy Version (백업용)
# 공통 엔진(update_collect_job) 도입 후 보관 중
# 충분한 운영 검증 후 삭제 예정
# ==========================================================
def update_all_regions_trades_12m_old():
    months = get_recent_months(12)
    regions = get_all_region_codes()
    total = len(regions)

    # 이전 진행상황 불러오기
    progress = load_sale_12m_progress()

    start_region_index = progress["last_region_index"]
    last_completed_month = progress["last_month"]

    success_count = 0
    fail_count = 0

    print("=" * 70)
    print("🚀 전국 최근 12개월 매매 재수집 시작")
    print(f"대상 지역 : {total}개")
    print(f"대상 월   : {months}")

    if start_region_index > 0:
        print(
            f"🔄 재개 위치: "
            f"{start_region_index}번째 지역 / "
            f"마지막 완료 월 {last_completed_month}"
        )

    print("=" * 70)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):

        # 이미 완료된 이전 지역은 건너뜀
        if index < start_region_index:
            continue

        print(f"[{index}/{total}] {sido} {sigungu} 최근 12개월 수집 시작")

        for ym in months:

            # 중단됐던 동일 지역에서는
            # 마지막 완료 월까지 건너뛰고 그다음 월부터 재개
            if index == start_region_index and last_completed_month:
                if ym == last_completed_month:
                    last_completed_month = ""
                continue

            try:
                save_month_trades(
                    lawd_cd=lawd_cd,
                    region=sido,
                    sigungu=sigungu,
                    deal_ymd=ym
                )

                success_count += 1

                # PostgreSQL 진행상황 저장
                save_collect_progress(
                    job_type="sale_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="running",
                    last_error=None
                )

                print(
                    f"✅ [{index}/{total}] "
                    f"{sido} {sigungu} {ym} 완료"
                )

            except Exception as e:
                fail_count += 1

                save_collect_progress(
                    job_type="sale_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="error",
                    last_error=str(e)
                )

                
                print("=" * 70)
                print(
                    f"❌ 수집 실패: "
                    f"{sido} {sigungu} / {ym} / {e}"
                )
                print("⚠️ 현재 위치를 유지하고 수집을 중단합니다.")
                print("⚠️ 다시 실행하면 실패한 월부터 재시도합니다.")
                print(f"성공: {success_count}회")
                print(f"실패: {fail_count}회")
                print("=" * 70)

                return

            time.sleep(0.2)

    # 전국 268개 지역이 모두 정상 완료된 경우
    if regions:
        last_sido, last_sigungu, last_lawd_cd = regions[-1]

        save_collect_progress(
            job_type="sale_12m",
            index=total,
            sido=last_sido,
            sigungu=last_sigungu,
            lawd_cd=last_lawd_cd,
            month=months[-1] if months else "",
            success_count=success_count,
            fail_count=fail_count,
            status="completed",
            last_error=None
        )

    print_collect_summary(
        "매매",
        success_count,
        fail_count
    )


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

# ==========================================================
# Legacy Version (백업용)
# 공통 엔진(update_collect_job) 도입 후 보관 중
# 충분한 운영 검증 후 삭제 예정
# ==========================================================
def update_all_rent_trades_12m_old():
    months = get_recent_months(12)
    regions = get_all_region_codes()
    total = len(regions)

    # 이전 전월세 진행상황 불러오기
    progress = load_rent_12m_progress()

    start_region_index = progress["last_region_index"]
    last_completed_month = progress["last_month"]

    success_count = 0
    fail_count = 0

    print("=" * 70)
    print("🚀 전국 최근 12개월 전월세 재수집 시작")
    print(f"대상 지역 : {total}개")
    print(f"대상 월   : {months}")

    if start_region_index > 0:
        print(
            f"🔄 재개 위치: "
            f"{start_region_index}번째 지역 / "
            f"마지막 완료 월 {last_completed_month}"
        )

    print("=" * 70)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):

        # 이미 완료된 이전 지역 건너뛰기
        if index < start_region_index:
            continue

        print(
            f"[{index}/{total}] "
            f"{sido} {sigungu} 최근 12개월 전월세 수집 시작"
        )

        for ym in months:

            # 중단됐던 동일 지역에서는 마지막 완료 월까지 건너뛰고
            # 그다음 월부터 재개
            if index == start_region_index and last_completed_month:
                if ym == last_completed_month:
                    last_completed_month = ""
                continue

            try:
                save_month_rent_trades(
                    lawd_cd=lawd_cd,
                    region=sido,
                    sigungu=sigungu,
                    deal_ymd=ym
                )

                success_count += 1

                save_collect_progress(
                    job_type="rent_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="running"
                )

                print(
                    f"✅ [{index}/{total}] "
                    f"{sido} {sigungu} {ym} 전월세 완료"
                )

            except Exception as e:
                fail_count += 1

                save_collect_progress(
                    job_type="rent_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="error",
                    last_error=str(e)
                )

                print("=" * 70)
                print(
                    f"❌ 전월세 수집 실패: "
                    f"{sido} {sigungu} / {ym} / {e}"
                )
                print("⚠️ 현재 위치를 유지하고 수집을 중단합니다.")
                print("⚠️ 다시 실행하면 실패한 월부터 재시도합니다.")
                print(f"성공: {success_count}회")
                print(f"실패: {fail_count}회")
                print("=" * 70)

                return

            time.sleep(0.2)

    # 전국 전월세 수집 정상 완료
    if regions:
        last_sido, last_sigungu, last_lawd_cd = regions[-1]

        save_collect_progress(
            job_type="rent_12m",
            index=total,
            sido=last_sido,
            sigungu=last_sigungu,
            lawd_cd=last_lawd_cd,
            month=months[-1] if months else "",
            success_count=success_count,
            fail_count=fail_count,
            status="completed"
        )

    print("=" * 70)
    print("✅ 전국 최근 12개월 전월세 거래 갱신 완료")
    print(f"성공: {success_count}회")
    print(f"실패: {fail_count}회")
    print("=" * 70)

# ==========================================================
# Legacy Version (백업용)
# 공통 엔진(update_collect_job) 도입 후 보관 중
# 충분한 운영 검증 후 삭제 예정
# ==========================================================
def update_all_presale_trades_12m_old():
    months = get_recent_months(12)
    regions = get_all_region_codes()
    total = len(regions)

    progress = load_presale_12m_progress()

    start_region_index = progress["last_region_index"]
    last_completed_month = progress["last_month"]

    success_count = 0
    fail_count = 0

    print("=" * 70)
    print("🚀 전국 최근 12개월 분양권 재수집 시작")
    print(f"대상 지역 : {total}개")
    print(f"대상 월   : {months}")

    if start_region_index > 0:
        print(
            f"🔄 재개 위치: "
            f"{start_region_index}번째 지역 / "
            f"마지막 완료 월 {last_completed_month}"
        )

    print("=" * 70)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):

        if index < start_region_index:
            continue

        print(
            f"[{index}/{total}] "
            f"{sido} {sigungu} 최근 12개월 분양권 수집 시작"
        )

        for ym in months:

            if index == start_region_index and last_completed_month:
                if ym == last_completed_month:
                    last_completed_month = ""
                continue

            try:
                save_month_presale_trades(
                    lawd_cd=lawd_cd,
                    region=sido,
                    sigungu=sigungu,
                    deal_ymd=ym
                )

                success_count += 1

                save_collect_progress(
                    job_type="presale_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="running"
                )

                print(
                    f"✅ [{index}/{total}] "
                    f"{sido} {sigungu} {ym} 분양권 완료"
                )

            except Exception as e:
                fail_count += 1

                save_collect_progress(
                    job_type="presale_12m",
                    index=index,
                    sido=sido,
                    sigungu=sigungu,
                    lawd_cd=lawd_cd,
                    month=ym,
                    success_count=success_count,
                    fail_count=fail_count,
                    status="error",
                    last_error=str(e)
                )

                print("=" * 70)
                print(
                    f"❌ 분양권 수집 실패: "
                    f"{sido} {sigungu} / {ym} / {e}"
                )
                print("⚠️ 현재 위치를 유지하고 수집을 중단합니다.")
                print("⚠️ 다시 실행하면 실패한 월부터 재시도합니다.")
                print(f"성공: {success_count}회")
                print(f"실패: {fail_count}회")
                print("=" * 70)

                return

            time.sleep(0.2)

    if regions:
        last_sido, last_sigungu, last_lawd_cd = regions[-1]

        save_collect_progress(
            job_type="presale_12m",
            index=total,
            sido=last_sido,
            sigungu=last_sigungu,
            lawd_cd=last_lawd_cd,
            month=months[-1] if months else "",
            success_count=success_count,
            fail_count=fail_count,
            status="completed"
        )

    print("=" * 70)
    print("✅ 전국 최근 12개월 분양권 거래 갱신 완료")
    print(f"성공: {success_count}회")
    print(f"실패: {fail_count}회")
    print("=" * 70)

def update_region_codes_from_file():
    print("법정동 코드 자동 갱신 시작")

    response = requests.get(REGION_CODE_URL)
    print("법정동 코드 다운로드 응답:", response.status_code)

    if response.status_code != 200:
        print("❌ 법정동 코드 다운로드 실패")
        return

    # ✅ 기존 지역코드 목록 임시 보관
    old_region_codes = get_all_region_codes()

    clear_region_codes()

    zip_file = zipfile.ZipFile(io.BytesIO(response.content))

    txt_name = None
    for name in zip_file.namelist():
        if name.endswith(".txt"):
            txt_name = name
            break

    if not txt_name:
        print("❌ txt 파일을 찾지 못함")
        return

    content = zip_file.read(txt_name).decode("cp949")

    count = 0

    for line in content.splitlines():
        parts = line.split("\t")

        if len(parts) < 3:
            continue

        code = parts[0].strip()
        full_name = parts[1].strip()
        status = parts[2].strip()

        if status != "존재":
            continue

        # 시군구 대표 코드만 사용: 뒤 5자리가 00000
        if not code.endswith("00000"):
            continue

        lawd_cd = code[:5]
        name_parts = full_name.split()

        if len(name_parts) < 2:
            continue

        sido = name_parts[0]

        if sido == "세종특별자치시":
            sigungu = "세종시"
        else:
            sigungu = " ".join(name_parts[1:])

        insert_region_code(sido, sigungu, lawd_cd)
        count += 1

    # ===========================
    # 지역코드 변경 비교 준비
    # ===========================

    # 기존 목록
    old_set = {
        (sido, sigungu, lawd_cd)
        for sido, sigungu, lawd_cd in old_region_codes
    }

    # 새 목록
    new_region_codes = get_all_region_codes()

    new_set = {
        (sido, sigungu, lawd_cd)
        for sido, sigungu, lawd_cd in new_region_codes
    }

    # ===========================
    # 변경 감지용 Dictionary 생성
    # ===========================

    old_by_code = {
        lawd_cd: (sido, sigungu)
        for sido, sigungu, lawd_cd in old_region_codes
    }

    new_by_code = {
        lawd_cd: (sido, sigungu)
        for sido, sigungu, lawd_cd in new_region_codes
    }

    # ===========================
    # 변경된 지역 비교
    # ===========================

    added = new_set - old_set
    removed = old_set - new_set

    changed = []

    for lawd_cd in old_by_code.keys() & new_by_code.keys():

        if old_by_code[lawd_cd] != new_by_code[lawd_cd]:

            changed.append({
                "lawd_cd": lawd_cd,
                "old": old_by_code[lawd_cd],
                "new": new_by_code[lawd_cd]
            })

    print("법정동 코드 자동 갱신 완료:", count)

    if not added and not removed and not changed:
        print("✅ 행정구역 변경사항 없음")

    else:

        if added:
            print(f"🆕 신규 행정구역 {len(added)}건")

            for sido, sigungu, lawd_cd in sorted(added):
                print(f"   + {sido} {sigungu} ({lawd_cd})")

        if removed:
            print(f"❌ 삭제된 행정구역 {len(removed)}건")

            for sido, sigungu, lawd_cd in sorted(removed):
                print(f"   - {sido} {sigungu} ({lawd_cd})")

        if changed:
            print(f"🔄 변경된 행정구역 {len(changed)}건")

            for item in changed:
                old_sido, old_sigungu = item["old"]
                new_sido, new_sigungu = item["new"]

                print(
                    f"   * {item['lawd_cd']} : "
                    f"{old_sido} {old_sigungu}"
                    f" → "
                    f"{new_sido} {new_sigungu}"
                )

        # ✅ 행정구역 변경 이력 DB 저장
        for sido, sigungu, lawd_cd in added:
            save_region_change_log(
                change_type="added",
                lawd_cd=lawd_cd,
                new_sido=sido,
                new_sigungu=sigungu
            )

        for sido, sigungu, lawd_cd in removed:
            save_region_change_log(
                change_type="removed",
                lawd_cd=lawd_cd,
                old_sido=sido,
                old_sigungu=sigungu
            )

        for item in changed:
            old_sido, old_sigungu = item["old"]
            new_sido, new_sigungu = item["new"]

            save_region_change_log(
                change_type="changed",
                lawd_cd=item["lawd_cd"],
                old_sido=old_sido,
                old_sigungu=old_sigungu,
                new_sido=new_sido,
                new_sigungu=new_sigungu
            )


def update_all_regions_trades_12m():
    return update_collect_job(
        job_type="sale_12m",
        title="매매",
        save_month_func=save_month_trades,
        load_progress_func=load_sale_12m_progress
    )

def update_collect_job_recent(
    title,
    save_month_func
):
    months = get_recent_months(2)
    regions = get_all_region_codes()
    total = len(regions)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):
        print(f"[{index}/{total}] {sido} {sigungu} 수집 시작")

        for ym in months:
            save_month_func(
                lawd_cd=lawd_cd,
                region=sido,
                sigungu=sigungu,
                deal_ymd=ym
            )

            time.sleep(0.2)

    print("=" * 70)
    print(f"✅ 전국 최근 2개월 {title} 거래 저장 완료")
    print("=" * 70)

def update_all_rent_trades_12m():
    return update_collect_job(
        job_type="rent_12m",
        title="전월세",
        save_month_func=save_month_rent_trades,
        load_progress_func=load_rent_12m_progress
    )

def update_all_presale_trades_12m():
    return update_collect_job(
        job_type="presale_12m",
        title="분양권",
        save_month_func=save_month_presale_trades,
        load_progress_func=load_presale_12m_progress
    )

def print_collect_summary(title, success_count, fail_count):
    print("=" * 70)
    print(f"✅ 전국 최근 12개월 {title} 거래 갱신 완료")
    print(f"성공: {success_count}회")
    print(f"실패: {fail_count}회")
    print("=" * 70)

def save_region_change_log(
    change_type,
    lawd_cd,
    old_sido=None,
    old_sigungu=None,
    new_sido=None,
    new_sigungu=None
):
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        # ✅ 이미 같은 변경 이력이 있는지 확인
        cur.execute("""
            SELECT 1
            FROM region_change_logs
            WHERE
                change_type = %s
                AND lawd_cd = %s
                AND old_sido IS NOT DISTINCT FROM %s
                AND old_sigungu IS NOT DISTINCT FROM %s
                AND new_sido IS NOT DISTINCT FROM %s
                AND new_sigungu IS NOT DISTINCT FROM %s
            LIMIT 1
        """, (
            change_type,
            lawd_cd,
            old_sido,
            old_sigungu,
            new_sido,
            new_sigungu
        ))

        # ✅ 이미 저장되어 있으면 종료
        if cur.fetchone():
            return
        
        cur.execute("""
            INSERT INTO region_change_logs (
                change_type,
                lawd_cd,
                old_sido,
                old_sigungu,
                new_sido,
                new_sigungu
            )
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            change_type,
            lawd_cd,
            old_sido,
            old_sigungu,
            new_sido,
            new_sigungu
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        release_pg_connection(conn)

# ==========================================================
# ✅ 전남광주통합특별시 북구 분양권 단일 테스트
#
# 목적
#   - 실제 거래가 있는 월을 대상으로
#     구 지역명 데이터까지 함께 삭제되는지 확인
# ==========================================================
def test_integrated_region_presale_replace():
    save_month_presale_trades(
        lawd_cd="12300",
        region="전남광주통합특별시",
        sigungu="북구",
        deal_ymd="202602"
    )

    print("✅ 북구 202602 분양권 교체 테스트 완료")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "recent"

    if mode == "recent":
        update_recent_all()

    elif mode == "12m":
        update_12m_all()

    elif mode == "all":
        update_recent_all()
        update_12m_all()

    else:
        print(f"❌ 잘못된 실행 모드: {mode}")
        print("사용법:")
        print("  py update_trades.py recent")
        print("  py update_trades.py 12m")
        print("  py update_trades.py all")