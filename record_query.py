"""Select and summarize recent Xiaoheihe NARAKA match records.

The match-list endpoint returns a mixed, time-ordered pool even when given a
mode. Its rows carry exact damage, while detail.data contains display values.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime


MODES = {
    "4": "天人单排",
    "13": "天人双排",
    "5": "天人三排",
    "5000000": "天选单排",
    "12": "天选双排",
    "5000001": "天选三排",
    "6": "快速单排",
    "7": "快速三排",
    "5000010": "无尽试炼",
}
RANKED_IDS = frozenset(("4", "13", "5", "5000000", "12", "5000001"))

# 裸写模式：不带"天选/天人"前缀时，固定归到天选
BARE_MODES = {
    "单排": "5000000",
    "双排": "12",
    "三排": "5000001",
}

MODE_USAGE = "模式请填 单排/双排/三排、天选单排/双排/三排、天人单排/双排/三排，或快速单排/三排"
# Fallback catalog from the user's September 2026 Xiaoheihe home/data capture.
# A fresh API catalog overrides these names when available.
KNOWN_HERO_NAMES = {
    "1000021": "季莹莹", "1000031": "李寻欢", "1000017": "殷紫萍",
    "1000003": "宁红夜", "1000018": "沈妙", "1000032": "巫真",
    "1000009": "妖刀姬", "1000026": "张起灵", "1000027": "席拉",
    "1000022": "玉玲珑", "1000015": "顾清寒", "1000005": "特木尔",
    "1000001": "土御门胡桃", "1000010": "崔三娘", "1000004": "迦南",
    "1000029": "万钧", "1000030": "万钧", "1000020": "胡为", "1000013": "无尘",
    "1000023": "哈迪", "1000006": "季沧海", "1000024": "魏轻",
    "1000016": "武田信忠", "1000011": "岳山", "1000007": "天海",
    "1000025": "刘炼", "1000033": "甘璇", "1000028": "蓝梦",
}
KNOWN_HERO_SUFFIXES = {"70": "南宫锦", "73": "叶修"}


def apply_hero_mappings(hero_names: dict[str, str], records: list[dict], entries: object) -> dict[str, str]:
    """Apply editable short or full hero IDs over the API catalog."""
    result = dict(hero_names)
    if not isinstance(entries, list):
        return result
    overrides = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hero_id = str(entry.get("hero_id") or "").strip()
        hero_name = str(entry.get("hero_name") or "").strip()
        if hero_id.isdigit() and hero_name:
            overrides[hero_id] = hero_name
    observed_ids = set(result)
    observed_ids.update(str(record.get("hero_id") or "") for record in records)
    for hero_id in observed_ids:
        if not hero_id.isdigit():
            continue
        exact = overrides.get(hero_id)
        suffix = overrides.get(hero_id[-2:].zfill(2))
        if exact or suffix:
            result[hero_id] = exact or suffix
    return result


def resolve_mode(text: str) -> str:
    """Return the list's battle_tid for an explicit mode, or raise.

    支持的写法（全部收敛到这里，其它地方不要再做补全）：
      * 数字 battle_tid：4 / 13 / 5 / 5000000 / 12 / 5000001 / 6 / 7 / 5000010
      * 四字组合：天人单排、天选三排、快速单排……
      * 长写法：天人之战单排、天选之人三排、快速匹配三排
      * 裸写：单排 / 双排 / 三排  → 固定归到「天选」
    """
    name = re.sub(r"[·・\s_—－-]", "", text)
    if not name:
        raise ValueError(MODE_USAGE)
    if name in MODES:
        return name
    if name in BARE_MODES:
        return BARE_MODES[name]
    for mode_id, label in MODES.items():
        if name in (label, label.replace("天人", "天人之战"), label.replace("天选", "天选之人"),
                    label.replace("快速", "快速匹配")):
            return mode_id
    raise ValueError(MODE_USAGE)


def recent_rows(body: dict, limit: int = 30) -> list[dict]:
    """Deduplicate and sort the returned mixed pool, capped by configuration."""
    rows = body.get("match_list")
    if not isinstance(rows, list):
        raise ValueError("对局列表格式已变化：缺少 match_list")
    seen = set()
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_id = str(row.get("match_id") or "")
        if match_id and match_id not in seen:
            seen.add(match_id)
            matches.append(row)
    matches.sort(key=lambda row: _number(row.get("time")) or 0, reverse=True)
    return matches[:limit]


def select_matches(rows: list[dict], requested_mode: str = "", limit: int = 10) -> tuple[str | None, list[dict]]:
    """Auto-select the newest ranked mode, excluding quick and other modes."""
    if requested_mode:
        mode_id = resolve_mode(requested_mode)
    else:
        mode_id = next((str(row.get("battle_tid")) for row in rows
                        if str(row.get("battle_tid")) in RANKED_IDS), None)
    if mode_id is None:
        return None, []
    return mode_id, [row for row in rows if str(row.get("battle_tid")) == mode_id][:limit]


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(k|万|km|min|h)?\s*", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    return number * {"k": 1000, "万": 10000, "km": 1, "min": 1, "h": 60, "": 1}[suffix]


def _metric(detail: dict, label: str) -> float | None:
    entries = detail.get("data")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("desc") == label:
            return _number(entry.get("value"))
    return None


def _match_time(row: dict, detail: dict) -> float | None:
    """从详情数据里找对局时间戳。优先 detail.data 里 desc 含"时间"的项。"""
    entries = detail.get("data")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            desc = str(entry.get("desc") or "")
            if "时间" in desc:
                ts = _number(entry.get("value"))
                if ts and ts > 0:
                    # 秒级时间戳 ×1000 转毫秒
                    if ts < 1e12:
                        ts *= 1000
                    return ts
    # fallback：detail.time / row.time 都是秒级时间戳，转毫秒
    ts = _number(detail.get("time")) or _number(row.get("time"))
    if ts and ts < 1e12:
        ts *= 1000
    return ts


def _whole(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.1f}{unit}"


def _integer(value: float | None) -> str:
    return str(round(value)) if value is not None else "—"


def _average(records: list[dict], key: str) -> float | None:
    values = [record[key] for record in records if record.get(key) is not None]
    return sum(values) / len(values) if values else None


def make_record(row: dict, detail: dict) -> dict:
    """Prefer exact list fields where available; retain rounded detail fields."""
    kills = _metric(detail, "击杀")
    damage = _number(row.get("damage"))
    rating_delta = _number(detail.get("rating_delta"))
    weapons = []
    for item in detail.get("weapon_list") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        part = str(item["name"])
        share = _number(item.get("per"))
        if share is not None:
            part += f"{share * 100:.0f}%"
        if item.get("damage") is not None:
            part += f"/伤害{_integer(_number(item['damage']))}"
        weapons.append(part)
    return {
        "time": _match_time(row, detail),
        "rank": _number(detail.get("rank")) or _number(row.get("rank")),
        "kills": kills if kills is not None else _number(row.get("kill_times")),
        "damage": damage if damage is not None else _metric(detail, "总伤害"),
        "recovery": _metric(detail, "总恢复"),
        "parry": _metric(detail, "振刀"),
        "grapple_used": _metric(detail, "使用钩索"),
        "grapple_hit": _metric(detail, "钩索命中"),
        "move_hits": _metric(detail, "招式命中"),
        "skills": _metric(detail, "释放技能"),
        "survival_min": _metric(detail, "存活时长"),
        "rescues": _metric(detail, "救援"),
        "distance_km": _metric(detail, "移动距离"),
        "revives": _metric(detail, "复活数"),
        "currency": _metric(detail, "消耗货币"),
        "rating_delta": rating_delta if rating_delta is not None else _number(row.get("rating_delta")),
        "rating": _number(detail.get("rating")) or _number(row.get("rating")),
        "total_users": _number(row.get("total_users_count")),
        "map": str(detail.get("map_name") or row.get("map_name") or ""),
        "grade": str(detail.get("grade") or row.get("grade") or ""),
        "hero_id": str(row.get("hero_id") or ""),
        "weapons": weapons,
        "souls": [str(item.get("name")) for item in detail.get("soul_item_list") or []
                  if isinstance(item, dict) and item.get("name")],
        "tags": [str(item.get("name")) for item in detail.get("tags") or []
                 if isinstance(item, dict) and item.get("name")],
    }


def short_role_id(role_id: object) -> str:
    """把角色主键压缩成便于阅读的形式。

    小黑盒端游角色主键形如 ``<4 位前缀><18 位数字>``（22 字符）：
    4 位随机前缀 + 15 位序号 + 3 位服务器号。展示时去掉前缀和前导零，
    得到 15 位序号；其它格式原样返回。
    """
    value = str(role_id or "").strip()
    if re.fullmatch(r"[0-9A-Za-z]{4}\d{18}", value):
        body = value[4:].lstrip("0")
        return body or value[4:]
    return value


def format_identity(player_name: str, role_id: object = "") -> list[str]:
    """统一的标题：昵称与 ID **各自独占一行**。"""
    lines = [f"昵称：{player_name}"]
    if role_id is not None and str(role_id).strip():
        lines.append(f"ID：{role_id}")
    return lines


def _score(scores: dict[str, str], mode_id: str) -> str:
    raw = str(scores.get(mode_id) or "").strip()
    return f"{int(raw):,}" if raw.isdigit() and int(raw) > 0 else "—"


def _hero_label(hero_id: object, hero_names: dict[str, str]) -> str:
    value = str(hero_id or "")
    name = hero_names.get(value)
    if name:
        return name
    if value.isdigit():
        suffix = value[-2:].zfill(2)
        return KNOWN_HERO_SUFFIXES.get(suffix, suffix)
    return "未知英雄"


def format_summary(player_name: str, total: int, mode_id: str, selected: int,
                   records: list[dict], scores: dict[str, str], hero_names: dict[str, str],
                   failed: int = 0, role_id: object = "") -> str:
    label = MODES.get(mode_id, f"模式 {mode_id}")
    lines = [
        *format_identity(player_name, role_id),
        f"排位：{_score(scores, '5000000')} {_score(scores, '12')} {_score(scores, '5000001')}",
        f"天人：{_score(scores, '4')} {_score(scores, '13')} {_score(scores, '5')}",
        f"当前模式：{label}",
        f"当前模式分数：{_score(scores, mode_id)}",
        f"查询最近场次：{total}",
        f"选中对局：{selected}",
        f"有效对局：{len(records)}",
    ]
    if failed:
        lines.append(f"详情失败：{failed}（未计入场均）")
    counts = Counter(_hero_label(record.get("hero_id"), hero_names) for record in records)
    lines.append("常用英雄（最多五个）：")
    if counts:
        for index, (hero_name, count) in enumerate(counts.most_common(5), 1):
            lines.append(f"{index}、{hero_name}  {count}场")
    else:
        lines.append("暂无")
    count = len(records)
    lines.extend((
        f"最近{count}场场均伤害：{_whole(_average(records, 'damage'))}",
        f"最近{count}场场均振刀：{_whole(_average(records, 'parry'))}",
        f"最近{count}场场均击败：{_whole(_average(records, 'kills'))}",
    ))
    return "\n".join(lines)


def format_details(player_name: str, total: int, mode_id: str,
                   selected: int, records: list[dict], hero_names: dict[str, str],
                   failed: int = 0, role_id: object = "") -> str:
    label = MODES.get(mode_id, f"模式 {mode_id}")
    lines = [*format_identity(player_name, role_id),
             f"查询到对局 {total} 场｜模式：{label}",
             f"选中 {selected} 场｜有效对局 {len(records)} 场"]
    if failed:
        lines.append(f"详情未成功 {failed} 场")
    if not records:
        return "\n".join(lines + ["暂无可展示的单局详情。"])
    lines.append("有效对局详情（新→旧）：")
    for index, record in enumerate(records, 1):
        hero = _hero_label(record.get("hero_id"), hero_names)
        ts = record.get("time")
        time_str = datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M") if ts else "—"
        lines.append(
            f"第{index}局｜英雄：{hero}｜时间：{time_str}\n"
            f"击杀：{_integer(record.get('kills'))}｜伤害：{_integer(record.get('damage'))}\n"
            f"恢复：{_integer(record.get('recovery'))}｜振刀：{_integer(record.get('parry'))}｜"
            f"存活：{_whole(record.get('survival_min'), '分')}"
        )
        lines.append("")
    return "\n".join(lines).strip()


def split_detail_report(report: str, max_chars: int = 1200) -> list[str]:
    """Keep each QQ message reasonably short without splitting a match."""
    sections = report.split("\n\n")
    chunks = []
    current = ""
    for section in sections:
        if current and len(current) + len(section) + 2 > max_chars:
            chunks.append(current)
            current = section
        else:
            current = f"{current}\n\n{section}" if current else section
    if current:
        chunks.append(current)
    return chunks
