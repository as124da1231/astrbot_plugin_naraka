# -*- coding: utf-8 -*-
"""永劫无间战绩查询插件 - 数据排版与聚合层。

核心约定：所有统计（有效对局、常用英雄、场均、队友、逐场明细）都只基于
“当前模式的场”。当前模式由 main 判断后传入（cur_subtype）。
"""

from collections import OrderedDict
from datetime import datetime
from typing import Optional

from .constants import split_subtype


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "??-?? ??:??"


def _comma(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _dl_value(data_list, key):
    if not isinstance(data_list, list):
        return None
    for item in data_list:
        if isinstance(item, dict) and item.get("key") == key:
            return item.get("value")
    return None


# --------------------------- 赛季 id ---------------------------
def extract_current_season_id(seasons_data) -> Optional[str]:
    def pick(d):
        if not isinstance(d, dict):
            return None
        for k in ("code", "seasonId", "season_id", "id", "sid"):
            if d.get(k):
                return str(d[k])
        return None

    if isinstance(seasons_data, list) and seasons_data:
        return pick(seasons_data[0])
    if isinstance(seasons_data, dict):
        for k in ("list", "seasons", "data"):
            v = seasons_data.get(k)
            if isinstance(v, list) and v:
                return pick(v[0])
        return pick(seasons_data)
    return None


# --------------------------- 当前模式判断 ---------------------------
def current_mode_from_recent(match_list):
    """从最近对局里找当前模式：从前往后，第一场属于天选/天人的对局。

    返回 (大类, 人数, subtype)；若全是匹配/不支持模式，返回 (None, None, None)。
    """
    for m in match_list:
        cat, team = split_subtype(m.get("subtype"))
        if cat in ("天选", "天人"):
            return cat, team, m.get("subtype")
    return None, None, None


def filter_by_subtype(match_list, subtype):
    """筛出 subtype 完全相同的场（保持顺序）。subtype 为 None 时返回全部。"""
    if subtype is None:
        return list(match_list)
    return [m for m in match_list if m.get("subtype") == subtype]


# --------------------------- 排除自己 ---------------------------
def _exclude_self(teammates, person_damage, person_shock):
    """从 teammates 里剔除“自己”。

    主：找 damage 与 shock_count 都等于本人 person 数据的那条。
    兜底：没匹配上则去掉第一个（本人通常排首位）。
    person_damage/person_shock 为 None 时直接走兜底。
    """
    if not isinstance(teammates, list) or not teammates:
        return []
    idx = None
    if person_damage is not None and person_shock is not None:
        for i, mate in enumerate(teammates):
            d = _num(_dl_value(mate.get("dataList"), "damage"))
            s = _num(_dl_value(mate.get("dataList"), "shock_count"))
            if d == person_damage and s == person_shock:
                idx = i
                break
    if idx is None:
        idx = 0
    return [m for i, m in enumerate(teammates) if i != idx]


# --------------------------- 自己场均（当前模式的场） ---------------------------
def detail_rows(mode_matches, person_map):
    """给图片版组装逐场明细。返回 [{rank,hero,damage,cure,shock}, ...]，按时间倒序（接口原序）。"""
    rows = []
    for m in mode_matches:
        pr = person_map.get(m.get("battleId"))
        dl = pr.get("dataList") if isinstance(pr, dict) else None
        rows.append(
            {
                "rank": m.get("rank", "?"),
                "hero": (m.get("hero") or {}).get("heroName", "?"),
                "damage": int(_num(m.get("damage"))),
                "cure": int(_num(_dl_value(dl, "cure"))) if dl else 0,
                "shock": int(_num(_dl_value(dl, "shock_count"))) if dl else 0,
            }
        )
    return rows


def self_averages(mode_matches, person_map):
    """mode_matches: 当前模式的对局列表；person_map: {battleId: person_data}。

    振刀要求每场 person 都成功，否则返回 {"error":..., "rate_limited":bool}。
    """
    n = len(mode_matches)
    if n == 0:
        return {"error": "当前模式无对局"}
    shocks = []
    cures = []
    for m in mode_matches:
        bid = m.get("battleId")
        pr = person_map.get(bid)
        if not isinstance(pr, dict) or "error" in pr:
            return {
                "error": "部分单场详情查询失败",
                "rate_limited": bool(isinstance(pr, dict) and pr.get("rate_limited")),
            }
        shocks.append(_num(_dl_value(pr.get("dataList"), "shock_count")))
        cures.append(_num(_dl_value(pr.get("dataList"), "cure")))
    return {
        "avg_damage": sum(_num(m.get("damage")) for m in mode_matches) / n,
        "avg_kill": sum(_num(m.get("kill")) for m in mode_matches) / n,
        "avg_shock": sum(shocks) / n,
        "avg_cure": sum(cures) / n,
        "count": n,
    }


# --------------------------- 常用英雄（当前模式的场） ---------------------------
def top_heroes(mode_matches, top_n=5):
    """统计常用英雄。返回 [(英雄名, 场数, 图标URL), ...]，按场数降序。"""
    counter = OrderedDict()
    icons = {}
    for m in mode_matches:
        h = m.get("hero") or {}
        hero = h.get("heroName")
        if hero:
            counter[hero] = counter.get(hero, 0) + 1
            # 记录该英雄的图标（取第一次出现的）
            if hero not in icons and h.get("heroIcon"):
                icons[hero] = h.get("heroIcon")
    ranked = sorted(counter.items(), key=lambda kv: -kv[1])[:top_n]
    return [(name, cnt, icons.get(name, "")) for name, cnt in ranked]


# --------------------------- 队友统计（当前模式的场） ---------------------------
def teammate_stats(mode_matches, team_map, person_map, team_count):
    """统计队友。mode_matches 为当前模式的场；team_map/person_map: {battleId: data}。

    每场用 person 数据剔除自己，再聚合剩余队友。返回 [{name,plays,avg_damage,avg_shock}]。
    team 详情失败的场跳过。
    """
    if team_count <= 0:
        return []
    agg = {}
    for m in mode_matches:
        bid = m.get("battleId")
        tr = team_map.get(bid)
        if not isinstance(tr, dict) or "error" in tr:
            continue
        mates = tr.get("teammates")
        if not isinstance(mates, list):
            continue
        pr = person_map.get(bid)
        p_dmg = p_shock = None
        if isinstance(pr, dict) and "error" not in pr:
            p_dmg = _num(_dl_value(pr.get("dataList"), "damage"))
            p_shock = _num(_dl_value(pr.get("dataList"), "shock_count"))
        real_mates = _exclude_self(mates, p_dmg, p_shock)
        for mate in real_mates:
            role = mate.get("role") or {}
            name = role.get("roleName")
            if not name:
                continue
            d = _num(_dl_value(mate.get("dataList"), "damage"))
            s = _num(_dl_value(mate.get("dataList"), "shock_count"))
            c = _num(_dl_value(mate.get("dataList"), "cure"))
            slot = agg.setdefault(name, {"dmg": [], "shock": [], "cure": []})
            slot["dmg"].append(d)
            slot["shock"].append(s)
            slot["cure"].append(c)
    ranked = sorted(agg.items(), key=lambda kv: -len(kv[1]["dmg"]))
    out = []
    for name, d in ranked[:team_count]:
        plays = len(d["dmg"])
        out.append(
            {
                "name": name,
                "plays": plays,
                "avg_damage": sum(d["dmg"]) / plays if plays else 0,
                "avg_shock": sum(d["shock"]) / plays if plays else 0,
                "avg_cure": sum(d["cure"]) / plays if plays else 0,
            }
        )
    return out


# =========================== 概览排版 ===========================
def format_overview(
    player, tier_scores, cur_stats, cur_mode_name, mode_matches, self_avg, heroes, mates
):
    """概览。mode_matches 为当前模式的场（决定有效对局数与“最近N场”的 N）。"""
    lines = []

    rk = tier_scores.get("排位", [0, 0, 0])
    tr = tier_scores.get("天人", [0, 0, 0])
    lines.append(f"排位： {rk[0]} {rk[1]} {rk[2]}")
    lines.append(f"天人： {tr[0]} {tr[1]} {tr[2]}")

    grade_name = ""
    grade_score = None
    if cur_stats and isinstance(cur_stats.get("grade"), dict):
        g = cur_stats["grade"]
        grade_name = f"{g.get('gradeName', '')}{g.get('gradeLevel', '') or ''}".strip()
        grade_score = g.get("gradeScore")
    if grade_name:
        lines.append(f"当前模式: {grade_name} {cur_mode_name}")
    else:
        lines.append(f"当前模式: {cur_mode_name}")
    if grade_score is not None:
        lines.append(f"当前模式分数： {_comma(grade_score)}")

    n = len(mode_matches)
    lines.append(f"有效对局: {n}")

    lines.append("常用英雄(最多五个)：")
    for i, (hero, cnt, _icon) in enumerate(heroes, 1):
        lines.append(f"{i}、{hero}    {cnt}场")

    lines.append(f"最近{n}场场均伤害为: {self_avg['avg_damage']:.1f}")
    lines.append(f"最近{n}场场均治疗为: {self_avg.get('avg_cure', 0):.1f}")
    lines.append(f"最近{n}场场均振刀数量为: {self_avg['avg_shock']:.1f}")
    lines.append(f"最近{n}场场均击败数量为: {self_avg['avg_kill']:.1f}")

    for i, mate in enumerate(mates, 1):
        lines.append(f"队友{i}： 名称： {mate['name']}，游玩次数： {mate['plays']}")
        lines.append(
            f"场均伤害： {mate['avg_damage']:.1f}，"
            f"场均治疗： {mate.get('avg_cure', 0):.1f}，"
            f"场均振刀： {mate['avg_shock']:.1f}"
        )

    return "\n".join(lines)


# =========================== 详细排版 ===========================
def format_detail(player, mode_matches, person_map, mode_name):
    """逐场明细（只当前模式的场）+ 底部场均。"""
    uid = player.get("roleIdSimple", "")
    name = player.get("roleName") or "未知"
    if name == uid:
        name = "未知"
    n = len(mode_matches)
    if n == 0:
        return f"{name}\nUID: {uid}\n当前模式暂无对局记录。"

    lines = [f"详细查询  {name}", f"UID: {uid}　（{mode_name}）", ""]
    total_dmg = total_kill = total_shock = total_cure = 0.0
    for idx, m in enumerate(mode_matches, 1):
        rank = m.get("rank", "?")
        dmg = _num(m.get("damage"))
        kill = _num(m.get("kill"))
        hero = (m.get("hero") or {}).get("heroName", "?")
        ts = _fmt_ts(m.get("battleEndTime"))
        pr = person_map.get(m.get("battleId"))
        shock = (
            _num(_dl_value(pr.get("dataList"), "shock_count"))
            if isinstance(pr, dict)
            else 0
        )
        cure = (
            _num(_dl_value(pr.get("dataList"), "cure")) if isinstance(pr, dict) else 0
        )
        total_dmg += dmg
        total_kill += kill
        total_shock += shock
        total_cure += cure
        rank_str = f"第{rank}".ljust(3)
        lines.append(
            f"{idx:<2} {rank_str} {hero}　振刀{int(shock)}　"
            f"伤害{int(dmg)}　治疗{int(cure)}　{ts}"
        )

    lines.append("")
    lines.append(f"最近{n}场")
    lines.append(f"场均伤害 {total_dmg / n:.1f}")
    lines.append(f"场均治疗 {total_cure / n:.1f}")
    lines.append(f"场均击败 {total_kill / n:.1f}")
    lines.append(f"场均振刀 {total_shock / n:.1f}")
    heroes = top_heroes(mode_matches, 5)
    lines.append(
        "常用英雄(最多五个)： " + "　".join(f"{h} {c}场" for h, c, _ in heroes)
    )
    return "\n".join(lines)
