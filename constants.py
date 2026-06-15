# -*- coding: utf-8 -*-
"""永劫无间战绩查询插件 - 常量定义。

集中存放接口地址、gameMode 对照表、模式别名等，均来自对
naraka.drivod.top（小程序数据源）的实测。
"""

SITE_ROOT = "https://naraka.drivod.top"

# 接口（全部小程序数据源）
API_SEARCH = SITE_ROOT + "/api/record/search"
API_SEASONS = SITE_ROOT + "/api/record/seasons"
API_STATS = SITE_ROOT + "/api/record/mini-program/stats"
API_RECENT = SITE_ROOT + "/api/record/mini-program/battle/recent"
API_DETAIL_PERSON = SITE_ROOT + "/api/record/mini-program/battle/detail/person"
API_DETAIL_TEAM = SITE_ROOT + "/api/record/mini-program/battle/detail/team"

# 请求头：靠 Referer + 正常 UA 放行
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": SITE_ROOT + "/",
}

# gameMode / subtype 对照（均经实测确认）
# 天选：个位正序 1单2双3三
# 天人：个位反序 1三2双3单（注意与天选相反！）
# 对局接口的 subtype == stats 接口的 gameMode（同一套编码）
# 大类码（对局里的 gameMode 字段）：天选=1，天人=4（匹配=2、地脉等本插件不做）
GAME_MODES = {
    ("天选", "单排"): 101,
    ("天选", "双排"): 102,
    ("天选", "三排"): 103,
    ("天人", "单排"): 403,
    ("天人", "双排"): 402,
    ("天人", "三排"): 401,
}
GAME_MODE_NAMES = {v: f"{cat}{team}" for (cat, team), v in GAME_MODES.items()}

# 只做这两个大类
SUPPORTED_CATEGORIES = ("天选", "天人")
# 大类百位码 -> 名称
CATEGORY_BY_CODE = {1: "天选", 4: "天人"}
TEAMMATE_COUNT = {"单排": 0, "双排": 1, "三排": 2}

CATEGORY_ALIASES = {
    "天选": "天选",
    "排位": "天选",
    "排位赛": "天选",
    "天选赛季": "天选",
    "天人": "天人",
    "天人赛季": "天人",
    "天人之境": "天人",
}
TEAM_ALIASES = {
    "单": "单排",
    "单排": "单排",
    "单人": "单排",
    "solo": "单排",
    "1": "单排",
    "双": "双排",
    "双排": "双排",
    "双人": "双排",
    "duo": "双排",
    "2": "双排",
    "三": "三排",
    "三排": "三排",
    "三人": "三排",
    "trio": "三排",
    "组排": "三排",
    "3": "三排",
}

DEFAULT_CATEGORY = "天选"


def split_subtype(subtype) -> tuple:
    """把 subtype/gameMode（如 401）拆成 (大类名, 人数名)。

    天选个位正序、天人个位反序；不支持的大类（匹配/地脉等）返回 (None, None)。
    """
    try:
        s = int(subtype)
    except (TypeError, ValueError):
        return None, None
    cat = CATEGORY_BY_CODE.get(s // 100)
    if cat is None:
        return None, None
    if s // 100 == 4:  # 天人反序
        team = {1: "三排", 2: "双排", 3: "单排"}.get(s % 10)
    else:  # 天选正序
        team = {1: "单排", 2: "双排", 3: "三排"}.get(s % 10)
    return cat, team
