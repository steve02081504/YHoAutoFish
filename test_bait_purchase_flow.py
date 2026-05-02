import unittest
import tempfile
import queue
from pathlib import Path

import numpy as np

from core.state_machine import StateMachine
from core.user_activity_monitor import UserActivityMonitor
from core.vision import VisionCore


class FakeCapture:
    def __init__(self, shape=(820, 360, 3)):
        self.image = np.zeros(shape, dtype=np.uint8)

    def capture_relative(self, rect, *roi):
        return self.image.copy()


class RelativeImageCapture:
    def __init__(self, image):
        self.image = image

    def capture_relative(self, rect, rx, ry, rw, rh):
        height, width = self.image.shape[:2]
        x1 = max(0, min(width, int(round(width * float(rx)))))
        y1 = max(0, min(height, int(round(height * float(ry)))))
        x2 = max(0, min(width, int(round(width * float(rx + rw)))))
        y2 = max(0, min(height, int(round(height * float(ry + rh)))))
        return self.image[y1:y2, x1:x2].copy()


class FakeVision:
    def __init__(self, matches):
        self.matches = matches

    def find_template_matches(self, *args, **kwargs):
        return list(self.matches)


class FakeController:
    def __init__(self):
        self.clicked = None
        self.released = False

    def release_all(self):
        self.released = True

    def mouse_click(self, x, y, duration=0.05):
        self.clicked = (int(x), int(y), float(duration))
        return True


class FakeWindow:
    def __init__(self):
        self.foreground = False

    def set_foreground(self):
        self.foreground = True
        return True


class BaitItemMachine(StateMachine):
    def __init__(self, matches=None, name_reads=None):
        super().__init__(config={})
        self.sc = FakeCapture()
        self.vis = FakeVision(matches or [
            {"location": (80, 250), "confidence": 0.91, "template": "currency"},
            {"location": (190, 250), "confidence": 0.89, "template": "currency"},
        ])
        self.name_reads = list(name_reads) if name_reads is not None else [["茶谷饵"], ["万能鱼饵"]]

    def _unlimited_bait_currency_templates(self):
        return ["currency"]

    def _unlimited_bait_full_item_templates(self):
        return []

    def _read_bait_item_name_texts(self, image):
        if not self.name_reads:
            return []
        value = self.name_reads.pop(0)
        return [(value[0], 0.95)]


class DirectBaitItemMachine(StateMachine):
    def __init__(self, full_matches=None, currency_matches=None, name_reads=None, debug_dir=None):
        super().__init__(config={})
        self.sc = FakeCapture()
        self.full_matches = list(full_matches or [])
        self.currency_matches = list(currency_matches or [])
        self.name_reads = list(name_reads or [])
        self.debug_dir = Path(debug_dir) if debug_dir else None

    def _find_unlimited_bait_full_item_matches(self, shop_img, rect):
        return list(self.full_matches)

    def _find_unlimited_bait_currency_matches(self, shop_img, rect):
        return list(self.currency_matches)

    def _read_bait_item_name_texts(self, image):
        if not self.name_reads:
            return []
        value = self.name_reads.pop(0)
        return [(value[0], 0.95)]

    def _debug_output_dir(self):
        return self.debug_dir or super()._debug_output_dir()


class CastShortageMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_buy_bait_amount": 198})
        self.is_running = True
        self.current_state = self.STATE_WAITING
        self._last_cast_time = 100.0
        self._waiting_start_time = 100.0
        self.clock = 100.0
        self.check_times = []

        class FakeWindow:
            def get_client_rect(inner_self):
                return None

        self.wm = FakeWindow()

    def _should_stop(self):
        return False

    def _sleep_interruptible(self, seconds, step=0.05):
        self.clock += float(seconds)
        return True

    def _detect_bait_shortage_prompt(self, rect, *args, **kwargs):
        self.check_times.append(self.clock)
        if self.clock >= 100.30:
            return {"text": "需要装备鱼饵才可以钓鱼"}
        return None

    def _bait_shortage_context_allows_purchase(self, rect):
        return True


class BannerShortageMachine(StateMachine):
    def __init__(self, image, text_info=None, text_candidates=None):
        super().__init__(config={"auto_buy_bait_amount": 99})
        self.sc = FakeCapture(shape=image.shape)
        self.sc.image = image
        self.ocr_term_checks = 0
        self.text_info = text_info
        self.text_candidates = text_candidates

    def _read_text_candidates_from_image(self, *args, **kwargs):
        self.ocr_term_checks += 1
        if self.text_candidates is not None:
            return list(self.text_candidates)
        if not self.text_info:
            return []
        text = self.text_info.get("text", "")
        return [(text, float(self.text_info.get("score", 0.90)))]


class BannerVisualFallbackMachine(BannerShortageMachine):
    def _detect_initial_control_cluster(self, rect):
        return {"count": 3, "matches": [], "confidence": 0.92, "valid": True}


class CastVisualFallbackMachine(CastShortageMachine):
    def _detect_bait_shortage_prompt(self, rect, *args, **kwargs):
        self.check_times.append((self.clock, dict(kwargs)))
        if self.clock >= 100.30 and kwargs.get("allow_visual_fallback"):
            return {"source": "banner-visual"}
        return None


