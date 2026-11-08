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
INCLUDE_DIAMOND1_PAGES = int(os.environ.get("DIAMOND1_PAGES", "2"))
TIER_LABEL = "다이아 I+" if INCLUDE_DIAMOND1_PAGES > 0 else "마스터+"

MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "50"))   # 한 번 실행 시 최대 수집 시간
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
STATE_PATH = os.path.join(ROOT, "data", "ranking_state.json")
OUTPUT_PATH = os.path.join(ROOT, "ranking_data.js")
CHAMP_OUTPUT_PATH = os.path.join(ROOT, "champ_data.js")

MIN_ROLE_GAMES = 30        # 챔피언 상세(빌드/카운터)를 보여줄 최소 판수
MIN_VS_GAMES = 8           # 상대별 승률을 보여줄 최소 판수
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
        if "Boots" in tags and "1001" in it.get("from", []):
            boots[iid] = it["name"]                      # 2단계 신발
        elif not it.get("into") and it["gold"].get("total", 0) >= 2000 and "Consumable" not in tags:
            items[iid] = it["name"]                      # 완성 아이템

    runes = {}
    styles = {}
    for style in http_get_json(f"{base}/runesReforged.json", riot=False):
        styles[str(style["id"])] = {"name": style["name"], "icon": style["icon"]}
        for rune in style["slots"][0]["runes"]:          # 핵심 룬만
            runes[str(rune["id"])] = {"name": rune["name"], "icon": rune["icon"]}

    spells = {}
    for sp in http_get_json(f"{base}/summoner.json", riot=False)["data"].values():
        spells[sp["key"]] = {"id": sp["id"], "name": sp["name"]}

    return {"names": names, "items": items, "boots": boots, "runes": runes,
            "styles": styles, "spells": spells}


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


def patch_key(p):
    try:
        return tuple(int(x) for x in p.split("."))
    except Exception:
        return (0, 0)


# ───────────────────────── 수집 ─────────────────────────
def get_players():
    """마스터+ (와 다이아 I 일부) 유저 puuid 목록. LP 높은 순."""
    entries = []
    for kind in ("challengerleagues", "grandmasterleagues", "masterleagues"):
        res = riot_platform(f"/lol/league/v4/{kind}/by-queue/RANKED_SOLO_5x5")
        if res:
            entries += res.get("entries", [])
    for page in range(1, INCLUDE_DIAMOND1_PAGES + 1):
        res = riot_platform(f"/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/I?page={page}")
        if res:
            entries += res
    entries.sort(key=lambda e: e.get("leaguePoints", 0), reverse=True)

    puuids = []
    for e in entries:
        if e.get("puuid"):
            puuids.append(e["puuid"])
    log(f"수집 대상 유저 {len(puuids)}명")
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
        except (KeyError, IndexError, TypeError):
            pass
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


def build_ranking(bucket, names):
    total = bucket["games"]
    result = {}
    for line in LINES:
        rows = []
        for cid, (games, wins) in bucket["picks"].get(line, {}).items():
            pick = games / total
            if games < MIN_CHAMP_GAMES or pick < MIN_PICK_SHARE:
                continue
            raw_wr = wins / games
            adj_wr = (wins + 0.5 * SMOOTHING_GAMES) / (games + SMOOTHING_GAMES)
            ban = bucket["bans"].get(cid, 0) / total
            name, ddid = names.get(cid, (cid, None))
            rows.append({"cid": cid, "name": name, "id": ddid, "games": games,
                         "wr": raw_wr, "adj": adj_wr, "pick": pick, "ban": ban})
        rows.sort(key=lambda r: r["adj"], reverse=True)
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
        "patch": bucket["patch"],
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

            items = [{"id": k, "name": game["items"].get(k, k), "pick": fmt(v[0] / games), "winRate": fmt(_wr(v))}
                     for k, v in _top(d.get("items"), 3)]
            boots = [{"id": k, "name": game["boots"].get(k, k), "pick": fmt(v[0] / games), "winRate": fmt(_wr(v))}
                     for k, v in _top(d.get("boots"), 1)]
            keystone = None
            for k, v in _top(d.get("keys"), 1):
                r = game["runes"].get(k, {"name": k, "icon": ""})
                keystone = {"name": r["name"], "icon": r["icon"], "pick": fmt(v[0] / games), "winRate": fmt(_wr(v))}
            secondary = None
            for k, v in _top(d.get("sec"), 1):
                st = game["styles"].get(k, {"name": k, "icon": ""})
                secondary = {"name": st["name"], "icon": st["icon"]}
            spells = None
            for k, v in _top(d.get("spells"), 1):
                ids = k.split("_")
                sp = [game["spells"].get(i, {"id": "SummonerFlash", "name": "?"}) for i in ids]
                sp.sort(key=lambda x: 0 if x["id"] == "SummonerFlash" else 1)  # 점멸을 앞으로
                spells = {"list": sp, "pick": fmt(v[0] / games), "winRate": fmt(_wr(v))}

            vs_rows = []
            for ocid, v in (d.get("vs") or {}).items():
                if v[0] < MIN_VS_GAMES:
                    continue
                oname, oid = names.get(ocid, (ocid, None))
                vs_rows.append({"name": oname, "id": oid, "games": v[0], "winRate": fmt(_wr(v)), "_s": _smooth(v)})
            vs_rows.sort(key=lambda r: r["_s"])
            hard = [{k: r[k] for k in ("name", "id", "games", "winRate")} for r in vs_rows[:3]]
            easy = [{k: r[k] for k in ("name", "id", "games", "winRate")} for r in reversed(vs_rows[-3:])] if len(vs_rows) > 3 else []

            entry = champs.setdefault(name, {"id": ddid, "roles": {}})
            entry["roles"][role] = {
                "games": rec[0], "winRate": fmt(_wr(rec)), "pickRate": fmt(rec[0] / total),
                "banRate": fmt(bucket["bans"].get(cid, 0) / total),
                "items": items, "boots": boots[0] if boots else None,
                "keystone": keystone, "secondary": secondary, "spells": spells,
                "hard": hard, "easy": easy,
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
    trend = {"up": [], "down": [], "from": prev["patch"] if prev else None}
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

    return {"champs": champs, "popular": popular[:20], "trend": trend}


def write_champ_data(bucket, prev, game):
    data = build_champ_data(bucket, prev, game)
    data["meta"] = {"patch": bucket["patch"], "games": bucket["games"], "tierLabel": TIER_LABEL,
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
