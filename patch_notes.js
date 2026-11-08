// 패치노트 요약 데이터 (패치가 나오면 이 파일만 바꾸면 됩니다)
// change 에는 "상향" / "하향" / "조정" 중 하나를 씁니다.
window.PATCH_NOTES = {
  patch: "26.22",
  date: "2026-11-03",
  url: "https://www.leagueoflegends.com/ko-kr/news/game-updates/league-of-legends-patch-26-22-notes",
  summary: "프로 경기 위주로 억제된 코르키·럼블 상향, 금지율이 높은 로크·야스오 하향, 멜 중간 규모 업데이트. 도란의 활과 탐욕의 군화가 원딜 고정 빌드가 되지 않도록 조정.",
  champions: [
    { name: "아우렐리온 솔", change: "상향" },
    { name: "코르키", change: "상향" },
    { name: "케일", change: "상향" },
    { name: "마오카이", change: "상향" },
    { name: "나피리", change: "상향" },
    { name: "럼블", change: "상향" },
    { name: "쉔", change: "상향" },
    { name: "스몰더", change: "상향" },
    { name: "빅토르", change: "상향" },
    { name: "자헨", change: "상향" },
    { name: "로크", change: "하향" },
    { name: "쓰레쉬", change: "하향" },
    { name: "야스오", change: "하향" },
    { name: "멜", change: "조정" }
  ],
  items: [
    { name: "지배자의 피갑옷", change: "상향" },
    { name: "도란의 활", change: "하향" },
    { name: "탐욕의 군화", change: "조정" }
  ]
};