class NoHookVision:
    def find_best_template(self, *args, **kwargs):
        return None, 0.0, None


class WaitingRecastVisualShortageMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_buy_bait_amount": 99, "cast_retry_delay": 6})
        self.is_running = True
        self.current_state = self.STATE_WAITING
        self.clock = 110.0
        self._last_cast_time = 100.0
        self._waiting_start_time = 100.0
        self._waiting_ready_recheck_last = 0.0
        self.sc = FakeCapture()
        self.vis = NoHookVision()
        self.recast_sent = False

    def _should_stop(self):
        return False

    def _sleep_interruptible(self, seconds, step=0.05):
        return True

    def _detect_ready_to_cast(self, *args, **kwargs):
        return {"kind": "钓鱼初始界面组合控件", "location": (120, 80), "confidence": 0.98}

    def _detect_bait_shortage_prompt(self, rect, *args, **kwargs):
        if kwargs.get("allow_visual_fallback"):
            return {"source": "banner-visual"}
        return None

    def _bait_shortage_context_allows_purchase(self, rect):
        return True

    def _send_cast_input(self, ready_info, source_label):
        self.recast_sent = True
        return True


class RealTemplateBaitItemMachine(StateMachine):
    def __init__(self, image, template_path, template_kind, currency_template_path=None, name_reads=None):
        super().__init__(config={})
        self.sc = FakeCapture(shape=image.shape)
        self.sc.image = image
        self.template_path = str(template_path)
        self.template_kind = template_kind
        self.currency_template_path = str(currency_template_path) if currency_template_path else None
        self.name_reads = list(name_reads or [])

    def _unlimited_bait_currency_templates(self):
        if self.template_kind == "currency":
            return [self.template_path]
        return [self.currency_template_path] if self.currency_template_path else []

    def _unlimited_bait_full_item_templates(self):
        return [self.template_path] if self.template_kind == "full" else []

    def _read_bait_item_name_texts(self, image):
        if not self.name_reads:
            return []
        value = self.name_reads.pop(0)
        return [(value, 0.95)]



class DetailCostMachine(StateMachine):
    def __init__(self, image, template_path):
        super().__init__(config={})
        self.sc = FakeCapture(shape=image.shape)
        self.sc.image = image
        self.template_path = str(template_path)

    def _unlimited_bait_currency_templates(self):
        return [self.template_path]


class DetailOcrMachine(DetailCostMachine):
    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return {"text": "适合新手使用的鱼饵任何钓鱼点特殊效果", "candidates": []}


class DetailIdentityMachine(StateMachine):
    def __init__(self, cost_info=None, text_info=None):
        super().__init__(config={})
        self.cost_info = cost_info
        self.text_info = text_info

    def _detect_bait_detail_cost_marker(self, rect, *args, **kwargs):
        return self.cost_info

    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return self.text_info or {"text": "", "candidates": []}


class DetailDebugCropCapture:
    def __init__(self, image):
        self.image = image
        self.base_roi = (0.60, 0.10, 0.40, 0.88)

    def capture_relative(self, rect, rx, ry, rw, rh):
        height, width = self.image.shape[:2]
        base_x, base_y, base_w, base_h = self.base_roi
        x1_ratio = max(float(rx), base_x)
        y1_ratio = max(float(ry), base_y)
        x2_ratio = min(float(rx) + float(rw), base_x + base_w)
        y2_ratio = min(float(ry) + float(rh), base_y + base_h)
        if x2_ratio <= x1_ratio or y2_ratio <= y1_ratio:
            return np.zeros((1, 1, 3), dtype=np.uint8)
        x1 = max(0, min(width, int(round((x1_ratio - base_x) / base_w * width))))
        y1 = max(0, min(height, int(round((y1_ratio - base_y) / base_h * height))))
        x2 = max(0, min(width, int(round((x2_ratio - base_x) / base_w * width))))
        y2 = max(0, min(height, int(round((y2_ratio - base_y) / base_h * height))))
        return self.image[y1:y2, x1:x2].copy()


class DetailDebugCropMachine(StateMachine):
    def __init__(self, image, text):
        super().__init__(config={})
        self.sc = DetailDebugCropCapture(image)
        self.text = text

    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return {"text": self.text, "candidates": []}


class ConfirmDialogMachine(StateMachine):
    def __init__(self, image, text_info=None):
        super().__init__(config={})
        self.sc = FakeCapture(shape=image.shape)
        self.sc.image = image
        self.text_info = text_info

    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return self.text_info


class ShopExitReadyMachine(StateMachine):
    def __init__(self):
        super().__init__(config={})
        self.clock = 0.0
        self.ready_probe_calls = 0

        class Window:
            def get_client_rect(inner_self):
                return (0, 0, 1920, 1080)

        self.wm = Window()

    def _should_stop(self):
        return False

    def _sleep_interruptible(self, seconds, step=0.05):
        self.clock += float(seconds)
        return True

    def _detect_ready_to_cast(self, *args, **kwargs):
        self.ready_probe_calls += 1
        if self.ready_probe_calls < 3:
            return {
                "kind": "partial",
                "confidence": 0.96,
                "location": None,
                "initial_controls": {"count": 1, "valid": False},
            }
        return {
            "kind": "ready",
            "confidence": 0.98,
            "location": (120, 80),
            "initial_controls": {"count": 2, "valid": True},
        }


