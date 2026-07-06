import time

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


def collect_daily(test_mode=False):
    print("===== 일일 자동 수집 시작 =====")

    create_tables()

    # ✅ 행정구역 자동 갱신
    update_region_codes_from_file()

    # ✅ 매일은 최근 2개월만 수집
    months = get_recent_months(2)
    regions = get_all_region_codes()

    # ===== 테스트 모드 =====
    if test_mode:
        regions = [
            ("경기도", "의왕시", "41430"),
            ("경기도", "부천시 원미구", "41192"),
            ("서울특별시", "강동구", "11740"),
        ]

    print("수집 대상 지역 수:", len(regions))
    print("수집 대상 월:", months)

    for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=1):
        print(f"[{index}/{len(regions)}] {sido} {sigungu} 수집 시작")

        for ym in months:
            save_month_trades(lawd_cd, sido, sigungu, ym)
            save_month_presale_trades(lawd_cd, sido, sigungu, ym)
            save_month_rent_trades(lawd_cd, sido, sigungu, ym)

            time.sleep(0.2)

    rebuild_apt_sale_list()

    print("===== 일일 자동 수집 완료 =====")


if __name__ == "__main__":
    collect_daily(test_mode=False)