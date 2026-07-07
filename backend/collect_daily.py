import time
import os

PROGRESS_FILE = "collect_progress.txt"
from datetime import datetime

from db import (
    create_tables,
    get_all_region_codes,
    rebuild_apt_sale_list,
)

from update_trades import (
    get_recent_months,
    update_region_codes_from_file,
    save_month_trades,
    save_month_presale_trades,
    save_month_rent_trades,
)

def save_progress(index):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        f.write(str(index))


def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return 1

    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            last_success_index = int(f.read().strip())

        return last_success_index + 1

    except Exception:
        return 1


def clear_progress():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

def collect_daily(test_mode=False, start_index=None):
    start_time = datetime.now()

    print("===== 일일 자동 수집 시작 =====")
    print("시작 시간:", start_time.strftime("%Y-%m-%d %H:%M:%S"))

    create_tables()

    # ✅ 행정구역 자동 갱신
    update_region_codes_from_file()

    # ✅ 매일은 최근 2개월만 수집
    months = get_recent_months(2)
    regions = get_all_region_codes()

    if test_mode:
        start_index = 1
    else:
        if start_index is None:
            start_index = load_progress()

    # ===== 테스트 모드 =====
    if test_mode:
        regions = [
            ("경기도", "의왕시", "41430"),
            ("경기도", "부천시 원미구", "41192"),
            ("서울특별시", "강동구", "11740"),
        ]

    print("수집 대상 지역 수:", len(regions))
    print("수집 대상 월:", months)

    if not test_mode and start_index > 1:
        regions = regions[start_index - 1:]

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=start_index):
        print(f"[{index}/{len(regions)}] {sido} {sigungu} 수집 시작")

        for ym in months:
            save_month_trades(lawd_cd, sido, sigungu, ym)
            save_month_presale_trades(lawd_cd, sido, sigungu, ym)
            save_month_rent_trades(lawd_cd, sido, sigungu, ym)

            time.sleep(0.2)

        save_progress(index)

    rebuild_apt_sale_list()

    end_time = datetime.now()
    elapsed = end_time - start_time

    clear_progress()
    
    print("종료 시간:", end_time.strftime("%Y-%m-%d %H:%M:%S"))
    print("실행 시간:", str(elapsed).split(".")[0])
    print("===== 일일 자동 수집 완료 =====")

if __name__ == "__main__":
    collect_daily(test_mode=False, start_index=0)