class BuyingBaitHandleMachine(StateMachine):
    def __init__(self, exit_shop_sent):
        super().__init__(config={})
        self.is_running = True
        self.current_state = self.STATE_BUYING_BAIT
        self.exit_shop_sent = exit_shop_sent
        self.recovery_press_esc = None

    def _should_stop(self):
        return False

    def _run_bait_purchase_flow(self, rect):
        self._bait_purchase_exit_shop_sent = self.exit_shop_sent
        return False

    def _enter_recovering(self, reason, record_empty=False, press_esc=False):
        self.recovery_press_esc = press_esc


class CachedWrongDetailFlowMachine(StateMachine):
    def __init__(self):
        super().__init__(config={"auto_buy_bait_amount": 99})
        self.is_running = True
        self._bait_purchase_batches_target = 1
        self.ctrl = FakeController()
        self.clicked_labels = []
        self.keys = []

        class Window:
            def get_client_rect(inner_self):
                return (0, 0, 1920, 1080)

        self.wm = Window()

    def _should_stop(self):
        return False

    def _tap_key_if_running(self, key, duration=0.1):
        self.keys.append(key)
        return True

    def _sleep_interruptible(self, seconds, step=0.05):
        return True

    def _wait_for_bait_condition(self, rect, predicate, timeout=5.0, interval=0.25):
        return predicate(rect)

    def _detect_unlimited_bait_item(self, rect):
        return {"click_ratio": (0.22, 0.32), "source": "name+currency", "confidence": 0.92}

    def _detect_bait_detail_cost_marker(self, rect, *args, **kwargs):
        return {"source": "detail-cost", "confidence": 0.91}

    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return {"text": "茶谷饵使用后可以吸引不同鱼类", "candidates": []}

    def _click_client_ratio(self, rect, rx, ry, label="界面按钮"):
        self.clicked_labels.append(label)
        return True


class SelectedDetailFlowMachine(CachedWrongDetailFlowMachine):
    def __init__(self):
        super().__init__()
        self.item_detect_called = False

    def _detect_unlimited_bait_item(self, rect):
        self.item_detect_called = True
        return None

    def _detect_text_terms_in_rois(self, *args, **kwargs):
        return {"text": "万能鱼饵适合新手使用的鱼饵在任何钓鱼点都可以使用", "candidates": []}

    def _click_template_in_rois(self, rect, templates, rois, label, threshold=0.66):
        self.clicked_labels.append(label)
        return True

    def _detect_bait_confirm_dialog(self, rect):
        return {"confirm_click_ratio": (0.60, 0.69)}

    def _detect_bait_reward_popup(self, rect):
        return {"source": "reward"}

    def _detect_bait_purchase_exit_ready(self, rect):
        return {"location": (120, 80)}


