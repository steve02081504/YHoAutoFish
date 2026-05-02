import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from core.state_machine import StateMachine
from core.vision import VisionCore


class FakeCapture:
    def capture_relative(self, rect, *roi):
        return np.zeros((360, 480, 3), dtype=np.uint8)


class FakeVision:
    def __init__(self, control_points):
        self.control_points = control_points

    def find_best_template_multi_strategy(self, image, templates, strategies, threshold=0.75, **kwargs):
        marker = templates[0] if templates else ""
        if marker == "F":
            return (420, 310), 0.86, "F", "fake"
        point = self.control_points.get(marker)
        if point is None:
            return None, 0.0, marker, "fake"
        return point, 0.66, marker, "fake"


class ReadyDetectMachine(StateMachine):
    def __init__(self, control_points):
        super().__init__(config={})
        self.sc = FakeCapture()
        self.vis = FakeVision(control_points)

    def _f_button_templates(self):
        return ["F"]

    def _initial_q_button_templates(self):
        return ["Q"]

    def _initial_e_button_templates(self):
        return ["E"]

    def _initial_r_button_templates(self):
        return ["R"]


class ResultConfirmMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"empty_ready_confirm_delay": 0.45})
        self.finished_empty = False

    def _detect_fast_success_result(self, rect, fast_only=False):
        return None

    def _detect_fast_failed_result(self, rect):
        return None

    def _detect_success_result(self, rect):
        return None

    def _detect_failed_result(self, rect):
        return None

    def _maybe_finish_failed_result(self, rect, failed_info, source_label="结算"):
        return False

    def _try_finish_success_by_settlement_probe(self, rect, source_label="结算"):
        return False

    def _finish_empty_ready_result(self, ready_info, source_label="结算"):
        self.finished_empty = True


class SettlementProbeMachine(StateMachine):
    def __init__(self):
        super().__init__(config={})
        self.finished = None

        class FakeRecordManager:
            def get_encyclopedia(self):
                return {"白锦鲤": {}}

        self.record_mgr = FakeRecordManager()

    def _read_settlement_info(self, rect, save_unknown_debug=True):
        return "白锦鲤", 1234

    def _finish_success_result(self, rect, success_info, attempt=1, max_attempts=1, source_label="结算", settlement_info=None):
        self.finished = settlement_info


class FastResultFirstMachine(StateMachine):
    def __init__(self):
        super().__init__(config={})
        self.is_running = True
        self.finished = False
        self.full_called = False

    def _detect_fast_success_result(self, rect, fast_only=False):
        return {"location": (1, 1), "confidence": 0.95, "signals": []}

    def _detect_success_result(self, rect):
        self.full_called = True
        return None

    def _finish_success_result(self, rect, success_info, attempt=1, max_attempts=1, source_label="结算", settlement_info=None):
        self.finished = True


class ResultTextProbeMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"result_detect_timeout": 6})
        self.is_running = True
        self._round_had_fishing_bar = True
        self.probe_called = False

    def _detect_fast_success_result(self, rect, fast_only=False):
        return None

    def _detect_fast_failed_result(self, rect):
        return None

    def _detect_success_result(self, rect):
        return None

    def _detect_failed_result(self, rect):
        return None

    def _maybe_finish_failed_result(self, rect, failed_info, source_label="结算"):
        return False

    def _detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        return None

    def _try_finish_success_by_settlement_probe(self, rect, source_label="结算"):
        self.probe_called = True
        return True

    def _sleep_interruptible(self, seconds, step=0.05):
        return True


class CloseRetryController:
    def __init__(self):
        self.keys = []

    def release_all(self):
        return None

    def key_tap(self, key, duration=0.01):
        self.keys.append((key, float(duration)))
        return True


class PendingSettlementCloseMachine(StateMachine):
    def __init__(self, settlement_visible=False):
        super().__init__(config={"settlement_close_delay": 0.4})
        self.is_running = True
        self.current_state = self.STATE_RESULT
        self.ctrl = CloseRetryController()
        self.settlement_visible = settlement_visible
        self._success_recorded_pending_close = True
        self._success_close_retry_count = 1
        self._success_close_last_esc = 100.0
        self.clock = 101.0

    def _detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        return None

    def _detect_fast_success_result(self, rect, fast_only=False):
        if self.settlement_visible:
            return {"location": (1, 1), "confidence": 0.95, "signals": []}
        return None

    def _detect_success_result(self, rect):
        return None

    def _wait_after_settlement_close(self, rect, max_delay):
        return False

    def _tap_key_if_running(self, key, duration=0.01):
        self.ctrl.key_tap(key, duration=duration)
        return True

    def _sleep_interruptible(self, seconds, step=0.05):
        return True


