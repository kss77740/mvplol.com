import json
import datetime

now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
print(f"[{now}] 랭킹 데이터 갱신 스크립트 실행 중...")

ranking_data = {
    "top": [
        { "rank": 1, "name": "아트록스", "tier": "OP", "winRate": "52.4%", "pickRate": "12.1%", "banRate": "8.5%" },
        { "rank": 2, "name": "트런들", "tier": "1티어", "winRate": "51.8%", "pickRate": "6.4%", "banRate": "11.2%" },
        { "rank": 3, "name": "레넥톤", "tier": "1티어", "winRate": "50.9%", "pickRate": "9.8%", "banRate": "4.1%" }
    ],
    "jungle": [
        { "rank": 1, "name": "리 신", "tier": "OP", "winRate": "51.2%", "pickRate": "18.5%", "banRate": "12.4%" },
        { "rank": 2, "name": "마스터 이", "tier": "1티어", "winRate": "50.8%", "pickRate": "7.2%", "banRate": "15.0%" }
    ],
    "mid": [
        { "rank": 1, "name": "탈론", "tier": "OP", "winRate": "52.1%", "pickRate": "6.5%", "banRate": "9.1%" },
        { "rank": 2, "name": "제드", "tier": "1티어", "winRate": "51.0%", "pickRate": "14.2%", "banRate": "22.5%" }
    ],
    "adc": [
        { "rank": 1, "name": "징크스", "tier": "OP", "winRate": "52.6%", "pickRate": "22.1%", "banRate": "4.5%" },
        { "rank": 2, "name": "이즈리얼", "tier": "1티어", "winRate": "50.9%", "pickRate": "28.4%", "banRate": "6.1%" }
    ],
    "support": [
        { "rank": 1, "name": "쓰레쉬", "tier": "OP", "winRate": "51.9%", "pickRate": "16.8%", "banRate": "10.4%" },
        { "rank": 2, "name": "레오나", "tier": "1티어", "winRate": "51.1%", "pickRate": "11.5%", "banRate": "7.2%" }
    ]
}

file_content = f"window.RANKING_DATABASE = {json.dumps(ranking_data, ensure_ascii=False, indent=2)};"

with open("ranking_data.js", "w", encoding="utf-8") as f:
    f.write(file_content)

print("ranking_data.js 파일 생성 및 갱신 완료!")
