"""Integration tests for full-history board snapshots and message delivery.

Run with: python -m unittest discover -s tests -p 'test_image_integration.py'
The AstrBot stubs are shared with the repository's original self-test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("guess_selftest", ROOT / "tests" / "selftest.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class FakeImage:
    def __init__(self, data):
        self.data = data

    @classmethod
    def fromBytes(cls, data):
        return cls(data)


class ImageIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guess-image-test-")
        helpers._install_stubs(Path(self.temp.name))
        sys.modules["astrbot.api.message_components"].Image = FakeImage
        # AstrBot loads plugins as packages; preserve that context for the
        # renderer's relative import instead of selftest's standalone import.
        package = types.ModuleType("guess_plugin_test")
        package.__path__ = [str(ROOT)]
        sys.modules[package.__name__] = package
        module_spec = importlib.util.spec_from_file_location("guess_plugin_test.main", ROOT / "main.py")
        self.m = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = self.m
        module_spec.loader.exec_module(self.m)
        self.plugin = self.m.GuessProPlugin(helpers._Context(), {"timeout_minutes": 0})
        self.db = self.plugin.dbs["cs"]
        self.theme = self.m.THEMES["cs"]
        self.event = helpers.FakeEvent(group="10086", user="9527")
        self.plugin._begin(self.event, "cs", "")
        self.key = self.m.SessionKey.game("10086", "9527", "cs")
        self.game = self.plugin.games[self.key]
        self.game.target = "ZywOo"
        self.first = self.db.get("s1mple")
        self.second = self.db.get("device")
        self.answer = self.db.get("ZywOo")
        self.assertTrue(all((self.first, self.second, self.answer)))

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, player):
        snapshot = {}
        text = self.plugin._apply_guess(self.game, player, snapshot=snapshot)
        return text, snapshot

    def test_history_persists_and_restores_with_full_comparisons(self):
        _, first = self.apply(self.first)
        _, second = self.apply(self.second)
        self.assertEqual([r["name"] for r in first["rows"]], ["s1mple"])
        self.assertEqual([r["name"] for r in second["rows"]], ["s1mple", "device"])
        expected = [(c.label, c.value, c.level) for c in self.theme.judge(self.first, self.answer)]
        actual = [(c["label"], c["value"], c["level"]) for c in second["rows"][0]["cells"]]
        self.assertEqual(actual, expected)
        self.assertEqual(len(first["rows"]), 1, "snapshot must not mutate on a later guess")
        restored = self.m.GuessProPlugin(helpers._Context(), {"timeout_minutes": 0})
        self.assertEqual(restored.games[self.key].board_history, self.game.board_history)

    def test_legacy_game_reconstructs_previous_guess(self):
        old = self.game.to_dict()
        old.pop("board_history", None)
        old["guessed"] = ["s1mple"]
        self.game = self.m.Game.from_dict(old)
        self.plugin.games[self.key] = self.game
        _, snapshot = self.apply(self.second)
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["s1mple", "device"])
        self.assertTrue(snapshot["rows"][0]["cells"])

    def test_legacy_missing_player_is_not_dropped_from_history(self):
        old = self.game.to_dict()
        old.pop("board_history", None)
        old["guessed"] = ["旧题库中已删除的选手"]
        self.game = self.m.Game.from_dict(old)
        self.plugin.games[self.key] = self.game
        _, snapshot = self.apply(self.second)
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["旧题库中已删除的选手", "device"])
        self.assertEqual(snapshot["rows"][0]["cells"][0]["level"], "unknown")

    def test_duplicate_guess_does_not_produce_snapshot(self):
        self.apply(self.first)
        text, snapshot = self.apply(self.first)
        self.assertIn("已经猜过", text)
        self.assertFalse(snapshot)
        self.assertEqual(len(self.game.guessed), 1)

    def test_hit_snapshot_survives_game_removal(self):
        self.apply(self.first)
        text, snapshot = self.apply(self.answer)
        self.assertIn("猜中了", text)
        self.assertNotIn(self.key, self.plugin.games)
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["s1mple", "ZywOo"])
        self.assertTrue(all(c["level"] == "correct" for c in snapshot["rows"][-1]["cells"]))

    def test_exhausted_attempts_keep_board_snapshot(self):
        self.game.attempts = 1
        text, snapshot = self.apply(self.first)
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["s1mple"])
        self.assertNotIn(self.key, self.plugin.games)

    def test_every_theme_produces_full_history_png(self):
        from guess_plugin_test.image_renderer import render_board_image

        for theme_key in ("cs", "football", "lol", "valorant"):
            with self.subTest(theme=theme_key):
                event = helpers.FakeEvent(group="theme-" + theme_key, user="one")
                self.plugin._begin(event, theme_key, "")
                key = self.m.SessionKey.game(event.get_group_id(), event.get_sender_id(), theme_key)
                game = self.plugin.games[key]
                db, theme = self.plugin.dbs[theme_key], self.m.THEMES[theme_key]
                target = db.players[0]
                game.target = theme.title_of(target)
                wrong = next(p for p in db.players[1:] if any(c.level != "correct" for c in theme.judge(p, target)))
                snapshot = {}
                self.plugin._apply_guess(game, wrong, snapshot=snapshot)
                self.assertEqual(len(snapshot["rows"][0]["cells"]), len(theme.judge(wrong, target)))
                png = render_board_image(snapshot["title"], snapshot["rows"], snapshot["status"])
                self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_bo3_round_reset_keeps_finished_round_snapshot(self):
        self.game.mode = "team"
        self.game.best_of = 3
        self.game.members = [{"id": "1", "name": "一号"}, {"id": "2", "name": "二号"}]
        self.game.turn = 0
        self.game.scores = {"1": 0, "2": 0}
        snap1 = {}
        self.plugin._apply_guess(self.game, self.first, "1", "一号", snapshot=snap1)
        snapshot = {}
        self.plugin._apply_guess(self.game, self.answer, "2", "二号", snapshot=snapshot)
        self.assertEqual(self.game.round_no, 2)
        self.assertEqual(self.game.guessed, [])
        self.assertEqual(self.game.board_history, [])
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["s1mple", "ZywOo"])

    def test_bo3_wrong_turn_does_not_mutate_or_generate_snapshot(self):
        self.game.mode = "team"
        self.game.best_of = 3
        self.game.members = [{"id": "1", "name": "一号"}, {"id": "2", "name": "二号"}]
        self.game.turn = 0
        snapshot = {}
        text = self.plugin._apply_guess(self.game, self.first, "2", "二号", snapshot=snapshot)
        self.assertIn("轮到", text)
        self.assertEqual(self.game.guessed, [])
        self.assertFalse(snapshot)

    def test_bo3_exhaustion_keeps_previous_round_snapshot(self):
        self.game.mode = "team"
        self.game.best_of = 3
        self.game.members = [{"id": "1", "name": "一号"}, {"id": "2", "name": "二号"}]
        self.game.scores = {"1": 0, "2": 0}
        self.game.attempts = 1
        snapshot = {}
        text = self.plugin._apply_guess(self.game, self.first, "1", "一号", snapshot=snapshot)
        self.assertEqual(self.game.round_no, 2)
        self.assertEqual([r["name"] for r in snapshot["rows"]], ["s1mple"])

    async def test_on_message_sends_png_and_rejects_duplicate(self):
        event = helpers.FakeEvent(text="s1mple")
        await self.plugin.on_message(event)
        self.assertEqual(len(event.results), 1)
        kind, chain = event.results[0]
        self.assertEqual(kind, "chain")
        images = [part for part in chain if isinstance(part, FakeImage)]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn("🟥国家", helpers._flatten(event.results[0]))
        import guess_plugin_test.image_renderer as renderer
        with patch.object(renderer, "render_board_image", wraps=renderer.render_board_image) as draw:
            following = helpers.FakeEvent(text="device")
            await self.plugin.on_message(following)
        self.assertEqual([r["name"] for r in draw.call_args.args[1]], ["s1mple", "device"])
        self.assertTrue(any(isinstance(part, FakeImage) for part in following.results[0][1]))
        duplicate = helpers.FakeEvent(text="s1mple")
        await self.plugin.on_message(duplicate)
        self.assertIn("已经猜过", helpers._flatten(duplicate.results[0]))
        self.assertFalse(any(isinstance(part, FakeImage) for part in duplicate.results[0][1]))

    async def test_render_failure_falls_back_without_repeating_guess(self):
        import guess_plugin_test.image_renderer as renderer
        with patch.object(renderer, "render_board_image", side_effect=RuntimeError("render failed")):
            event = helpers.FakeEvent(text="s1mple")
            await self.plugin.on_message(event)
        self.assertEqual(len(event.results), 1)
        self.assertEqual(self.game.guessed, ["s1mple"])
        self.assertIn("第 1/8 猜", helpers._flatten(event.results[0]))
        self.assertFalse(any(isinstance(part, FakeImage) for part in event.results[0][1]))

    async def test_send_failure_is_not_retried(self):
        event = helpers.FakeEvent(text="s1mple")
        with patch.object(event, "send", side_effect=RuntimeError("send uncertain")) as send:
            with self.assertRaisesRegex(RuntimeError, "send uncertain"):
                await self.plugin.on_message(event)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.game.guessed, ["s1mple"])

    async def test_queued_guess_does_not_reopen_finished_game(self):
        winner = helpers.FakeEvent(text="ZywOo")
        late = helpers.FakeEvent(text="s1mple")
        async with self.plugin._lock:
            tasks = [asyncio.create_task(self.plugin.on_message(event)) for event in (winner, late)]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        self.assertEqual(len(winner.results), 1)
        self.assertEqual(late.results, [])
        self.assertEqual(self.game.guessed, ["ZywOo"])
        self.assertNotIn(self.key, self.plugin.games)


if __name__ == "__main__":
    unittest.main()
