import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.monthly_card_reset import (
    BEIJING_TZ,
    CONFIG_KEY_ENABLED,
    CONFIG_KEY_LAST_DATE,
    DEFAULT_CONFIG,
    MonthlyCardDailyResetScheduler,
    perform_double_escape_reset,
)


class FakeController:
    def __init__(self):
        self.events = []

    def release_all(self):
        self.events.append(("release_all",))

    def key_tap(self, key, duration=0.01):
        self.events.append(("key_tap", key, duration))


class FakeWindow:
    def __init__(self, alive=True, found=True, focus_ok=True, foreground=False):
        self.alive = alive
        self.found = found
        self.focus_ok = focus_ok
        self.foreground = foreground
        self.find_calls = 0
        self.focus_calls = 0

    def is_window_alive(self):
        return self.alive

    def find_window(self):
        self.find_calls += 1
        self.alive = self.found
        return self.found

    def set_foreground(self):
        self.focus_calls += 1
        self.foreground = self.focus_ok
        return self.focus_ok

    def is_foreground(self):
        return self.foreground


class FakeUserActivity:
    def __init__(self):
        self.notes = []

    def note_program_input(self, keys=(), duration=None):
        self.notes.append((tuple(keys), duration))


class MonthlyCardResetSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.scheduler = MonthlyCardDailyResetScheduler()

    def test_default_config_is_disabled_for_old_configs(self):
        merged = dict(DEFAULT_CONFIG)
        merged.update({"tracking_strength": 180})

        self.assertFalse(merged[CONFIG_KEY_ENABLED])
        self.assertEqual(merged[CONFIG_KEY_LAST_DATE], "")

    def test_disabled_switch_never_triggers(self):
        now = datetime(2026, 5, 2, 5, 2, 10, tzinfo=BEIJING_TZ)

        self.assertFalse(self.scheduler.should_trigger(False, "", now))

    def test_triggers_during_beijing_0502_minute_once_per_date(self):
        now = datetime(2026, 5, 2, 5, 2, 30, tzinfo=BEIJING_TZ)

        self.assertTrue(self.scheduler.should_trigger(True, "", now))
        self.assertEqual(self.scheduler.date_key(now), "2026-05-02")
        self.assertFalse(self.scheduler.should_trigger(True, "2026-05-02", now))

    def test_next_beijing_date_can_trigger_again(self):
        now = datetime(2026, 5, 3, 5, 2, 0, tzinfo=BEIJING_TZ)

        self.assertTrue(self.scheduler.should_trigger(True, "2026-05-02", now))

    def test_does_not_catch_up_after_0502_minute(self):
        now = datetime(2026, 5, 2, 5, 3, 0, tzinfo=BEIJING_TZ)

        self.assertFalse(self.scheduler.should_trigger(True, "", now))

    def test_utc_now_is_converted_to_beijing_time(self):
        now = datetime(2026, 5, 1, 21, 2, 5, tzinfo=timezone.utc)

        self.assertTrue(self.scheduler.should_trigger(True, "", now))
        self.assertEqual(self.scheduler.date_key(now), "2026-05-02")


class MonthlyCardResetActionTest(unittest.TestCase):
    def test_double_escape_sequence_uses_existing_controller_order(self):
        controller = FakeController()
        window = FakeWindow(alive=True, focus_ok=True)
        activity = FakeUserActivity()
        sleeps = []

        ok = perform_double_escape_reset(
            controller,
            window_manager=window,
            user_activity=activity,
            input_lock=threading.RLock(),
            delay_seconds=2.0,
            tap_duration=0.12,
            sleeper=sleeps.append,
        )

        self.assertTrue(ok)
        self.assertEqual(window.focus_calls, 1)
        self.assertEqual(
            controller.events,
            [
                ("release_all",),
                ("key_tap", "esc", 0.12),
                ("key_tap", "esc", 0.12),
                ("release_all",),
            ],
        )
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(activity.notes[0][0], ("esc",))
        self.assertGreaterEqual(activity.notes[0][1], 2.0)

    def test_missing_game_window_does_not_send_escape(self):
        controller = FakeController()
        window = FakeWindow(alive=False, found=False)

        ok = perform_double_escape_reset(controller, window_manager=window)

        self.assertFalse(ok)
        self.assertEqual(controller.events, [])
        self.assertEqual(window.find_calls, 1)


class AppWindowMonthlyCardResetConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_app_window_defaults_and_settings_include_monthly_card_switch(self):
        from gui import app as app_module

        original_config_file = app_module.CONFIG_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            app_module.CONFIG_FILE = str(Path(temp_dir, "config.json"))
            try:
                window = app_module.AppWindow()
                self.addCleanup(window.shutdown_background_tasks)
                self.addCleanup(window.close)

                self.assertFalse(window.config[CONFIG_KEY_ENABLED])
                self.assertEqual(window.config[CONFIG_KEY_LAST_DATE], "")
                self.assertIn(CONFIG_KEY_ENABLED, window._settings_snapshot_keys())
                widget_info = window._setting_widgets.get(CONFIG_KEY_ENABLED)
                self.assertIsNotNone(widget_info)
                self.assertEqual(widget_info["type"], "toggle")
                self.assertEqual(widget_info["widget"].text(), "已关闭")
            finally:
                app_module.CONFIG_FILE = original_config_file


if __name__ == "__main__":
    unittest.main()