class VerifiedClickedNoisyDetailFlowMachine(CachedWrongDetailFlowMachine):
    def _detect_unlimited_bait_item(self, rect):
        return {
            "click_ratio": (0.22, 0.32),
            "source": "full+name+currency",
            "confidence": 0.98,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

    def _click_template_in_rois(self, rect, templates, rois, label, threshold=0.66):
        self.clicked_labels.append(label)
        return True

    def _detect_bait_confirm_dialog(self, rect):
        return {"confirm_click_ratio": (0.60, 0.69)}

    def _detect_bait_reward_popup(self, rect):
        return {"source": "reward"}

    def _detect_bait_purchase_exit_ready(self, rect):
        return {"location": (120, 80)}


class BaitPurchaseFlowTest(unittest.TestCase):
    def test_bait_purchase_amount_is_limited_to_99_multiples(self):
        machine = StateMachine(config={"auto_buy_bait_amount": 100})
        self.assertEqual(machine._normalized_auto_buy_bait_amount(), 99)
        self.assertEqual(machine._bait_purchase_batch_count(), 1)

        machine.config["auto_buy_bait_amount"] = 9999
        self.assertEqual(machine._normalized_auto_buy_bait_amount(), 9999)
        self.assertEqual(machine._bait_purchase_batch_count(), 101)

        machine.config["auto_buy_bait_amount"] = 10000
        self.assertEqual(machine._normalized_auto_buy_bait_amount(), 9999)

    def test_program_mouse_input_is_excluded_from_takeover_detection(self):
        monitor = UserActivityMonitor(enabled=False)
        self.assertEqual(monitor._vk_for_key("mouse_left"), 0x01)

    def test_client_ratio_click_uses_window_rect_screen_position(self):
        machine = StateMachine(config={})
        machine.is_running = True
        machine.ctrl = FakeController()
        machine.wm = FakeWindow()

        clicked = machine._click_client_ratio((10, 20, 100, 200), 0.50, 0.25, label="测试按钮")

        self.assertTrue(clicked)
        self.assertTrue(machine.wm.foreground)
        self.assertEqual(machine.ctrl.clicked[:2], (60, 70))

    def test_detect_unlimited_bait_item_requires_name_and_currency_in_same_card(self):
        machine = BaitItemMachine()
        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertIn("万能", result.get("text", ""))
        self.assertEqual(result.get("source"), "name+currency")
        click_ratio = result.get("click_ratio")
        self.assertIsNotNone(click_ratio)
        self.assertGreater(click_ratio[0], 0.00)
        self.assertLess(click_ratio[0], 0.42)

    def test_detect_unlimited_bait_item_rejects_strong_currency_when_name_ocr_empty(self):
        machine = BaitItemMachine(
            matches=[{"location": (190, 250), "confidence": 0.88, "template": "currency", "size": (51, 28)}],
            name_reads=[],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_rejects_weak_currency_without_name_ocr(self):
        machine = BaitItemMachine(
            matches=[{"location": (190, 250), "confidence": 0.76, "template": "currency", "size": (51, 28)}],
            name_reads=[],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_rejects_conflicting_item_name(self):
        machine = BaitItemMachine(
            matches=[{"location": (190, 250), "confidence": 0.92, "template": "currency", "size": (51, 28)}],
            name_reads=[["茶谷饵"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_rejects_full_template_without_currency_confirmation(self):
        machine = DirectBaitItemMachine(
            full_matches=[{"location": (190, 180), "confidence": 0.92, "template": "full", "size": (160, 210)}],
            currency_matches=[],
            name_reads=[["万能鱼饵"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_accepts_full_template_only_with_same_card_currency_and_name(self):
        machine = DirectBaitItemMachine(
            full_matches=[{"location": (190, 180), "confidence": 0.94, "template": "full", "size": (160, 210)}],
            currency_matches=[{"location": (190, 250), "confidence": 0.90, "template": "currency", "size": (51, 28)}],
            name_reads=[["万能鱼饵"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "full+name+currency")
        self.assertIn("万能", result.get("text", ""))

    def test_detect_unlimited_bait_item_rejects_full_template_with_conflicting_name(self):
        machine = DirectBaitItemMachine(
            full_matches=[{"location": (190, 180), "confidence": 0.94, "template": "full", "size": (160, 210)}],
            currency_matches=[{"location": (190, 250), "confidence": 0.90, "template": "currency", "size": (51, 28)}],
            name_reads=[["茶谷饵"], ["茶谷饵"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_accepts_exact_full_and_currency_when_name_ocr_misses(self):
        machine = DirectBaitItemMachine(
            full_matches=[{"location": (190, 180), "confidence": 0.99, "template": "full", "size": (160, 210)}],
            currency_matches=[{"location": (190, 250), "confidence": 0.98, "template": "currency", "size": (51, 28)}],
            name_reads=[["LCTLCTLCT"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "full+name+currency")
        self.assertEqual(result.get("text"), "visual-full-card")
        self.assertTrue(result.get("visual_card_confirmed"))

    def test_detect_unlimited_bait_item_rejects_noisy_name_without_exact_full_card(self):
        machine = DirectBaitItemMachine(
            full_matches=[{"location": (190, 180), "confidence": 0.95, "template": "full", "size": (160, 210)}],
            currency_matches=[{"location": (190, 250), "confidence": 0.98, "template": "currency", "size": (51, 28)}],
            name_reads=[["LCTLCTLCT"]],
        )

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_bait_shop_candidate_debug_is_not_saved_when_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            machine = DirectBaitItemMachine(
                full_matches=[{"location": (190, 180), "confidence": 0.94, "template": "full", "size": (160, 210)}],
                currency_matches=[{"location": (190, 250), "confidence": 0.90, "template": "currency", "size": (51, 28)}],
                name_reads=[["茶谷饵"], ["茶谷饵"]],
                debug_dir=tmp_dir,
            )
            machine.config["bait_shop_debug_mode"] = False

            result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))
            debug_paths = machine._save_bait_shop_debug_snapshot((0, 0, 1920, 1080), reason="unit_test")

            self.assertIsNone(result)
            self.assertIsNone(debug_paths)
            self.assertEqual(list(Path(tmp_dir).iterdir()), [])

    def test_bait_shop_candidate_debug_saves_image_and_details_when_switch_is_on(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            machine = DirectBaitItemMachine(
                full_matches=[{"location": (190, 180), "confidence": 0.94, "template": "full", "size": (160, 210)}],
                currency_matches=[{"location": (190, 250), "confidence": 0.90, "template": "currency", "size": (51, 28)}],
                name_reads=[["茶谷饵"], ["茶谷饵"]],
                debug_dir=tmp_dir,
            )
            machine.config["bait_shop_debug_mode"] = True

            result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))
            debug_paths = machine._save_bait_shop_debug_snapshot((0, 0, 1920, 1080), reason="unit_test")

            self.assertIsNone(result)
            self.assertIsNotNone(debug_paths)
            image_path = Path(debug_paths["image"])
            details_path = Path(debug_paths["details"])
            self.assertTrue(image_path.exists())
            self.assertTrue(details_path.exists())
            details = details_path.read_text(encoding="utf-8")
            self.assertIn("reason=unit_test", details)
            self.assertIn("full_candidate_count=1", details)
            self.assertIn("currency_candidate_count=1", details)
            self.assertIn("verification_records", details)
            self.assertIn("conflicting=True", details)

    def test_detect_unlimited_bait_item_rejects_actual_currency_template_without_name(self):
        import cv2
        from pathlib import Path

        template_path = next(Path("assets").glob("*货币图标.png"))
        template = cv2.imdecode(np.fromfile(str(template_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(template)

        image = np.full((760, 760, 3), 152, dtype=np.uint8)
        y, x = 330, 300
        h, w = template.shape[:2]
        image[y:y + h, x:x + w] = template
        machine = RealTemplateBaitItemMachine(image, template_path, "currency")

        result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_detect_unlimited_bait_item_finds_actual_full_item_template(self):
        import cv2
        from pathlib import Path

        template_path = next(Path("assets").glob("*完整图标.png"))
        currency_template_path = next(Path("assets").glob("*货币图标.png"))
        template = cv2.imdecode(np.fromfile(str(template_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(template)

        for scale in (0.75, 1.0, 1.35):
            with self.subTest(scale=scale):
                h, w = template.shape[:2]
                scaled = cv2.resize(template, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)
                image = np.full((760, 760, 3), 152, dtype=np.uint8)
                y, x = 260, 260
                sh, sw = scaled.shape[:2]
                image[y:y + sh, x:x + sw] = scaled
                machine = RealTemplateBaitItemMachine(
                    image,
                    template_path,
                    "full",
                    currency_template_path=currency_template_path,
                    name_reads=["万能鱼饵"],
                )

                result = machine._detect_unlimited_bait_item((0, 0, 1920, 1080))

                self.assertIsNotNone(result)
                self.assertEqual(result.get("source"), "full+name+currency")
                self.assertGreaterEqual(result.get("confidence", 0.0), 0.58)

    def test_detect_unlimited_bait_item_finds_sub1080_and_2k_shop_screenshots(self):
        import cv2

        samples = [
            Path("debug_bait_shop_fullscreen_20260501_040341.png"),
            Path("debug_bait_shop_fullscreen_20260501_040355.png"),
            Path("debug_bait_shop_fullscreen_20260501_040408.png"),
            Path("debug_bait_shop_fullscreen_20260501_040611.png"),
        ]
        existing = [path for path in samples if path.exists()]
        if not existing:
            self.skipTest("缺少本地鱼饵商店回归截图")

        for image_path in existing:
            with self.subTest(image=image_path.name):
                image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
                self.assertIsNotNone(image)
                machine = StateMachine(config={})
                machine.sc = RelativeImageCapture(image)
                machine._read_bait_item_name_texts = lambda _image: [("noise", 0.20)]

                result = machine._detect_unlimited_bait_item((0, 0, image.shape[1], image.shape[0]))

                self.assertIsNotNone(result)
                self.assertEqual(result.get("source"), "full+name+currency")
                self.assertTrue(result.get("visual_card_confirmed"))
                self.assertEqual(result.get("visual_confirm_reason"), "full-gray-same-card-currency")
                self.assertEqual(result.get("text"), "visual-full-card")
                click_ratio = result.get("click_ratio")
                self.assertIsNotNone(click_ratio)
                self.assertGreater(click_ratio[0], 0.15)
                self.assertLess(click_ratio[0], 0.22)
                self.assertGreater(click_ratio[1], 0.18)
                self.assertLess(click_ratio[1], 0.27)

    def test_bait_detail_cost_marker_accepts_actual_template(self):
        import cv2
        from pathlib import Path

        template_path = next(Path("assets").glob("*货币图标.png"))
        template = cv2.imdecode(np.fromfile(str(template_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(template)

        image = np.full((140, 260, 3), 44, dtype=np.uint8)
        y, x = 56, 116
        h, w = template.shape[:2]
        image[y:y + h, x:x + w] = template
        machine = DetailCostMachine(image, template_path)

        result = machine._detect_bait_detail_cost_marker((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-cost")

    def test_bait_detail_ready_requires_cost_and_identity(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.88},
            text_info={"text": "万能鱼饵适合新手使用任何钓鱼点特殊效果", "candidates": []},
        )

        result = machine._detect_bait_detail_ready((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified")

    def test_bait_detail_ready_rejects_cached_wrong_item_detail(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.88},
            text_info={"text": "茶谷饵使用后可以吸引不同鱼类", "candidates": []},
        )

        result = machine._detect_bait_detail_ready((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_bait_detail_ready_rejects_identity_without_unlimited_cost(self):
        machine = DetailIdentityMachine(
            cost_info=None,
            text_info={"text": "万能鱼饵适合新手使用任何钓鱼点特殊效果", "candidates": []},
        )

        result = machine._detect_bait_detail_ready((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_bait_detail_after_verified_click_accepts_cost_when_text_ocr_is_noisy(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.94},
            text_info={"text": "AM大M4学业路T氏", "candidates": []},
        )
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.98,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 1920, 1080), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_verified_click_accepts_scaled_card_confidence(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.96},
            text_info={"text": "a大大m送4送4AMNA4大", "candidates": []},
        )
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.93,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 1600, 900), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_verified_click_accepts_debug_low_card_confidence(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.9453},
            text_info={"text": "AAA大M4:437M4Ao0氏学业Td", "candidates": []},
        )
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.8428,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((103, 224, 1920, 1080), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_verified_click_accepts_2k_low_card_confidence_with_strong_cost(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.9609},
            text_info={"text": "&“大大mnHA”m4zArmn:40务食", "candidates": []},
        )
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.7934927046298981,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 2560, 1440), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_verified_click_accepts_1600_debug_detail_crop(self):
        import cv2

        image_path = Path("debug_bait_detail_20260502_042533.png")
        if not image_path.exists():
            self.skipTest("本地鱼饵详情调试图不存在")
        image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)
        machine = DetailDebugCropMachine(image, "am大大”送4送4MNA4大国#A4热线货生4S")
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.9297933280467987,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((563, 273, 1600, 900), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_verified_click_accepts_2k_debug_detail_crop(self):
        import cv2

        image_path = Path("debug_bait_detail_20260502_042843.png")
        if not image_path.exists():
            self.skipTest("本地鱼饵详情调试图不存在")
        image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)
        machine = DetailDebugCropMachine(image, "&“大大mnHA”m4zArmn:40务食")
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.7934927046298981,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "full-gray-same-card-currency",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 2560, 1440), item_info)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "detail-verified-after-click")

    def test_bait_detail_after_click_rejects_cost_when_card_was_not_strong_visual(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.94},
            text_info={"text": "AM大M4学业路T氏", "candidates": []},
        )
        item_info = {"source": "name+currency", "confidence": 0.98, "visual_card_confirmed": False}

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 1920, 1080), item_info)

        self.assertIsNone(result)

    def test_bait_detail_after_click_rejects_untrusted_visual_reason(self):
        machine = DetailIdentityMachine(
            cost_info={"source": "detail-cost", "confidence": 0.96},
            text_info={"text": "AM大M4学业路T氏", "candidates": []},
        )
        item_info = {
            "source": "full+name+currency",
            "confidence": 0.96,
            "visual_card_confirmed": True,
            "visual_confirm_reason": "visual_confidence_low",
        }

        result = machine._detect_bait_detail_ready_after_verified_click((0, 0, 1920, 1080), item_info)

        self.assertIsNone(result)

    def test_bait_purchase_flow_stops_before_buying_cached_wrong_detail(self):
        machine = CachedWrongDetailFlowMachine()

        result = machine._run_bait_purchase_flow((0, 0, 1920, 1080))

        self.assertFalse(result)
        self.assertIn("无上限万能鱼饵商品", machine.clicked_labels)
        self.assertNotIn("购买数量最大值按钮", machine.clicked_labels)
        self.assertNotIn("购买按钮", machine.clicked_labels)

    def test_bait_purchase_flow_continues_after_verified_card_click_with_noisy_detail_text(self):
        machine = VerifiedClickedNoisyDetailFlowMachine()

        result = machine._run_bait_purchase_flow((0, 0, 1920, 1080))

        self.assertTrue(result)
        self.assertIn("无上限万能鱼饵商品", machine.clicked_labels)
        self.assertIn("购买数量最大值按钮", machine.clicked_labels)
        self.assertIn("购买按钮", machine.clicked_labels)

    def test_bait_purchase_flow_uses_verified_selected_detail_without_card_click(self):
        machine = SelectedDetailFlowMachine()

        result = machine._run_bait_purchase_flow((0, 0, 1920, 1080))

        self.assertTrue(result)
        self.assertFalse(machine.item_detect_called)
        self.assertNotIn("无上限万能鱼饵商品", machine.clicked_labels)
        self.assertIn("购买数量最大值按钮", machine.clicked_labels)
        self.assertIn("购买按钮", machine.clicked_labels)

    def test_bait_item_name_region_stays_above_currency_region(self):
        machine = StateMachine(config={})

        regions = machine._bait_item_regions_from_currency(
            (928, 806, 3),
            {"location": (312, 339), "confidence": 0.94, "template": "currency", "size": (84, 46)},
        )

        self.assertIsNotNone(regions)
        self.assertLess(regions["name"][3], regions["currency"][1])

    def test_floating_window_hide_restore_commands_are_sent_for_capture(self):
        messages = queue.Queue()
        machine = StateMachine(log_queue=messages, config={})
        machine.is_running = True

        machine._set_floating_hidden_for_capture(True)
        machine._set_floating_hidden_for_capture(False)

        self.assertEqual(messages.get_nowait(), "CMD_MAIN_HIDE_FOR_CAPTURE")
        self.assertEqual(messages.get_nowait(), "CMD_FLOATING_HIDE_FOR_CAPTURE")
        self.assertEqual(messages.get_nowait(), "CMD_FLOATING_RESTORE_AFTER_CAPTURE")
        self.assertEqual(messages.get_nowait(), "CMD_MAIN_RESTORE_AFTER_CAPTURE")

    def test_bait_confirm_dialog_accepts_visual_prompt_without_ocr(self):
        image = np.full((520, 1300, 3), 30, dtype=np.uint8)
        image[130:310, :] = (235, 235, 235)
        image[360:430, 285:575] = (232, 232, 232)
        image[360:430, 725:1015] = (232, 232, 232)

        import cv2

        cv2.putText(image, "495", (560, 225), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (82, 82, 220), 5, cv2.LINE_AA)
        cv2.putText(image, "99", (760, 225), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (82, 82, 220), 5, cv2.LINE_AA)
        machine = ConfirmDialogMachine(image, text_info=None)

        result = machine._detect_bait_confirm_dialog((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "confirm-visual")
        click_ratio = result.get("confirm_click_ratio")
        self.assertIsNotNone(click_ratio)
        self.assertGreater(click_ratio[0], 0.55)
        self.assertLess(click_ratio[0], 0.75)
        self.assertGreater(click_ratio[1], 0.55)
        self.assertLess(click_ratio[1], 0.80)

    def test_bait_confirm_dialog_rejects_prompt_without_price_highlight(self):
        image = np.full((520, 1300, 3), 30, dtype=np.uint8)
        image[130:310, :] = (235, 235, 235)
        image[360:430, 285:575] = (232, 232, 232)
        image[360:430, 725:1015] = (232, 232, 232)
        machine = ConfirmDialogMachine(image, text_info=None)

        result = machine._detect_bait_confirm_dialog((0, 0, 1920, 1080))

        self.assertIsNone(result)

    def test_bait_confirm_dialog_accepts_title_layout_without_ocr(self):
        image = np.full((520, 1300, 3), 30, dtype=np.uint8)
        image[130:310, :] = (235, 235, 235)
        image[360:430, 285:575] = (232, 232, 232)
        image[360:430, 725:1015] = (232, 232, 232)

        import cv2

        cv2.putText(image, "TIP", (592, 88), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (245, 245, 245), 4, cv2.LINE_AA)
        machine = ConfirmDialogMachine(image, text_info=None)

        result = machine._detect_bait_confirm_dialog((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "confirm-visual")
        self.assertTrue(result.get("has_title_band"))

    def test_bait_confirm_dialog_keeps_ocr_fallback(self):
        image = np.full((520, 1300, 3), 30, dtype=np.uint8)
        machine = ConfirmDialogMachine(image, text_info={"text": "是否花费495鱼鳞币购买99个万能鱼饵", "candidates": []})

        result = machine._detect_bait_confirm_dialog((0, 0, 1920, 1080))

        self.assertIsNotNone(result)
        self.assertEqual(result.get("source"), "confirm-ocr")

    def test_start_bait_purchase_flow_respects_disable_switch(self):
        machine = StateMachine(config={"auto_buy_bait_amount": 0})
        machine.is_running = True

        handled = machine._start_bait_purchase_flow((0, 0, 1920, 1080), {})

        self.assertTrue(handled)
        self.assertFalse(machine.is_running)

    def test_start_bait_purchase_flow_enters_purchase_state(self):
        machine = StateMachine(config={"auto_buy_bait_amount": 198})
        machine.is_running = True

        handled = machine._start_bait_purchase_flow((0, 0, 1920, 1080), {})

        self.assertTrue(handled)
        self.assertEqual(machine.current_state, machine.STATE_BUYING_BAIT)
        self.assertEqual(machine._bait_purchase_batches_target, 2)

    def test_bait_purchase_exit_wait_ignores_partial_ready_probe(self):
        machine = ShopExitReadyMachine()

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            result = machine._wait_for_bait_condition(
                (0, 0, 1920, 1080),
                machine._detect_bait_purchase_exit_ready,
                timeout=2.0,
                interval=0.35,
            )
        finally:
            state_machine_module.time.time = original_time

        self.assertIsNotNone(result)
        self.assertEqual(result.get("location"), (120, 80))
        self.assertEqual(machine.ready_probe_calls, 3)

    def test_bait_purchase_recovery_skips_extra_esc_after_shop_exit(self):
        machine = BuyingBaitHandleMachine(exit_shop_sent=True)

        machine._handle_buying_bait((0, 0, 1920, 1080))

        self.assertFalse(machine.recovery_press_esc)

    def test_bait_purchase_recovery_uses_esc_before_shop_exit(self):
        machine = BuyingBaitHandleMachine(exit_shop_sent=False)

        machine._handle_buying_bait((0, 0, 1920, 1080))

        self.assertTrue(machine.recovery_press_esc)

    def test_cast_animation_wait_polls_short_lived_bait_prompt(self):
        machine = CastShortageMachine()

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            handled = machine._wait_after_cast_or_bait_shortage((0, 0, 1920, 1080), 2.0)
        finally:
            state_machine_module.time.time = original_time

        self.assertTrue(handled)
        self.assertEqual(machine.current_state, machine.STATE_BUYING_BAIT)
        self.assertEqual(machine._bait_purchase_batches_target, 2)
        self.assertTrue(any(100.28 <= item <= 100.70 for item in machine.check_times))

    def test_cast_animation_wait_accepts_strict_visual_bait_prompt(self):
        machine = CastVisualFallbackMachine()

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            handled = machine._wait_after_cast_or_bait_shortage((0, 0, 1920, 1080), 2.0)
        finally:
            state_machine_module.time.time = original_time

        self.assertTrue(handled)
        self.assertEqual(machine.current_state, machine.STATE_BUYING_BAIT)
        self.assertTrue(any(item[1].get("allow_visual_fallback") for item in machine.check_times))

    def test_waiting_recast_enters_purchase_on_visual_bait_prompt(self):
        machine = WaitingRecastVisualShortageMachine()

        import core.state_machine as state_machine_module

        original_time = state_machine_module.time.time
        state_machine_module.time.time = lambda: machine.clock
        try:
            machine._handle_waiting((0, 0, 1920, 1080), (0.20, 0.20, 0.60, 0.25))
        finally:
            state_machine_module.time.time = original_time

        self.assertEqual(machine.current_state, machine.STATE_BUYING_BAIT)
        self.assertFalse(machine.recast_sent)

    def test_bait_shortage_prompt_accepts_center_banner_with_ocr_terms(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        image[88:152, :] = (18, 22, 24)

        import cv2

        cv2.putText(
            image,
            "NEED BAIT TO FISH",
            (260, 131),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.12,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        machine = BannerShortageMachine(
            image,
            text_info={"text": "需要装备鱼饵才可以钓鱼", "candidates": []},
        )

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080))

        self.assertIsNotNone(info)
        self.assertEqual(info.get("source"), "banner-ocr")
        self.assertIn("banner", info)
        self.assertEqual(machine.ocr_term_checks, 1)

    def test_bait_shortage_prompt_rejects_center_banner_without_ocr_terms(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        image[88:152, :] = (18, 22, 24)

        import cv2

        cv2.putText(
            image,
            "CASTING",
            (350, 131),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.12,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        machine = BannerShortageMachine(image)

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080))

        self.assertIsNone(info)
        self.assertGreaterEqual(machine.ocr_term_checks, 1)

    def test_bait_shortage_prompt_visual_fallback_accepts_banner_with_initial_controls(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        image[88:152, :] = (18, 22, 24)

        import cv2

        cv2.putText(
            image,
            "PROMPT",
            (330, 131),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.12,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        machine = BannerVisualFallbackMachine(image)

        info = machine._detect_bait_shortage_prompt(
            (0, 0, 1920, 1080),
            require_visual_hint=True,
            allow_visual_fallback=True,
        )

        self.assertIsNotNone(info)
        self.assertEqual(info.get("source"), "banner-visual")
        self.assertIn("banner", info)

    def test_bait_shortage_prompt_rejects_plain_center_area_without_banner(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        machine = BannerShortageMachine(image)

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080))

        self.assertIsNone(info)
        self.assertEqual(machine.ocr_term_checks, 0)

    def test_bait_shortage_context_rejects_active_fishing_fullscreen(self):
        import cv2

        image_path = Path("debug_bait_shop_fullscreen_20260501_031809.png")
        if not image_path.exists():
            self.skipTest("缺少本地钓鱼状态负样本截图")
        image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)
        machine = StateMachine(config={"auto_buy_bait_amount": 99})
        machine.sc = RelativeImageCapture(image)

        allowed = machine._bait_shortage_context_allows_purchase((0, 0, image.shape[1], image.shape[0]))

        self.assertFalse(allowed)

    def test_cast_short_window_skips_ocr_when_banner_is_absent(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        machine = BannerShortageMachine(image)

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080), require_visual_hint=True)

        self.assertIsNone(info)
        self.assertEqual(machine.ocr_term_checks, 0)

    def test_cast_short_window_rejects_banner_without_bait_text(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        image[88:152, :] = (18, 22, 24)

        import cv2

        cv2.putText(
            image,
            "CASTING",
            (350, 131),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.12,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        machine = BannerShortageMachine(
            image,
            text_info={"text": "正在抛竿请稍候", "candidates": []},
        )

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080), require_visual_hint=True)

        self.assertIsNone(info)
        self.assertGreaterEqual(machine.ocr_term_checks, 1)

    def test_bait_shortage_prompt_rejects_split_terms_across_candidates(self):
        image = np.full((240, 900, 3), 118, dtype=np.uint8)
        image[88:152, :] = (18, 22, 24)

        import cv2

        cv2.putText(
            image,
            "NOTICE",
            (350, 131),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.12,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        machine = BannerShortageMachine(
            image,
            text_candidates=[
                ("鱼饵", 0.92),
                ("钓鱼", 0.91),
                ("需要装备", 0.90),
                ("才可以", 0.89),
            ],
        )

        info = machine._detect_bait_shortage_prompt((0, 0, 1920, 1080), require_visual_hint=True)

        self.assertIsNone(info)

    def test_bait_shortage_text_requires_complete_prompt_meaning(self):
        machine = StateMachine(config={})

        self.assertTrue(machine._bait_shortage_text_matches("需要装备鱼饵才可以钓鱼"))
        self.assertFalse(machine._bait_shortage_text_matches("鱼饵 钓鱼 购买"))
        self.assertFalse(machine._bait_shortage_text_matches("正在抛竿请稍候"))

    def test_find_template_matches_returns_multiple_locations(self):
        vision = VisionCore()
        template = np.array(
            [
                [0, 255, 0, 255, 0],
                [255, 255, 255, 255, 255],
                [0, 255, 0, 255, 0],
                [255, 255, 255, 255, 255],
                [0, 255, 0, 255, 0],
            ],
            dtype=np.uint8,
        )
        image = np.zeros((24, 40), dtype=np.uint8)
        image[5:10, 6:11] = template
        image[14:19, 25:30] = template

        import tempfile
        import cv2
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "template.png"
            cv2.imwrite(str(path), template)
            matches = vision.find_template_matches(image, str(path), threshold=0.99, max_matches=4, min_distance=4, scale_range=(1.0, 1.0), scale_steps=1)

        self.assertGreaterEqual(len(matches), 2)


if __name__ == "__main__":
    unittest.main()
