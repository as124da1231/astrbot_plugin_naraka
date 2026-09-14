import importlib.util
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "record_query.py"
spec = importlib.util.spec_from_file_location("record_query", MODULE)
query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query)


class RecordQueryTests(unittest.TestCase):
    def test_auto_mode_skips_quick_then_uses_latest_ranked_mode(self):
        source = {"match_list": [
            {"match_id": "old", "battle_tid": "12", "time": "100"},
            {"match_id": "quick", "battle_tid": "6", "time": "300"},
            {"match_id": "new", "battle_tid": "13", "time": "200"},
            {"match_id": "old", "battle_tid": "12", "time": "100"},
        ]}
        rows = query.recent_rows(source)
        self.assertEqual(len(rows), 3)
        self.assertEqual(query.select_matches(rows)[0], "13")
        self.assertEqual([r["match_id"] for r in query.select_matches(rows)[1]], ["new"])
        self.assertEqual(query.select_matches(rows, "快速单排")[1][0]["match_id"], "quick")
        self.assertEqual(len(query.recent_rows(source, 2)), 2)

    def test_selection_is_bounded_to_ten_within_returned_pool(self):
        rows = [{"match_id": str(i), "battle_tid": "12", "time": str(100 - i)} for i in range(18)]
        self.assertEqual(len(query.select_matches(rows, "天选双排")[1]), 10)
        self.assertEqual(len(query.select_matches(rows, "天选双排", 15)[1]), 15)
        self.assertEqual(query.select_matches(rows, "天人三排")[1], [])

    def test_detail_display_values_and_exact_list_damage(self):
        row = {"time": "1789315567", "rank": 5, "kill_times": 5, "damage": 19779, "hero_id": "1001"}
        detail = {"rank": 5, "rating_delta": 34, "data": [
            {"desc": "总伤害", "value": "19.8k"},
            {"desc": "总恢复", "value": "8.1k"},
            {"desc": "击杀", "value": "5"},
            {"desc": "振刀", "value": "2"},
            {"desc": "存活时长", "value": "19min"},
        ]}
        record = query.make_record(row, detail)
        self.assertEqual(record["damage"], 19779)
        self.assertEqual(record["recovery"], 8100)
        self.assertEqual(record["parry"], 2)
        self.assertEqual(record["survival_min"], 19)
        summary = query.format_summary("玩家", 18, "5000001", 10, [record] * 8,
                                       {"5000001": "4934"}, {"1001": "万钧"}, 2)
        self.assertIn("查询最近场次：18", summary)
        self.assertIn("有效对局：8", summary)
        self.assertIn("详情失败：2", summary)
        self.assertIn("万钧  8场", summary)
        self.assertIn("场均伤害：19779.0", summary)
        self.assertIn("排位：— — 4,934", summary)
        self.assertIn("天人：— — —", summary)
        self.assertNotIn("注：振刀等详情字段", summary)
        details = query.format_details("玩家", 18, "5000001", 10, [record], {"1001": "万钧"})
        self.assertIn("英雄：万钧", details)
        self.assertIn("振刀：2", details)
        self.assertNotIn("排名", details)
        self.assertNotIn("钩索", details)
        self.assertNotIn("魂玉", details)
        longer = query.format_details("玩家", 18, "5000001", 10, [record] * 3, {"1001": "万钧"})
        chunks = query.split_detail_report(longer, max_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(chunk.count("英雄：万钧") for chunk in chunks), 3)

    def test_missing_ranked_mode_and_changed_schema(self):
        self.assertEqual(query.select_matches([{"battle_tid": "7"}]), (None, []))
        with self.assertRaises(ValueError):
            query.recent_rows({"matches": []})

    def test_unknown_hero_keeps_match_and_contributes_to_averages(self):
        records = [
            {"hero_id": "1000029", "damage": 10000, "parry": 1, "kills": 2},
            {"hero_id": "1000030", "damage": 20000, "parry": 3, "kills": 4},
        ]
        summary = query.format_summary("玩家", 2, "12", 2, records,
                                       {}, query.KNOWN_HERO_NAMES)
        self.assertIn("有效对局：2", summary)
        self.assertIn("万钧  2场", summary)
        self.assertIn("场均伤害：15000.0", summary)
        self.assertIn("场均振刀：2.0", summary)
        details = query.format_details("玩家", 2, "12", 2, records,
                                       query.KNOWN_HERO_NAMES)
        self.assertIn("有效对局 2 场", details)
        self.assertIn("第2局｜英雄：万钧", details)
        self.assertEqual(query._hero_label("1000034", {}), "34")
        self.assertEqual(query._hero_label("1000001", {}), "01")
        self.assertEqual(query._hero_label("73", {}), "叶修")
        self.assertEqual(query._hero_label("1000073", {}), "叶修")
        self.assertEqual(query._hero_label("1000070", {}), "南宫锦")
        self.assertEqual(query._hero_label("", {}), "未知英雄")

    def test_editable_hero_mappings_override_catalog_and_support_full_ids(self):
        catalog = {"1000030": "接口名称", "1000070": "旧名称"}
        records = [{"hero_id": "1000030"}, {"hero_id": "1000070"}]
        entries = [
            {"__template_key": "hero", "hero_id": "30", "hero_name": "万钧"},
            {"__template_key": "hero", "hero_id": "1000070", "hero_name": "南宫锦"},
            {"__template_key": "hero", "hero_id": "", "hero_name": "无效"},
        ]
        names = query.apply_hero_mappings(catalog, records, entries)
        self.assertEqual(query._hero_label("1000030", names), "万钧")
        self.assertEqual(query._hero_label("1000070", names), "南宫锦")
        self.assertEqual(query._hero_label("1000099", names), "99")
        self.assertEqual(catalog["1000030"], "接口名称")


if __name__ == "__main__":
    unittest.main()
