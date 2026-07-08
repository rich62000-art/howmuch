import time

from datetime import datetime

from db import (
    create_tables,
    get_all_region_codes,
    rebuild_apt_sale_list,
    get_pg_connection,
    release_pg_connection,
)

from update_trades import (
    get_recent_months,
    update_region_codes_from_file,
    save_month_trades,
    save_month_presale_trades,
    save_month_rent_trades,
)

def save_progress(index, sido, sigungu, lawd_cd, step="region_done"):
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
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (job_name)
            DO UPDATE SET
                last_region_index = EXCLUDED.last_region_index,
                last_sido = EXCLUDED.last_sido,
                last_sigungu = EXCLUDED.last_sigungu,
                last_lawd_cd = EXCLUDED.last_lawd_cd,
                last_step = EXCLUDED.last_step,
                updated_at = NOW()
        """, (
            "collect_daily",
            index,
            sido,
            sigungu,
            lawd_cd,
            step
        ))

        conn.commit()

    finally:
        cur.close()
        release_pg_connection(conn)


def load_progress():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT last_region_index
            FROM collect_progress
            WHERE job_name = %s
        """, ("collect_daily",))

        row = cur.fetchone()

        if not row or row[0] is None:
            return 1

        return int(row[0]) + 1

    except Exception as e:
        print("진행상황 불러오기 실패:", e)
        return 1

    finally:
        cur.close()
        release_pg_connection(conn)


def clear_progress():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            DELETE FROM collect_progress
            WHERE job_name = %s
        """, ("collect_daily",))

        conn.commit()

    except Exception as e:
        print("진행상황 삭제 실패:", e)

    finally:
        cur.close()
        release_pg_connection(conn)

def start_collect_log():
    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO collect_logs (
                job_name,
                status,
                started_at
            )
            VALUES (%s, %s, NOW())
            RETURNING id
        """, (
            "collect_daily",
            "running"
        ))

        log_id = cur.fetchone()[0]
        conn.commit()

        return log_id

    except Exception as e:
        print("수집 로그 시작 저장 실패:", e)
        return None

    finally:
        cur.close()
        release_pg_connection(conn)

def finish_collect_log(log_id, success_count=0, fail_count=0):
    if log_id is None:
        return

    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE collect_logs
            SET
                status = %s,
                ended_at = NOW(),
                success_count = %s,
                fail_count = %s
            WHERE id = %s
        """, (
            "success",
            success_count,
            fail_count,
            log_id
        ))

        conn.commit()

    except Exception as e:
        print("수집 로그 완료 저장 실패:", e)

    finally:
        cur.close()
        release_pg_connection(conn)

def fail_collect_log(log_id, error_message, sido=None, sigungu=None, lawd_cd=None, success_count=0):
    if log_id is None:
        return

    conn = get_pg_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE collect_logs
            SET
                status = %s,
                ended_at = NOW(),
                success_count = %s,
                fail_count = %s,
                last_sido = %s,
                last_sigungu = %s,
                last_lawd_cd = %s,
                error_message = %s
            WHERE id = %s
        """, (
            "failed",
            success_count,
            1,
            sido,
            sigungu,
            lawd_cd,
            str(error_message)[:1000],
            log_id
        ))

        conn.commit()

    except Exception as e:
        print("수집 로그 실패 저장 실패:", e)

    finally:
        cur.close()
        release_pg_connection(conn)

def collect_daily(test_mode=False, start_index=None):
    start_time = datetime.now()

    print("===== 일일 자동 수집 시작 =====")
    print("시작 시간:", start_time.strftime("%Y-%m-%d %H:%M:%S"))

    log_id = start_collect_log()

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

    success_count = 0

    try:
        for index, (sido, sigungu, lawd_cd) in enumerate(regions, start=start_index):
            print(f"[{index}/{len(regions)}] {sido} {sigungu} 수집 시작")

            for ym in months:
                save_month_trades(lawd_cd, sido, sigungu, ym)
                save_month_presale_trades(lawd_cd, sido, sigungu, ym)
                save_month_rent_trades(lawd_cd, sido, sigungu, ym)

                time.sleep(0.2)

            save_progress(index, sido, sigungu, lawd_cd)
            success_count += 1

    except KeyboardInterrupt:
        fail_collect_log(
            log_id,
            "사용자 중단(Ctrl+C)",
            sido=sido,
            sigungu=sigungu,
            lawd_cd=lawd_cd,
            success_count=success_count
        )
        print("사용자가 자동수집을 중단했습니다.")
        raise

    except Exception as e:
        fail_collect_log(
            log_id,
            e,
            sido=sido,
            sigungu=sigungu,
            lawd_cd=lawd_cd,
            success_count=success_count
        )
        print("자동수집 중 오류 발생:", e)
        raise

    rebuild_apt_sale_list()

    end_time = datetime.now()
    elapsed = end_time - start_time

    clear_progress()

    finish_collect_log(
        log_id,
        success_count=success_count,
        fail_count=0
    )

    print("종료 시간:", end_time.strftime("%Y-%m-%d %H:%M:%S"))
    print("실행 시간:", str(elapsed).split(".")[0])
    print("===== 일일 자동 수집 완료 =====")

if __name__ == "__main__":
    collect_daily(test_mode=False)