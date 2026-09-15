"""Exercise the command flow with AstrBot and Xiaoheihe replaced in memory."""

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
TEST_ROLE_ID = "100000000000000000163"


def _install_astrbot_stub(data_dir):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    class Star:
        def __init__(self, context):
            self.context = context

    class Filter:
        class PermissionType:
            ADMIN = "admin"

        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(*args, **kwargs):
            return lambda function: function

        @staticmethod
        def permission_type(*args, **kwargs):
            def decorate(function):
                function._required_permission = args[0]
                return function
            return decorate

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda function: function

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return data_dir

    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(warning=lambda *args: None)
    event.AstrMessageEvent = object
    event.MessageChain = list
    event.filter = Filter
    star.Context = object
    star.Star = Star
    star.StarTools = StarTools
    star.register = lambda *args: lambda cls: cls
    components.At = lambda qq: ("at", qq)
    components.Reply = lambda id: ("reply", id)
    components.Plain = lambda value: ("plain", value)
    components.Image = types.SimpleNamespace(
        fromURL=lambda url: ("image", url),
        fromFileSystem=lambda path: ("image", path),
        fromBytes=lambda data: ("image", data),
    )
    astrbot.api = api
    api.event = event
    api.star = star
    api.message_components = components
    for name, module in (("astrbot", astrbot), ("astrbot.api", api),
                         ("astrbot.api.event", event), ("astrbot.api.star", star),
                         ("astrbot.api.message_components", components)):
        sys.modules[name] = module


class FakeEvent:
    def __init__(self, group="123", message_str=""):
        self.group = group
        self.message_str = message_str
        self.message_obj = types.SimpleNamespace(message_id="789")
        self.bot = types.SimpleNamespace(api=types.SimpleNamespace(call_action=AsyncMock()))

    def is_private_chat(self):
        return not self.group

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "456"

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, value):
        return value

    def chain_result(self, value):
        return value


class PluginFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        _install_astrbot_stub(cls.temp.name)
        sys.modules.setdefault("qrcode", types.ModuleType("qrcode"))
        cls.main = importlib.import_module("astrbot_plugin_naraka.main")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    async def test_whitelist_blocks_unlisted_group(self):
        plugin = self.main.NarakaPlugin(None, {"enable_group_whitelist": True,
                                              "group_whitelist": ["999"]})
        outputs = [item async for item in plugin.query_records(FakeEvent(), TEST_ROLE_ID)]
        self.assertEqual(outputs, [])
        self.assertTrue(plugin._allowed_group(FakeEvent("")))

    async def test_group_login_qr_switch_keeps_admin_permission(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["allow_group_login_qr"]["default"])
        self.assertEqual(self.main.NarakaPlugin.login._required_permission, "admin")
        plugin = self.main.NarakaPlugin(None, {})
        denied = [item async for item in plugin.login(FakeEvent())]
        self.assertIn("请私聊", denied[0])

        class FakeLoginClient:
            async def request_qr(self):
                return types.SimpleNamespace(qr_content="test", expires_at=self.main_time() + 60)

            async def check_qr(self, qr):
                return types.SimpleNamespace(state=self_state)

            @staticmethod
            def main_time():
                return self_main.time.time()

        self_main = self.main
        self_state = self.main.LoginState.EXPIRED
        plugin.config["allow_group_login_qr"] = True
        with patch.object(self.main, "XiaoheiheLoginClient", FakeLoginClient), \
             patch.object(self.main, "generate_qr_png", return_value=b"qr"), \
             patch.object(self.main.asyncio, "sleep", AsyncMock()):
            allowed = [item async for item in plugin.login(FakeEvent())]
        self.assertIn(("image", b"qr"), allowed[0])
        self.assertIn("二维码已过期", allowed[-1])

    async def test_query_cooldown_does_not_delay_normal_api_requests(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        hero_field = schema["hero_mappings"]
        self.assertEqual(hero_field["type"], "template_list")
        self.assertEqual(hero_field["templates"]["hero"]["display_item"], "hero_id")
        defaults = {item["hero_id"]: item["hero_name"] for item in hero_field["default"]}
        self.assertEqual(defaults["30"], "万钧")
        self.assertEqual(defaults["70"], "南宫锦")
        self.assertEqual(defaults["73"], "叶修")
        self.assertEqual(schema["query_interval_seconds"]["default"], 4.0)
        self.assertEqual(schema["detail_interval_seconds"]["default"], 4.0)
        self.assertIn("发起查询", schema["query_interval_seconds"]["hint"])
        self.assertIn("互不影响", schema["detail_interval_seconds"]["hint"])
        plugin = self.main.NarakaPlugin(None, {"query_interval_seconds": 1,
                                              "detail_interval_seconds": 4})
        plugin._auth = {"cookies": {"test": "ok"}, "device_id": "test"}
        clock = [0.0]
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        class FakeResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                pass

            async def json(self, **kwargs):
                return {"status": "ok", "result": {}}

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

        with patch.object(self.main, "XiaoheiheLoginClient", lambda *args: types.SimpleNamespace(device_id="test")), \
             patch.object(self.main, "_sign_params", lambda *args: {}), \
             patch.object(self.main.time, "monotonic", lambda: clock[0]), \
             patch.object(self.main.asyncio, "sleep", fake_sleep):
            session = FakeSession()
            await plugin._post_raw("/game/yjwj/match/detail", {}, session)
            await plugin._post_raw("/game/yjwj/match/list", {}, session)
            await plugin._post_raw("/game/yjwj/match/detail", {}, session)
            self.assertEqual(plugin._start_query_cooldown(), 0)
            self.assertEqual(plugin._start_query_cooldown(), 1)
        self.assertEqual(waits, [4.0])

    async def test_summary_and_detail_commands_are_separate_and_mention_sender(self):
        plugin = self.main.NarakaPlugin(None, {"query_match_count": 2,
                                              "selected_match_count": 1,
                                              "query_interval_seconds": 0,
                                              "detail_interval_seconds": 0})
        rows = [
            {"match_id": "quick", "battle_tid": "6", "time": "200", "hero_id": "1"},
            {"match_id": "ranked", "battle_tid": "12", "time": "100", "hero_id": "1",
             "damage": 12000, "scene": "3", "rank": 2, "kill_times": 3},
        ]
        home = {"heroes": [{"hero_id": "1", "name": "万钧"}],
                "seasons": [{"key": "qianji", "value": "千机"}]}
        detail = {"rank": 2, "data": [{"desc": "振刀", "value": "2"},
                                         {"desc": "击杀", "value": "3"},
                                         {"desc": "总恢复", "value": "4.7k"}]}

        async def fake_post(path, params, session=None):
            if path.endswith("match/list"):
                return {"match_list": rows}
            if params.get("season") == "pre-01":
                return home
            return {"player_info": {"rating": "4934"}}

        plugin._post = AsyncMock(side_effect=fake_post)
        plugin._post_raw = AsyncMock(return_value={"status": "ok", "result": detail})

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with patch.object(self.main.aiohttp, "ClientSession", FakeSession):
            summary = [item async for item in plugin.query_records(
                FakeEvent(), TEST_ROLE_ID)]
            details = [item async for item in plugin.query_match_details(
                FakeEvent(), TEST_ROLE_ID)]
        self.assertEqual(len(summary), 1)  # Start notice defaults to off.
        self.assertEqual(summary[-1][0], ("at", "456"))
        self.assertIn("有效对局：1", summary[-1][1][1])
        self.assertIn("万钧  1场", summary[-1][1][1])
        self.assertNotIn("排名#", summary[-1][1][1])
        self.assertIn("英雄：万钧", details[-1][1][1])
        self.assertNotIn("排名#", details[-1][1][1])
        self.assertEqual(plugin._post_raw.await_count, 1)  # Shared detail cache.

    async def test_plain_and_slash_entry_dispatch_once(self):
        plugin = self.main.NarakaPlugin(None, {})
        plugin._query_records = lambda *args, **kwargs: _single_async_reply("done")
        plain = [item async for item in plugin.on_message(
            FakeEvent(message_str="详细查询 测试玩家 天人三排"))]
        slash = [item async for item in plugin.on_message(
            FakeEvent(message_str="/战绩查询 测试玩家"))]
        unrelated = [item async for item in plugin.on_message(
            FakeEvent(message_str="我想查战绩查询 测试玩家"))]
        self.assertEqual(plain, ["done"])
        self.assertEqual(slash, ["done"])
        self.assertEqual(unrelated, [])

    async def test_search_and_admin_logout_have_plain_message_entries(self):
        plugin = self.main.NarakaPlugin(None, {"query_interval_seconds": 0})
        plugin._search = AsyncMock(return_value=[{
            "role_name": "测试玩家", "role_id": TEST_ROLE_ID,
            "level": "100", "rank_score": "5000",
        }])
        searched = [item async for item in plugin.on_message(
            FakeEvent(message_str="永劫搜索 测试玩家"))]
        self.assertIn(TEST_ROLE_ID, searched[0][-1][1])

        plugin._auth = {"cookies": {"test": "ok"}}
        logged_out = [item async for item in plugin.on_admin_plain_message(
            FakeEvent(message_str="永劫退出"))]
        self.assertIn("已清除", logged_out[0])
        self.assertEqual(plugin._auth, {})
        self.assertEqual(plugin.on_admin_plain_message._required_permission, "admin")

        raw_login = [item async for item in plugin.on_message(
            FakeEvent(message_str="永劫登录"))]
        self.assertEqual(raw_login, [])

    async def test_plain_season_query_shows_all_stats_with_shared_notice(self):
        plugin = self.main.NarakaPlugin(None, {"enable_start_notice": True,
                                            "start_notice_method": "quote"})
        overview = [{"desc": f"指标{i}", "value": str(i)} for i in range(16)]
        overview.append({"desc": "未提供", "value": ""})
        matches = [{"battle_tid": "12", "rank": i, "kill_times": 0,
                    "damage": i * 100, "grade": "A"} for i in range(10)]

        async def fake_post(path, params):
            if params["season"] == "pre-01":
                return {"seasons": [{"key": "qianji", "value": "千机赛季"}]}
            return {"player_info": {"name": "测试玩家", "rating": "6852", "level": "无相龙王"},
                    "overview": overview, "matches": matches, "battle_tid": "12"}

        plugin._post = AsyncMock(side_effect=fake_post)
        outputs = [item async for item in plugin.on_message(
            FakeEvent(message_str=f"永劫赛季 {TEST_ROLE_ID} 天选双排"))]
        report = "\n".join(item[-1][1] for item in outputs)
        self.assertIn("测试玩家", report)
        self.assertIn("指标15：15", report)
        self.assertNotIn("近期对局", report)
        self.assertNotIn("排名 #9", report)
        self.assertNotIn("未提供", report)
        self.assertEqual(outputs[0][0], ("reply", "789"))
        self.assertEqual(outputs[1][0], ("at", "456"))
        self.assertEqual(plugin._post.await_count, 2)

    async def test_season_without_mode_uses_latest_ranked_match(self):
        plugin = self.main.NarakaPlugin(None, {"query_match_count": 2})

        async def fake_post(path, params, session=None):
            if path.endswith("match/list"):
                return {"match_list": [
                    {"match_id": "quick", "battle_tid": "6", "time": "200"},
                    {"match_id": "ranked", "battle_tid": "5", "time": "100"},
                ]}
            if params["season"] == "pre-01":
                return {"seasons": [{"key": "qianji", "value": "千机赛季"}]}
            return {"player_info": {"rating": "4934"}, "overview": []}

        plugin._post = AsyncMock(side_effect=fake_post)
        outputs = [item async for item in plugin.on_message(
            FakeEvent(message_str=f"永劫赛季 {TEST_ROLE_ID}"))]
        self.assertIn("天人三排", outputs[0][-1][1])
        self.assertEqual(plugin._post.call_args_list[1].args[1]["battle_tid"], "5")

    async def test_configured_hero_name_updates_both_reports_without_dropping_matches(self):
        plugin = self.main.NarakaPlugin(None, {"query_match_count": 2,
                                              "selected_match_count": 2,
                                              "query_interval_seconds": 0,
                                              "hero_mappings": [{"__template_key": "hero",
                                                                 "hero_id": "30", "hero_name": "自定义万钧"}],
                                              "detail_interval_seconds": 0})
        rows = [{"match_id": str(i), "battle_tid": "12", "time": str(200 - i),
                 "hero_id": hero, "damage": damage, "scene": "3"}
                for i, (hero, damage) in enumerate((("1000029", 10000),
                                                    ("1000030", 20000)))]

        async def fake_post(path, params, session=None):
            if path.endswith("match/list"):
                return {"match_list": rows}
            if params.get("season") == "pre-01":
                return {"heroes": [], "seasons": []}
            return {"player_info": {}}

        plugin._post = AsyncMock(side_effect=fake_post)
        plugin._post_raw = AsyncMock(return_value={"status": "ok", "result": {
            "data": [{"desc": "振刀", "value": "2"}, {"desc": "击杀", "value": "3"}]}})

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with patch.object(self.main.aiohttp, "ClientSession", FakeSession):
            summary = [item async for item in plugin.query_records(FakeEvent(), TEST_ROLE_ID)]
            details = [item async for item in plugin.query_match_details(FakeEvent(), TEST_ROLE_ID)]
        self.assertIn("有效对局：2", summary[-1][1][1])
        self.assertIn("1、万钧  1场", summary[-1][1][1])
        self.assertIn("2、自定义万钧  1场", summary[-1][1][1])
        self.assertIn("场均伤害：15000.0", summary[-1][1][1])
        self.assertIn("有效对局 2 场", details[-1][1][1])
        self.assertIn("英雄：自定义万钧", details[-1][1][1])

    async def test_notice_and_result_reply_methods(self):
        plugin = self.main.NarakaPlugin(None, {})
        event = FakeEvent()
        self.assertIsNone(await plugin._start_notice(event))
        plugin.config.update({"enable_start_notice": True,
                              "start_notice_method": "quote"})
        self.assertEqual((await plugin._start_notice(event))[0], ("reply", "789"))
        plugin.config["start_notice_method"] = "mention"
        self.assertEqual((await plugin._start_notice(event))[0], ("at", "456"))
        plugin.config["start_notice_method"] = "reaction"
        plugin.config["react_emoji_id"] = "123"
        self.assertIsNone(await plugin._start_notice(event))
        event.bot.api.call_action.assert_awaited_once_with(
            "set_msg_emoji_like", message_id="789", emoji_id="123")
        plugin.config["result_reply_method"] = "quote"
        self.assertEqual(plugin._reply(event, "完成")[0], ("reply", "789"))
        event.message_obj.message_id = ""
        self.assertEqual(plugin._reply(event, "完成")[0], ("at", "456"))
        event.message_obj.message_id = "789"
        plugin.config["result_reply_method"] = "mention"
        self.assertEqual(plugin._reply(event, "完成")[0], ("at", "456"))

        event.bot.api.call_action.reset_mock()
        event.bot.api.call_action.side_effect = RuntimeError("not supported")
        self.assertIsNone(await plugin._start_notice(event))

    async def test_pagination_stops_when_upstream_repeats_page(self):
        plugin = self.main.NarakaPlugin(None, {"query_match_count": 50})
        self.assertEqual(plugin._count_setting("query_match_count", 30), 50)

        async def fake_post(path, params, session):
            page = int(params["page"])
            first = [{"match_id": str(i), "time": str(1000 - i)} for i in range(30)]
            second = [{"match_id": str(i), "time": str(970 - i)} for i in range(30, 60)]
            return {"match_list": first if page == 1 else second if page == 2 else second}

        plugin._post = AsyncMock(side_effect=fake_post)
        rows = await plugin._fetch_recent_pool({}, 50, None)
        self.assertEqual(len(rows), 50)
        self.assertEqual(plugin._post.await_count, 2)
        more = await plugin._fetch_recent_pool({}, 70, None)
        self.assertEqual(len(more), 60)
        self.assertEqual(plugin._post.await_count, 5)  # Third page repeated.


async def _single_async_reply(value):
    yield value


if __name__ == "__main__":
    unittest.main()
