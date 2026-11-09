"""
Mvp.lol 챔피언 랭킹 자동 계산 스크립트

라이엇 공식 API로 한국 서버 솔로랭크 경기를 모아서
라인별 승률 / 픽률 / 밴률 TOP 10 → ranking_data.js
챔피언별 빌드, 룬, 스펠, 카운터, 인기 구도, 급상승/급하락 → champ_data.js

- GitHub Actions에서 하루 한 번 실행되도록 만들어져 있습니다.
- API 키는 저장소 Secrets 의 RIOT_API_KEY 에서 읽습니다. (코드에 절대 직접 쓰지 마세요)
- 모은 경기 결과는 data/ranking_state.json 에 누적되며, 패치가 바뀌면 새로 집계합니다.
- 외부 라이브러리 없이 파이썬 기본 기능만 사용합니다.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

# ───────────────────────── 설정 ─────────────────────────
API_KEY = os.environ.get("RIOT_API_KEY", "").strip()

PLATFORM = "kr"            # 한국 서버
REGION = "asia"            # match-v5 는 지역 단위
QUEUE_ID = 420             # 솔로랭크

# 수집할 티어: 챌린저, 그랜드마스터, 마스터 (+ 다이아 I 일부)
# 수집할 티어: 에메랄드 이상 (OP.GG 기본값과 같은 구간)
# 티어별로 몇 페이지(1페이지 = 약 205명)씩 표본 유저를 뽑을지
EMERALD_PAGES = int(os.environ.get("EMERALD_PAGES", "4"))   # 에메랄드 I~IV 각각
DIAMOND_PAGES = int(os.environ.get("DIAMOND_PAGES", "2"))   # 다이아 I~IV 각각
TIER_LABEL = "에메랄드+"

MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "110"))  # 한 번 실행 시 최대 수집 시간
FETCH_TIMELINE = os.environ.get("FETCH_TIMELINE", "1") == "1"      # 아이템 구매 순서(1·2·3코어)용 타임라인 수집
MATCHES_PER_PLAYER = 20                                           # 한 명당 최근 경기 몇 개를 볼지
LOOKBACK_DAYS = 14

# 개인(Personal) 키 기본 제한: 1초 20회, 2분 100회
RATE_LIMITS = [
    (int(os.environ.get("RATE_PER_SEC", "20")), 1.0),
    (int(os.environ.get("RATE_PER_2MIN", "100")), 120.0),
]

MIN_GAMES_TO_PUBLISH = 500     # 이번 패치 표본이 이보다 적으면 이전 패치 결과를 계속 보여줌
MIN_PICK_SHARE = 0.01          # 해당 라인에서 픽률 1% 미만은 순위에서 제외
MIN_CHAMP_GAMES = 20           # 최소 표본 판수
SMOOTHING_GAMES = 50           # 판수가 적은 챔프의 승률을 50% 쪽으로 보정하는 강도

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DATA_DIR 를 주면 데이터 파일을 그 폴더(= data 브랜치)에 저장. 없으면 저장소 안에 저장.
DATA_DIR = os.environ.get("DATA_DIR", "").strip()
if DATA_DIR:
    STATE_PATH = os.path.join(DATA_DIR, "ranking_state.json")
    OUTPUT_PATH = os.path.join(DATA_DIR, "ranking_data.js")
    CHAMP_OUTPUT_PATH = os.path.join(DATA_DIR, "champ_data.js")
else:
    STATE_PATH = os.path.join(ROOT, "data", "ranking_state.json")
    OUTPUT_PATH = os.path.join(ROOT, "ranking_data.js")
    CHAMP_OUTPUT_PATH = os.path.join(ROOT, "champ_data.js")

MIN_ROLE_GAMES = 10        # 챔피언 상세(빌드/카운터)를 보여줄 최소 판수 (30판 미만은 사이트에서 '표본 적음' 안내)
MIN_VS_GAMES = 3           # 상대별 승률을 보여줄 최소 판수
MIN_TREND_GAMES = 40       # 급상승/급하락 비교에 필요한 패치별 최소 판수

ROLE_MAP = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}
LINES = ["top", "jungle", "mid", "adc", "support"]

KST = timezone(timedelta(hours=9))
START_TIME = time.time()


def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def time_left():
    return MAX_RUNTIME_MIN * 60 - (time.time() - START_TIME)


# ───────────────────────── HTTP / 호출 제한 ─────────────────────────
_call_log = deque()


def _wait_for_rate_limit():
    while True:
        now = time.time()
        wait = 0.0
        for limit, window in RATE_LIMITS:
            recent = [t for t in _call_log if now - t < window]
            if len(recent) >= limit:
                wait = max(wait, window - (now - recent[0]) + 0.05)
        if wait <= 0:
            break
        time.sleep(wait)
    _call_log.append(time.time())
    while _call_log and time.time() - _call_log[0] > 130:
        _call_log.popleft()


def http_get_json(url, riot=True, retries=4):
    for attempt in range(retries):
        if riot:
            _wait_for_rate_limit()
        req = urllib.request.Request(url, headers={"User-Agent": "mvp-lol-ranking/1.0"})
        if riot:
            req.add_header("X-Riot-Token", API_KEY)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10"))
                log(f"호출 제한(429) → {wait}초 대기")
                time.sleep(wait + 1)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            if e.code in (401, 403):
                raise SystemExit(f"API 키 오류({e.code}): 키가 만료됐거나 잘못됐어요. Secrets 의 RIOT_API_KEY 를 확인하세요.")
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 * (attempt + 1))
    return None


def riot_platform(path):
    return http_get_json(f"https://{PLATFORM}.api.riotgames.com{path}")


def riot_region(path):
    return http_get_json(f"https://{REGION}.api.riotgames.com{path}")


# ───────────────────────── 게임 데이터 (Data Dragon) ─────────────────────────
def load_game_data():
    """챔피언 / 아이템 / 룬 / 스펠 이름 정보 (한국어)"""
    versions = http_get_json("https://ddragon.leagueoflegends.com/api/versions.json", riot=False)
    v = versions[0]
    base = f"https://ddragon.leagueoflegends.com/cdn/{v}/data/ko_KR"

    champs = http_get_json(f"{base}/champion.json", riot=False)["data"]
    names = {c["key"]: (c["name"], c["id"]) for c in champs.values()}

    items_raw = http_get_json(f"{base}/item.json", riot=False)["data"]
    items, boots = {}, {}
    for iid, it in items_raw.items():
        if not (it.get("maps", {}).get("11") and it.get("gold", {}).get("purchasable")):
            continue
        if it.get("requiredChampion") or it.get("requiredAlly"):
            continue
        tags = it.get("tags", [])
        if "Boots" in tags:
            if iid != "1001":
                boots[iid] = it["name"]                  # 업그레이드 신발 (2단계 이상)
        elif not it.get("into") and it["gold"].get("total", 0) >= 2000 and "Consumable" not in tags:
            items[iid] = it["name"]                      # 완성 아이템

    runes = {}
    styles = {}
    tree = []   # 인게임 룬 화면처럼 그리기 위한 전체 룬 구조
    for style in http_get_json(f"{base}/runesReforged.json", riot=False):
        styles[str(style["id"])] = {"name": style["name"], "icon": style["icon"]}
        slots = []
        for slot in style["slots"]:
            row = []
            for rune in slot["runes"]:
                runes[str(rune["id"])] = {"name": rune["name"], "icon": rune["icon"]}
                row.append({"id": rune["id"], "name": rune["name"], "icon": rune["icon"]})
            slots.append(row)
        tree.append({"id": style["id"], "name": style["name"], "icon": style["icon"], "slots": slots})

    spells = {}
    for sp in http_get_json(f"{base}/summoner.json", riot=False)["data"].values():
        spells[sp["key"]] = {"id": sp["id"], "name": sp["name"]}

    return {"names": names, "items": items, "boots": boots, "runes": runes,
            "styles": styles, "spells": spells, "tree": tree}


# 능력치 파편 (Data Dragon 에 없어서 직접 정의)
STAT_SHARDS = {
    "5008": {"name": "적응형 능력치", "icon": "perk-images/StatMods/StatModsAdaptiveForceIcon.png"},
    "5005": {"name": "공격 속도", "icon": "perk-images/StatMods/StatModsAttackSpeedIcon.png"},
    "5007": {"name": "스킬 가속", "icon": "perk-images/StatMods/StatModsCDRScalingIcon.png"},
    "5010": {"name": "이동 속도", "icon": "perk-images/StatMods/StatModsMovementSpeedIcon.png"},
    "5001": {"name": "체력 증가 (레벨 비례)", "icon": "perk-images/StatMods/StatModsHealthPlusIcon.png"},
    "5011": {"name": "체력", "icon": "perk-images/StatMods/StatModsHealthScalingIcon.png"},
    "5013": {"name": "강인함 및 둔화 저항", "icon": "perk-images/StatMods/StatModsTenacityIcon.png"},
    "5002": {"name": "방어력", "icon": "perk-images/StatMods/StatModsArmorIcon.png"},
    "5003": {"name": "마법 저항력", "icon": "perk-images/StatMods/StatModsMagicResIcon.MagicResist_Fix.png"},
}
SHARD_ROWS = [["5008", "5005", "5007"], ["5008", "5010", "5001"], ["5011", "5013", "5001"]]


# ───────────────────────── 상태 저장 ─────────────────────────
def empty_bucket(patch):
    return {"patch": patch, "games": 0, "picks": {}, "bans": {}, "detail": {}}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"current": None, "previous": None, "seen": [], "cursor": 0, "oldCutoff": 0}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


def patch_of(game_version):
    parts = str(game_version).split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else None


def display_patch(p):
    """경기 데이터 버전(16.22) → 실제 패치 이름(26.22)"""
    try:
        major, minor = p.split(".")[:2]
        major = int(major)
        return f"{major + 10 if major < 20 else major}.{minor}"
    except Exception:
        return p


def patch_key(p):
    try:
        return tuple(int(x) for x in p.split("."))
    except Exception:
        return (0, 0)


# ───────────────────────── 수집 ─────────────────────────
def get_players():
    """에메랄드+ 유저 puuid 목록. 티어가 골고루 섞이도록 순서를 섞어서 돌려줌."""
    import hashlib
    entries = []
    for kind in ("challengerleagues", "grandmasterleagues", "masterleagues"):
        res = riot_platform(f"/lol/league/v4/{kind}/by-queue/RANKED_SOLO_5x5")
        if res:
            entries += res.get("entries", [])
    for tier, pages in (("DIAMOND", DIAMOND_PAGES), ("EMERALD", EMERALD_PAGES)):
        for division in ("I", "II", "III", "IV"):
            for page in range(1, pages + 1):
                res = riot_platform(f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}?page={page}")
                if res:
                    entries += res

    puuids = list(dict.fromkeys(e["puuid"] for e in entries if e.get("puuid")))
    # 항상 같은 순서로 섞기 (매 실행마다 이어서 돌 수 있게, 티어는 골고루 섞이게)
    puuids.sort(key=lambda p: hashlib.md5(p.encode()).hexdigest())
    log(f"수집 대상 유저 {len(puuids)}명 (에메랄드+)")
    return puuids


def _inc(d, key, win):
    rec = d.setdefault(str(key), [0, 0])  # [판수, 승리]
    rec[0] += 1
    if win:
        rec[1] += 1


def add_match_to_bucket(bucket, info, game):
    bucket["games"] += 1
    detail = bucket.setdefault("detail", {})
    lane = {}  # (라인, 팀) → 참가자

    for p in info["participants"]:
        role = ROLE_MAP.get(p.get("teamPosition") or "")
        if not role:
            continue
        cid = str(p["championId"])
        win = bool(p.get("win"))
        _inc(bucket["picks"].setdefault(role, {}), cid, win)
        lane[(role, p.get("teamId"))] = p

        d = detail.setdefault(role, {}).setdefault(cid, {})
        inv = {str(p.get(f"item{i}", 0)) for i in range(6)}
        for iid in inv:
            if iid in game["items"]:
                _inc(d.setdefault("items", {}), iid, win)
            elif iid in game["boots"]:
                _inc(d.setdefault("boots", {}), iid, win)
        try:
            styles = p["perks"]["styles"]
            _inc(d.setdefault("keys", {}), styles[0]["selections"][0]["perk"], win)
            _inc(d.setdefault("sec", {}), styles[1]["style"], win)
            sp = p["perks"].get("statPerks", {})
            page = "{}:{}|{}:{}|{}".format(
                styles[0]["style"], ",".join(str(x["perk"]) for x in styles[0]["selections"]),
                styles[1]["style"], ",".join(str(x["perk"]) for x in styles[1]["selections"]),
                ",".join(str(sp.get(k, 0)) for k in ("offense", "flex", "defense")))
            _inc(d.setdefault("pages", {}), page, win)   # 룬 페이지 전체 (꼬마 룬 + 파편)
        except (KeyError, IndexError, TypeError):
            pass
        cores = (info.get("_cores") or {}).get(p.get("participantId"))
        if cores:
            for k, iid in enumerate(cores[:3]):
                _inc(d.setdefault(f"core{k + 1}", {}), iid, win)
        pair = "_".join(sorted([str(p.get("summoner1Id")), str(p.get("summoner2Id"))]))
        _inc(d.setdefault("spells", {}), pair, win)

    # 같은 라인 맞상대 기록
    for role in LINES:
        a, b = lane.get((role, 100)), lane.get((role, 200))
        if not a or not b:
            continue
        for me, opp in ((a, b), (b, a)):
            d = detail[role][str(me["championId"])]
            _inc(d.setdefault("vs", {}), opp["championId"], bool(me.get("win")))

    banned = set()
    for team in info.get("teams", []):
        for b in team.get("bans", []):
            if b.get("championId", -1) > 0:
                banned.add(str(b["championId"]))
    for cid in banned:
        bucket["bans"][cid] = bucket["bans"].get(cid, 0) + 1


def core_orders(timeline, game):
    """타임라인에서 선수별로 완성 아이템을 산 순서를 뽑음 (되돌리기 반영)"""
    bought = {}
    try:
        frames = timeline["info"]["frames"]
    except (KeyError, TypeError):
        return {}
    for fr in frames:
        for ev in fr.get("events", []):
            pid = ev.get("participantId")
            if ev.get("type") == "ITEM_PURCHASED":
                bought.setdefault(pid, []).append(str(ev.get("itemId")))
            elif ev.get("type") == "ITEM_UNDO":
                lst = bought.get(pid, [])
                before = str(ev.get("beforeId"))
                for i in range(len(lst) - 1, -1, -1):
                    if lst[i] == before:
                        del lst[i]
                        break
    result = {}
    for pid, lst in bought.items():
        order = []
        for iid in lst:
            if iid in game["items"] and iid not in order:
                order.append(iid)
        if order:
            result[pid] = order
    return result


def is_valid_match(info):
    if not info or info.get("queueId") != QUEUE_ID:
        return False
    if info.get("gameDuration", 0) < 300:  # 다시하기 / 5분 이하 제외
        return False
    if any(p.get("gameEndedInEarlySurrender") for p in info.get("participants", [])):
        return False
    return True


def collect(state, game):
    puuids = get_players()
    if not puuids:
        log("유저 목록을 못 받아왔어요. 이번 실행은 집계만 다시 합니다.")
        return 0

    seen = dict.fromkeys(state["seen"])  # 이미 본 경기 ID (순서 유지)
    start_ts = int(max(time.time() - LOOKBACK_DAYS * 86400, state.get("oldCutoff", 0)))
    cursor = state.get("cursor", 0) % len(puuids)
    added = 0
    checked_players = 0

    while time_left() > 60 and checked_players < len(puuids):
        puuid = puuids[cursor]
        cursor = (cursor + 1) % len(puuids)
        checked_players += 1

        q = urllib.parse.urlencode({"queue": QUEUE_ID, "startTime": start_ts, "count": MATCHES_PER_PLAYER})
        ids = riot_region(f"/lol/match/v5/matches/by-puuid/{puuid}/ids?{q}") or []

        for mid in ids:
            if time_left() <= 60:
                break
            if mid in seen:
                continue
            seen[mid] = None
            match = riot_region(f"/lol/match/v5/matches/{mid}")
            info = (match or {}).get("info")
            if not is_valid_match(info):
                continue

            patch = patch_of(info.get("gameVersion"))
            if not patch:
                continue
            cur = state["current"]

            if cur is None:
                state["current"] = cur = empty_bucket(patch)
            elif patch_key(patch) > patch_key(cur["patch"]):
                # 새 패치 시작 → 지금까지 모은 걸 '이전 패치'로 넘기고 새로 집계
                log(f"새 패치 감지: {cur['patch']} → {patch}")
                state["previous"] = cur
                state["current"] = cur = empty_bucket(patch)
                state["oldCutoff"] = 0

            if patch == cur["patch"]:
                if FETCH_TIMELINE and time_left() > 60:
                    tl = riot_region(f"/lol/match/v5/matches/{mid}/timeline")
                    info["_cores"] = core_orders(tl, game) if tl else {}
                add_match_to_bucket(cur, info, game)
                added += 1
            else:
                # 지난 패치 경기: 이전 패치 집계에 보태고, 다음부터 그 시점 이전은 안 봄
                prev = state.get("previous")
                if prev and prev["patch"] == patch:
                    add_match_to_bucket(prev, info, game)
                end_ts = int(info.get("gameEndTimestamp", info.get("gameCreation", 0)) / 1000)
                state["oldCutoff"] = max(state.get("oldCutoff", 0), end_ts)
                start_ts = max(start_ts, state["oldCutoff"])

        if checked_players % 25 == 0:
            log(f"유저 {checked_players}명 확인, 새 경기 {added}판 (이번 패치 누적 {state['current']['games'] if state['current'] else 0}판)")

    state["cursor"] = cursor
    state["seen"] = list(seen)[-80000:]  # 파일이 너무 커지지 않게 최근 8만 개만 기억
    return added


# ───────────────────────── 계산 ─────────────────────────
def fmt(x):
    return f"{x * 100:.1f}%"


def tier_for(rank):
    if rank == 1:
        return "OP"
    if rank <= 4:
        return "1티어"
    if rank <= 7:
        return "2티어"
    return "3티어"


RANK_MIN_GAMES = [100, 50, 20]   # 순위에 넣을 최소 판수 (데이터가 적을 땐 단계적으로 완화)
RANK_SMOOTHING = 200             # 판수가 적은 챔피언의 승률을 50% 쪽으로 당기는 강도


def rank_score(r):
    """OP.GG 방식처럼 승률 + 픽률 + 밴률을 함께 보는 점수.
    - 승률: 50%에서 1%p 높을 때마다 +2점 (판수가 적으면 50% 쪽으로 보정)
    - 픽률: 많이 쓰일수록 가산 (제곱근이라 너무 크게 튀지 않음)
    - 밴률: 조금 가산"""
    return ((r["adj"] - 0.5) * 200
            + 1.2 * (r["pick"] * 100) ** 0.5
            + 0.6 * (r["ban"] * 100) ** 0.5)


def build_ranking(bucket, names):
    total = bucket["games"]
    result = {}
    for line in LINES:
        all_rows = []
        for cid, (games, wins) in bucket["picks"].get(line, {}).items():
            pick = games / total
            if pick < MIN_PICK_SHARE:
                continue
            name, ddid = names.get(cid, (cid, None))
            all_rows.append({
                "cid": cid, "name": name, "id": ddid, "games": games,
                "wr": wins / games,
                "adj": (wins + 0.5 * RANK_SMOOTHING) / (games + RANK_SMOOTHING),
                "pick": pick, "ban": bucket["bans"].get(cid, 0) / total,
            })
        rows = []
        for min_games in RANK_MIN_GAMES:
            rows = [r for r in all_rows if r["games"] >= min_games]
            if len(rows) >= 10:
                break
        rows.sort(key=rank_score, reverse=True)
        result[line] = [
            {
                "rank": i + 1,
                "name": r["name"],
                "id": r["id"],
                "tier": tier_for(i + 1),
                "winRate": fmt(r["wr"]),
                "pickRate": fmt(r["pick"]),
                "banRate": fmt(r["ban"]),
                "games": r["games"],
            }
            for i, r in enumerate(rows[:10])
        ]
    return result


def write_output(bucket, names):
    ranking = build_ranking(bucket, names)
    if not any(ranking.values()):
        log("순위를 만들 만큼 데이터가 없어서 ranking_data.js 는 그대로 둡니다.")
        return False
    meta = {
        "patch": display_patch(bucket["patch"]),
        "games": bucket["games"],
        "tierLabel": TIER_LABEL,
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d"),
        "source": "Riot API 직접 집계",
    }
    content = (
        "// 자동 생성 파일입니다. 직접 수정하지 마세요. (scripts/update_ranking.py)\n"
        f"window.RANKING_META = {json.dumps(meta, ensure_ascii=False)};\n"
        f"window.RANKING_DATABASE = {json.dumps(ranking, ensure_ascii=False, indent=2)};\n"
    )
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"ranking_data.js 저장 완료 (패치 {bucket['patch']}, {bucket['games']}판 기준)")
    return True


# ───────────────────────── 챔피언 상세 데이터 ─────────────────────────
def _wr(rec):
    return rec[1] / rec[0] if rec[0] else 0


def _top(d, n, min_games=1):
    rows = [(k, v) for k, v in (d or {}).items() if v[0] >= min_games]
    rows.sort(key=lambda kv: kv[1][0], reverse=True)
    return rows[:n]


def _wr_str(row):
    return float(row["winRate"].rstrip("%")) / 100


def _smooth(rec, k=20):
    return (rec[1] + 0.5 * k) / (rec[0] + k)


def build_champ_data(bucket, prev, game):
    names, total = game["names"], bucket["games"]
    detail = bucket.get("detail", {})
    champs = {}

    for role in LINES:
        for cid, rec in bucket["picks"].get(role, {}).items():
            if rec[0] < MIN_ROLE_GAMES:
                continue
            name, ddid = names.get(cid, (cid, None))
            d = detail.get(role, {}).get(cid, {})
            # 빌드 정보가 기록된 판수 (예전 버전 스크립트로 모은 경기는 빌드 정보가 없어서 따로 셈)
            games = sum(v[0] for v in (d.get("spells") or {}).values()) or rec[0]

            stat = lambda v: {"pick": fmt(v[0] / games), "winRate": fmt(_wr(v)), "games": v[0]}

            items = [{"id": k, "name": game["items"].get(k, k), **stat(v)} for k, v in _top(d.get("items"), 6)]
            boots_list = [{"id": k, "name": game["boots"].get(k, k), **stat(v)} for k, v in _top(d.get("boots"), 3)]
            keystones = []
            for k, v in _top(d.get("keys"), 3):
                r = game["runes"].get(k, {"name": k, "icon": ""})
                keystones.append({"name": r["name"], "icon": r["icon"], **stat(v)})
            secondaries = []
            for k, v in _top(d.get("sec"), 2):
                st = game["styles"].get(k, {"name": k, "icon": ""})
                secondaries.append({"name": st["name"], "icon": st["icon"], **stat(v)})
            spells_list = []
            for k, v in _top(d.get("spells"), 3):
                sp = [game["spells"].get(i, {"id": "SummonerFlash", "name": "?"}) for i in k.split("_")]
                sp.sort(key=lambda x: 0 if x["id"] == "SummonerFlash" else 1)  # 점멸을 앞으로
                spells_list.append({"list": sp, **stat(v)})

            vs_rows = []
            for ocid, v in (d.get("vs") or {}).items():
                if v[0] < MIN_VS_GAMES:
                    continue
                oname, oid = names.get(ocid, (ocid, None))
                vs_rows.append({"name": oname, "id": oid, "games": v[0], "winRate": fmt(_wr(v)), "_s": _smooth(v)})
            pick4 = lambda r: {k: r[k] for k in ("name", "id", "games", "winRate")}
            vs_rows.sort(key=lambda r: r["_s"])
            # 어려운 상대 / 쉬운 상대 각각 최대 3명
            # 승률 50% 미만 = 어려운 상대, 50% 초과 = 쉬운 상대
            hard = [pick4(r) for r in vs_rows if _wr_str(r) < 0.5][:3]
            easy = [pick4(r) for r in reversed(vs_rows) if _wr_str(r) > 0.5][:3]

            # 1·2·3코어: 순서별로 가장 많이 산 아이템 (앞 코어와 겹치지 않게) + 대안 2개
            cores, used = [], set()
            for k in (1, 2, 3):
                slot = d.get(f"core{k}") or {}
                total_k = sum(v[0] for v in slot.values())
                opts = [(iid, v) for iid, v in sorted(slot.items(), key=lambda kv: kv[1][0], reverse=True)
                        if iid not in used][:3]
                if not opts or total_k == 0:
                    break
                used.add(opts[0][0])
                cores.append([{"id": iid, "name": game["items"].get(iid, iid), "pick": fmt(v[0] / total_k),
                               "winRate": fmt(_wr(v)), "games": v[0]} for iid, v in opts])

            # 가장 많이 쓰는 룬 페이지 (꼬마 룬, 능력치 파편까지)
            rune_pages = []
            for key, v in _top(d.get("pages"), 2):
                try:
                    prim, sub, shards = key.split("|")
                    pst, pperks = prim.split(":")
                    sst, sperks = sub.split(":")
                    rune_pages.append({
                        "primary": {"style": int(pst), "perks": [int(x) for x in pperks.split(",")]},
                        "sub": {"style": int(sst), "perks": [int(x) for x in sperks.split(",")]},
                        "shards": [int(x) for x in shards.split(",")],
                        **stat(v),
                    })
                except ValueError:
                    continue
            # 룬 하나하나의 픽률·승률 (OP.GG 룬 화면처럼): 모든 룬 페이지 기록을 룬별로 합산
            #   prim = 주 룬으로 고른 판, sub = 보조 룬으로 고른 판, shard = "줄번호:파편id"
            rune_stats = {"total": 0, "prim": {}, "sub": {}, "shard": {}}
            def _acc(dst, key, v):
                rec2 = dst.setdefault(str(key), [0, 0])
                rec2[0] += v[0]
                rec2[1] += v[1]
            for key, v in (d.get("pages") or {}).items():
                try:
                    prim, sub, shards = key.split("|")
                    pperks = prim.split(":")[1].split(",")
                    sperks = sub.split(":")[1].split(",")
                    shard_ids = shards.split(",")
                except (ValueError, IndexError):
                    continue
                rune_stats["total"] += v[0]
                for x in pperks:
                    _acc(rune_stats["prim"], x, v)
                for x in sperks:
                    _acc(rune_stats["sub"], x, v)
                for i, x in enumerate(shard_ids):
                    _acc(rune_stats["shard"], f"{i}:{x}", v)
            vs_all = [pick4(r) for r in sorted(vs_rows, key=lambda r: r["games"], reverse=True)[:30]]

            entry = champs.setdefault(name, {"id": ddid, "roles": {}})
            entry["roles"][role] = {
                "games": rec[0], "winRate": fmt(_wr(rec)), "pickRate": fmt(rec[0] / total),
                "banRate": fmt(bucket["bans"].get(cid, 0) / total),
                "buildGames": games,
                "items": items,
                "boots": boots_list[0] if boots_list else None, "bootsList": boots_list,
                "keystone": keystones[0] if keystones else None, "keystones": keystones,
                "secondary": secondaries[0] if secondaries else None, "secondaries": secondaries,
                "spells": spells_list[0] if spells_list else None, "spellsList": spells_list,
                "hard": hard, "easy": easy, "vs": vs_all,
                "cores": cores, "runePages": rune_pages, "runeStats": rune_stats,
            }

    # 인기 구도: 같은 라인에서 가장 많이 만난 조합
    popular = []
    for role in LINES:
        for cid, d in detail.get(role, {}).items():
            for ocid, v in (d.get("vs") or {}).items():
                if int(cid) < int(ocid) and v[0] >= MIN_VS_GAMES:
                    a, aid = names.get(cid, (cid, None))
                    b, bid = names.get(ocid, (ocid, None))
                    popular.append({"a": a, "aId": aid, "b": b, "bId": bid, "role": role,
                                    "games": v[0], "aWinRate": fmt(_wr(v))})
    popular.sort(key=lambda r: r["games"], reverse=True)

    # 급상승 / 급하락: 지난 패치 대비 승률 변화
    trend = {"up": [], "down": [], "from": display_patch(prev["patch"]) if prev else None}
    if prev:
        rows = []
        for role in LINES:
            for cid, rec in bucket["picks"].get(role, {}).items():
                old = prev["picks"].get(role, {}).get(cid)
                if rec[0] < MIN_TREND_GAMES or not old or old[0] < MIN_TREND_GAMES:
                    continue
                name, ddid = names.get(cid, (cid, None))
                delta = _wr(rec) - _wr(old)
                rows.append({"name": name, "id": ddid, "role": role, "winRate": fmt(_wr(rec)),
                             "delta": f"{delta * 100:+.1f}%p", "_d": delta})
        rows.sort(key=lambda r: r["_d"], reverse=True)
        clean = lambda r: {k: v for k, v in r.items() if k != "_d"}
        trend["up"] = [clean(r) for r in rows[:10] if r["_d"] > 0]
        trend["down"] = [clean(r) for r in reversed(rows[-10:]) if r["_d"] < 0]

    # 라인별 TOP 5
    popular_top = []
    for role in LINES:
        popular_top += [r for r in popular if r["role"] == role][:5]

    rune_tree = {
        "styles": game.get("tree", []),
        "shards": STAT_SHARDS,
        "shardRows": SHARD_ROWS,
    }
    return {"champs": champs, "popular": popular_top, "trend": trend, "runeTree": rune_tree}


def write_champ_data(bucket, prev, game):
    data = build_champ_data(bucket, prev, game)
    data["meta"] = {"patch": display_patch(bucket["patch"]), "games": bucket["games"], "tierLabel": TIER_LABEL,
                    "updatedAt": datetime.now(KST).strftime("%Y-%m-%d")}
    content = (
        "// 자동 생성 파일입니다. 직접 수정하지 마세요. (scripts/update_ranking.py)\n"
        f"window.CHAMP_DATA = {json.dumps(data, ensure_ascii=False, separators=(',', ':'))};\n"
    )
    with open(CHAMP_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"champ_data.js 저장 완료 (챔피언 {len(data['champs'])}명, 인기 구도 {len(data['popular'])}개)")


# ───────────────────────── 실행 ─────────────────────────
def main():
    if not API_KEY:
        sys.exit("RIOT_API_KEY 가 없어요. 저장소 Settings → Secrets and variables → Actions 에 등록하세요.")

    game = load_game_data()
    names = game["names"]
    state = load_state()

    added = collect(state, game)
    save_state(state)
    log(f"이번 실행에서 새로 모은 경기: {added}판")

    cur, prev = state.get("current"), state.get("previous")
    if cur and cur["games"] >= MIN_GAMES_TO_PUBLISH:
        target = cur
    elif prev and prev["games"] >= MIN_GAMES_TO_PUBLISH:
        log(f"이번 패치 표본({cur['games'] if cur else 0}판)이 아직 적어서 이전 패치 결과를 유지합니다.")
        target = prev
    else:
        target = cur or prev

    if target:
        write_output(target, names)
        write_champ_data(target, prev if target is cur else None, game)
    else:
        log("집계된 경기가 아직 없어요.")


if __name__ == "__main__":
    main()
