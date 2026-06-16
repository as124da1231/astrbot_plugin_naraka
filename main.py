# -*- coding: utf-8 -*-
"""永劫无间战绩查询插件 - 主入口。

指令：
- 战绩查询 <昵称/UID> [模式]   概览（排位/天人三档分数、当前模式赛季数据、
  最近N场场均伤害/振刀/击败、常用英雄、当前模式队友统计）
- 详细查询 <昵称/UID>          最近N场逐场明细（含逐场振刀）

数据源：小程序。会 @ 发起查询的人（QQ）。
"""

import re
import time

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

from . import fetcher
from . import formatter
from . import render
from .constants import (
    GAME_MODES,
    TEAMMATE_COUNT,
    CATEGORY_ALIASES,
    TEAM_ALIASES,
    DEFAULT_HEADERS,
)


@register(
    "astrbot_plugin_naraka",
    "YourName",
    "永劫无间战绩查询：排位/天人三档分数、最近对局场均、队友统计、逐场明细、水墨风战绩图片。",
    "2.5.0",
)
class NarakaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache: dict = {}  # 整体结果短期缓存
        self._detail_cache: dict = {}  # 单场详情长期缓存（历史对局不变）

    # ----------------------- 配置 -----------------------
    @property
    def _timeout(self):
        return int(self.config.get("timeout", 15))

    @property
    def _cache_ttl(self):
        return int(self.config.get("cache_ttl", 60))

    @property
    def _default_season_id(self):
        return str(self.config.get("default_season_id", "9620020"))

    @property
    def _image_scale(self):
        """根据清晰度配置返回渲染倍数（用于 HTML 内部 zoom 放大）。"""
        quality = str(self.config.get("image_quality", "high")).lower()
        scale_map = {"normal": 1.5, "high": 2.0, "ultra": 3.0}
        return scale_map.get(quality, 2.0)

    @property
    def _react_enabled(self):
        return bool(self.config.get("react_emoji", True))

    @property
    def _react_emoji_id(self):
        return str(self.config.get("react_emoji_id", "277"))

    async def _react_to_msg(self, event):
        """给触发指令的消息贴一个表情回应（仅 aiocqhttp/QQ）。失败静默，不影响主流程。"""
        if not self._react_enabled:
            return
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            client = event.bot
            await client.api.call_action(
                "set_msg_emoji_like",
                message_id=event.message_obj.message_id,
                emoji_id=self._react_emoji_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[naraka] 贴表情失败（不影响主流程）: {type(e).__name__}: {e}"
            )

    @staticmethod
    def _image_chain(event, image_url, caption):
        """组装 @发起人 + 文字 + 图片 的消息链。"""
        sender = event.get_sender_id()
        return [
            Comp.At(qq=sender),
            Comp.Plain(f" {caption}\n"),
            Comp.Image.fromURL(image_url)
            if str(image_url).startswith("http")
            else Comp.Image.fromFileSystem(image_url),
        ]

    # ----------------------- 参数解析 -----------------------
    @staticmethod
    def _extract_args(event):
        text = (event.message_str or "").strip()
        parts = text.split()
        return parts[1:] if len(parts) > 1 else []

    @staticmethod
    def _is_uid(s):
        return bool(re.fullmatch(r"\d{6,}", s))

    def _parse_mode(self, tokens):
        """返回 (player, category 或 None, team 或 None)。"""
        if not tokens:
            return "", None, None
        player = tokens[0]
        category = team = None
        for tk in tokens[1:]:
            low = tk.lower()
            if tk in CATEGORY_ALIASES:
                category = CATEGORY_ALIASES[tk]
            elif low in TEAM_ALIASES:
                team = TEAM_ALIASES[low]
            elif tk in TEAM_ALIASES:
                team = TEAM_ALIASES[tk]
            else:
                for ca, cs in CATEGORY_ALIASES.items():
                    if tk.startswith(ca):
                        rest = tk[len(ca) :]
                        if rest in TEAM_ALIASES:
                            category, team = cs, TEAM_ALIASES[rest]
                            break
        return player, category, team

    # ----------------------- 单场详情（带缓存） -----------------------
    async def _persons_cached(self, session, role_id, battle_ids):
        """取多场个人详情（命中缓存不再请求）。返回 {battleId: data_or_error}。"""
        need = [b for b in battle_ids if (role_id, b, "p") not in self._detail_cache]
        if need:
            results = await fetcher.fetch_persons(session, role_id, need, self._timeout)
            for b, r in zip(need, results):
                if isinstance(r, dict) and "error" not in r:
                    self._detail_cache[(role_id, b, "p")] = r
                else:
                    self._detail_cache[(role_id, b, "p_err")] = r
        out = {}
        for b in battle_ids:
            if (role_id, b, "p") in self._detail_cache:
                out[b] = self._detail_cache[(role_id, b, "p")]
            else:
                out[b] = self._detail_cache.get(
                    (role_id, b, "p_err"), {"error": "查询失败"}
                )
        return out

    async def _teams_cached(self, session, role_id, battle_ids):
        """取多场队伍详情（命中缓存不再请求）。返回 {battleId: data_or_error}。"""
        need = [b for b in battle_ids if (role_id, b, "t") not in self._detail_cache]
        if need:
            results = await fetcher.fetch_teams(session, role_id, need, self._timeout)
            for b, r in zip(need, results):
                if isinstance(r, dict) and "error" not in r:
                    self._detail_cache[(role_id, b, "t")] = r
                else:
                    self._detail_cache[(role_id, b, "t_err")] = r
        out = {}
        for b in battle_ids:
            if (role_id, b, "t") in self._detail_cache:
                out[b] = self._detail_cache[(role_id, b, "t")]
            else:
                out[b] = self._detail_cache.get(
                    (role_id, b, "t_err"), {"error": "查询失败"}
                )
        return out

    # ----------------------- 解析玩家 -----------------------
    async def _resolve_player(self, session, query, recent_hint=None):
        if self._is_uid(query):
            return {"roleIdSimple": query, "roleName": query, "roleLevel": ""}
        p = await fetcher.search_player(session, query, self._timeout)
        return p  # 可能含 error

    # ===================== 概览 =====================
    async def _gather_overview_data(self, raw_args):
        """获取概览所需的全部数据。成功返回数据字典，失败返回 {"error": ...}。"""
        player_query, category, team = self._parse_mode(raw_args)
        if not player_query:
            return {"error": "__usage__"}

        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            player = await self._resolve_player(session, player_query)
            if "error" in player:
                return {"error": player["error"]}
            role_id = player["roleIdSimple"]

            recent = await fetcher.fetch_recent(session, role_id, 20, self._timeout)
            if "error" in recent:
                return {"error": recent["error"]}
            base_list = recent.get("list", [])
            if not base_list:
                return {"error": f"{player.get('roleName', role_id)} 暂无最近对局记录"}

            if player.get("roleName") == role_id:
                pinfo = recent.get("playerInfo")
                if isinstance(pinfo, dict) and pinfo.get("name"):
                    player["roleName"] = pinfo["name"]

            # 确定当前模式
            if category and team:
                cur_cat, cur_team = category, team
                cur_subtype = GAME_MODES.get((cur_cat, cur_team))
                cur_mode_name = f"{cur_cat}{cur_team}"
            else:
                auto_cat, auto_team, auto_sub = formatter.current_mode_from_recent(
                    base_list
                )
                if auto_cat:
                    cur_cat, cur_team, cur_subtype = auto_cat, auto_team, auto_sub
                    cur_mode_name = f"{cur_cat}{cur_team}"
                else:
                    cur_cat = cur_team = cur_subtype = None
                    cur_mode_name = "最近对局"

            # 取“当前模式的对局”
            if cur_subtype is not None:
                mode_recent = await fetcher.fetch_recent(
                    session, role_id, 20, self._timeout, game_mode=cur_subtype
                )
                if isinstance(mode_recent, dict) and "error" not in mode_recent:
                    mode_matches = mode_recent.get("list", []) or []
                else:
                    mode_matches = formatter.filter_by_subtype(base_list, cur_subtype)
            else:
                mode_matches = list(base_list)
            if not mode_matches:
                mode_matches = list(base_list)

            # 三档分数
            season_id = (
                formatter.extract_current_season_id(
                    await fetcher.fetch_seasons(session, self._timeout)
                )
                or self._default_season_id
            )
            tier_modes = [101, 102, 103, 403, 402, 401]
            if cur_subtype and cur_subtype not in tier_modes:
                tier_modes.append(cur_subtype)
            stats_map = await fetcher.fetch_stats_multi(
                session, role_id, tier_modes, season_id, self._timeout
            )

            def score_of(gm):
                d = stats_map.get(gm)
                if (
                    isinstance(d, dict)
                    and "error" not in d
                    and isinstance(d.get("grade"), dict)
                ):
                    return d["grade"].get("gradeScore", 0) or 0
                return 0

            tier_scores = {
                "排位": [score_of(101), score_of(102), score_of(103)],
                "天人": [score_of(403), score_of(402), score_of(401)],
            }
            cur_stats = stats_map.get(cur_subtype)
            if not (isinstance(cur_stats, dict) and "error" not in cur_stats):
                cur_stats = None

            # 自己场均
            mode_ids = [m.get("battleId") for m in mode_matches]
            person_map = await self._persons_cached(session, role_id, mode_ids)
            self_avg = formatter.self_averages(mode_matches, person_map)
            if "error" in self_avg:
                if self_avg.get("rate_limited"):
                    return {"error": "查询过于频繁，站点限流了，请过一会儿再查～"}
                return {"error": f"战绩查询失败：{self_avg['error']}"}

            heroes = formatter.top_heroes(mode_matches, 5)
            detail = formatter.detail_rows(mode_matches, person_map)

            team_count = TEAMMATE_COUNT.get(cur_team, 0) if cur_team else 0
            mates = []
            if team_count > 0:
                team_map = await self._teams_cached(session, role_id, mode_ids)
                mates = formatter.teammate_stats(
                    mode_matches, team_map, person_map, team_count
                )

            display_name = player.get("roleName") or "未知"
            if display_name == role_id:
                display_name = "未知"

            return {
                "player": player,
                "role_id": role_id,
                "display_name": display_name,
                "tier_scores": tier_scores,
                "cur_stats": cur_stats,
                "cur_mode_name": cur_mode_name,
                "mode_matches": mode_matches,
                "self_avg": self_avg,
                "heroes": heroes,
                "mates": mates,
                "detail": detail,
            }

    async def _do_overview(self, raw_args):
        player_query, category, team = self._parse_mode(raw_args)
        if not player_query:
            return self._usage()

        cache_key = f"O|{player_query}|{category}|{team}"
        now = time.time()
        c = self._cache.get(cache_key)
        if c and c[0] > now:
            return c[1]

        data = await self._gather_overview_data(raw_args)
        if "error" in data:
            if data["error"] == "__usage__":
                return self._usage()
            return f"❌ {data['error']}"

        result = formatter.format_overview(
            data["player"],
            data["tier_scores"],
            data["cur_stats"],
            data["cur_mode_name"],
            data["mode_matches"],
            data["self_avg"],
            data["heroes"],
            data["mates"],
        )
        final = f" 当前ID: {data['display_name']}\nUID: {data['role_id']}\n" + result
        self._cache[cache_key] = (now + self._cache_ttl, final)
        return final

    # ===================== 详细 =====================
    async def _do_detail(self, raw_args):
        player_query, category, team = self._parse_mode(raw_args)
        if not player_query:
            return self._usage()

        cache_key = f"D|{player_query}|{category}|{team}"
        now = time.time()
        c = self._cache.get(cache_key)
        if c and c[0] > now:
            return c[1]

        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            player = await self._resolve_player(session, player_query)
            if "error" in player:
                return f"❌ {player['error']}"
            role_id = player["roleIdSimple"]

            recent = await fetcher.fetch_recent(session, role_id, 20, self._timeout)
            if "error" in recent:
                return f"❌ {recent['error']}"
            base_list = recent.get("list", [])
            if not base_list:
                return f"❌ {player.get('roleName', role_id)} 暂无最近对局记录"
            if player.get("roleName") == role_id:
                pinfo = recent.get("playerInfo")
                if isinstance(pinfo, dict) and pinfo.get("name"):
                    player["roleName"] = pinfo["name"]

            # 确定当前模式（同概览）
            if category and team:
                cur_subtype = GAME_MODES.get((category, team))
                cur_mode_name = f"{category}{team}"
            else:
                auto_cat, auto_team, auto_sub = formatter.current_mode_from_recent(
                    base_list
                )
                if auto_cat:
                    cur_subtype = auto_sub
                    cur_mode_name = f"{auto_cat}{auto_team}"
                else:
                    cur_subtype = None
                    cur_mode_name = "最近对局"

            # 取该模式的对局
            if cur_subtype is not None:
                mode_recent = await fetcher.fetch_recent(
                    session, role_id, 20, self._timeout, game_mode=cur_subtype
                )
                if isinstance(mode_recent, dict) and "error" not in mode_recent:
                    mode_matches = mode_recent.get("list", []) or []
                else:
                    mode_matches = formatter.filter_by_subtype(base_list, cur_subtype)
            else:
                mode_matches = list(base_list)
            if not mode_matches:
                mode_matches = list(base_list)

            mode_ids = [m.get("battleId") for m in mode_matches]
            person_map = await self._persons_cached(session, role_id, mode_ids)
            for bid in mode_ids:
                r = person_map.get(bid)
                if not isinstance(r, dict) or "error" in r:
                    if isinstance(r, dict) and r.get("rate_limited"):
                        return "❌ 查询过于频繁，站点限流了，请过一会儿再查～"
                    return "❌ 战绩查询失败：部分单场详情查询失败"

            result = formatter.format_detail(
                player, mode_matches, person_map, cur_mode_name
            )
            self._cache[cache_key] = (now + self._cache_ttl, result)
            return result

    # ----------------------- 公共 -----------------------
    @staticmethod
    def _usage():
        return (
            "用法：\n"
            "战绩查询 <昵称或UID> [模式]\n"
            "详细查询 <昵称或UID>\n"
            "模式可选：天选/天人 + 单排/双排/三排（不填按最近一场自动判断）"
        )

    @staticmethod
    def _build_chain(event, text, prefix=""):
        """构造带 @ 的消息链；prefix 接在 @ 之后（如「 当前ID: 」前缀由调用方拼好）。"""
        try:
            sid = event.get_sender_id()
        except Exception:  # noqa: BLE001
            sid = None
        body = "\u200b" + text
        if sid:
            return [Comp.At(qq=sid), Comp.Plain(body)]
        return [Comp.Plain(text)]

    # ----------------------- 指令 -----------------------
    @filter.command("战绩查询")
    async def cmd_overview(self, event: AstrMessageEvent):
        """战绩查询 <昵称/UID> [模式]"""
        try:
            args = self._extract_args(event)
            text = await self._do_overview(args)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[naraka] 概览异常 {type(e).__name__}: {e}")
            text = "❌ 查询时发生错误，请稍后再试。"
        yield event.chain_result(self._build_chain(event, text))

    @filter.command("详细查询")
    async def cmd_detail(self, event: AstrMessageEvent):
        """详细查询 <昵称/UID>"""
        try:
            args = self._extract_args(event)
            text = await self._do_detail(args)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[naraka] 详细异常 {type(e).__name__}: {e}")
            text = "❌ 查询时发生错误，请稍后再试。"
        yield event.chain_result(self._build_chain(event, text))

    @filter.command("永劫战绩", alias={"战绩图"})
    async def cmd_overview_image(self, event: AstrMessageEvent):
        """永劫战绩 <昵称/UID> [模式]：水墨风战绩图片，渲染失败则降级为文字"""
        args = self._extract_args(event)
        if not args:
            yield event.chain_result(self._build_chain(event, self._usage()))
            return

        # 收到指令先贴表情回应，告知用户正在处理
        await self._react_to_msg(event)

        # 获取数据
        try:
            data = await self._gather_overview_data(args)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[naraka] 图片数据获取异常 {type(e).__name__}: {e}")
            yield event.chain_result(
                self._build_chain(event, "❌ 查询时发生错误，请稍后再试。")
            )
            return

        if "error" in data:
            if data["error"] == "__usage__":
                yield event.chain_result(self._build_chain(event, self._usage()))
            else:
                yield event.chain_result(
                    self._build_chain(event, f"❌ {data['error']}")
                )
            return

        # 尝试渲染图片
        try:
            html = render.build_html(
                data["display_name"],
                data["role_id"],
                data["tier_scores"],
                data["cur_stats"],
                data["cur_mode_name"],
                len(data["mode_matches"]),
                data["heroes"],
                data["self_avg"],
                data["mates"],
                data["detail"],
                self._image_scale,
            )
            # 清晰度由 HTML 内部 zoom 放大实现（不依赖框架 device_scale_factor）
            image_url = await self.html_render(
                html, {}, options={"full_page": True, "type": "png"}
            )
            if image_url:
                # @发起人 + 完成文字 + 图片
                yield event.chain_result(
                    self._image_chain(event, image_url, "永劫战绩生成完毕")
                )
                return
            logger.warning("[naraka] html_render 返回空，降级为文字")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[naraka] 图片渲染失败，降级文字 {type(e).__name__}: {e}")

        # 降级：发文字版概览（复用已获取的数据，不再重复请求）
        result = formatter.format_overview(
            data["player"],
            data["tier_scores"],
            data["cur_stats"],
            data["cur_mode_name"],
            data["mode_matches"],
            data["self_avg"],
            data["heroes"],
            data["mates"],
        )
        text = f" 当前ID: {data['display_name']}\nUID: {data['role_id']}\n" + result
        yield event.chain_result(self._build_chain(event, text))
