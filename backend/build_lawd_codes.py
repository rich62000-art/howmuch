import json

SOURCE_FILE = "법정동코드 전체자료.txt"
OUTPUT_FILE = "lawd_codes.json"


def normalize_region_name(name: str) -> str:
    return " ".join(name.split())


def build_lawd_codes():
    lawd_map = {}

    with open(SOURCE_FILE, "r", encoding="cp949") as f:
        lines = f.readlines()

    for line in lines[1:]:  # 헤더 제외
        parts = line.strip().split("\t")

        if len(parts) < 3:
            continue

        code = parts[0].strip()
        name = parts[1].strip()
        status = parts[2].strip()

        # 폐지 지역 제외
        if status != "존재":
            continue

        # 시군구 단위만 사용 (뒤 5자리가 00000)
        if not code.endswith("00000"):
            continue

        name = normalize_region_name(name)

        # 예: "서울특별시 종로구"
        lawd_map[name] = code[:5]

        # 축약형도 추가
        short_name = (
            name.replace("특별시", "")
                .replace("광역시", "")
                .replace("특별자치시", "")
                .replace("특별자치도", "")
                .replace("자치시", "")
                .replace("자치도", "")
        )
        short_name = normalize_region_name(short_name)

        if short_name != name:
            lawd_map[short_name] = code[:5]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(lawd_map, f, ensure_ascii=False, indent=2)

    print(f"완료: {OUTPUT_FILE} 생성됨")
    print(f"총 지역 수: {len(lawd_map)}")


if __name__ == "__main__":
    build_lawd_codes()