"""战绩接口：对局列表、详情、赛季数据、生涯统计。"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from .client import APIClient

logger = logging.getLogger("astrbot")

MATCH_LIST_PATH = "/go/agame/career/record/list"
MATCH_LIST_QS = "source_game_zone=agame&game_zone=agame"
MATCH_DETAIL_PATHS = [
    "/go/agame/career/record/scoreboard",
    "/go/agame/career/record/player/roundoverview",
    "/go/agame/career/record/player/rounds",
]


class MatchAPI:
    """战绩/对局 API。"""

    def __init__(self, api_client: APIClient):
        self.api = api_client

    async def fetch_match_list(self, user: Dict, scene_token: str, game_role_id: str,
                               count: int, max_pages: int = 10) -> List[Dict]:
        """拉取对局列表（过滤竞技/非排位模式）。"""
        ALLOWED_GAME_ENUMS = {"competitive", "unrated"}
        ALLOWED_MODE_KW = ("竞技", "匹配", "一般", "非排位")

        path = f"{MATCH_LIST_PATH}?{MATCH_LIST_QS}"
        ts = int(time.time())
        records = []
        baton = ""

        for page in range(max_pages):
            body = {
                "game_role_id": game_role_id, "scene": scene_token,
                "page_no": page + 1, "page_size": 10, "_t": ts,
            }
            if baton:
                body["baton"] = baton
            page_data, err = await self.api.post(path, user, body, cookie_variant="real_token")
            if err or not page_data:
                break
            page_records = page_data.get("battle_list") or page_data.get("list") or []
            if not page_records:
                break
            for rec in page_records:
                ge = str(rec.get("game_enum", "")).lower()
                gm = str(rec.get("game_mode", ""))
                if ge in ALLOWED_GAME_ENUMS or any(kw in gm for kw in ALLOWED_MODE_KW):
                    records.append(rec)
            baton = page_data.get("next_baton", "")
            if len(records) >= count or not baton:
                break
        return records

    async def fetch_career_stats(self, user: Dict, scene_token: str, sample_n: int = 20) -> Optional[Dict]:
        """从近期对局计算生涯平均 KDA / ACS / 爆头率。"""
        if not scene_token:
            return None
        grid = user.get("game_role_id", "")
        path = f"{MATCH_LIST_PATH}?{MATCH_LIST_QS}"
        ts = int(time.time())
        records = []
        baton = ""
        for page in range(3):
            body = {"game_role_id": grid, "scene": scene_token,
                    "page_no": page + 1, "page_size": 10, "_t": ts}
            if baton:
                body["baton"] = baton
            page_data, err = await self.api.post(path, user, body, cookie_variant="real_token")
            if err or not page_data:
                break
            page_records = page_data.get("battle_list") or page_data.get("list") or []
            if not page_records:
                break
            for rec in page_records:
                ge = str(rec.get("game_enum", "")).lower()
                gm = str(rec.get("game_mode", ""))
                if ge in ("competitive", "unrated") or any(
                    kw in gm for kw in ("竞技", "匹配", "一般", "非排位")
                ):
                    records.append(rec)
            baton = page_data.get("next_baton", "")
            if len(records) >= sample_n or not baton:
                break

        if not records:
            return None

        records = records[:sample_n]
        total_k = total_d = total_a = total_score = total_hs = 0
        n = 0
        for rec in records:
            kda_str = str(rec.get("kda", ""))
            parts = kda_str.split("/")
            if len(parts) < 3:
                continue
            try:
                k = int(parts[0]); d = int(parts[1]); a = int(parts[2])
            except ValueError:
                continue
            score = int(rec.get("score_avg") or rec.get("score") or 0)
            hs_rate_str = str(rec.get("head_shots_rate") or rec.get("headshot_rate") or "")
            hs_pct = 0.0
            if hs_rate_str and "%" in hs_rate_str:
                try:
                    hs_pct = float(hs_rate_str.replace("%", ""))
                except ValueError:
                    pass
            total_k += k; total_d += d; total_a += a
            total_score += score; total_hs += hs_pct
            n += 1

        if n == 0:
            return None

        avg_kda = round((total_k + total_a) / max(total_d, 1), 2)
        return {
            "matches": n, "kda": f"{round(total_k/n,1)}/{round(total_d/n,1)}/{round(total_a/n,1)}",
            "kda_ratio": str(avg_kda), "avg_acs": str(round(total_score / n, 1)),
            "hs_pct": f"{round(total_hs / n, 1)}%",
        }

    async def fetch_battle_detail(self, user, battle_id, match_id, scene):
        """获取单场对战详情。"""
        if not battle_id or not match_id or not scene:
            return None
        ts = int(time.time())
        grid = user.get("game_role_id", "")
        result = {}

        # scoreboard
        for path, qs in [(MATCH_DETAIL_PATHS[0], MATCH_LIST_QS), (MATCH_DETAIL_PATHS[0], "")]:
            for body in [
                {"battle_id": battle_id, "match_id": match_id, "scene": scene, "_t": ts},
                {"battle_id": battle_id, "match_id": match_id, "scene": scene,
                 "game_role_id": grid, "_t": ts},
            ]:
                p = f"{path}?{qs}" if qs else path
                data, err = await self.api.post(p, user, body, cookie_variant="real_token")
                if err is None and data is not None:
                    result["scoreboard"] = data
                    break
            if result.get("scoreboard"):
                break

        # player_rounds
        for extra_path, result_key in [(MATCH_DETAIL_PATHS[2], "player_rounds")]:
            eb = {"battle_id": battle_id, "match_id": match_id, "scene": scene, "_t": ts}
            p = f"{extra_path}?{MATCH_LIST_QS}"
            data, err = await self.api.post(p, user, eb, cookie_variant="real_token")
            if data and not err:
                result[result_key] = data

        # roundoverview
        for body in [
            {"battle_id": battle_id, "match_id": match_id, "scene": scene, "_t": ts},
            {"battle_id": battle_id, "match_id": match_id, "scene": scene,
             "game_role_id": grid, "_t": ts},
        ]:
            p = f"{MATCH_DETAIL_PATHS[1]}?{MATCH_LIST_QS}"
            data, err = await self.api.post(p, user, body, cookie_variant="real_token")
            if err is None and data is not None:
                result["roundoverview"] = data
                break
        return result if result else None

    async def probe_season_endpoints(self, user: Dict, scene_token: str, grid: str) -> Optional[Dict]:
        """探测赛季数据端点。"""
        ts = int(time.time())
        candidates = [
            ("/go/mine/card/val_card", "POST",
             {"game_role_id": grid, "scene": scene_token, "_t": ts}),
            ("/go/agame/career/summary", "POST",
             {"game_role_id": grid, "scene": scene_token, "_t": ts}),
            ("/go/agame/career/index", "POST",
             {"game_role_id": grid, "scene": scene_token, "_t": ts}),
        ]
        for path, method, body in candidates:
            try:
                if method == "POST":
                    data, err = await self.api.post(path, user, body, cookie_variant="real_token")
                else:
                    qs = "&".join(f"{k}={v}" for k, v in body.items())
                    data, err = await self.api.get(path, user, query=qs, cookie_variant="real_token")
                if err or data is None:
                    continue
                parsed = _parse_season_data(data)
                if parsed:
                    logger.info(f"[ValAPI] 赛季端点命中: {path} → {json.dumps(parsed, ensure_ascii=False)}")
                    return parsed
            except Exception:
                continue
        return None


def _parse_season_data(data: Dict) -> Optional[Dict]:
    """从探测到的 JSON 中提取赛季数据。"""
    if not isinstance(data, dict):
        return None

    def _try_flat(root):
        if not isinstance(root, dict):
            return None
        keys = set(root.keys())
        season_keys = {"kda", "kda_score", "win_rate", "score_avg", "acs",
                       "headshot_rate", "head_shots_rate", "kast",
                       "round_win_rate", "total_time", "season_match"}
        if not keys & season_keys:
            return None
        def _get(*fns):
            for fn in fns:
                v = root.get(fn)
                if v is not None and v != "" and v != 0:
                    return str(v)
            return ""
        return {
            "kda": _get("kda", "kda_score", "season_kda"),
            "win_rate": _get("win_rate", "season_win_rate"),
            "avg_acs": _get("score_avg", "acs", "season_acs"),
            "hs_pct": _get("headshot_rate", "head_shots_rate", "season_hs"),
            "kast": _get("kast", "season_kast"),
            "round_win_rate": _get("round_win_rate", "round_wr"),
            "total_time": _get("total_time", "play_time", "game_time"),
            "matches": _get("season_match", "match_count", "total_match"),
        }

    def _try_val_card(card):
        if not isinstance(card, dict):
            return None
        result = {}
        pairs = {}
        for section_key in ("left_data", "right_data"):
            section = card.get(section_key, {}) if isinstance(card.get(section_key), dict) else {}
            for item in section.get("list", []) or []:
                if isinstance(item, dict) and "title" in item and "content" in item:
                    pairs[item["title"]] = item["content"]
        md = card.get("middle_data", {}) or {}
        if isinstance(md, dict) and "title" in md and "content" in md:
            pairs[md["title"]] = md["content"]
        rwr = card.get("round_win_rate", {}) or {}
        if isinstance(rwr, dict) and "title" in rwr and "content" in rwr:
            pairs[rwr["title"]] = rwr["content"]
        left = card.get("left_data", {}) or {}
        if isinstance(left, dict) and left.get("title"):
            pairs["段位信息"] = left["title"]
        if not pairs:
            return None
        TITLE_MAP = {
            "游戏等级": "game_level", "游戏时长": "total_time",
            "ACS": "avg_acs", "KAST": "kast", "赛季KDA": "kda",
            "赛季胜率": "win_rate", "赛季精准击败": "hs_pct",
            "回合胜率": "round_win_rate", "段位信息": "rank_detail",
        }
        for title, content in pairs.items():
            for kw, field in TITLE_MAP.items():
                if kw in title:
                    result[field] = content
                    break
        return result if result else None

    card = data.get("card")
    if isinstance(card, dict):
        parsed = _try_val_card(card)
        if parsed:
            return parsed
    for root in [data] + [
        data.get(k) for k in ("card", "layer_small", "layer_big",
                               "role_info", "season_info", "career", "summary",
                               "card_info", "data")
        if isinstance(data.get(k), dict)
    ]:
        parsed = _try_flat(root)
        if parsed:
            return parsed
    return None


def normalize_match(raw: Dict) -> Dict[str, Any]:
    """将原始对局记录规范化为统一格式。"""
    m = {}
    m["match_id"] = str(raw.get("match_id") or raw.get("battle_id") or "")
    m["match_time"] = str(raw.get("ts") or "")
    m["queue_type"] = str(raw.get("game_mode") or raw.get("game_enum") or "")
    mp = raw.get("used_map", {}) or {}
    m["map_name"] = str(mp.get("name") or raw.get("map_name") or "")
    content = str(raw.get("content") or "")
    if not m["map_name"] and "|" in content:
        m["map_name"] = content.split("|")[-1].strip()
    kda_str = str(raw.get("kda") or "")
    parts = kda_str.split("/")
    m["kills"] = int(parts[0]) if len(parts) >= 1 else 0
    m["deaths"] = int(parts[1]) if len(parts) >= 2 else 0
    m["assists"] = int(parts[2]) if len(parts) >= 3 else 0
    m["score"] = int(raw.get("score_avg") or raw.get("score") or 0)
    m["headshots"] = int(raw.get("headshots") or 0)
    m["first_bloods"] = int(raw.get("first_bloods") or 0)
    m["first_deaths"] = int(raw.get("first_deaths") or 0)
    m["rounds_played"] = int(raw.get("round_won") or 0) + int(raw.get("round_fail") or 0)
    m["agent_name"] = str(raw.get("hero_name") or raw.get("agent_name") or "")
    m["rank"] = str(raw.get("rank") or "")
    m["team"] = str(raw.get("team") or "")
    m["team_score"] = int(raw.get("round_won") or 0)
    m["enemy_score"] = int(raw.get("round_fail") or 0)
    result = str(raw.get("result_title") or raw.get("result") or "").lower()
    if result in ("win", "victory", "胜利", "胜", "true", "1", "获胜"):
        m["is_win"] = True
    elif result in ("lose", "defeat", "失败", "败", "负", "false", "0"):
        m["is_win"] = False
    else:
        m["is_win"] = None
    m["kda"] = round((m["kills"] + m["assists"]) / max(m["deaths"], 1), 2)
    detail = raw.get("_detail", {})
    sb = detail.get("scoreboard", {}) or {}
    ro = detail.get("roundoverview", {}) or {}
    for player in sb.get("scoreboard", []) or []:
        if player.get("is_me"):
            m["headshots"] = int(player.get("total_head_shots") or 0)
            m["headshot_rate"] = str(player.get("head_shots_rate") or "")
            m["body_shots"] = int(player.get("total_body_shots") or 0)
            m["leg_shots"] = int(player.get("total_leg_shots") or 0)
            m["damage"] = int(player.get("damage") or 0)
            m["tier_name"] = str(player.get("tier_name") or "")
            m["competitive_tier"] = int(player.get("competitive_tier") or 0)
            m["is_friend"] = bool(player.get("is_friend"))
            break
    opponents = ro.get("opponents", []) or []
    if opponents:
        total_dmg_to = sum(int(op.get("damage", 0)) for op in opponents if isinstance(op, dict))
        m["damage_dealt_to_opponents"] = total_dmg_to
    return m


def aggregate_matches(matches: List[Dict]) -> Dict[str, Any]:
    """聚合多场对局数据为统计汇总。"""
    n = len(matches)
    if n == 0:
        return {}
    wins = sum(1 for m in matches if m.get("is_win"))
    total_k = sum(m.get("kills", 0) for m in matches)
    total_d = sum(m.get("deaths", 0) for m in matches)
    total_a = sum(m.get("assists", 0) for m in matches)
    total_score = sum(m.get("score", 0) for m in matches)
    agents = {}
    maps = {}
    friends = {}
    for m in matches:
        an = m.get("agent_name", "?")
        if an not in agents:
            agents[an] = {"cnt": 0, "wins": 0, "total_k": 0, "total_d": 0, "total_a": 0, "total_score": 0}
        agents[an]["cnt"] += 1
        if m.get("is_win"):
            agents[an]["wins"] += 1
        agents[an]["total_k"] += m.get("kills", 0)
        agents[an]["total_d"] += m.get("deaths", 0)
        agents[an]["total_a"] += m.get("assists", 0)
        agents[an]["total_score"] += m.get("score", 0)
        mn = m.get("map_name", "?")
        if mn not in maps:
            maps[mn] = {"cnt": 0, "wins": 0}
        maps[mn]["cnt"] += 1
        if m.get("is_win"):
            maps[mn]["wins"] += 1
        detail = m.get("_detail", {}) or {}
        sb = detail.get("scoreboard", {}) or {}
        for player in sb.get("scoreboard", []) or []:
            if player.get("is_friend") and not player.get("is_me"):
                fn = player.get("user_name", "?")
                if fn not in friends:
                    friends[fn] = {"games": 0, "total_k": 0, "total_d": 0, "total_a": 0,
                                   "total_score": 0, "total_hs": 0, "agents": {}, "tier": ""}
                friends[fn]["games"] += 1
                friends[fn]["total_k"] += int(player.get("kill", 0))
                friends[fn]["total_d"] += int(player.get("death", 0))
                friends[fn]["total_a"] += int(player.get("assist", 0))
                score_val = player.get("score_avg") or player.get("score", 0)
                friends[fn]["total_score"] += int(score_val) if score_val else 0
                hs_rate_str = str(player.get("head_shots_rate") or "").replace("%", "")
                try:
                    friends[fn]["total_hs"] += float(hs_rate_str)
                except ValueError:
                    pass
                agent_name = (player.get("used_agent", {}) or {}).get("name", "") or player.get("hero_name", "")
                if agent_name:
                    friends[fn]["agents"][agent_name] = friends[fn]["agents"].get(agent_name, 0) + 1
                friends[fn]["tier"] = str(player.get("tier_name") or "")

    avg_kda = round((total_k + total_a) / max(total_d, 1), 2)
    avg_acs = round(total_score / n, 1)
    win_rate = round(wins / n * 100, 1)
    top_agents = []
    for an, s in agents.items():
        cnt = s["cnt"]
        wr = round(s["wins"] / cnt * 100, 1) if cnt else 0
        avg_kda_a = round((s["total_k"] + s["total_a"]) / max(s["total_d"], 1), 2)
        avg_score_a = round(s["total_score"] / cnt, 1) if cnt else 0
        top_agents.append({"name": an, "cnt": cnt, "wr": wr,
                           "avg_kda": avg_kda_a, "avg_score": avg_score_a})
    top_agents.sort(key=lambda x: (x["cnt"], x["wr"]), reverse=True)
    for mn, s in maps.items():
        s["wr"] = round(s["wins"] / s["cnt"] * 100, 1) if s["cnt"] else 0
    friend_ranking = []
    for fn, s in friends.items():
        if s["games"] < 2:
            continue
        g = s["games"]
        avg_hs = round(s["total_hs"] / g, 1) if g else 0
        avg_k = round(s["total_k"] / g, 1)
        avg_d = round(s["total_d"] / g, 1)
        avg_a = round(s["total_a"] / g, 1)
        kda = round((avg_k + avg_a) / max(avg_d, 1), 2)
        acs = round(s["total_score"] / g, 1)
        top_agent = max(s["agents"], key=s["agents"].get) if s["agents"] else "?"
        score = round(kda * 30 + acs * 0.2 + avg_hs * 0.5, 1)
        friend_ranking.append({
            "name": fn, "games": g, "avg_k": avg_k, "avg_d": avg_d, "avg_a": avg_a,
            "kda": kda, "acs": acs, "hs_pct": avg_hs,
            "top_agent": top_agent, "top_agent_cnt": s["agents"].get(top_agent, 0),
            "tier": s["tier"], "score": score,
        })
    friend_ranking.sort(key=lambda x: x["score"], reverse=True)
    return {
        "matches": n, "wins": wins, "losses": n - wins,
        "win_rate": win_rate, "total_k": total_k, "total_d": total_d, "total_a": total_a,
        "avg_kda": avg_kda, "avg_acs": avg_acs,
        "top_agents": top_agents, "map_stats": maps,
        "friend_ranking": friend_ranking,
    }
