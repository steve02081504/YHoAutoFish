import unittest
import tempfile
import time
from pathlib import Path
from queue import Queue

import cv2
import numpy as np

from core.state_machine import StateMachine


class FakeRecordManager:
    def __init__(self):
        self.catches = []

    def add_catch(self, fish_name, weight_g, rarity=None):
        self.catches.append((fish_name, weight_g, rarity))


class SuccessAutoSellMachine(StateMachine):
    def __init__(self, threshold):
        super().__init__(config={"auto_sell_catch_threshold": threshold, "settlement_close_delay": 0.4})
        self.record_mgr = FakeRecordManager()
        self.esc_taps = []

    def _read_settlement_info(self, rect, save_unknown_debug=True):
        return "test fish", 123

    def _tap_key_if_running(self, key, duration=0.01):
        self.esc_taps.append((key, duration))
        return True

    def _wait_after_settlement_close(self, rect, max_delay):
        return False


class IdleAutoSellMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_sell_catch_threshold": 1})
        self.is_running = True
        self.sell_started = False
        self.cast_sent = False
        self.detect_calls = []

    def _detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        self.detect_calls.append(require_initial_controls)
        return {
            "kind": "ready",
            "confidence": 0.99,
            "location": (10, 10),
            "template": "ready.png",
            "strategy": "fake",
        }

    def _send_cast_input(self, ready_info, source_label):
        self.cast_sent = True
        return True

    def _start_auto_sell_flow(self, rect, ready_info):
        self.sell_started = True
        return True


class AutoSellExitMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_sell_catch_threshold": 1})
        self.is_running = True
        self.current_state = self.STATE_SELLING_CATCHES
        self._auto_sell_pending = True
        self._auto_sell_session_catch_count = 1
        self._auto_sell_started_at = 100.0
        self._auto_sell_step = "wait_after_confirm"
        self._auto_sell_step_started = 100.0
        self.clock = 102.1
        self.keys = []

    def _tap_key_if_running(self, key, duration=0.01):
        self.keys.append((key, duration))
        return True

    def _detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        return {
            "kind": "ready",
            "confidence": 0.99,
            "location": (1, 1),
        }


class AutoSellVisibilityMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_sell_catch_threshold": 1})
        self.is_running = True
        self.log_queue = Queue()
        self._auto_sell_pending = True
        self.keys = []

    def _tap_key_if_running(self, key, duration=0.01):
        self.keys.append((key, duration))
        return True


class AutoSellConfirmCapture:
    def __init__(self):
        self.last_roi = None

    def capture_relative(self, rect, *roi):
        self.last_roi = roi
        return object()

    def relative_rect(self, rect, *roi):
        return (
            rect[0] + int(rect[2] * roi[0]),
            rect[1] + int(rect[3] * roi[1]),
            int(rect[2] * roi[2]),
            int(rect[3] * roi[3]),
        )


class AutoSellConfirmVision:
    def __init__(self, capture):
        self.capture = capture
        self.rois = []

    def find_best_template_multi_strategy(self, image, templates, strategies, threshold=0.75, **kwargs):
        roi = self.capture.last_roi
        self.rois.append(roi)
        if roi and roi[1] < 0.50:
            return (10, 20), 0.96, "confirm.png", "fake-prompt-text"
        return (120, 45), 0.94, "confirm.png", "fake-button"


class AutoSellConfirmButtonMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_sell_catch_threshold": 1})
        self.is_running = True
        self.current_state = self.STATE_SELLING_CATCHES
        self._auto_sell_pending = True
        self._auto_sell_session_catch_count = 1
        self._auto_sell_started_at = time.time() - 2.0
        self._auto_sell_step = "confirm"
        self._auto_sell_step_started = time.time() - 1.0
        self.sc = AutoSellConfirmCapture()
        self.vis = AutoSellConfirmVision(self.sc)
        self.clicks = []

    def _auto_sell_confirm_templates(self):
        return ["confirm"]

    def _click_screen_point_if_running(self, x, y, duration=0.05):
        self.clicks.append((x, y, duration))
        return True


class AutoSellFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_success_record_marks_auto_sell_pending_when_threshold_reached(self):
        machine = SuccessAutoSellMachine(threshold=1)
        machine.is_running = True

        machine._finish_success_result(
            (0, 0, 1920, 1080),
            {"confidence": 0.95, "signals": []},
        )

        self.assertEqual(machine.record_mgr.catches, [("test fish", 123, None)])
        self.assertEqual(machine._auto_sell_session_catch_count, 1)
        self.assertTrue(machine._auto_sell_pending)

    def test_threshold_zero_does_not_mark_auto_sell_pending(self):
        machine = SuccessAutoSellMachine(threshold=0)
        machine.is_running = True

        machine._finish_success_result(
            (0, 0, 1920, 1080),
            {"confidence": 0.95, "signals": []},
        )

        self.assertEqual(machine._auto_sell_session_catch_count, 1)
        self.assertFalse(machine._auto_sell_pending)

    def test_threshold_is_clamped_to_999(self):
        machine = SuccessAutoSellMachine(threshold=5000)

        self.assertEqual(machine._auto_sell_threshold(), 999)

    def test_auto_sell_restarts_counting_after_successful_sale(self):
        machine = SuccessAutoSellMachine(threshold=2)
        machine._auto_sell_session_catch_count = 2
        machine._auto_sell_pending = True

        machine._finish_auto_sell_flow()
        machine._record_auto_sell_catch()

        self.assertEqual(machine._auto_sell_session_catch_count, 1)
        self.assertFalse(machine._auto_sell_pending)

        machine._record_auto_sell_catch()

        self.assertEqual(machine._auto_sell_session_catch_count, 2)
        self.assertTrue(machine._auto_sell_pending)

    def test_auto_sell_setting_slider_range_is_0_to_999(self):
        from gui import app as app_module

        original_config_file = app_module.CONFIG_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            app_module.CONFIG_FILE = str(Path(temp_dir, "config.json"))
            try:
                window = app_module.AppWindow()
                try:
                    slider = getattr(window, "slider_auto_sell_threshold", None)

                    self.assertIsNotNone(slider)
                    self.assertEqual(slider.minimum(), 0)
                    self.assertEqual(slider.maximum(), 999)
                finally:
                    window.shutdown_background_tasks()
                    window.close()
            finally:
                app_module.CONFIG_FILE = original_config_file

    def test_idle_auto_sell_preempts_cast_only_after_initial_controls_confirmed(self):
        machine = IdleAutoSellMachine()
        machine._auto_sell_pending = True

        machine._handle_idle((0, 0, 1920, 1080), (0.75, 0.75, 0.25, 0.25))

        self.assertTrue(machine.sell_started)
        self.assertFalse(machine.cast_sent)
        self.assertIn(True, machine.detect_calls)

    def test_auto_sell_exit_uses_two_esc_then_clears_pending_after_ready(self):
        machine = AutoSellExitMachine()

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        try:
            state_machine_module.time.time = lambda: machine.clock
            machine._handle_auto_sell((0, 0, 1920, 1080))

            machine.clock = 102.7
            machine._handle_auto_sell((0, 0, 1920, 1080))

            machine.clock = 102.8
            machine._handle_auto_sell((0, 0, 1920, 1080))
        finally:
            state_machine_module.time.time = original_time

        self.assertEqual(machine.keys, [("esc", 0.12), ("esc", 0.12)])
        self.assertEqual(machine.current_state, machine.STATE_IDLE)
        self.assertFalse(machine._auto_sell_pending)
        self.assertEqual(machine._auto_sell_session_catch_count, 0)

    def test_auto_sell_hides_only_floating_window_and_restores_it(self):
        machine = AutoSellVisibilityMachine()

        started = machine._start_auto_sell_flow(
            (0, 0, 1920, 1080),
            {"kind": "ready", "location": (1, 1)},
        )
        machine._finish_auto_sell_flow()

        commands = []
        while not machine.log_queue.empty():
            commands.append(machine.log_queue.get_nowait())

        self.assertTrue(started)
        self.assertIn("CMD_FLOATING_HIDE_FOR_CAPTURE", commands)
        self.assertIn("CMD_FLOATING_RESTORE_AFTER_CAPTURE", commands)
        self.assertNotIn("CMD_MAIN_HIDE_FOR_CAPTURE", commands)
        self.assertNotIn("CMD_MAIN_RESTORE_AFTER_CAPTURE", commands)

    def test_auto_sell_confirm_clicks_lower_button_not_prompt_text(self):
        machine = AutoSellConfirmButtonMachine()
        rect = (0, 0, 888, 420)

        machine._handle_auto_sell(rect)

        self.assertEqual(machine._auto_sell_step, "wait_after_confirm")
        self.assertEqual(len(machine.clicks), 1)
        self.assertGreaterEqual(machine.clicks[0][1], int(rect[3] * 0.68))
        self.assertTrue(machine.vis.rois)
        self.assertTrue(all(roi[1] >= 0.56 for roi in machine.vis.rois))

    def test_auto_sell_confirm_rois_cover_full_lower_right_button_template(self):
        machine = StateMachine(config={})
        template_path = machine._auto_sell_confirm_templates()[0]
        template = cv2.imdecode(np.fromfile(template_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertIsNotNone(template)
        template_width = int(template.shape[1])

        rois = machine._auto_sell_confirm_button_rois()

        self.assertTrue(rois)
        self.assertLessEqual(min(roi[1] for roi in rois), 0.60)
        self.assertTrue(all(roi[0] >= 0.43 for roi in rois))
        self.assertTrue(any(int(888 * roi[2]) >= template_width for roi in rois))


if __name__ == "__main__":
    unittest.main()
