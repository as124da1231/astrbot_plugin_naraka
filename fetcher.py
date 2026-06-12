# -*- coding: utf-8 -*-
"""永劫无间战绩查询插件 - 网络请求层（aiohttp，小程序数据源）。

统一用 _fetch_json，失败返回 {"error": ...}。提供并发批量查询单场详情，
以聚合“最近 N 场场均振刀”和“队友统计”。
"""

import asyncio
from typing import Any, Optional

import aiohttp

from astrbot.api import logger

from .constants import (
    API_SEARCH, API_SEASONS, API_STATS, API_RECENT,
    API_DETAIL_PERSON, API_DETAIL_TEAM,
)


async def _fetch_json(session, url, params=None, timeout=15, retries=2) -> Any:
    """通用 GET。遇 429（限流）自动退避重试 retries 次。"""
    attempt = 0
    while True:
        try:
            to = aiohttp.ClientTimeout(total=timeout)
            async with session.get(url, params=params, timeout=to) as resp:
                if resp.status == 429:
                    if attempt < retries:
                        # 退避：0.6s, 1.2s ...
                        await asyncio.sleep(0.6 * (attempt + 1))
                        attempt += 1
                        continue
                    logger.error(f"[naraka] HTTP 429（限流，重试已用尽）{url} {params}")
                    return {"error": "查询过于频繁，请稍后再试", "rate_limited": True}
                if resp.status != 200:
                    logger.error(f"[naraka] HTTP {resp.status} {url} {params}")
                    return {"error": f"接口访问失败（HTTP {resp.status}）"}
                try:
                    return await resp.json(content_type=None)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[naraka] JSON解析失败 {e} {url}")
                    return {"error": "接口返回了无法解析的数据"}
        except asyncio.TimeoutError:
            logger.error(f"[naraka] 超时 {url}")
            return {"error": "请求超时"}
        except aiohttp.ClientError as e:
            logger.error(f"[naraka] 网络错误 {type(e).__name__}: {e} {url}")
            return {"error": "网络连接失败"}
        except Exception as e:  # noqa: BLE001
            logger.error(f"[naraka] 未知错误 {type(e).__name__}: {e} {url}")
            return {"error": "查询时发生未知错误"}


def _check(data) -> Optional[dict]:
    if not isinstance(data, dict):
        return {"error": "接口返回数据格式异常"}
    if "error" in data:
        return data
    if data.get("code") != 200:
        return {"error": f"接口返回错误：{data.get('msg', '未知')}"}
    return None


async def search_player(session, name, timeout=15) -> dict:
    raw = await _fetch_json(session, API_SEARCH, {"name": name}, timeout)
    err = _check(raw)
    if err:
        return err
    data = raw.get("data")
    if not data or not data.get("roleIdSimple"):
        return {"error": f"没有找到玩家「{name}」，请检查昵称是否正确"}
    return data


async def fetch_seasons(session, timeout=15):
    raw = await _fetch_json(session, API_SEASONS, timeout=timeout)
    err = _check(raw)
    if err:
        logger.warning(f"[naraka] 赛季获取失败：{err.get('error')}")
        return None
    return raw.get("data")


async def fetch_stats(session, role_id, game_mode, season_id, timeout=15) -> dict:
    params = {"roleIdSimple": role_id, "gameMode": game_mode, "seasonId": season_id}
    raw = await _fetch_json(session, API_STATS, params, timeout)
    err = _check(raw)
    if err:
        return err
    data = raw.get("data")
    if not data:
        return {"error": "该模式暂无赛季数据"}
    return data


async def fetch_recent(session, role_id, page_size=20, timeout=15, game_mode=None) -> dict:
    """最近对局列表（小程序）。返回含 list 的 data。

    传 game_mode 时只返回该模式的对局（接口对每个模式独立返回最近约10场）。
    """
    params = {"roleIdSimple": role_id, "pageIndex": 1, "pageSize": page_size}
    if game_mode is not None:
        params["gameMode"] = game_mode
    raw = await _fetch_json(session, API_RECENT, params, timeout)
    err = _check(raw)
    if err:
        return err
    data = raw.get("data")
    if not data or not isinstance(data.get("list"), list):
        return {"error": "暂无最近对局记录"}
    return data


async def fetch_detail_person(session, role_id, battle_id, timeout=15) -> dict:
    params = {"roleIdSimple": role_id, "battleId": battle_id}
    raw = await _fetch_json(session, API_DETAIL_PERSON, params, timeout)
    err = _check(raw)
    if err:
        return err
    return raw.get("data") or {"error": "单场详情为空"}


async def fetch_detail_team(session, role_id, battle_id, timeout=15) -> dict:
    params = {"roleIdSimple": role_id, "battleId": battle_id}
    raw = await _fetch_json(session, API_DETAIL_TEAM, params, timeout)
    err = _check(raw)
    if err:
        return err
    return raw.get("data") or {"error": "队伍详情为空"}


# --------------------------- 并发批量（限并发） ---------------------------
# 限制同时在途的请求数，避免瞬间打太多触发 429 限流
_MAX_CONCURRENCY = 2


async def _gather_limited(coro_factories, concurrency=_MAX_CONCURRENCY):
    """按并发上限执行一批协程工厂，返回结果列表（保持顺序）。"""
    sem = asyncio.Semaphore(concurrency)

    async def _run(factory):
        async with sem:
            return await factory()

    return await asyncio.gather(*[_run(f) for f in coro_factories])


async def fetch_persons(session, role_id, battle_ids, timeout=15) -> list:
    """限并发拉取多场个人详情，返回与 battle_ids 等长的结果列表。"""
    factories = [
        (lambda bid=bid: fetch_detail_person(session, role_id, bid, timeout))
        for bid in battle_ids
    ]
    return await _gather_limited(factories)


async def fetch_teams(session, role_id, battle_ids, timeout=15) -> list:
    """限并发拉取多场队伍详情。"""
    factories = [
        (lambda bid=bid: fetch_detail_team(session, role_id, bid, timeout))
        for bid in battle_ids
    ]
    return await _gather_limited(factories)


async def fetch_stats_multi(session, role_id, game_modes, season_id, timeout=15) -> dict:
    """限并发拉取多个模式的 stats，返回 {game_mode: data_or_error}。"""
    factories = [
        (lambda gm=gm: fetch_stats(session, role_id, gm, season_id, timeout))
        for gm in game_modes
    ]
    results = await _gather_limited(factories)
    return dict(zip(game_modes, results))