class ResolutionResultFlowTest(unittest.TestCase):
    def test_template_scale_builder_keeps_common_resolution_anchors(self):
        vision = VisionCore()
        scales_900p = vision._build_scales((0.52, 1.80), 11)
        scales_2k = vision._build_scales((0.82, 2.88), 11)
        scales_fast = vision._build_scales((0.82, 1.28), 3)

        self.assertTrue(any(abs(scale - 0.833) < 0.002 for scale in scales_900p))
        self.assertTrue(any(abs(scale - 1.333) < 0.002 for scale in scales_2k))
        self.assertLessEqual(len(scales_fast), 4)

    def test_rejects_same_line_initial_control_false_positive(self):
        machine = ReadyDetectMachine({"Q": (120, 120), "E": (185, 124), "R": (245, 126)})
        result = machine._detect_ready_to_cast((0, 0, 1600, 900), require_initial_controls=True)
        self.assertIsNotNone(result)
        self.assertIsNone(result.get("location"))

    def test_accepts_horizontal_initial_control_cluster(self):
        machine = ReadyDetectMachine({"Q": (70, 450), "E": (180, 445), "R": (300, 455)})
        result = machine._detect_ready_to_cast((0, 0, 1920, 1080), require_initial_controls=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("kind"), "钓鱼初始界面")
        self.assertIsNotNone(result.get("location"))

    def test_accepts_vertical_initial_control_cluster(self):
        machine = ReadyDetectMachine({"Q": (380, 120), "E": (384, 210), "R": (382, 300)})
        result = machine._detect_ready_to_cast((0, 0, 1600, 900), require_initial_controls=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("kind"), "钓鱼初始界面")
        self.assertIsNotNone(result.get("location"))

    def test_result_ready_requires_longer_stable_confirmation_after_fishing(self):
        machine = ResultConfirmMachine()
        machine._round_had_fishing_bar = True
        machine._result_full_check_last = time.time()
        ready_info = {"kind": "钓鱼初始界面", "location": (1, 1), "confidence": 0.9}
        machine._result_ready_seen_time = time.time() - 2.8
        machine._result_ready_last_kind = "钓鱼初始界面"
        machine._result_ready_confirm_count = 3

        handled = machine._confirm_empty_ready_result((0, 0, 1600, 900), ready_info)

        self.assertFalse(handled)
        self.assertFalse(machine.finished_empty)

    def test_settlement_text_probe_records_known_fish(self):
        machine = SettlementProbeMachine()

        handled = machine._try_finish_success_by_settlement_probe((0, 0, 1600, 900))

        self.assertTrue(handled)
        self.assertEqual(machine.finished, ("白锦鲤", 1234))

    def test_result_state_uses_fast_success_before_full_scan(self):
        machine = FastResultFirstMachine()

        machine._handle_result((0, 0, 1920, 1080))

        self.assertTrue(machine.finished)
        self.assertFalse(machine.full_called)

    def test_result_state_uses_text_probe_when_visual_signals_miss(self):
        machine = ResultTextProbeMachine()

        machine._handle_result((0, 0, 1920, 1080))

        self.assertTrue(machine.probe_called)

    def test_recorded_success_does_not_retry_esc_without_visible_settlement(self):
        machine = PendingSettlementCloseMachine(settlement_visible=False)

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            machine._handle_result((0, 0, 1920, 1080))
        finally:
            state_machine_module.time.time = original_time

        self.assertEqual(machine.ctrl.keys, [])
        self.assertEqual(machine.current_state, machine.STATE_IDLE)
        self.assertFalse(machine._success_recorded_pending_close)

    def test_recorded_success_retries_esc_only_when_settlement_still_visible(self):
        machine = PendingSettlementCloseMachine(settlement_visible=True)

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            machine._handle_result((0, 0, 1920, 1080))
        finally:
            state_machine_module.time.time = original_time

        self.assertEqual(machine.ctrl.keys, [("esc", 0.15)])
        self.assertEqual(machine.current_state, machine.STATE_RESULT)
        self.assertEqual(machine._success_close_retry_count, 2)

    def test_initial_fishing_screenshot_is_not_success_settlement(self):
        image_path = Path("debug_settlement_unknown_20260430_173200.png")
        if not image_path.exists():
            self.skipTest("缺少本地误判截图")

        image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)
        rect = (0, 0, image.shape[1], image.shape[0])

        class ScreenshotCapture:
            def capture_relative(self, rect, rx, ry, rw, rh):
                h, w = image.shape[:2]
                x1, y1 = int(w * rx), int(h * ry)
                x2, y2 = int(w * (rx + rw)), int(h * (ry + rh))
                return image[y1:y2, x1:x2].copy()

        machine = StateMachine(config={})
        machine.sc = ScreenshotCapture()

        start = time.perf_counter()
        self.assertTrue(machine._has_initial_fishing_ui(rect))
        initial_elapsed = time.perf_counter() - start
        self.assertLess(initial_elapsed, 5.0)

        start = time.perf_counter()
        self.assertIsNone(machine._detect_fast_success_result(rect, fast_only=True))
        fast_elapsed = time.perf_counter() - start
        self.assertLess(fast_elapsed, 2.0)

        self.assertIsNone(machine._detect_fast_success_result(rect, fast_only=False))

        start = time.perf_counter()
        self.assertIsNone(machine._detect_success_result(rect))
        full_elapsed = time.perf_counter() - start
        self.assertLess(full_elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
