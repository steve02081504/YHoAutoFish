import time
import threading
import queue
import cv2
import numpy as np
import os
import re
import shutil
import traceback
from pathlib import Path
from importlib import metadata
from PIL import Image, ImageDraw, ImageFont

from core.window_manager import WindowManager
from core.screen_capture import ScreenCapture
from core.controller import Controller
from core.vision import VisionCore
from core.pid import PIDController
from core.record_manager import RecordManager
from core.paths import resource_path
from core.user_activity_monitor import UserActivityMonitor

CnOcr = None

OCR_MODEL_BUNDLE_DIR = "ocr_models"
OCR_REQUIRED_MODELS = (
    (
        "cnocr",
        ("2.3", "densenet_lite_136-gru"),
        "cnocr-v2.3-densenet_lite_136-gru-epoch=004-ft-model.onnx",
    ),
)

class StateMachine:
    STATE_IDLE = 0
    STATE_WAITING = 1
    STATE_FISHING = 2
    STATE_RESULT = 3
    STATE_FAILED = 4
    STATE_PAUSED = 5
    STATE_RECOVERING = 6
    STATE_BUYING_BAIT = 7
    
    def __init__(self, log_queue=None, debug_queue=None, config=None):
        self.log_queue = log_queue
        self.debug_queue = debug_queue
        
        self.wm = WindowManager()
        self.sc = None 
        self.ctrl = Controller()
        self.user_activity = UserActivityMonitor()
        self._user_takeover_exclude_rects = []
        self._input_lock = threading.RLock()
        self.vis = VisionCore()
        self.record_mgr = RecordManager()
        self.ocr = {}
        self.ocr_available = True
        self._ocr_import_checked = False
        self._ocr_roots = None
        self.last_ocr_init_error = ""
        self.last_ocr_init_trace = ""
        self._fish_matcher_refs = None
        self._weight_digit_templates = None
        self._last_name_ocr_candidates = []
        self._last_weight_ocr_candidates = []
        self._last_weight_corrections = []
        self.roi_f_btn = (0.75, 0.75, 0.25, 0.25)
        self.roi_initial_controls = (0.70, 0.50, 0.30, 0.50)
        self._ready_heavy_last_check = 0
        
        self.is_running = False
        self.current_state = self.STATE_IDLE
        self.fishing_start_time = 0
        self.fishing_timeout = 180 # 3分钟超时防卡死
        self.fish_count = 0
        
        # 实例化真正的 PID 控制器
        # Kp: 比例，影响追赶速度
        # Ki: 积分，消除长期偏差（设为极小）
        # Kd: 微分，物理刹车预测防过冲（异环这种带惯性的游戏，Kd需要比较大）
        self.pid = PIDController(kp=1.2, ki=0.01, kd=0.4, output_limits=(-100, 100))
        self.total_runtime = 0
        self.start_timestamp = 0
        self._stop_requested = False
        
        # 参数配置 (后续可由 GUI 更新)
        self.config = config or {
            "t_hold": 5,        # 安全区内重新触发按键的阈值
            "t_deadzone": 1,    # 追赶触发死区
            "tracking_strength": 180,
            "debug_mode": False,
            "cast_animation_delay": 2,
            "settlement_close_delay": 1,
            "bar_missing_timeout": 3,
            "pre_control_timeout": 14,
            "hook_wait_timeout": 90,
            "recovery_timeout": 8,
            "fishing_result_check_interval": 0.65,
            "fishing_failed_check_interval": 1.25,
            "empty_ready_confirm_delay": 0.45,
            "bar_confidence_threshold": 0.45,
            "feed_forward_gain": 0.18,
            "safe_zone_ratio": 0.08,
            "control_release_cross_ratio": 0.012,
            "control_reengage_ratio": 0.018,
            "control_switch_ratio": 0.08,
            "control_min_hold_time": 0.14,
            "user_takeover_protection": True,
            "user_takeover_mouse_threshold": 12,
            "user_takeover_start_grace": 1.20,
            "auto_buy_bait_amount": 0,
            "bait_shop_debug_mode": False,
        }
        self._asset_template_cache = {}
        
    def _log(self, msg):
        """线程安全的日志发送"""
        if self.log_queue is not None:
            self.log_queue.put(msg)
        else:
            print(msg)

    def _should_stop(self):
        return bool(getattr(self, "_stop_requested", False) or not getattr(self, "is_running", False))

    def _tap_key_if_running(self, key, duration=0.01):
        with self._input_lock:
            if self._should_stop():
                return False
            if self.wm.is_foreground() and self._check_user_takeover():
                return False
            self._note_program_input((key,), duration=float(duration) + 0.45)
            self.ctrl.key_tap(key, duration=duration)
            return True

    def _sleep_interruptible(self, seconds, step=0.05):
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            if getattr(self, "_stop_requested", False):
                return False
            time.sleep(min(step, deadline - time.time()))
        return not getattr(self, "_stop_requested", False)

    def _note_program_input(self, keys=(), duration=0.45):
        if getattr(self, "user_activity", None) is not None:
            self.user_activity.note_program_input(keys, duration=duration)

    def _record_runtime_for_current_run(self):
        if self.start_timestamp > 0:
            duration = int(time.time() - self.start_timestamp)
            if duration > 0:
                self.total_runtime += duration
                self.record_mgr.add_runtime(duration)
            self.start_timestamp = 0

    def _pause_for_user_takeover(self, reason):
        with self._input_lock:
            if not self.is_running:
                return
            self._stop_requested = True
            self.is_running = False
            self.current_state = self.STATE_PAUSED
            self.ctrl.release_all()
        self._record_runtime_for_current_run()
        detail = reason or "检测到用户输入"
        self._log(f"[安全] {detail}。已暂停自动钓鱼并释放全部按键。需要继续时请重新点击开始，并保持挂机状态不要操作游戏。")
        if self.log_queue:
            self.log_queue.put(f"CMD_USER_TAKEOVER_PAUSED::{detail}")

    def _check_user_takeover(self, game_rect=None):
        if self._should_stop() or getattr(self, "user_activity", None) is None:
            return False
        reason = self.user_activity.check(
            getattr(self.ctrl, "pressed_keys", set()),
            game_rect=game_rect,
            excluded_rects=getattr(self, "_user_takeover_exclude_rects", []),
        )
        if not reason:
            return False
        self._pause_for_user_takeover(reason)
        return True

    def start(self):
        """启动状态机"""
        if self.is_running: return
        self._stop_requested = False
        self.is_running = True
        self.current_state = self.STATE_IDLE
        self._reset_round_state()
        self.user_activity.reset()
        self.start_timestamp = time.time()
        self._log("钓鱼脚本启动中，正在寻找游戏窗口...")
        
        # 在独立线程运行主循环
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def stop(self):
        """停止状态机"""
        if not self.is_running: return
        with self._input_lock:
            self._stop_requested = True
            self.is_running = False
            self.ctrl.release_all()
        self._log("[系统] 收到停止指令。")
        
        # 记录本次运行时长
        if self.start_timestamp > 0:
            self._record_runtime_for_current_run()
            
        self.ctrl.release_all()
        # 释放系统绘图句柄，防止二次启动时抛出 BitBlt 和 SelectObject 异常
        if hasattr(self, 'sc') and self.sc:
            self.sc.close()
        self._log("钓鱼脚本已停止。")
        # 通知 UI 更新
        if self.log_queue:
            self.log_queue.put("CMD_STOP_UPDATE_GUI")

    def update_config(self, key, value):
        self.config[key] = value
        # 对于超时设置，直接同步到实例变量
        if key == "fishing_timeout":
            self.fishing_timeout = value
        elif key == "user_takeover_protection":
            self.user_activity.update_config(enabled=value)
        elif key == "user_takeover_mouse_threshold":
            self.user_activity.update_config(mouse_move_threshold=value)
        elif key == "user_takeover_start_grace":
            self.user_activity.update_config(start_grace=value)
        elif key == "user_takeover_exclude_rects":
            self._user_takeover_exclude_rects = self._normalize_exclude_rects(value)

    def _normalize_exclude_rects(self, rects):
        normalized = []
        for rect in rects or []:
            try:
                left, top, width, height = rect
                width = int(width)
                height = int(height)
                if width > 0 and height > 0:
                    normalized.append((int(left), int(top), width, height))
            except Exception:
                continue
        return normalized

    def _resolve_asset_templates(self, cache_key, exact_names=(), required_keywords=()):
        if cache_key in self._asset_template_cache:
            return self._asset_template_cache[cache_key]

        assets_dir = Path(resource_path("assets"))
        paths = []
        seen = set()

        def add_path(path):
            normalized = str(path)
            if path.exists() and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)

        for name in exact_names:
            add_path(assets_dir / name)

        if assets_dir.exists():
            for path in assets_dir.glob("*.png"):
                filename = path.name
                if all(keyword in filename for keyword in required_keywords):
                    add_path(path)

        self._asset_template_cache[cache_key] = paths
        if not paths:
            self._log(f"[识别] 未找到模板资源: {cache_key}，请检查 assets 目录。")
        return paths

    def _f_button_templates(self):
        return self._resolve_asset_templates(
            "f_button",
            exact_names=("F键图标.png", "F键图标2.png", "F键图标3.png"),
            required_keywords=("F键图标",),
        )

    def _initial_q_button_templates(self):
        return self._resolve_asset_templates(
            "initial_q_button",
            exact_names=("初始钓鱼界面的Q键进入售鱼界面按钮图标（暗色）.png", "初始钓鱼界面的Q键进入售鱼界面按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "Q键"),
        )

    def _initial_e_button_templates(self):
        return self._resolve_asset_templates(
            "initial_e_button",
            exact_names=("初始钓鱼界面的E键更换鱼饵按钮图标（暗色）.png", "初始钓鱼界面的E键更换鱼饵按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "E键"),
        )

    def _initial_r_button_templates(self):
        return self._resolve_asset_templates(
            "initial_r_button",
            exact_names=("初始钓鱼界面的R键进入钓鱼商店按钮图标（暗色）.png", "初始钓鱼界面的R键进入钓鱼商店按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "R键"),
        )

    def _ready_start_button_templates(self):
        return self._resolve_asset_templates(
            "ready_start_button",
            exact_names=("钓鱼准备界面开始钓鱼按钮.png",),
            required_keywords=("开始钓鱼",),
        )

    def _ready_panel_templates(self):
        return self._resolve_asset_templates(
            "ready_panel",
            exact_names=("钓鱼准备界面右侧UI.png",),
            required_keywords=("钓鱼准备界面", "右侧UI"),
        )

    def _hook_text_templates(self):
        return self._resolve_asset_templates(
            "hook_text",
            exact_names=("上钩文字.png", "钓鱼上钩文字.png"),
            required_keywords=("上钩文字",),
        )

    def _failed_text_templates(self):
        return self._resolve_asset_templates(
            "failed_text",
            exact_names=("鱼儿溜走了.png", "钓鱼结算界面鱼儿溜走了.png"),
            required_keywords=("鱼儿溜走了",),
        )

    def _weight_unit_templates(self):
        return self._resolve_asset_templates(
            "weight_unit_g",
            exact_names=("成功上鱼结算画面重量单位银色的g.png",),
            required_keywords=("重量单位", "g"),
        )

    def _success_close_prompt_templates(self):
        return self._resolve_asset_templates(
            "success_close_prompt",
            exact_names=("成功上鱼结算画面点击关闭提示（辅助判断成功上鱼）.png",),
            required_keywords=("成功上鱼结算画面", "点击关闭提示"),
        )

    def _success_exp_templates(self):
        return self._resolve_asset_templates(
            "success_exp",
            exact_names=("成功上鱼结算画面获得经验（辅助判断成功上鱼）.png",),
            required_keywords=("成功上鱼结算画面", "获得经验"),
        )

    def _unlimited_bait_currency_templates(self):
        return self._resolve_asset_templates(
            "unlimited_bait_currency",
            exact_names=("万能鱼饵无限购买货品的购买货币图标.png",),
            required_keywords=("万能鱼饵", "购买货币图标"),
        )

    def _unlimited_bait_full_item_templates(self):
        return self._resolve_asset_templates(
            "unlimited_bait_full_item",
            exact_names=("万能鱼饵无限购买货品完整图标.png",),
            required_keywords=("万能鱼饵", "完整图标"),
        )

    def _bait_max_button_templates(self):
        return self._resolve_asset_templates(
            "bait_max_button",
            exact_names=("钓鱼商店内购买商品时选中最大值图标.png",),
            required_keywords=("钓鱼商店", "最大值图标"),
        )

    def _cursor_templates(self):
        return self._resolve_asset_templates(
            "fishing_cursor",
            exact_names=("溜鱼游标1.png", "溜鱼游标2.png", "溜鱼游标3.png", "溜鱼游标4.png", "溜鱼游标5.png"),
            required_keywords=("溜鱼", "游标"),
        )

    def _target_bar_templates(self):
        return self._resolve_asset_templates(
            "fishing_target_bar",
            exact_names=("溜鱼耐力条1.png",),
            required_keywords=("溜鱼", "耐力条"),
        )

    def _template_scale_range(self, rect, low_factor=0.65, high_factor=1.45):
        if not rect:
            base_scale = 1.0
        else:
            base_scale = max(0.40, min(float(rect[3]) / 900.0, 3.00))
        return max(0.25, base_scale * low_factor), min(4.00, base_scale * high_factor)

    def _f_button_match_strategies(self):
        return (
            {"name": "binary-145-mask", "threshold": 0.58, "use_binary": True, "binary_threshold": 145, "use_mask": True},
            {"name": "binary-115-mask", "threshold": 0.56, "use_binary": True, "binary_threshold": 115, "use_mask": True},
            {"name": "binary-175-mask", "threshold": 0.58, "use_binary": True, "binary_threshold": 175, "use_mask": True},
            {"name": "edge", "threshold": 0.52, "use_edge": True, "use_binary": False, "use_mask": False},
            {"name": "gray-mask", "threshold": 0.55, "use_edge": False, "use_binary": False, "use_mask": True},
        )

    def _f_button_fast_match_strategies(self):
        return (
            {"name": "gray-mask-fast", "threshold": 0.60, "use_mask": True, "mask_threshold": 6, "early_accept": 0.94},
            {"name": "edge-fast", "threshold": 0.55, "use_edge": True, "early_accept": 0.92},
        )

    def _initial_control_match_strategies(self):
        return (
            {"name": "control-gray-mask", "threshold": 0.60, "use_mask": True, "mask_threshold": 6, "early_accept": 0.90},
            {"name": "control-edge", "threshold": 0.54, "use_edge": True, "early_accept": 0.88},
            {"name": "control-plain", "threshold": 0.62, "early_accept": 0.90},
        )

    def _normalize_tracking_strength(self):
        try:
            raw_value = float(self.config.get("tracking_strength", 180))
        except (TypeError, ValueError):
            raw_value = 180.0
        strength = raw_value / 100.0 if raw_value > 5 else raw_value
        return max(0.70, min(strength, 2.40))

    def _normalize_ratio_config(self, key, default, minimum, maximum):
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _reset_round_state(self, release_keys=True):
        if release_keys:
            self.ctrl.release_all()
        self.pid.reset()
        self._waiting_start_time = 0
        self._last_cast_time = 0
        self._waiting_recast_count = 0
        self._waiting_ready_recheck_last = 0
        self._bait_shortage_check_last = 0
        self._fishing_start_time = 0
        self.fishing_start_time = 0
        self._missing_start_time = 0
        self._last_cursor_x = None
        self._seen_fishing_bar = False
        self._last_target_time = 0
        self._last_target_x = None
        self._target_velocity = 0
        self._last_valid_target_x = None
        self._last_valid_target_w = None
        self._last_valid_bar_time = 0
        self._last_valid_cursor_x = None
        self._last_valid_cursor_time = 0
        self._last_cursor_template_time = 0
        self._bar_cursor_jump_reject_count = 0
        self._bar_jump_reject_count = 0
        self._fish_control_direction = 0
        self._fish_control_min_hold_until = 0
        self._fish_control_last_change = 0
        self._confirmed_fishing_bar = False
        self._bar_seen_streak = 0
        self._bar_first_seen_time = 0
        self._last_bar_seen_time = 0
        self._fishing_bar_confirmed_time = 0
        self._fishing_control_started = False
        self._fishing_control_started_time = 0
        self._fishing_control_frame_count = 0
        self._capture_missing_start_time = 0
        self._last_bar_capture_failed = False
        self._last_control_error = 0
        self._last_control_target_w = None
        self._round_had_fishing_bar = False
        self._result_empty_recorded = False
        self._result_quick_check_last = 0
        self._result_full_check_last = 0
        self._fishing_result_check_last = 0
        self._fishing_failed_check_last = 0
        self._result_ready_seen_time = 0
        self._result_ready_confirm_count = 0
        self._result_ready_last_kind = ""
        self._result_ready_debug_saved = False
        self._result_text_probe_done = False
        self._success_recorded_pending_close = False
        self._success_close_retry_count = 0
        self._success_close_last_esc = 0
        self._failed_result_candidate_seen_time = 0
        self._failed_result_candidate_count = 0
        self._failed_result_candidate_signature = ""
        self._recovery_start_time = 0
        self._recovery_reason = ""
        self._recovery_esc_requested = False
        self._recovery_esc_sent = False
        self._recovery_second_esc_sent = False
        self._recovery_empty_recorded = False
        self._bait_purchase_batches_target = 0
        self._bait_purchase_batches_done = 0
        self._bait_purchase_in_progress = False
        self._bait_purchase_exit_shop_sent = False

    def _prepare_fishing_round_state(self, start_time=None):
        start_time = start_time or time.time()
        self.pid.reset()
        self._fishing_start_time = start_time
        self.fishing_start_time = start_time
        self._missing_start_time = 0
        self._last_cursor_x = None
        self._seen_fishing_bar = False
        self._last_target_time = 0
        self._last_target_x = None
        self._target_velocity = 0
        self._last_valid_target_x = None
        self._last_valid_target_w = None
        self._last_valid_bar_time = 0
        self._last_valid_cursor_x = None
        self._last_valid_cursor_time = 0
        self._last_cursor_template_time = 0
        self._bar_cursor_jump_reject_count = 0
        self._bar_jump_reject_count = 0
        self._fish_control_direction = 0
        self._fish_control_min_hold_until = 0
        self._fish_control_last_change = 0
        self._confirmed_fishing_bar = False
        self._bar_seen_streak = 0
        self._bar_first_seen_time = 0
        self._last_bar_seen_time = 0
        self._fishing_bar_confirmed_time = 0
        self._fishing_control_started = False
        self._fishing_control_started_time = 0
        self._fishing_control_frame_count = 0
        self._capture_missing_start_time = 0
        self._last_bar_capture_failed = False
        self._last_control_error = 0
        self._last_control_target_w = None
        self._round_had_fishing_bar = False
        self._result_empty_recorded = False
        self._result_quick_check_last = 0
        self._result_full_check_last = 0
        self._fishing_result_check_last = 0
        self._fishing_failed_check_last = 0
        self._result_ready_seen_time = 0
        self._result_ready_confirm_count = 0
        self._result_ready_last_kind = ""
        self._result_ready_debug_saved = False
        self._result_text_probe_done = False
        self._success_recorded_pending_close = False
        self._success_close_retry_count = 0
        self._success_close_last_esc = 0
        self._failed_result_candidate_seen_time = 0
        self._failed_result_candidate_count = 0
        self._failed_result_candidate_signature = ""

    def _detect_initial_control_cluster(self, rect):
        if self.sc is None or not rect:
            return {"count": 0, "matches": [], "confidence": 0.0, "valid": False}

        controls_roi = getattr(self, "roi_initial_controls", (0.70, 0.50, 0.30, 0.50))
        controls_img = self.sc.capture_relative(rect, *controls_roi)
        if controls_img is None:
            return {"count": 0, "matches": [], "confidence": 0.0, "valid": False}

        button_sets = (
            ("Q", self._initial_q_button_templates()),
            ("E", self._initial_e_button_templates()),
            ("R", self._initial_r_button_templates()),
        )
        matches = []
        best_conf = 0.0
        for key_name, templates in button_sets:
            loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
                controls_img,
                templates,
                self._initial_control_match_strategies(),
                threshold=0.58,
                scale_range=self._template_scale_range(rect, 0.50, 1.80),
                scale_steps=5,
            )
            best_conf = max(best_conf, float(conf or 0.0))
            if loc:
                matches.append({
                    "key": key_name,
                    "location": loc,
                    "confidence": conf,
                    "template": matched_path,
                    "strategy": strategy_name,
                })

        if matches:
            avg_conf = sum(item["confidence"] for item in matches) / len(matches)
        else:
            avg_conf = best_conf
        return {
            "count": len(matches),
            "matches": matches,
            "confidence": avg_conf,
            "valid": self._initial_control_cluster_is_valid(matches, controls_img.shape),
        }

    def _initial_control_cluster_is_valid(self, matches, image_shape):
        if not matches or len(matches) < 2 or not image_shape:
            return False
        height, width = image_shape[:2]
        if width <= 0 or height <= 0:
            return False

        centers = [item.get("location") for item in matches if item.get("location")]
        if len(centers) < 2:
            return False

        xs = [float(point[0]) for point in centers]
        ys = [float(point[1]) for point in centers]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        horizontal_layout = (
            x_span >= max(56.0, width * 0.10)
            and x_span <= max(360.0, width * 0.72)
            and y_span <= max(90.0, height * 0.24)
            and min(ys) >= height * 0.52
        )
        vertical_layout = (
            x_span <= max(130.0, width * 0.28)
            and y_span >= max(42.0, height * 0.14)
            and min(ys) >= height * 0.20
        )
        if not horizontal_layout and not vertical_layout:
            return False

        confidences = [float(item.get("confidence") or 0.0) for item in matches]
        if len(matches) >= 3:
            return sum(confidences) / len(confidences) >= 0.56
        return sum(confidences) / len(confidences) >= 0.62

    def _detect_initial_f_prompt_quick(self, rect, threshold=0.88):
        if self.sc is None or not rect:
            return None

        f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
        btn_img = self.sc.capture_relative(rect, *f_roi)
        if btn_img is None:
            return None

        loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
            btn_img,
            self._f_button_templates(),
            (
                {"name": "f-quick-gray-mask", "threshold": threshold, "use_mask": True, "mask_threshold": 6, "early_accept": max(threshold, 0.94)},
                {"name": "f-quick-edge", "threshold": max(0.80, threshold - 0.04), "use_edge": True, "early_accept": max(threshold, 0.92)},
            ),
            threshold=threshold,
            scale_range=self._template_scale_range(rect, 0.84, 1.20),
            scale_steps=3,
        )
        if not loc:
            return None
        return {
            "kind": "F键图标",
            "confidence": conf,
            "location": loc,
            "template": matched_path,
            "strategy": strategy_name,
        }

    def _has_initial_fishing_ui(self, rect):
        info = self._detect_cast_prompt_after_settlement(rect)
        return bool(info and info.get("location"))

    def _format_initial_controls(self, cluster_info):
        parts = []
        for item in (cluster_info or {}).get("matches", []):
            matched_path = item.get("template")
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            parts.append(f"{item.get('key')}:{item.get('confidence', 0):.2f}/{matched_name}/{item.get('strategy') or '默认'}")
        return "；".join(parts) if parts else "无"

    def _detect_ready_to_cast(self, rect, allow_heavy=False, require_initial_controls=False, include_f=True, include_prepare_ui=False):
        if self.sc is None or not rect:
            return None

        best_conf = -1.0
        initial_cluster = None

        if include_f:
            f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
            btn_img = self.sc.capture_relative(rect, *f_roi)
        else:
            btn_img = None

        if include_f and btn_img is not None:
            loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
                btn_img,
                self._f_button_templates(),
                self._f_button_fast_match_strategies(),
                threshold=0.58,
                scale_range=self._template_scale_range(rect, 0.82, 1.18),
                scale_steps=4,
            )
            best_conf = conf
            if loc:
                if require_initial_controls:
                    initial_cluster = self._detect_initial_control_cluster(rect)
                else:
                    initial_cluster = {"count": 0, "matches": [], "confidence": 0.0}
                if require_initial_controls and (initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid")):
                    return {
                        "kind": "F键图标",
                        "confidence": conf,
                        "location": None,
                        "template": matched_path,
                        "strategy": strategy_name,
                        "initial_controls": initial_cluster,
                    }
                return {
                    "kind": "钓鱼初始界面" if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid") else "F键图标",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                    "initial_controls": initial_cluster,
                }

            loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
                btn_img,
                self._f_button_templates(),
                self._f_button_match_strategies(),
                threshold=0.58,
                scale_range=self._template_scale_range(rect, 0.55, 1.65),
                scale_steps=11,
            )
            best_conf = conf
            if loc:
                if require_initial_controls:
                    initial_cluster = self._detect_initial_control_cluster(rect)
                else:
                    initial_cluster = {"count": 0, "matches": [], "confidence": 0.0}
                if require_initial_controls and (initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid")):
                    return {
                        "kind": "F键图标",
                        "confidence": conf,
                        "location": None,
                        "template": matched_path,
                        "strategy": strategy_name,
                        "initial_controls": initial_cluster,
                    }
                return {
                    "kind": "钓鱼初始界面" if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid") else "F键图标",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                    "initial_controls": initial_cluster,
                }

        if require_initial_controls or not include_f:
            initial_cluster = self._detect_initial_control_cluster(rect)
            if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid"):
                first_match = initial_cluster.get("matches", [{}])[0]
                return {
                    "kind": "钓鱼初始界面组合控件",
                    "confidence": initial_cluster.get("confidence", 0.0),
                    "location": first_match.get("location") or (0, 0),
                    "template": first_match.get("template"),
                    "strategy": first_match.get("strategy") or "initial-controls",
                    "initial_controls": initial_cluster,
                }
            if require_initial_controls:
                return {
                    "kind": "钓鱼初始界面组合控件",
                    "confidence": initial_cluster.get("confidence", best_conf if best_conf >= 0 else 0.0),
                    "location": None,
                    "template": None,
                    "strategy": "initial-controls",
                    "initial_controls": initial_cluster,
                }

        if not include_prepare_ui:
            return {
                "kind": "",
                "confidence": best_conf,
                "location": None,
                "template": None,
            } if best_conf >= 0 else None

        start_button_roi = (0.15, 0.74, 0.70, 0.23)
        start_img = self.sc.capture_relative(rect, *start_button_roi)
        if start_img is not None:
            loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
                start_img,
                self._ready_start_button_templates(),
                (
                    {"name": "gray-mask", "threshold": 0.56, "use_mask": True},
                    {"name": "edge", "threshold": 0.54, "use_edge": True},
                    {"name": "plain", "threshold": 0.58},
                ),
                threshold=0.56,
                scale_range=self._template_scale_range(rect, 0.62, 1.55),
                scale_steps=9,
            )
            if conf > best_conf:
                best_conf = conf
            if loc:
                return {
                    "kind": "开始钓鱼按钮",
                    "confidence": conf,
                    "location": loc,
                    "template": matched_path,
                    "strategy": strategy_name,
                }

        if allow_heavy:
            now = time.time()
            if now - getattr(self, "_ready_heavy_last_check", 0) >= 3.0:
                self._ready_heavy_last_check = now
                full_img = self.sc.capture_relative(rect, 0, 0, 1, 1)
                if full_img is not None:
                    loc, conf, matched_path = self.vis.find_best_template(
                        full_img,
                        self._ready_panel_templates(),
                        threshold=0.70,
                        use_edge=True,
                        use_binary=False,
                        scale_range=self._template_scale_range(rect, 0.62, 1.55),
                        scale_steps=7,
                    )
                    if conf > best_conf:
                        best_conf = conf
                    if loc:
                        return {
                            "kind": "钓鱼准备界面",
                            "confidence": conf,
                            "location": loc,
                            "template": matched_path,
                        }

        return {
            "kind": "",
            "confidence": best_conf,
            "location": None,
            "template": None,
        } if best_conf >= 0 else None

    def _send_cast_input(self, ready_info, source_label):
        with self._input_lock:
            if self._should_stop():
                return False
            matched_path = ready_info.get("template") if ready_info else None
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            confidence = float((ready_info or {}).get("confidence") or 0.0)
            strategy = (ready_info or {}).get("strategy") or "默认"
            kind = (ready_info or {}).get("kind") or "可抛钩提示"
            self._log(f"[{source_label}] 识别到{kind} (置信度: {confidence:.2f}，模板: {matched_name}，策略: {strategy})。准备抛竿。")
            self._log(f"[{source_label}] > 正在向游戏发送 'F' 键点按指令 (150ms)...")
            self.ctrl.release_all()
            self._note_program_input(("F",), duration=0.70)
            self.ctrl.key_tap('F', duration=0.15)
            self._last_cast_time = time.time()
            self._waiting_start_time = self._last_cast_time
            self._bait_shortage_check_last = 0
            return True

    def _normalized_auto_buy_bait_amount(self):
        try:
            value = int(float(self.config.get("auto_buy_bait_amount", 0)))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(value, 9999))
        return (value // 99) * 99

    def _bait_purchase_batch_count(self, amount=None):
        amount = self._normalized_auto_buy_bait_amount() if amount is None else int(amount)
        return max(0, amount // 99)

    def _client_point_from_ratio(self, rect, rx, ry):
        if not rect:
            return None
        left, top, width, height = rect
        return int(round(left + width * float(rx))), int(round(top + height * float(ry)))

    def _client_ratio_from_roi_point(self, roi, image_shape, point):
        if not roi or not image_shape or point is None:
            return None
        image_h, image_w = image_shape[:2]
        if image_w <= 0 or image_h <= 0:
            return None
        rx, ry, rw, rh = roi
        return (
            float(rx) + (float(point[0]) / float(image_w)) * float(rw),
            float(ry) + (float(point[1]) / float(image_h)) * float(rh),
        )

    def _click_screen_point(self, x, y, label="界面按钮"):
        with self._input_lock:
            if self._should_stop():
                return False
            self.ctrl.release_all()
            self.wm.set_foreground()
            self._note_program_input(("mouse_left",), duration=0.90)
            if not self.ctrl.mouse_click(x, y, duration=0.06):
                self._log(f"[鱼饵] 点击{label}失败，坐标: {int(x)}, {int(y)}")
                return False
            self._log(f"[鱼饵] 已点击{label}。")
            return True

    def _click_client_ratio(self, rect, rx, ry, label="界面按钮"):
        point = self._client_point_from_ratio(rect, rx, ry)
        if point is None:
            return False
        return self._click_screen_point(point[0], point[1], label=label)

    def _normalize_ui_text(self, text):
        text = str(text or "")
        table = str.maketrans({
            "魚": "鱼",
            "餌": "饵",
            "萬": "万",
            "臺": "台",
            "，": "",
            "。": "",
            "？": "",
            "?": "",
            "【": "",
            "】": "",
            "[": "",
            "]": "",
            " ": "",
            "\n": "",
            "\t": "",
        })
        return re.sub(r"\s+", "", text.translate(table))

    def _text_has_terms(self, text, terms):
        normalized = self._normalize_ui_text(text)
        return all(self._normalize_ui_text(term) in normalized for term in terms)

    def _read_text_candidates_from_image(self, image, mode="name"):
        candidates = []
        if image is None or image.size == 0:
            return candidates
        mode = mode if mode in {"name", "general"} else "name"
        for variant in self._build_ocr_variants(image, mode):
            for text, score in self._collect_ocr_candidates(variant, mode):
                cleaned = self._normalize_ui_text(text)
                if cleaned:
                    candidates.append((cleaned, float(score or 0.0)))
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def _read_text_candidates_from_rois(self, rect, rois, mode="name"):
        candidates = []
        if self.sc is None:
            return candidates
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            candidates.extend(self._read_text_candidates_from_image(image, mode=mode))
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def _detect_text_terms_in_rois(self, rect, rois, required_terms=(), any_terms=(), mode="name"):
        candidates = self._read_text_candidates_from_rois(rect, rois, mode=mode)
        combined = "".join(text for text, _ in candidates[:10])
        if required_terms and not self._text_has_terms(combined, required_terms):
            return None
        if any_terms and not any(self._text_has_terms(combined, (term,)) for term in any_terms):
            return None
        return {"text": combined, "candidates": candidates}

    def _detect_center_text_banner_in_image(self, image):
        if image is None or image.size == 0:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        if width < 240 or height < 60:
            return None

        dark_fraction_by_row = np.mean(gray < 62, axis=1)
        mean_by_row = np.mean(gray, axis=1)
        candidate_rows = (dark_fraction_by_row >= 0.58) & (mean_by_row <= 96)

        groups = []
        start = None
        for index, is_candidate in enumerate(candidate_rows):
            if is_candidate and start is None:
                start = index
            elif not is_candidate and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            groups.append((start, height))

        min_band_h = max(18, int(height * 0.13))
        max_band_h = max(min_band_h + 1, int(height * 0.72))
        center_x1 = int(width * 0.24)
        center_x2 = int(width * 0.76)
        side_w = max(16, int(width * 0.08))

        best = None
        for y1, y2 in groups:
            band_h = y2 - y1
            if band_h < min_band_h or band_h > max_band_h:
                continue
            band = gray[y1:y2, :]
            center = band[:, center_x1:center_x2]
            if center.size == 0:
                continue

            left_dark = float(np.mean(band[:, :side_w] < 72))
            right_dark = float(np.mean(band[:, width - side_w:] < 72))
            if left_dark < 0.55 or right_dark < 0.55:
                continue

            bright = center >= 178
            bright_ratio = float(np.mean(bright))
            if bright_ratio < 0.006 or bright_ratio > 0.22:
                continue

            bright_rows = float(np.mean(np.any(bright, axis=1)))
            bright_cols = float(np.mean(np.any(bright, axis=0)))
            if bright_rows < 0.16 or bright_cols < 0.12:
                continue

            edges = cv2.Canny(center, 70, 170)
            edge_ratio = float(np.mean(edges > 0))
            if edge_ratio < 0.010:
                continue

            contrast = float(np.percentile(center, 92) - np.percentile(center, 18))
            score = min(0.99, 0.50 + bright_ratio * 2.0 + edge_ratio * 2.8 + min(0.20, contrast / 900.0))
            if best is None or score > best["confidence"]:
                best = {
                    "source": "banner",
                    "confidence": score,
                    "band": (0, y1, width, band_h),
                    "bright_ratio": bright_ratio,
                    "edge_ratio": edge_ratio,
                }

        return best

    def _detect_center_text_banner_in_rois(self, rect, rois):
        if self.sc is None:
            return None
        best = None
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            info = self._detect_center_text_banner_in_image(image)
            if not info:
                continue
            info = dict(info)
            info["roi"] = roi
            if best is None or info.get("confidence", 0.0) > best.get("confidence", 0.0):
                best = info
        return best

    def _bait_shortage_text_matches(self, text):
        normalized = self._normalize_ui_text(text)
        if "鱼饵" not in normalized or "钓鱼" not in normalized:
            return False
        strong_terms = (
            "需要装备鱼饵",
            "装备鱼饵才可以钓鱼",
            "鱼饵才可以钓鱼",
            "需要鱼饵才可以钓鱼",
        )
        if any(term in normalized for term in strong_terms):
            return True
        return (
            ("需要" in normalized or "装备" in normalized)
            and "才可以" in normalized
            and "钓鱼" in normalized
        )

    def _detect_bait_shortage_text_in_image(self, image):
        candidates = self._read_text_candidates_from_image(image, mode="general")
        for text, score in candidates[:8]:
            if self._bait_shortage_text_matches(text):
                return {"text": text, "score": float(score or 0.0), "candidates": candidates}
        return None

    def _detect_bait_shortage_text_for_banner(self, rect, banner_info):
        if self.sc is None or not rect or not banner_info:
            return None
        roi = banner_info.get("roi")
        if not roi:
            return None
        image = self.sc.capture_relative(rect, *roi)
        if image is None or image.size == 0:
            return None

        band = banner_info.get("band")
        if band:
            _, y, _, h = band
            height = image.shape[0]
            y1 = max(0, int(y) - max(2, int(h * 0.10)))
            y2 = min(height, int(y + h) + max(2, int(h * 0.10)))
            if y2 > y1:
                info = self._detect_bait_shortage_text_in_image(image[y1:y2, :])
                if info:
                    info["roi"] = roi
                    info["source"] = "banner-ocr"
                    return info

        info = self._detect_bait_shortage_text_in_image(image)
        if info:
            info["roi"] = roi
            info["source"] = "banner-ocr"
            return info
        return None

    def _bait_shortage_visual_fallback_info(self, rect, banner_info):
        if not banner_info:
            return None
        confidence = float(banner_info.get("confidence", 0.0) or 0.0)
        bright_ratio = float(banner_info.get("bright_ratio", 0.0) or 0.0)
        edge_ratio = float(banner_info.get("edge_ratio", 0.0) or 0.0)
        if confidence < 0.72:
            return None
        if bright_ratio < 0.030 or edge_ratio < 0.012:
            return None

        initial_cluster = self._detect_initial_control_cluster(rect)
        if initial_cluster.get("count", 0) >= 2 and initial_cluster.get("valid"):
            return {
                "source": "banner-visual",
                "initial_controls": initial_cluster,
                "confidence": confidence,
                "bright_ratio": bright_ratio,
                "edge_ratio": edge_ratio,
            }
        return None

    def _bait_shortage_context_allows_purchase(self, rect):
        initial_cluster = self._detect_initial_control_cluster(rect)
        return bool(initial_cluster.get("valid") and initial_cluster.get("count", 0) >= 2)

    def _detect_bait_shortage_prompt(self, rect, allow_ocr=True, require_visual_hint=False, allow_visual_fallback=False):
        banner_info = self._detect_center_text_banner_in_rois(
            rect,
            (
                (0.00, 0.40, 1.00, 0.24),
                (0.00, 0.44, 1.00, 0.18),
                (0.04, 0.40, 0.92, 0.24),
            ),
        )
        if not allow_ocr:
            return None
        if require_visual_hint and not banner_info:
            return None
        if not banner_info:
            return None

        text_info = self._detect_bait_shortage_text_for_banner(rect, banner_info)
        if text_info:
            text_info["banner"] = banner_info
            return text_info
        if allow_visual_fallback:
            visual_info = self._bait_shortage_visual_fallback_info(rect, banner_info)
            if visual_info:
                visual_info["banner"] = banner_info
                visual_info["text"] = ""
                return visual_info
        return None

    def _read_bait_item_name_texts(self, image):
        return self._read_text_candidates_from_image(image)

    def _bait_currency_scale_range(self, rect):
        if not rect:
            base_scale = 1.0
        else:
            base_scale = max(0.40, min(float(rect[3]) / 1080.0, 3.00))
        return max(0.30, base_scale * 0.55), min(4.00, base_scale * 1.65)

    def _bait_full_item_scale_range(self, rect):
        if not rect:
            base_scale = 1.0
        else:
            base_scale = max(0.40, min(float(rect[3]) / 1080.0, 3.00))
        return max(0.30, base_scale * 0.55), min(4.00, base_scale * 1.55)

    def _find_unlimited_bait_full_item_matches(self, shop_img, rect):
        if shop_img is None or shop_img.size == 0:
            return []

        strategies = (
            {"name": "full-gray", "threshold": 0.58, "use_mask": False, "scale_steps": 13, "priority": 3},
            {"name": "full-edge", "threshold": 0.64, "use_edge": True, "use_mask": False, "scale_steps": 9, "priority": 2},
            {"name": "full-gray-mask", "threshold": 0.74, "use_mask": True, "mask_threshold": 5, "scale_steps": 9, "priority": 1},
        )
        min_distance = max(36, int(min(shop_img.shape[:2]) * 0.070))
        scale_range = self._bait_full_item_scale_range(rect)
        matches = []
        for template in self._unlimited_bait_full_item_templates():
            for strategy in strategies:
                params = {key: value for key, value in strategy.items() if key not in {"name", "threshold", "scale_steps", "priority"}}
                for match in self.vis.find_template_matches(
                    shop_img,
                    template,
                    threshold=float(strategy["threshold"]),
                    max_matches=8,
                    min_distance=min_distance,
                    scale_range=scale_range,
                    scale_steps=int(strategy.get("scale_steps", 9)),
                    **params,
                ):
                    item = dict(match)
                    item["strategy"] = strategy["name"]
                    item["strategy_priority"] = int(strategy.get("priority", 0))
                    matches.append(item)

        deduped = []
        for item in sorted(matches, key=lambda value: (value.get("strategy_priority", 0), value.get("confidence", 0.0)), reverse=True):
            loc = item.get("location")
            if not loc:
                continue
            if any((loc[0] - kept["location"][0]) ** 2 + (loc[1] - kept["location"][1]) ** 2 < min_distance ** 2 for kept in deduped):
                continue
            deduped.append(item)
        return deduped[:8]

    def _bait_item_regions_from_full_match(self, shop_shape, item):
        if not shop_shape or not item:
            return None
        image_h, image_w = shop_shape[:2]
        cx, cy = item.get("location") or (0, 0)
        tw, th = item.get("size") or (167, 210)
        tw = max(60, int(tw))
        th = max(90, int(th))
        x1 = max(0, int(cx - tw * 0.50))
        y1 = max(0, int(cy - th * 0.50))
        x2 = min(image_w, int(cx + tw * 0.50))
        y2 = min(image_h, int(cy + th * 0.50))
        if x2 <= x1 or y2 <= y1:
            return None

        real_w = x2 - x1
        real_h = y2 - y1
        name_x1 = max(0, int(x1 + real_w * 0.12))
        name_x2 = min(image_w, int(x2 - real_w * 0.06))
        name_y1 = max(0, int(y1 + real_h * 0.38))
        name_y2 = min(image_h, int(y1 + real_h * 0.64))
        currency_x1 = max(0, int(x1 + real_w * 0.12))
        currency_x2 = min(image_w, int(x2 - real_w * 0.08))
        currency_y1 = max(0, int(y1 + real_h * 0.72))
        currency_y2 = min(image_h, y2)
        if name_x2 <= name_x1 or name_y2 <= name_y1 or currency_x2 <= currency_x1 or currency_y2 <= currency_y1:
            return None

        return {
            "card": (x1, y1, x2, y2),
            "name": (name_x1, name_y1, name_x2, name_y2),
            "currency": (currency_x1, currency_y1, currency_x2, currency_y2),
            "click": (int((name_x1 + name_x2) / 2), int((name_y1 + name_y2) / 2)),
        }

    def _find_unlimited_bait_currency_matches(self, shop_img, rect):
        if shop_img is None or shop_img.size == 0:
            return []

        strategies = (
            {"name": "gray-mask", "threshold": 0.64, "use_mask": True, "mask_threshold": 5, "scale_steps": 9},
            {"name": "gray", "threshold": 0.70, "use_mask": False, "scale_steps": 9},
            {"name": "edge", "threshold": 0.55, "use_edge": True, "use_mask": False, "scale_steps": 9},
            {"name": "binary", "threshold": 0.58, "use_binary": True, "binary_threshold": 155, "use_mask": False, "scale_steps": 7},
        )
        min_distance = max(16, int(min(shop_img.shape[:2]) * 0.030))
        scale_range = self._bait_currency_scale_range(rect)
        matches = []
        for template in self._unlimited_bait_currency_templates():
            for strategy in strategies:
                params = {k: v for k, v in strategy.items() if k not in {"name", "threshold", "scale_steps"}}
                for match in self.vis.find_template_matches(
                    shop_img,
                    template,
                    threshold=float(strategy["threshold"]),
                    max_matches=24,
                    min_distance=min_distance,
                    scale_range=scale_range,
                    scale_steps=int(strategy.get("scale_steps", 9)),
                    **params,
                ):
                    item = dict(match)
                    item["strategy"] = strategy["name"]
                    matches.append(item)

        deduped = []
        for item in sorted(matches, key=lambda value: value.get("confidence", 0.0), reverse=True):
            loc = item.get("location")
            if not loc:
                continue
            if any((loc[0] - kept["location"][0]) ** 2 + (loc[1] - kept["location"][1]) ** 2 < min_distance ** 2 for kept in deduped):
                continue
            deduped.append(item)
        return deduped[:24]

    def _bait_item_regions_from_currency(self, shop_shape, item):
        if not shop_shape or not item:
            return None
        image_h, image_w = shop_shape[:2]
        cx, cy = item.get("location") or (0, 0)
        tw, th = item.get("size") or (51, 28)
        tw = max(18, int(tw))
        th = max(12, int(th))

        card_w = int(max(110, min(image_w * 0.34, max(image_w * 0.20, tw * 3.65))))
        card_h = int(max(150, min(image_h * 0.36, max(image_h * 0.23, th * 7.20))))
        card_x1 = max(0, int(cx - card_w * 0.50))
        card_x2 = min(image_w, int(cx + card_w * 0.50))
        card_y1 = max(0, int(cy - card_h * 0.86))
        card_y2 = min(image_h, int(cy + card_h * 0.18))
        if card_x2 <= card_x1 or card_y2 <= card_y1:
            return None

        real_w = card_x2 - card_x1
        real_h = card_y2 - card_y1
        name_x1 = max(0, int(card_x1 + real_w * 0.14))
        name_x2 = min(image_w, int(card_x2 - real_w * 0.08))
        name_y1 = max(0, int(card_y1 + real_h * 0.38))
        name_y2 = min(image_h, int(card_y1 + real_h * 0.64))
        if name_x2 <= name_x1 or name_y2 <= name_y1:
            return None

        return {
            "card": (card_x1, card_y1, card_x2, card_y2),
            "name": (name_x1, name_y1, name_x2, name_y2),
            "currency": (max(0, int(card_x1 + real_w * 0.10)), max(0, int(card_y1 + real_h * 0.72)), card_x2, card_y2),
            "click": (int((name_x1 + name_x2) / 2), int((name_y1 + name_y2) / 2)),
        }

    def _point_in_region(self, point, region, margin=0):
        if not point or not region:
            return False
        x, y = point
        x1, y1, x2, y2 = region
        return x1 - margin <= x <= x2 + margin and y1 - margin <= y <= y2 + margin

    def _bait_currency_match_for_regions(self, regions, candidates):
        if not regions:
            return None
        target = regions.get("currency") or regions.get("card")
        card = regions.get("card")
        if not target or not card:
            return None
        for item in sorted(candidates or [], key=lambda value: value.get("confidence", 0.0), reverse=True):
            loc = item.get("location")
            if self._point_in_region(loc, target, margin=4) and self._point_in_region(loc, card, margin=4):
                return item
        return None

    def _bait_visual_card_confirmation(self, regions, currency_item, full_item):
        if not full_item or not regions or not currency_item:
            return False, ""
        card = regions.get("card")
        currency_region = regions.get("currency")
        loc = currency_item.get("location")
        if not card or not currency_region or not self._point_in_region(loc, currency_region, margin=4):
            return False, "currency_outside_region"

        x1, y1, x2, y2 = card
        card_w = max(1, int(x2 - x1))
        card_h = max(1, int(y2 - y1))
        cur_x, cur_y = loc
        if cur_y < y1 + card_h * 0.68 or cur_y > y2 + card_h * 0.04:
            return False, "currency_bad_vertical"

        cur_w, cur_h = currency_item.get("size") or (0, 0)
        try:
            cur_w = int(cur_w)
            cur_h = int(cur_h)
        except (TypeError, ValueError):
            cur_w = 0
            cur_h = 0
        if cur_w <= 0 or cur_h <= 0 or cur_w > card_w * 0.62 or cur_h > card_h * 0.32:
            return False, "currency_bad_size"
        if cur_x < x1 + card_w * 0.12 or cur_x > x2 - card_w * 0.10:
            return False, "currency_bad_horizontal"

        full_confidence = float(full_item.get("confidence", 0.0) or 0.0)
        currency_confidence = float(currency_item.get("confidence", 0.0) or 0.0)
        strategy = str(full_item.get("strategy") or "")
        if strategy == "full-gray" and full_confidence >= 0.58 and currency_confidence >= 0.90:
            return True, "full-gray-same-card-currency"
        if strategy == "full-edge" and full_confidence >= 0.68 and currency_confidence >= 0.92:
            return True, "full-edge-same-card-currency"
        if full_confidence >= 0.96 and currency_confidence >= 0.96:
            return True, "high-confidence-same-card-currency"
        return False, "visual_confidence_low"

    def _verify_unlimited_bait_item_card(self, shop_roi, shop_img, regions, currency_item, full_item=None, debug_records=None, debug_source=""):
        if shop_img is None or shop_img.size == 0 or not regions or not currency_item:
            return None
        name_x1, name_y1, name_x2, name_y2 = regions["name"]
        if name_x2 <= name_x1 or name_y2 <= name_y1:
            return None

        name_img = shop_img[name_y1:name_y2, name_x1:name_x2].copy()
        texts = self._read_bait_item_name_texts(name_img)
        combined = "".join(text for text, _ in texts[:6])
        name_confirmed = self._text_has_terms(combined, ("万能",)) and self._text_has_terms(combined, ("饵",))
        conflicting_name = any(
            float(score or 0.0) >= 0.55
            and self._text_has_terms(text, ("饵",))
            and not self._text_has_terms(text, ("万能",))
            for text, score in texts[:6]
        )
        currency_confidence = float(currency_item.get("confidence", 0.0) or 0.0)
        full_confidence = float((full_item or {}).get("confidence", 0.0) or 0.0)
        visual_card_confirmed, visual_confirm_reason = self._bait_visual_card_confirmation(regions, currency_item, full_item)
        record = None
        if debug_records is not None:
            record = {
                "source": debug_source or ("full" if full_item else "currency"),
                "regions": regions,
                "currency": currency_item,
                "full": full_item,
                "texts": texts[:6],
                "combined": combined,
                "name_confirmed": bool(name_confirmed),
                "conflicting_name": bool(conflicting_name),
                "visual_card_confirmed": bool(visual_card_confirmed),
                "visual_confirm_reason": visual_confirm_reason,
                "accepted": False,
                "reject_reason": "",
            }
            debug_records.append(record)
        if visual_card_confirmed and not name_confirmed:
            name_confirmed = True
            combined = "visual-full-card"
            if record is not None:
                record["name_confirmed"] = True
                record["combined"] = combined
        if conflicting_name and not name_confirmed:
            if record is not None:
                record["reject_reason"] = "conflicting_name"
            return None
        if not name_confirmed:
            if record is not None:
                record["reject_reason"] = "name_not_confirmed"
            return None

        click_local = regions["click"]
        ratio = self._client_ratio_from_roi_point(shop_roi, shop_img.shape, click_local)
        if ratio is None:
            if record is not None:
                record["reject_reason"] = "bad_click_ratio"
            return None
        confidence = currency_confidence
        source = "name+currency"
        template = currency_item.get("template")
        strategy = currency_item.get("strategy")
        if full_item:
            confidence = min(0.99, (currency_confidence + full_confidence) / 2.0)
            source = "full+name+currency"
            template = full_item.get("template") or template
            full_strategy = full_item.get("strategy") or ""
            currency_strategy = currency_item.get("strategy") or ""
            strategy = f"{full_strategy}+{currency_strategy}".strip("+")
        if record is not None:
            record["accepted"] = True
            record["reject_reason"] = ""
        return {
            "location": click_local,
            "click_ratio": ratio,
            "confidence": confidence,
            "text": combined,
            "template": template,
            "strategy": strategy,
            "source": source,
            "visual_card_confirmed": bool(visual_card_confirmed),
            "visual_confirm_reason": visual_confirm_reason,
        }

    def _debug_output_dir(self):
        return Path(".")

    def _write_debug_image(self, path, image):
        if image is None or image.size == 0:
            return False
        try:
            ok, encoded = cv2.imencode(".png", image)
            if not ok:
                return False
            encoded.tofile(str(path))
            return True
        except Exception:
            return False

    def _match_bbox(self, item):
        if not item:
            return None
        cx, cy = item.get("location") or (0, 0)
        tw, th = item.get("size") or (0, 0)
        try:
            tw = max(2, int(tw))
            th = max(2, int(th))
            return (int(cx - tw / 2), int(cy - th / 2), int(cx + tw / 2), int(cy + th / 2))
        except (TypeError, ValueError):
            return None

    def _draw_debug_region(self, image, region, color, label=None, thickness=2):
        if image is None or region is None:
            return
        h, w = image.shape[:2]
        x1, y1, x2, y2 = region
        x1 = max(0, min(w - 1, int(x1)))
        y1 = max(0, min(h - 1, int(y1)))
        x2 = max(0, min(w - 1, int(x2)))
        y2 = max(0, min(h - 1, int(y2)))
        if x2 <= x1 or y2 <= y1:
            return
        cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)
        if label:
            cv2.putText(image, str(label)[:28], (x1 + 2, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)

    def _format_bait_match_debug_line(self, index, item):
        if not item:
            return f"{index}: none"
        return (
            f"{index}: loc={item.get('location')} size={item.get('size')} "
            f"conf={float(item.get('confidence', 0.0) or 0.0):.4f} "
            f"strategy={item.get('strategy') or ''} template={Path(str(item.get('template') or '')).name}"
        )

    def _bait_debug_record_score(self, record):
        if not record:
            return (0, 0, 0.0, 0.0, 0.0)
        currency_item = record.get("currency") or {}
        full_item = record.get("full") or {}
        return (
            1 if record.get("accepted") else 0,
            1 if record.get("visual_card_confirmed") else 0,
            float(full_item.get("confidence", 0.0) or 0.0),
            float(currency_item.get("confidence", 0.0) or 0.0),
            0.0 if record.get("reject_reason") == "missing_same_card_currency" else 1.0,
        )

    def _save_bait_shop_debug_snapshot(self, rect, reason="locate_failed"):
        if not self.config.get("bait_shop_debug_mode", False):
            return None
        payload = getattr(self, "_last_bait_shop_debug_payload", None)
        if not payload:
            return None
        shop_img = payload.get("shop_img")
        if shop_img is None or shop_img.size == 0:
            return None

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = self._debug_output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        base = f"debug_bait_shop_candidates_{timestamp}"
        image_path = output_dir / f"{base}.png"
        details_path = output_dir / f"{base}.txt"
        full_path = output_dir / f"debug_bait_shop_fullscreen_{timestamp}.png"

        annotated = shop_img.copy()
        full_matches = list(payload.get("full_matches") or [])
        currency_matches = list(payload.get("currency_matches") or [])
        records = list(payload.get("records") or [])

        for index, item in enumerate(full_matches[:6], start=1):
            self._draw_debug_region(annotated, self._match_bbox(item), (0, 210, 255), f"F{index} {float(item.get('confidence', 0.0) or 0.0):.2f}", 2)
        for index, item in enumerate(currency_matches[:10], start=1):
            loc = item.get("location")
            if loc:
                x, y = int(loc[0]), int(loc[1])
                cv2.circle(annotated, (x, y), 5, (255, 0, 255), 2)
                cv2.putText(annotated, f"C{index}", (x + 5, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 0, 255), 1, cv2.LINE_AA)

        records_to_draw = sorted(records, key=self._bait_debug_record_score, reverse=True)[:10]
        for index, record in enumerate(records_to_draw, start=1):
            regions = record.get("regions") or {}
            color = (0, 220, 0) if record.get("accepted") else (0, 0, 255)
            label = f"R{index} ok" if record.get("accepted") else f"R{index} {record.get('reject_reason') or 'reject'}"
            self._draw_debug_region(annotated, regions.get("card"), color, label, 2)
            self._draw_debug_region(annotated, regions.get("name"), (255, 180, 0), f"N{index}", 1)
            self._draw_debug_region(annotated, regions.get("currency"), (255, 0, 255), f"M{index}", 1)
            click = regions.get("click")
            if click:
                cv2.drawMarker(annotated, (int(click[0]), int(click[1])), color, cv2.MARKER_CROSS, 14, 2)

        saved = self._write_debug_image(image_path, annotated)
        full_saved = False
        if self.sc is not None and rect:
            try:
                full_img = self.sc.capture_relative(rect, 0.0, 0.0, 1.0, 1.0)
                full_saved = self._write_debug_image(full_path, full_img)
            except Exception:
                full_saved = False

        lines = [
            f"reason={reason}",
            f"time={timestamp}",
            f"client_rect={rect}",
            f"shop_roi={payload.get('shop_roi')}",
            f"shop_image_shape={getattr(shop_img, 'shape', None)}",
            f"full_candidate_count={len(full_matches)}",
            f"currency_candidate_count={len(currency_matches)}",
            "",
            "[full_candidates]",
        ]
        lines.extend(self._format_bait_match_debug_line(index, item) for index, item in enumerate(full_matches, start=1))
        lines.append("")
        lines.append("[currency_candidates]")
        lines.extend(self._format_bait_match_debug_line(index, item) for index, item in enumerate(currency_matches, start=1))
        lines.append("")
        lines.append("[verification_records]")
        for index, record in enumerate(records, start=1):
            currency_item = record.get("currency") or {}
            full_item = record.get("full") or {}
            text_items = ", ".join(f"{text}:{float(score or 0.0):.2f}" for text, score in record.get("texts", [])[:6])
            lines.append(
                f"{index}: source={record.get('source')} accepted={record.get('accepted')} "
                f"reason={record.get('reject_reason')} card={record.get('regions', {}).get('card')} "
                f"name={record.get('regions', {}).get('name')} currency_region={record.get('regions', {}).get('currency')} "
                f"currency_loc={currency_item.get('location')} currency_conf={float(currency_item.get('confidence', 0.0) or 0.0):.4f} "
                f"full_loc={full_item.get('location')} full_conf={float(full_item.get('confidence', 0.0) or 0.0):.4f} "
                f"name_confirmed={record.get('name_confirmed')} conflicting={record.get('conflicting_name')} "
                f"visual_card_confirmed={record.get('visual_card_confirmed')} "
                f"visual_reason={record.get('visual_confirm_reason') or ''} "
                f"combined={record.get('combined')} texts={text_items}"
            )

        try:
            details_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            details_path = None

        if not saved and details_path is None:
            return None
        result = {"image": str(image_path) if saved else "", "details": str(details_path) if details_path else ""}
        if full_saved:
            result["full"] = str(full_path)
        return result

    def _detect_unlimited_bait_item(self, rect):
        if self.sc is None or not rect:
            return None
        shop_roi = (0.00, 0.10, 0.38, 0.86)
        shop_img = self.sc.capture_relative(rect, *shop_roi)
        if shop_img is None or shop_img.size == 0:
            return None
        debug_enabled = bool(self.config.get("bait_shop_debug_mode", False))
        if debug_enabled:
            self._last_bait_shop_debug_payload = None
        debug_records = [] if debug_enabled else None

        full_matches = self._find_unlimited_bait_full_item_matches(shop_img, rect)
        self._last_bait_full_candidate_count = len(full_matches)
        self._last_bait_full_best_confidence = max((item.get("confidence", 0.0) for item in full_matches), default=0.0)

        candidates = self._find_unlimited_bait_currency_matches(shop_img, rect)
        self._last_bait_currency_candidate_count = len(candidates)
        self._last_bait_currency_best_confidence = max((item.get("confidence", 0.0) for item in candidates), default=0.0)

        for item in full_matches:
            regions = self._bait_item_regions_from_full_match(shop_img.shape, item)
            if debug_records is not None and not regions:
                debug_records.append({"source": "full", "full": item, "regions": {}, "accepted": False, "reject_reason": "bad_region"})
                continue
            currency_item = self._bait_currency_match_for_regions(regions, candidates)
            if debug_records is not None and not currency_item:
                debug_records.append({"source": "full", "full": item, "regions": regions, "accepted": False, "reject_reason": "missing_same_card_currency"})
                continue
            result = self._verify_unlimited_bait_item_card(shop_roi, shop_img, regions, currency_item, full_item=item, debug_records=debug_records, debug_source="full")
            if result:
                return result

        for item in candidates:
            regions = self._bait_item_regions_from_currency(shop_img.shape, item)
            if not regions:
                if debug_records is not None:
                    debug_records.append({"source": "currency", "currency": item, "regions": {}, "accepted": False, "reject_reason": "bad_region"})
                continue
            result = self._verify_unlimited_bait_item_card(shop_roi, shop_img, regions, item, debug_records=debug_records, debug_source="currency")
            if result:
                return result
        if debug_enabled:
            self._last_bait_shop_debug_payload = {
                "shop_img": shop_img.copy(),
                "shop_roi": shop_roi,
                "rect": rect,
                "full_matches": full_matches,
                "currency_matches": candidates,
                "records": debug_records or [],
            }
        return None

    def _detect_bait_detail_identity_text(self, rect):
        rois = (
            (0.76, 0.18, 0.20, 0.12),
            (0.70, 0.16, 0.28, 0.18),
            (0.70, 0.34, 0.28, 0.28),
            (0.70, 0.38, 0.28, 0.24),
            (0.66, 0.20, 0.32, 0.45),
        )
        text_info = self._detect_text_terms_in_rois(
            rect,
            rois,
            mode="general",
        )
        text = (text_info or {}).get("text", "")
        self._last_bait_detail_identity_text = text[:40]
        has_name = self._text_has_terms(text, ("万能", "鱼饵"))
        has_desc = (
            self._text_has_terms(text, ("适合新手", "任何钓鱼点"))
            or self._text_has_terms(text, ("任何钓鱼点", "特殊效果"))
        )
        if not (has_name or has_desc):
            return None
        text_info["source"] = "detail-identity"
        text_info["identity"] = "name" if has_name else "description"
        return text_info

    def _detect_bait_detail_ready(self, rect):
        cost_info = self._detect_bait_detail_cost_marker(rect)
        if cost_info:
            self._last_bait_detail_cost_best_confidence = max(
                float(getattr(self, "_last_bait_detail_cost_best_confidence", 0.0) or 0.0),
                float(cost_info.get("confidence", 0.0) or 0.0),
            )
        identity_info = self._detect_bait_detail_identity_text(rect)
        if cost_info and identity_info:
            ready_info = dict(cost_info)
            ready_info["source"] = "detail-verified"
            ready_info["identity"] = identity_info
            return ready_info
        return None

    def _item_info_allows_visual_detail_confirm(self, item_info):
        if not item_info:
            return False
        if not item_info.get("visual_card_confirmed"):
            return False
        if item_info.get("source") != "full+name+currency":
            return False
        reason = str(item_info.get("visual_confirm_reason") or "")
        min_conf_by_reason = {
            "full-gray-same-card-currency": 0.78,
            "full-edge-same-card-currency": 0.82,
            "high-confidence-same-card-currency": 0.88,
        }
        min_conf = min_conf_by_reason.get(reason)
        if min_conf is None:
            return False
        return float(item_info.get("confidence", 0.0) or 0.0) >= min_conf

    def _detect_bait_detail_ready_after_verified_click(self, rect, item_info):
        detail_info = self._detect_bait_detail_ready(rect)
        if detail_info:
            return detail_info
        if not self._item_info_allows_visual_detail_confirm(item_info):
            return None
        cost_info = self._detect_bait_detail_cost_marker(rect, allow_high_confidence_fallback=True)
        if not cost_info:
            return None
        item_confidence = float((item_info or {}).get("confidence", 0.0) or 0.0)
        if item_confidence < 0.80:
            required_cost_confidence = 0.94
        elif item_confidence < 0.88:
            required_cost_confidence = 0.90
        else:
            required_cost_confidence = 0.84
        if float(cost_info.get("confidence", 0.0) or 0.0) < required_cost_confidence:
            return None
        ready_info = dict(cost_info)
        ready_info["source"] = "detail-verified-after-click"
        ready_info["identity"] = {
            "source": "clicked-card",
            "identity": "visual-card",
            "text": getattr(self, "_last_bait_detail_identity_text", "") or "",
        }
        return ready_info

    def _save_bait_detail_debug_snapshot(self, rect, item_info=None, reason="detail_verify_failed"):
        if not self.config.get("bait_shop_debug_mode", False):
            return None
        if self.sc is None or not rect:
            return None
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = self._debug_output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        image_path = output_dir / f"debug_bait_detail_{timestamp}.png"
        details_path = output_dir / f"debug_bait_detail_{timestamp}.txt"
        try:
            image = self.sc.capture_relative(rect, 0.60, 0.10, 0.40, 0.88)
        except Exception:
            image = None
        saved = self._write_debug_image(image_path, image)

        lines = [
            f"reason={reason}",
            f"time={timestamp}",
            f"client_rect={rect}",
            f"item_info={item_info or {}}",
            f"last_detail_cost_confidence={float(getattr(self, '_last_bait_detail_cost_best_confidence', 0.0) or 0.0):.4f}",
            f"last_detail_identity_text={getattr(self, '_last_bait_detail_identity_text', '') or ''}",
        ]
        try:
            details_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            details_path = None

        if not saved and details_path is None:
            return None
        return {"image": str(image_path) if saved else "", "details": str(details_path) if details_path else ""}

    def _detect_bait_detail_cost_marker(self, rect, allow_high_confidence_fallback=False):
        if self.sc is None or not rect:
            return None
        best = None
        rois = (
            (0.78, 0.76, 0.20, 0.14),
            (0.70, 0.74, 0.28, 0.18),
            (0.82, 0.78, 0.15, 0.10),
        )
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None or image.size == 0:
                continue
            loc, conf, matched_path, strategy = self.vis.find_best_template_multi_strategy(
                image,
                self._unlimited_bait_currency_templates(),
                (
                    {"name": "detail-cost-gray-mask", "threshold": 0.62, "use_mask": True, "mask_threshold": 5, "early_accept": 0.84},
                    {"name": "detail-cost-gray", "threshold": 0.68, "use_mask": False, "early_accept": 0.86},
                    {"name": "detail-cost-edge", "threshold": 0.54, "use_edge": True, "early_accept": 0.78},
                    {"name": "detail-cost-binary", "threshold": 0.56, "use_binary": True, "binary_threshold": 155, "early_accept": 0.78},
                ),
                threshold=0.62,
                scale_range=self._bait_currency_scale_range(rect),
                scale_steps=9,
            )
            if best is None or conf > best.get("confidence", 0.0):
                best = {"location": loc, "confidence": conf, "template": matched_path, "strategy": strategy, "roi": roi}
            if loc:
                pixel_confirmed = self._detail_cost_marker_has_expected_pixels(image, loc, rect, matched_path)
                if not pixel_confirmed and not (allow_high_confidence_fallback and conf >= 0.94):
                    continue
                return {
                    "source": "detail-cost",
                    "location": loc,
                    "confidence": conf,
                    "template": matched_path,
                    "strategy": strategy,
                    "roi": roi,
                    "pixel_confirmed": pixel_confirmed,
                }
        self._last_bait_detail_cost_best_confidence = best.get("confidence", 0.0) if best else 0.0
        return None

    def _detail_cost_marker_has_expected_pixels(self, image, loc, rect, matched_path=None):
        if image is None or image.size == 0 or not loc:
            return False
        height, width = image.shape[:2]
        if width <= 0 or height <= 0:
            return False

        base_scale = max(0.40, min(float(rect[3]) / 1080.0, 3.00)) if rect else 1.0
        template_w = int(round(51 * base_scale))
        template_h = int(round(28 * base_scale))
        if matched_path:
            template = self.vis._read_template(matched_path)
            if template is not None and template.size > 0:
                template_h, template_w = template.shape[:2]
                template_w = int(round(template_w * base_scale))
                template_h = int(round(template_h * base_scale))

        crop_w = max(34, int(template_w * 1.45))
        crop_h = max(22, int(template_h * 1.55))
        cx, cy = int(loc[0]), int(loc[1])
        x1 = max(0, cx - crop_w // 2)
        x2 = min(width, cx + crop_w // 2)
        y1 = max(0, cy - crop_h // 2)
        y2 = min(height, cy + crop_h // 2)
        if x2 <= x1 or y2 <= y1:
            return False

        crop = image[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if float(np.std(gray)) < 12.0:
            return False

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        saturated = (sat >= 45) & (val >= 80)
        shell_color = saturated & (((hue >= 75) & (hue <= 150)) | (hue <= 18))
        color_ratio = float(np.mean(shell_color))
        dark_ratio = float(np.mean(gray <= 70))
        bright_ratio = float(np.mean(gray >= 170))
        return color_ratio >= 0.025 and dark_ratio >= 0.015 and bright_ratio >= 0.040

    def _detect_bait_confirm_dialog(self, rect):
        visual_info = self._detect_bait_confirm_dialog_visual(rect)
        if visual_info:
            return visual_info

        rois = (
            (0.18, 0.36, 0.64, 0.30),
            (0.24, 0.42, 0.52, 0.20),
        )
        info = self._detect_text_terms_in_rois(
            rect,
            rois,
            required_terms=("购买", "万能"),
            any_terms=("495", "99", "花费", "是否"),
            mode="general",
        )
        if not info:
            return None
        text = info.get("text", "")
        if "495" not in text and "99" not in text and "花费" not in text:
            return None
        info["source"] = "confirm-ocr"
        return info

    def _save_bait_confirm_debug_snapshot(self, rect, reason="confirm_verify_failed"):
        if not self.config.get("bait_shop_debug_mode", False):
            return None
        if self.sc is None or not rect:
            return None
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = self._debug_output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        image_path = output_dir / f"debug_bait_confirm_{timestamp}.png"
        details_path = output_dir / f"debug_bait_confirm_{timestamp}.txt"
        try:
            image = self.sc.capture_relative(rect, 0.10, 0.28, 0.80, 0.52)
        except Exception:
            image = None
        saved = self._write_debug_image(image_path, image)

        lines = [
            f"reason={reason}",
            f"time={timestamp}",
            f"client_rect={rect}",
            f"last_confirm_visual_confidence={float(getattr(self, '_last_bait_confirm_dialog_best_confidence', 0.0) or 0.0):.4f}",
        ]
        try:
            details_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            details_path = None

        if not saved and details_path is None:
            return None
        return {"image": str(image_path) if saved else "", "details": str(details_path) if details_path else ""}

    def _detect_bait_confirm_dialog_visual(self, rect):
        if self.sc is None or not rect:
            return None
        best = None
        rois = (
            (0.10, 0.28, 0.80, 0.52),
            (0.16, 0.30, 0.68, 0.48),
            (0.20, 0.34, 0.60, 0.40),
        )
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            info = self._analyze_bait_confirm_dialog_image(image)
            if not info:
                continue
            info["roi"] = roi
            if best is None or info.get("confidence", 0.0) > best.get("confidence", 0.0):
                best = info
            click = info.get("click")
            if click:
                ratio = self._client_ratio_from_roi_point(roi, image.shape, click)
                if ratio is not None:
                    info["confirm_click_ratio"] = ratio
                    return info
        self._last_bait_confirm_dialog_best_confidence = best.get("confidence", 0.0) if best else 0.0
        return best

    def _analyze_bait_confirm_dialog_image(self, image):
        if image is None or image.size == 0:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        if width < 360 or height < 180:
            return None

        bright = gray >= 205
        row_ratio = np.mean(bright, axis=1)
        body_rows = row_ratio >= 0.72
        groups = []
        start = None
        for index, active in enumerate(body_rows):
            if active and start is None:
                start = index
            elif not active and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            groups.append((start, height))

        body = None
        min_body_h = max(36, int(height * 0.16))
        for y1, y2 in groups:
            if y2 - y1 < min_body_h:
                continue
            if y1 < height * 0.18 or y2 > height * 0.72:
                continue
            body = (y1, y2)
            break
        if body is None:
            return None

        body_img = image[body[0]:body[1], :]
        hsv = cv2.cvtColor(body_img, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        center_mask = np.zeros(hue.shape, dtype=bool)
        center_mask[:, int(width * 0.24):int(width * 0.76)] = True
        red = center_mask & (((hue <= 10) | (hue >= 168)) & (sat >= 45) & (val >= 115))
        red_ratio = float(np.mean(red))

        title_y1 = max(0, int(body[0] - height * 0.24))
        title_y2 = max(title_y1, int(body[0] - 2))
        has_title_band = False
        if title_y2 > title_y1:
            title_region = gray[title_y1:title_y2, :]
            center_title = title_region[:, int(width * 0.35):int(width * 0.65)]
            dark_ratio = float(np.mean(title_region <= 64))
            title_bright_ratio = float(np.mean(center_title >= 165)) if center_title.size else 0.0
            has_title_band = dark_ratio >= 0.42 and title_bright_ratio >= 0.0035

        button_region_y1 = max(body[1], int(height * 0.58))
        button_region_y2 = min(height, int(height * 0.94))
        if button_region_y2 <= button_region_y1:
            return None
        button_region = gray[button_region_y1:button_region_y2, :]
        _, button_mask = cv2.threshold(button_region, 170, 255, cv2.THRESH_BINARY)
        button_mask = cv2.morphologyEx(button_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5)), iterations=1)
        contours, _ = cv2.findContours(button_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        buttons = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < width * 0.14 or h < max(16, height * 0.045):
                continue
            if w > width * 0.40 or h > height * 0.20:
                continue
            buttons.append((x, y + button_region_y1, w, h))
        if len(buttons) < 2:
            return None
        if red_ratio < 0.0010 and not has_title_band:
            return None
        buttons.sort(key=lambda item: item[0])
        confirm_button = buttons[-1]
        click = (int(confirm_button[0] + confirm_button[2] / 2), int(confirm_button[1] + confirm_button[3] / 2))
        confidence = min(0.99, 0.60 + red_ratio * 18.0 + (0.14 if has_title_band else 0.0) + min(0.18, len(buttons) * 0.04))
        return {
            "source": "confirm-visual",
            "confidence": confidence,
            "body": body,
            "buttons": buttons,
            "click": click,
            "red_ratio": red_ratio,
            "has_title_band": has_title_band,
        }

    def _detect_bait_reward_popup(self, rect):
        rois = (
            (0.20, 0.30, 0.60, 0.42),
            (0.30, 0.34, 0.40, 0.20),
            (0.30, 0.76, 0.40, 0.14),
        )
        return self._detect_text_terms_in_rois(
            rect,
            rois,
            required_terms=(),
            any_terms=("获得物品", "点击空白区域关闭"),
            mode="general",
        )

    def _click_template_in_rois(self, rect, templates, rois, label, threshold=0.70):
        best = None
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi) if self.sc is not None else None
            if image is None:
                continue
            loc, conf, matched_path, strategy = self.vis.find_best_template_multi_strategy(
                image,
                templates,
                (
                    {"name": "gray-mask", "threshold": threshold, "use_mask": True, "mask_threshold": 6, "early_accept": max(threshold, 0.90)},
                    {"name": "edge", "threshold": max(0.54, threshold - 0.10), "use_edge": True, "early_accept": max(threshold, 0.88)},
                ),
                threshold=threshold,
                scale_range=self._template_scale_range(rect, 0.72, 1.42),
                scale_steps=5,
            )
            if best is None or conf > best["confidence"]:
                best = {"location": loc, "confidence": conf, "template": matched_path, "strategy": strategy, "roi": roi, "shape": image.shape}
            if loc:
                ratio = self._client_ratio_from_roi_point(roi, image.shape, loc)
                if ratio is not None:
                    return self._click_client_ratio(rect, ratio[0], ratio[1], label=label)
        if best is not None:
            self._log(f"[鱼饵] 未能定位{label}，最高置信度: {best.get('confidence', 0.0):.2f}")
        return False

    def _wait_for_bait_condition(self, rect, predicate, timeout=5.0, interval=0.25):
        deadline = time.time() + max(0.1, float(timeout))
        last_info = None
        while time.time() < deadline:
            if self._should_stop():
                return None
            current_rect = self.wm.get_client_rect() or rect
            last_info = predicate(current_rect)
            if last_info:
                return last_info
            if not self._sleep_interruptible(interval):
                return None
        return last_info

    def _detect_bait_purchase_exit_ready(self, rect):
        info = self._detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
        if info and info.get("location"):
            return info
        return None

    def _start_bait_purchase_flow(self, rect, shortage_info=None):
        amount = self._normalized_auto_buy_bait_amount()
        batches = self._bait_purchase_batch_count(amount)
        if batches <= 0:
            self._log("[鱼饵] 检测到鱼饵不足，但自动购买鱼饵已关闭，已停止自动钓鱼。")
            self.stop()
            return True
        self._bait_purchase_batches_target = batches
        self._bait_purchase_batches_done = 0
        self._bait_purchase_exit_shop_sent = False
        self._waiting_start_time = 0
        self._last_cast_time = 0
        self.current_state = self.STATE_BUYING_BAIT
        self._log(f"[鱼饵] 检测到鱼饵不足，准备自动购买 {batches * 99} 个万能鱼饵，共 {batches} 次。")
        return True

    def _check_bait_shortage_after_cast(self, rect):
        current_rect = self.wm.get_client_rect() or rect
        shortage_info = self._detect_bait_shortage_prompt(
            current_rect,
            allow_ocr=True,
            require_visual_hint=True,
            allow_visual_fallback=True,
        )
        if not shortage_info:
            return False
        if not self._bait_shortage_context_allows_purchase(current_rect):
            return False
        return self._start_bait_purchase_flow(current_rect, shortage_info)

    def _wait_after_cast_or_bait_shortage(self, rect, total_delay):
        cast_time = float(getattr(self, "_last_cast_time", 0) or time.time())
        deadline = cast_time + max(0.0, float(total_delay))
        prompt_deadline = cast_time + min(1.80, max(0.60, float(total_delay)))
        first_check_at = cast_time + 0.28
        interval = 0.16
        next_check = first_check_at

        while time.time() < deadline:
            if self._should_stop():
                return True
            now = time.time()
            if now <= prompt_deadline and now >= next_check:
                self._bait_shortage_check_last = now
                if self._check_bait_shortage_after_cast(rect):
                    return True
                next_check = now + interval
            sleep_for = min(0.04, max(0.0, deadline - time.time()))
            if sleep_for <= 0:
                break
            if not self._sleep_interruptible(sleep_for, step=0.02):
                return True
        return False

    def _set_floating_hidden_for_capture(self, hidden):
        if self.log_queue is None:
            return
        if hidden:
            self.log_queue.put("CMD_MAIN_HIDE_FOR_CAPTURE")
            self.log_queue.put("CMD_FLOATING_HIDE_FOR_CAPTURE")
        else:
            self.log_queue.put("CMD_FLOATING_RESTORE_AFTER_CAPTURE")
            self.log_queue.put("CMD_MAIN_RESTORE_AFTER_CAPTURE")
        if hidden:
            self._sleep_interruptible(0.30, step=0.02)

    def _detect_unlimited_bait_item_or_selected_detail(self, rect):
        detail_info = self._detect_bait_detail_ready(rect)
        if detail_info:
            result = dict(detail_info)
            result["already_selected"] = True
            result["source"] = "selected-detail"
            return result
        return self._detect_unlimited_bait_item(rect)

    def _run_bait_purchase_flow(self, rect):
        batches = int(getattr(self, "_bait_purchase_batches_target", 0) or 0)
        if batches <= 0:
            return False

        self.ctrl.release_all()
        self._set_floating_hidden_for_capture(True)
        try:
            return self._run_bait_purchase_flow_core(rect, batches)
        finally:
            self._set_floating_hidden_for_capture(False)

    def _run_bait_purchase_flow_core(self, rect, batches):
        self._log("[鱼饵] 正在按 R 进入钓鱼商店。")
        if not self._tap_key_if_running("R", duration=0.12):
            return False
        self._sleep_interruptible(0.70)

        item_info = self._wait_for_bait_condition(rect, self._detect_unlimited_bait_item_or_selected_detail, timeout=8.0, interval=0.35)
        if not item_info:
            full_best_conf = float(getattr(self, "_last_bait_full_best_confidence", 0.0) or 0.0)
            full_candidate_count = int(getattr(self, "_last_bait_full_candidate_count", 0) or 0)
            best_conf = float(getattr(self, "_last_bait_currency_best_confidence", 0.0) or 0.0)
            candidate_count = int(getattr(self, "_last_bait_currency_candidate_count", 0) or 0)
            debug_paths = self._save_bait_shop_debug_snapshot(rect, reason="initial_locate_failed")
            if debug_paths:
                self._log(f"[排错] 已保存鱼饵商店候选调试图: {debug_paths.get('image') or '未生成'}；明细: {debug_paths.get('details') or '未生成'}")
            self._log(f"[鱼饵] 未能在商店中定位无上限万能鱼饵商品，停止本次自动购买。完整候选数: {full_candidate_count}，最高完整特征置信度: {full_best_conf:.2f}；货币候选数: {candidate_count}，最高货币特征置信度: {best_conf:.2f}")
            return False

        for index in range(batches):
            if self._should_stop():
                return False
            current_rect = self.wm.get_client_rect() or rect
            item_info = self._wait_for_bait_condition(current_rect, self._detect_unlimited_bait_item_or_selected_detail, timeout=2.5, interval=0.25)
            if not item_info:
                debug_paths = self._save_bait_shop_debug_snapshot(current_rect, reason="loop_locate_failed")
                if debug_paths:
                    self._log(f"[排错] 已保存鱼饵商店候选调试图: {debug_paths.get('image') or '未生成'}；明细: {debug_paths.get('details') or '未生成'}")
                self._log("[鱼饵] 无法确认无上限万能鱼饵商品位置，停止本次自动购买。")
                return False
            detail_info = item_info if item_info.get("already_selected") else None
            if not detail_info:
                click_ratio = item_info.get("click_ratio")
                if not click_ratio or not self._click_client_ratio(current_rect, click_ratio[0], click_ratio[1], label="无上限万能鱼饵商品"):
                    return False
                self._sleep_interruptible(0.30)
                detail_info = self._wait_for_bait_condition(
                    current_rect,
                    lambda probe_rect: self._detect_bait_detail_ready_after_verified_click(probe_rect, item_info),
                    timeout=3.0,
                    interval=0.25,
                )
            if not detail_info:
                debug_paths = self._save_bait_detail_debug_snapshot(current_rect, item_info, reason="detail_after_click_failed")
                if debug_paths:
                    self._log(f"[排错] 已保存鱼饵详情调试图: {debug_paths.get('image') or '未生成'}；明细: {debug_paths.get('details') or '未生成'}")
                best_cost_conf = float(getattr(self, "_last_bait_detail_cost_best_confidence", 0.0) or 0.0)
                identity_text = getattr(self, "_last_bait_detail_identity_text", "") or "未识别"
                self._log(f"[鱼饵] 未能确认无上限万能鱼饵商品详情，停止本次自动购买。最高详情消耗特征置信度: {best_cost_conf:.2f}，详情文字: {identity_text}")
                return False

            current_rect = self.wm.get_client_rect() or current_rect
            if not self._click_template_in_rois(
                current_rect,
                self._bait_max_button_templates(),
                ((0.88, 0.78, 0.10, 0.16), (0.78, 0.78, 0.20, 0.16)),
                "购买数量最大值按钮",
                threshold=0.66,
            ):
                return False
            self._sleep_interruptible(0.25)

            current_rect = self.wm.get_client_rect() or current_rect
            if not self._click_client_ratio(current_rect, 0.84, 0.955, label="购买按钮"):
                return False

            confirm_info = self._wait_for_bait_condition(current_rect, self._detect_bait_confirm_dialog, timeout=3.5, interval=0.25)
            if not confirm_info:
                debug_paths = self._save_bait_confirm_debug_snapshot(current_rect, reason="confirm_after_buy_failed")
                if debug_paths:
                    self._log(f"[排错] 已保存鱼饵购买确认调试图: {debug_paths.get('image') or '未生成'}；明细: {debug_paths.get('details') or '未生成'}")
                best_confirm_conf = float(getattr(self, "_last_bait_confirm_dialog_best_confidence", 0.0) or 0.0)
                self._log(f"[鱼饵] 未能确认 99 个万能鱼饵购买弹窗，停止本次自动购买。最高弹窗视觉置信度: {best_confirm_conf:.2f}")
                return False

            current_rect = self.wm.get_client_rect() or current_rect
            confirm_click_ratio = confirm_info.get("confirm_click_ratio") or (0.603, 0.688)
            if not self._click_client_ratio(current_rect, confirm_click_ratio[0], confirm_click_ratio[1], label="购买确认按钮"):
                return False

            reward_info = self._wait_for_bait_condition(current_rect, self._detect_bait_reward_popup, timeout=6.0, interval=0.35)
            if not reward_info:
                self._log("[鱼饵] 未检测到获得物品提示，可能鱼鳞币不足或购买未完成，停止本次自动购买。")
                return False

            self._bait_purchase_batches_done = index + 1
            self._log(f"[鱼饵] 已完成第 {index + 1}/{batches} 次购买，本次获得 99 个万能鱼饵。")
            if not self._tap_key_if_running("esc", duration=0.12):
                return False
            self._sleep_interruptible(0.65)

        self._log("[鱼饵] 购买数量已达到设置值，正在退出钓鱼商店。")
        if not self._tap_key_if_running("esc", duration=0.12):
            return False
        self._bait_purchase_exit_shop_sent = True
        ready_info = self._wait_for_bait_condition(
            rect,
            self._detect_bait_purchase_exit_ready,
            timeout=6.0,
            interval=0.35,
        )
        if ready_info and ready_info.get("location"):
            self._log("[鱼饵] 已回到钓鱼初始界面，继续自动钓鱼。")
            return True
        self._log("[鱼饵] 未确认回到钓鱼初始界面，进入恢复流程。")
        return False

    def _detect_cast_prompt_after_settlement(self, rect):
        if self.sc is None or not rect:
            return None

        f_roi = getattr(self, "roi_f_btn", (0.75, 0.75, 0.25, 0.25))
        btn_img = self.sc.capture_relative(rect, *f_roi)
        if btn_img is None:
            return None

        loc, conf, matched_path, strategy_name = self.vis.find_best_template_multi_strategy(
            btn_img,
            self._f_button_templates(),
            (
                {"name": "settlement-f-gray", "threshold": 0.60, "use_mask": True, "early_accept": 0.94},
            ),
            threshold=0.60,
            scale_range=self._template_scale_range(rect, 0.82, 1.28),
            scale_steps=3,
        )
        if not loc:
            return None
        initial_cluster = self._detect_initial_control_cluster(rect)
        if initial_cluster.get("count", 0) < 2 or not initial_cluster.get("valid"):
            return None
        return {
            "kind": "钓鱼初始界面",
            "confidence": conf,
            "location": loc,
            "template": matched_path,
            "strategy": strategy_name,
            "initial_controls": initial_cluster,
        }

    def _enter_recovering(self, reason, record_empty=False, press_esc=False):
        self.ctrl.release_all()
        if record_empty:
            self.record_mgr.add_empty_catch()
            self._log("[恢复] 已记录一次空杆/失败尝试。")
        self._reset_round_state()
        self._recovery_start_time = time.time()
        self._recovery_reason = reason
        self._recovery_esc_requested = bool(press_esc)
        self._recovery_esc_sent = False
        self._recovery_second_esc_sent = False
        self._recovery_empty_recorded = bool(record_empty)
        self.current_state = self.STATE_RECOVERING
        self._log(f"[恢复] {reason}，开始等待可抛钩界面恢复。")

    def _filter_bar_detection(self, target_x, cursor_x, target_w, confidence, roi_width):
        now = time.time()
        if target_x is None or cursor_x is None or target_w is None:
            previous_time = getattr(self, "_last_valid_bar_time", 0)
            if previous_time and now - previous_time <= 0.70:
                fallback_target = self._last_valid_target_x if target_x is None else target_x
                fallback_cursor = self._last_valid_cursor_x if cursor_x is None else cursor_x
                fallback_width = self._last_valid_target_w if target_w is None else target_w
                if fallback_target is not None and fallback_cursor is not None and fallback_width is not None:
                    return fallback_target, fallback_cursor, fallback_width, max(float(confidence or 0.0), 0.30)
            return None, cursor_x, target_w, confidence

        min_confidence = self._normalize_ratio_config("bar_confidence_threshold", 0.45, 0.25, 0.85)
        if confidence < min_confidence:
            previous_time = getattr(self, "_last_valid_bar_time", 0)
            if previous_time and now - previous_time <= 0.70:
                fallback_cursor = self._last_valid_cursor_x if cursor_x is None else cursor_x
                return self._last_valid_target_x, fallback_cursor, self._last_valid_target_w, confidence
            return None, cursor_x, target_w, confidence

        previous_cursor_x = getattr(self, "_last_valid_cursor_x", None)
        previous_cursor_time = getattr(self, "_last_valid_cursor_time", 0)
        if previous_cursor_x is not None and previous_cursor_time and now - previous_cursor_time <= 0.75:
            cursor_jump = abs(cursor_x - previous_cursor_x)
            cursor_jump_limit = max(72, int(roi_width * 0.24))
            if cursor_jump > cursor_jump_limit and confidence < 0.86:
                self._bar_cursor_jump_reject_count = int(getattr(self, "_bar_cursor_jump_reject_count", 0)) + 1
                if self._bar_cursor_jump_reject_count <= 2:
                    cursor_x = previous_cursor_x
                    confidence = max(0.0, confidence * 0.75)
                else:
                    return None, cursor_x, target_w, confidence

        previous_x = getattr(self, "_last_valid_target_x", None)
        previous_w = getattr(self, "_last_valid_target_w", None) or target_w
        previous_time = getattr(self, "_last_valid_bar_time", 0)
        if previous_x is not None and previous_time and now - previous_time <= 0.9:
            width_jump_limit = max(int(roi_width * 0.42), int(previous_w * 2.25), 120)
            if target_w > width_jump_limit:
                self._bar_jump_reject_count = int(getattr(self, "_bar_jump_reject_count", 0)) + 1
                if now - previous_time <= 0.75:
                    return previous_x, cursor_x, previous_w, max(0.0, confidence * 0.55)
                return None, cursor_x, target_w, confidence
            jump = abs(target_x - previous_x)
            jump_limit = max(56, int(roi_width * 0.18), int(max(previous_w, target_w) * 1.55))
            if jump > jump_limit and confidence < 0.82:
                self._bar_jump_reject_count = int(getattr(self, "_bar_jump_reject_count", 0)) + 1
                if self._bar_jump_reject_count <= 3 and now - previous_time <= 0.65:
                    return previous_x, cursor_x, previous_w, max(0.0, confidence * 0.7)
                return None, cursor_x, target_w, confidence

        self._last_valid_target_x = int(target_x)
        self._last_valid_target_w = int(target_w)
        self._last_valid_bar_time = now
        self._last_valid_cursor_x = int(cursor_x)
        self._last_valid_cursor_time = now
        self._bar_jump_reject_count = 0
        self._bar_cursor_jump_reject_count = 0
        return target_x, cursor_x, target_w, confidence

    def _bar_local_to_client_x(self, rect, roi, target_x, cursor_x):
        """把不同溜鱼 ROI 内的局部 x 坐标统一到客户区 x 坐标。"""
        if not rect or not roi:
            return target_x, cursor_x
        roi_left = int(rect[2] * roi[0])
        converted_target = None if target_x is None else int(round(roi_left + float(target_x)))
        converted_cursor = None if cursor_x is None else int(round(roi_left + float(cursor_x)))
        return converted_target, converted_cursor

    def _should_draw_fishing_debug_frame(self):
        if not self.config.get("debug_mode", False):
            return False
        if self.debug_queue is None or self.debug_queue.qsize() >= 2:
            return False
        now = time.time()
        return getattr(self, "_last_debug_time", 0) == 0 or (now - self._last_debug_time) >= 0.25

    def _cursor_templates_for_current_frame(self):
        now = time.time()
        if getattr(self, "_fishing_control_started", False) or getattr(self, "_confirmed_fishing_bar", False):
            return None
        recent_cursor_time = getattr(self, "_last_valid_cursor_time", 0)
        if recent_cursor_time and now - recent_cursor_time <= 1.20:
            return None
        last_template_time = getattr(self, "_last_cursor_template_time", 0)
        if last_template_time and now - last_template_time < 0.80:
            return None
        self._last_cursor_template_time = now
        return self._cursor_templates()

    def _analyze_fishing_bar_roi(self, rect, roi, draw_debug=False):
        bar_img = self.sc.capture_relative(rect, *roi)
        if bar_img is None:
            return {
                "target_x": None,
                "cursor_x": None,
                "target_w": None,
                "debug_img": None,
                "confidence": 0.0,
                "width": int(rect[2] * roi[2]) if rect else 0,
                "roi": roi,
                "capture_failed": True,
            }

        target_x, cursor_x, target_w, debug_img, confidence = self.vis.analyze_fishing_bar(
            bar_img,
            cursor_template_paths=self._cursor_templates_for_current_frame(),
            cursor_color_reference_paths=self._cursor_templates(),
            target_color_reference_paths=self._target_bar_templates(),
            cursor_scale_range=self._template_scale_range(rect, 0.70, 1.55),
            cursor_scale_steps=5,
            draw_debug=draw_debug,
        )
        target_x, cursor_x = self._bar_local_to_client_x(rect, roi, target_x, cursor_x)
        return {
            "target_x": target_x,
            "cursor_x": cursor_x,
            "target_w": target_w,
            "debug_img": debug_img,
            "confidence": float(confidence or 0.0),
            "width": bar_img.shape[1],
            "roi": roi,
            "capture_failed": False,
        }

    def _select_fishing_bar_detection(self, rect, primary_roi):
        self._last_bar_capture_failed = False
        primary = self._analyze_fishing_bar_roi(
            rect,
            primary_roi,
            draw_debug=self._should_draw_fishing_debug_frame(),
        )
        if primary is None:
            return None, None, None, None, 0.0
        if primary.get("capture_failed"):
            self._last_bar_capture_failed = True
            return None, None, None, None, 0.0

        target_x, cursor_x, target_w, confidence = self._filter_bar_detection(
            primary.get("target_x"),
            primary.get("cursor_x"),
            primary.get("target_w"),
            primary.get("confidence"),
            primary.get("width") or int(rect[2] * primary_roi[2]),
        )
        return target_x, cursor_x, target_w, primary.get("debug_img"), confidence

    def _control_pixels(self, target_w):
        width = max(1.0, float(target_w or 0))
        release_cross = width * self._normalize_ratio_config("control_release_cross_ratio", 0.012, 0.006, 0.12)
        reengage = width * self._normalize_ratio_config("control_reengage_ratio", 0.018, 0.008, 0.18)
        switch_error = width * self._normalize_ratio_config("control_switch_ratio", 0.08, 0.035, 0.25)
        try:
            deadzone_pixels = float(self.config.get("t_deadzone", 1))
        except (TypeError, ValueError):
            deadzone_pixels = 1.0
        deadzone_pixels = max(0.4, min(deadzone_pixels, 30.0))
        release_cross = min(release_cross, max(0.35, deadzone_pixels * 0.55))
        reengage = min(reengage, max(0.60, deadzone_pixels * 0.95))
        return {
            "release_cross": max(0.35, min(release_cross, 8.0)),
            "reengage": max(0.60, min(reengage, 14.0)),
            "switch_error": max(3.0, min(switch_error, 24.0)),
        }

    def _choose_fishing_control_direction(self, error, target_w, target_velocity, total_signal, engage_threshold):
        pixels = self._control_pixels(target_w)
        current = int(getattr(self, "_fish_control_direction", 0) or 0)
        if current not in (-1, 0, 1):
            current = 0

        now = time.time()
        error = float(error)
        signed_error = error * current if current else 0.0

        if current:
            if signed_error <= -pixels["switch_error"]:
                return -current
            if now < getattr(self, "_fish_control_min_hold_until", 0) and signed_error > -pixels["switch_error"]:
                return current
            if signed_error <= -pixels["release_cross"]:
                return 0
            return current

        abs_error = abs(error)
        abs_signal = abs(float(total_signal))
        if abs_error >= pixels["reengage"]:
            return 1 if error > 0 else -1
        if abs_error >= pixels["release_cross"] and abs_signal >= max(1.0, float(engage_threshold)):
            return 1 if error > 0 else -1
        if abs_signal >= max(2.0, float(engage_threshold) * 1.35):
            return 1 if total_signal > 0 else -1
        return 0

    def _apply_fishing_control_direction(self, direction):
        with self._input_lock:
            if self._should_stop():
                self.ctrl.release_all()
                return
            direction = 1 if direction > 0 else (-1 if direction < 0 else 0)
            now = time.time()
            previous = int(getattr(self, "_fish_control_direction", 0) or 0)
            if direction != previous:
                self._fish_control_last_change = now
                if direction:
                    hold_time = self._normalize_ratio_config("control_min_hold_time", 0.14, 0.03, 0.35)
                    self._fish_control_min_hold_until = now + hold_time
                else:
                    self._fish_control_min_hold_until = 0
            self._fish_control_direction = direction

            if direction > 0:
                self.ctrl.key_up('A')
                self.ctrl.key_down('D')
            elif direction < 0:
                self.ctrl.key_up('D')
                self.ctrl.key_down('A')
            else:
                self.ctrl.release_all()

    def _hold_recent_fishing_control_on_gap(self):
        """短时截图/识别断帧时保持当前 A/D，避免白天高亮环境下游标停住。"""
        if not getattr(self, "_fishing_control_started", False):
            return False

        now = time.time()
        last_valid_time = getattr(self, "_last_valid_bar_time", 0)
        if not last_valid_time or now - last_valid_time > 0.55:
            return False

        current_direction = int(getattr(self, "_fish_control_direction", 0) or 0)
        if current_direction == 0:
            last_error = float(getattr(self, "_last_control_error", 0) or 0)
            target_w = getattr(self, "_last_control_target_w", None) or getattr(self, "_last_valid_target_w", None) or 80
            if abs(last_error) >= max(0.8, min(float(target_w) * 0.012, 4.0)):
                current_direction = 1 if last_error > 0 else -1

        if current_direction == 0:
            return False

        self._apply_fishing_control_direction(current_direction)
        return True

    def _default_ocr_root(self, package_name):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / package_name
        return Path.home() / f".{package_name}"

    def _copy_tree_missing(self, source, target):
        copied = 0
        source = Path(source)
        target = Path(target)
        if not source.exists():
            return copied
        for src in source.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(source)
            dst = target / rel
            try:
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                self._log(f"[识别] OCR 模型文件复制失败: {dst}，原因: {exc}")
                raise
        return copied

    def _prepare_ocr_runtime_roots(self):
        """把随程序分发的 OCR 模型复制到 cnocr/cnstd 默认可写缓存目录。"""
        if self._ocr_roots is not None:
            return self._ocr_roots

        cnocr_root = Path(os.environ.get("CNOCR_HOME") or self._default_ocr_root("cnocr"))
        cnstd_root = Path(os.environ.get("CNSTD_HOME") or self._default_ocr_root("cnstd"))
        bundle_root = Path(resource_path(OCR_MODEL_BUNDLE_DIR))

        copied = 0
        if bundle_root.exists():
            copied += self._copy_tree_missing(bundle_root / "cnocr", cnocr_root)
            copied += self._copy_tree_missing(bundle_root / "cnstd", cnstd_root)
            if copied:
                self._log(f"[识别] 已补齐 OCR 本地模型缓存，共复制 {copied} 个文件。")

        os.environ["CNOCR_HOME"] = str(cnocr_root)
        os.environ["CNSTD_HOME"] = str(cnstd_root)
        self._ocr_roots = {"cnocr": cnocr_root, "cnstd": cnstd_root, "bundle": bundle_root}
        return self._ocr_roots

    def _missing_required_ocr_models(self):
        roots = self._prepare_ocr_runtime_roots()
        missing = []
        for package_name, rel_parts, filename in OCR_REQUIRED_MODELS:
            root = roots.get(package_name)
            if root is None:
                continue
            fp = root.joinpath(*rel_parts, filename)
            if not fp.exists():
                missing.append(fp)
        return missing

    def _package_version(self, package_name):
        try:
            return metadata.version(package_name)
        except metadata.PackageNotFoundError:
            return "未安装"
        except Exception:
            return "未知"

    def _set_ocr_init_error(self, phase, exc=None, detail=None):
        parts = [f"{phase}失败"]
        if detail:
            parts.append(detail)
        if exc is not None:
            parts.append(f"{type(exc).__name__}: {exc}")

        missing_models = self._missing_required_ocr_models()
        if missing_models:
            parts.append(
                "缺少本地 OCR 模型文件："
                + "；".join(str(path) for path in missing_models)
                + "。请使用包含 ocr_models 目录的完整发布包，或重新执行 build_release.ps1 打包。"
            )

        parts.append(
            "依赖版本："
            f"cnocr={self._package_version('cnocr')}，"
            f"cnstd={self._package_version('cnstd')}，"
            f"onnxruntime={self._package_version('onnxruntime')}，"
            f"rapidocr={self._package_version('rapidocr')}。"
        )

        self.last_ocr_init_error = " ".join(part for part in parts if part)
        if exc is not None:
            self.last_ocr_init_trace = traceback.format_exc(limit=6)
            self._log(f"[识别] OCR 详细异常: {self.last_ocr_init_trace.strip()}")
        self._log(f"[识别] OCR 模块{self.last_ocr_init_error}")

    def get_ocr_init_failure_message(self):
        if self.last_ocr_init_error:
            return "OCR 模块初始化失败：" + self.last_ocr_init_error
        missing_models = self._missing_required_ocr_models()
        if missing_models:
            return "OCR 模块初始化失败：本地 OCR 模型缺失，请使用完整发布包。"
        return "OCR 模块初始化失败，请检查完整发布包、cnocr/cnstd/onnxruntime 依赖与本地模型缓存。"

    def prepare_recognition_modules(self):
        """预热结算识别所需的 OCR 模块，避免首次上鱼时才加载导致卡顿。"""
        self.last_ocr_init_error = ""
        self.last_ocr_init_trace = ""
        self._prepare_ocr_runtime_roots()
        name_ocr = self._ensure_ocr("name")
        weight_ocr = self._ensure_ocr("weight")
        general_ocr = self._ensure_ocr("general")
        # 图像兜底匹配同样需要首次构建特征，放在初始化阶段完成。
        self._load_fish_matcher_refs()
        return name_ocr is not None and weight_ocr is not None and general_ocr is not None

    def _ensure_ocr(self, mode="general"):
        global CnOcr
        roots = self._prepare_ocr_runtime_roots()
        if CnOcr is None and not self._ocr_import_checked:
            self._ocr_import_checked = True
            try:
                from cnocr import CnOcr as LoadedCnOcr
                CnOcr = LoadedCnOcr
            except Exception as exc:
                self.ocr_available = False
                self._set_ocr_init_error("加载 cnocr/onnxruntime 依赖", exc)
                return None
        if CnOcr is None:
            self.ocr_available = False
            return None
        if not self.ocr_available:
            return None
        missing_models = self._missing_required_ocr_models()
        if missing_models:
            self.ocr_available = False
            self._set_ocr_init_error(
                "初始化本地模型",
                detail="随程序分发的 OCR 模型未能写入当前用户缓存。"
            )
            return None
        if mode not in self.ocr:
            try:
                common_kwargs = {
                    "det_model_name": "naive_det",
                    "rec_root": str(roots["cnocr"]),
                    "det_root": str(roots["cnstd"]),
                }
                if mode == "name":
                    self._log("[系统] 正在初始化鱼名 OCR 识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs)
                elif mode == "weight":
                    self._log("[系统] 正在初始化重量 OCR 识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs, cand_alphabet="0123456789gG克")
                else:
                    self._log("[系统] 正在初始化 OCR 单行识别模块...")
                    self.ocr[mode] = CnOcr(**common_kwargs)
            except Exception as exc:
                self.ocr_available = False
                self._set_ocr_init_error("初始化 OCR 模型", exc)
                self.ocr.pop(mode, None)
        return self.ocr.get(mode)

    def _collect_ocr_candidates(self, image, mode="general"):
        ocr = self._ensure_ocr(mode)
        if ocr is None or image is None or image.size == 0:
            return []

        candidates = []
        try:
            result = ocr.ocr_for_single_line(image)
        except Exception as exc:
            self._log(f"[识别] OCR 执行失败: {exc}")
            return []

        if isinstance(result, dict):
            cleaned = (result.get("text") or "").strip()
            if cleaned:
                candidates.append((cleaned, float(result.get("score") or 0.0)))
        elif result:
            cleaned = str(result).strip()
            if cleaned:
                candidates.append((cleaned, 0.0))

        if mode in {"name", "weight"}:
            candidates.sort(key=lambda item: item[1], reverse=True)
            return candidates

        if getattr(ocr, "det_model", None) is not None:
            try:
                results = ocr.ocr(image)
            except Exception:
                results = []
            for item in results or []:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                score = item.get("score", 0.0) if isinstance(item, dict) else 0.0
                cleaned = (text or "").strip()
                if cleaned:
                    candidates.append((cleaned, float(score or 0.0)))

        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def _collect_ocr_texts(self, image):
        return [text for text, _ in self._collect_ocr_candidates(image)]

    def _crop_name_text_region(self, image):
        if image is None or image.size == 0:
            return image

        # 结算鱼名是白色描边字，背景常有高亮光效；优先只框选中心标题行的低饱和高亮文字。
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 150), (179, 80, 255))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = image.shape[:2]
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < 20 or h < max(6, int(height * 0.10)) or w < 4:
                continue
            if w > width * 0.45 or h > height * 0.75:
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        center_x = width / 2
        center_y = height / 2
        boxes.sort(key=lambda item: abs((item[0] + item[2] / 2) - center_x) + abs((item[1] + item[3] / 2) - center_y) * 0.55)
        row_y = boxes[0][1] + boxes[0][3] / 2
        row_boxes = [
            box for box in boxes
            if abs((box[1] + box[3] / 2) - row_y) < max(18, int(height * 0.20))
        ]

        x1 = min(x for x, _, _, _ in row_boxes)
        y1 = min(y for _, y, _, _ in row_boxes)
        x2 = max(x + w for x, _, w, _ in row_boxes)
        y2 = max(y + h for _, y, _, h in row_boxes)

        pad_x = max(8, int((x2 - x1) * 0.18))
        pad_y = max(6, int((y2 - y1) * 0.40))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        if (x2 - x1) * (y2 - y1) > width * height * 0.72:
            return image
        return image[y1:y2, x1:x2]

    def _crop_weight_digits_region(self, image):
        if image is None or image.size == 0:
            return image

        # 重量数字比单位 g 更高更粗；先按亮色主体分割，再只保留数字高度等级的连通区域。
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 135), (179, 115, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1)
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = image.shape[:2]
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < 18 or h < max(12, int(height * 0.24)) or w < 4:
                continue
            if w > width * 0.40 or h > height * 0.92:
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        max_height = max(h for _, _, _, h in boxes)
        top_y = min(y for _, y, _, h in boxes if h >= max_height * 0.70)
        digit_boxes = [
            box for box in boxes
            if box[3] >= max_height * 0.68 and box[1] <= top_y + max(8, int(max_height * 0.24))
        ]
        if not digit_boxes:
            return image

        x1 = min(x for x, _, _, _ in digit_boxes)
        y1 = min(y for _, y, _, _ in digit_boxes)
        x2 = max(x + w for x, _, w, _ in digit_boxes)
        y2 = max(y + h for _, y, _, h in digit_boxes)

        pad_x = max(4, int((x2 - x1) * 0.08))
        pad_y = max(4, int((y2 - y1) * 0.18))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        return image[y1:y2, x1:x2]

    def _crop_text_region(self, image, mode):
        if image is None or image.size == 0:
            return image

        if mode == "name":
            return self._crop_name_text_region(image)
        if mode == "weight":
            return self._crop_weight_digits_region(image)

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        threshold = 115 if mode == "name" else 135
        mask = cv2.inRange(gray, threshold, 255)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        height, width = gray.shape[:2]
        min_area = max(12, int(width * height * 0.0007))
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h < min_area or h < max(6, int(height * 0.10)):
                continue
            boxes.append((x, y, w, h))

        if not boxes:
            return image

        x1 = min(x for x, _, _, _ in boxes)
        y1 = min(y for _, y, _, _ in boxes)
        x2 = max(x + w for x, _, w, _ in boxes)
        y2 = max(y + h for _, y, _, h in boxes)

        pad_x = max(8, int((x2 - x1) * 0.16))
        pad_y = max(5, int((y2 - y1) * 0.28))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)

        if x2 <= x1 or y2 <= y1:
            return image
        if (x2 - x1) * (y2 - y1) > width * height * 0.88:
            return image
        return image[y1:y2, x1:x2]

    def _build_ocr_variants(self, image, mode):
        if image is None or image.size == 0:
            return []

        variants = []
        sources = [image]
        cropped = self._crop_text_region(image, mode)
        if cropped is not image and cropped is not None and cropped.size > 0:
            sources.insert(0, cropped)

        scales = (2.0, 3.0, 4.0) if mode == "name" else (2.0,)
        for source in sources:
            for scale in scales:
                enlarged = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)

                variants.append(enlarged)

                if mode == "name":
                    _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
                    variants.append(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR))
                    continue

                denoised = cv2.GaussianBlur(gray, (3, 3), 0)
                _, binary = cv2.threshold(denoised, 165, 255, cv2.THRESH_BINARY)
                inverted = cv2.bitwise_not(binary)
                variants.append(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR))
                variants.append(cv2.cvtColor(inverted, cv2.COLOR_GRAY2BGR))

        return variants

    def _parse_weight_text(self, text):
        raw_text = str(text or "").strip()
        if not raw_text:
            return 0

        normalized = raw_text.translate(str.maketrans({
            "O": "0",
            "o": "0",
            "〇": "0",
            "I": "1",
            "l": "1",
            "|": "1",
            "S": "5",
            "s": "5",
            "B": "8",
        }))
        compact = re.sub(r"\s+", "", normalized)

        explicit_match = re.search(r"(\d{1,5})(?:[gG克])", compact)
        if explicit_match:
            value = int(explicit_match.group(1))
            return value if 0 < value < 50000 else 0

        if not re.fullmatch(r"\d{1,6}", compact):
            loose_match = re.search(r"(\d{1,6})", compact)
            if not loose_match:
                return 0
            compact = loose_match.group(1)

        value = int(compact)
        return value if 0 < value < 50000 else 0

    def _extract_weight_value(self, texts):
        for text in texts:
            value = self._parse_weight_text(text)
            if value > 0:
                return value
        return 0

    def _is_plausible_name(self, text):
        cleaned = re.sub(r"\s+", "", text or "")
        if len(cleaned) < 2:
            return False
        banned = ["点击空白区域关闭", "获得钓鱼经验", "等级", "LEVEL", "RESULT", "MASTER"]
        return not any(token in cleaned for token in banned)

    def _read_roi_text(self, rect, rois, mode):
        best_text = ""
        weight_candidates = []
        known_fishes = self.record_mgr.get_encyclopedia() if mode == "name" else {}
        name_candidates = []

        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            for variant in self._build_ocr_variants(image, mode):
                candidates = self._collect_ocr_candidates(variant, mode)
                if not candidates:
                    continue
                if mode == "weight":
                    for text, score in candidates:
                        self._last_weight_ocr_candidates.append((text, score))
                        if score < 0.12:
                            continue
                        value = self._parse_weight_text(text)
                        if value <= 0:
                            continue
                        digit_count = len(str(value))
                        compact = re.sub(r"\s+", "", str(text or "").translate(str.maketrans({
                            "O": "0",
                            "o": "0",
                            "〇": "0",
                            "I": "1",
                            "l": "1",
                            "|": "1",
                            "S": "5",
                            "s": "5",
                            "B": "8",
                        })))
                        has_unit = 1 if re.search(r"\d{1,5}(?:[gG克])", compact) else 0
                        weight_candidates.append((value, float(score or 0.0), has_unit, digit_count, text))
                else:
                    for text, score in candidates:
                        if mode == "name":
                            self._last_name_ocr_candidates.append((text, score))
                            name_candidates.append((text, score))
                            if score >= 0.88:
                                resolved, resolved_score, _ = self.record_mgr.resolve_fish_name_candidates([(text, score)])
                                if resolved in known_fishes and resolved_score >= 1.0:
                                    return resolved, 0
                        if score < 0.16:
                            continue
                        if len(text) > len(best_text):
                            best_text = text

        if mode == "weight":
            if not weight_candidates:
                return "", 0

            explicit_candidates = [item for item in weight_candidates if item[2]]
            pure_candidates = [item for item in weight_candidates if not item[2]]
            explicit_best_score = max((item[1] for item in explicit_candidates), default=-1.0)
            pure_best_score = max((item[1] for item in pure_candidates), default=-1.0)

            if explicit_candidates and explicit_best_score >= pure_best_score - 0.18:
                pool = explicit_candidates
            else:
                pool = weight_candidates

            best_score = max(item[1] for item in pool)
            near_best = [item for item in pool if item[1] >= max(0.12, best_score - 0.08)]
            near_best.sort(key=lambda item: (min(item[3], 5), item[1]), reverse=True)
            return "", near_best[0][0]
        resolved, score, raw_text = self.record_mgr.resolve_fish_name_candidates(name_candidates)
        if resolved in known_fishes:
            if raw_text and raw_text != resolved:
                self._log(f"[识别] 鱼名 OCR 已按图鉴词典修正: {raw_text} -> {resolved} ({score:.2f})")
            return resolved, 0
        return "", 0

    def _load_fish_matcher_refs(self):
        if self._fish_matcher_refs is not None:
            return self._fish_matcher_refs

        refs = []
        orb = cv2.ORB_create(nfeatures=300)
        encyclopedia = self.record_mgr.get_encyclopedia()
        for name, data in encyclopedia.items():
            image_path = data.get("image_path", "")
            if not image_path or not os.path.exists(image_path):
                continue
            try:
                image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            except Exception:
                continue
            if image is None:
                continue
            if len(image.shape) == 3 and image.shape[2] == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)

            h, w = image.shape[:2]
            crop = image[int(h * 0.12):int(h * 0.82), int(w * 0.12):int(w * 0.88)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            _, descriptors = orb.detectAndCompute(gray, None)
            if descriptors is None:
                continue
            refs.append((name, descriptors))

        self._fish_matcher_refs = refs
        return refs

    def _match_fish_by_image(self, rect, rois):
        refs = self._load_fish_matcher_refs()
        if not refs:
            return ""

        orb = cv2.ORB_create(nfeatures=350)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        best_name = ""
        best_score = 0
        second_score = 0

        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None or image.size == 0:
                continue
            h, w = image.shape[:2]
            crop = image[int(h * 0.12):int(h * 0.88), int(w * 0.12):int(w * 0.88)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            _, query_desc = orb.detectAndCompute(gray, None)
            if query_desc is None:
                continue

            for name, ref_desc in refs:
                matches = matcher.knnMatch(query_desc, ref_desc, k=2)
                good_matches = [
                    m for pair in matches if len(pair) == 2 for m, n in [pair] if m.distance < 0.72 * n.distance
                ]
                score = len(good_matches)
                if score > best_score:
                    second_score = best_score
                    best_score = score
                    best_name = name
                elif score > second_score:
                    second_score = score

        if best_score >= 28 and best_score >= int(second_score * 1.4):
            return best_name
        return ""

    def _build_weight_digit_templates(self):
        if self._weight_digit_templates is not None:
            return self._weight_digit_templates

        font_candidates = [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\bahnschrift.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\impact.ttf",
        ]
        templates = {digit: [] for digit in "0123456789"}

        for font_path in font_candidates:
            if not os.path.exists(font_path):
                continue
            try:
                font = ImageFont.truetype(font_path, 92)
            except Exception:
                continue

            for digit in "0123456789":
                canvas = Image.new("L", (120, 140), 0)
                drawer = ImageDraw.Draw(canvas)
                bbox = drawer.textbbox((0, 0), digit, font=font, stroke_width=7)
                text_x = (120 - (bbox[2] - bbox[0])) // 2 - bbox[0]
                text_y = (140 - (bbox[3] - bbox[1])) // 2 - bbox[1]
                drawer.text(
                    (text_x, text_y),
                    digit,
                    font=font,
                    fill=255,
                    stroke_width=7,
                    stroke_fill=0,
                )
                arr = np.array(canvas)
                _, binary = cv2.threshold(arr, 110, 255, cv2.THRESH_BINARY)
                coords = cv2.findNonZero(binary)
                if coords is None:
                    continue
                x, y, w, h = cv2.boundingRect(coords)
                crop = binary[y:y + h, x:x + w]
                crop = cv2.resize(crop, (52, 84), interpolation=cv2.INTER_AREA)
                templates[digit].append(crop)

        self._weight_digit_templates = templates
        return templates

    def _classify_digit_image(self, image):
        templates = self._build_weight_digit_templates()
        if image is None or image.size == 0:
            return "", -1.0

        resized = cv2.resize(image, (52, 84), interpolation=cv2.INTER_AREA)
        best_digit = ""
        best_score = -1.0
        for digit, variants in templates.items():
            for template in variants:
                score = cv2.matchTemplate(resized, template, cv2.TM_CCOEFF_NORMED)[0][0]
                if score > best_score:
                    best_score = score
                    best_digit = digit
        return best_digit, best_score

    def _read_weight_by_template(self, rect, rois):
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None or image.size == 0:
                continue
            digit_image = self._crop_weight_digits_region(image)
            value = self._extract_weight_from_image_by_template(
                digit_image if digit_image is not None and digit_image.size > 0 else image
            )
            if value > 0:
                return value
        return 0

    def _extract_weight_from_image_by_template(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(gray, 175, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        h, w = binary.shape[:2]
        for cnt in contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            if ch < h * 0.38 or cw < 8 or cw > w * 0.28:
                continue
            if y > h * 0.78:
                continue
            boxes.append((x, y, cw, ch))

        if not boxes:
            return 0

        boxes.sort(key=lambda item: item[0])
        top_y = min(box[1] for box in boxes)
        max_height = max(box[3] for box in boxes)
        digits = []
        for x, y, cw, ch in boxes:
            if y > top_y + max_height * 0.12:
                continue
            pad = 4
            crop = binary[max(0, y - pad):min(h, y + ch + pad), max(0, x - pad):min(w, x + cw + pad)]
            digit, score = self._classify_digit_image(crop)
            if digit and score >= 0.18:
                digits.append(digit)

        if not digits:
            return 0

        try:
            return int("".join(digits))
        except ValueError:
            return 0

    def _format_name_ocr_candidates(self):
        unique = []
        seen = set()
        for text, score in sorted(self._last_name_ocr_candidates, key=lambda item: item[1], reverse=True):
            cleaned = str(text or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(f"{cleaned}({score:.2f})")
            if len(unique) >= 8:
                break
        return "、".join(unique)

    def _format_weight_ocr_candidates(self):
        unique = []
        seen = set()
        for text, score in sorted(self._last_weight_ocr_candidates, key=lambda item: item[1], reverse=True):
            cleaned = str(text or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(f"{cleaned}({score:.2f})")
            if len(unique) >= 6:
                break
        return "、".join(unique)

    def _save_unknown_settlement_debug(self, rect, name_rois):
        if not self.config.get("debug_mode", False) or self.sc is None:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        full_image = self.sc.capture_relative(rect, 0, 0, 1, 1)
        if full_image is not None and full_image.size > 0:
            path = f"debug_settlement_unknown_{timestamp}.png"
            cv2.imwrite(path, full_image)
            self._log(f"[排错] 已保存未知鱼类结算截图: {path}")
        for index, roi in enumerate(name_rois, start=1):
            roi_image = self.sc.capture_relative(rect, *roi)
            if roi_image is not None and roi_image.size > 0:
                path = f"debug_settlement_unknown_name_roi_{timestamp}_{index}.png"
                cv2.imwrite(path, roi_image)

    def _read_settlement_info(self, rect, save_unknown_debug=True):
        fish_name = ""
        weight_g = 0
        self._last_name_ocr_candidates = []
        self._last_weight_ocr_candidates = []
        self._last_weight_corrections = []

        name_rois = [
            (0.30, 0.14, 0.40, 0.12),
            (0.26, 0.12, 0.48, 0.15),
            (0.34, 0.16, 0.32, 0.10),
            (0.28, 0.18, 0.44, 0.11),
            (0.24, 0.10, 0.52, 0.20),
        ]
        fish_image_rois = [
            (0.33, 0.24, 0.34, 0.34),
            (0.30, 0.22, 0.40, 0.38),
            (0.36, 0.26, 0.28, 0.30),
        ]
        weight_rois = [
            (0.33, 0.62, 0.34, 0.14),
            (0.30, 0.60, 0.40, 0.16),
            (0.36, 0.64, 0.28, 0.12),
        ]
        sample_offsets = [0.0, 0.22, 0.46, 0.75, 1.05]

        elapsed = 0.0
        for target_offset in sample_offsets:
            sleep_for = max(0.0, target_offset - elapsed)
            if sleep_for > 0:
                if not self._sleep_interruptible(sleep_for):
                    return fish_name or "未知鱼类", weight_g
            elapsed = target_offset

            if not fish_name:
                candidate_name, _ = self._read_roi_text(rect, name_rois, "name")
                if candidate_name:
                    fish_name = candidate_name

            if weight_g <= 0:
                _, candidate_weight = self._read_roi_text(rect, weight_rois, "weight")
                if candidate_weight > 0:
                    weight_g = candidate_weight

            if weight_g <= 0:
                candidate_weight = self._read_weight_by_template(rect, weight_rois)
                if candidate_weight > 0:
                    weight_g = candidate_weight

            if fish_name and weight_g > 0:
                break

        if not fish_name and not self.ocr_available:
            candidate_name = self._match_fish_by_image(rect, fish_image_rois)
            if candidate_name:
                fish_name = candidate_name

        if fish_name:
            self._log(f"[识别] 结算鱼名识别结果: {fish_name}")
        else:
            candidates = self._format_name_ocr_candidates()
            if candidates:
                self._log(f"[识别] 鱼名 OCR 候选未命中图鉴: {candidates}")
            if save_unknown_debug:
                self._save_unknown_settlement_debug(rect, name_rois)
            fish_name = "未知鱼类"
            self._log("[识别] 未能稳定识别到鱼名，已按未知鱼类记录。")

        if weight_g > 0:
            if self._last_weight_corrections:
                raw_text, corrected = self._last_weight_corrections[-1]
                self._log(f"[识别] 重量 OCR 候选疑似把单位 g 识别为数字，已修正: {raw_text} -> {corrected} g")
            self._log(f"[识别] 结算重量识别结果: {weight_g} g")
        else:
            candidates = self._format_weight_ocr_candidates()
            if candidates:
                self._log(f"[识别] 重量 OCR 候选未能稳定解析: {candidates}")
            self._log("[识别] 未能稳定识别到重量，已按 0 g 记录。")

        return fish_name, weight_g

    def _run_loop(self):
        # 确保在当前线程中实例化 ScreenCapture
        self.sc = ScreenCapture()
        
        # 初始化与绑定窗口
        if not self.wm.find_window():
            self._log("错误: 未找到游戏进程 HTGame.exe。请确保游戏正在运行。")
            self.stop()
            return
            
        initial_rect = self.wm.get_client_rect()
        dpi_scale = self.wm.get_dpi_scale()
        if initial_rect:
            self._log(f"成功绑定游戏窗口。客户区: {initial_rect[2]}x{initial_rect[3]}，DPI倍率: {dpi_scale:.2f}")
        else:
            self._log(f"成功绑定游戏窗口。DPI倍率: {dpi_scale:.2f}")
        self.wm.set_foreground()
        if not self._sleep_interruptible(1): # 等待窗口置顶完成
            self.sc.close()
            return
        self.user_activity.reset()
        
        # ROI 定义 (相对于客户区宽高)
        # 缩小寻找 F 键的范围，只截取屏幕真正的右下角边缘，避免把中间的发光背景截进去
        ROI_F_BTN = (0.75, 0.75, 0.25, 0.25)
        self.roi_f_btn = ROI_F_BTN # 保存给其他状态使用
        
        # 恢复合理的高度范围，根据用户提供的精确比例进行定位：
        # 横向占比是30%到70% (X: 0.3, Width: 0.4)
        # 竖向占比是从5.56%到8.33% (Y: 0.0556, Height: 0.0277)
        ROI_FISHING_BAR = (0.3, 0.0556, 0.4, 0.0277) 
        
        ROI_CENTER_TEXT = (0.2, 0.2, 0.6, 0.5)
        
        # DEBUG 计数器，防止写爆硬盘
        debug_save_count = 0

        while self.is_running:
            # 1. 焦点保护机制
            if not self.wm.is_foreground():
                # 检查当前焦点是否是被我们自己的 Debug 窗口抢走了
                import win32gui
                fg_hwnd = win32gui.GetForegroundWindow()
                if win32gui.GetWindowText(fg_hwnd) == "Fishing Bar Tracker (Debug)":
                    # 如果是被 Debug 窗口抢走的，不要暂停按键，尝试切回去
                    self.wm.set_foreground()
                else:
                    self._log("警告: 游戏窗口失去焦点，暂停按键发送。")
                    self.ctrl.release_all()
                    if not self._sleep_interruptible(1):
                        break
                    continue

            # 2. 获取实时窗口坐标 (防止窗口被拖动)
            rect = self.wm.get_client_rect()
            if not rect:
                self._log("获取窗口坐标失败，请不要最小化游戏。")
                if not self._sleep_interruptible(1):
                    break
                continue

            if self._check_user_takeover(game_rect=rect):
                break
                
            # 3. 状态分发
            if self.current_state == self.STATE_IDLE:
                self._handle_idle(rect, ROI_F_BTN)
            elif self.current_state == self.STATE_WAITING:
                self._handle_waiting(rect, ROI_CENTER_TEXT)
            elif self.current_state == self.STATE_FISHING:
                self._handle_fishing(rect, ROI_FISHING_BAR)
            elif self.current_state == self.STATE_RESULT:
                self._handle_result(rect)
            elif self.current_state == self.STATE_FAILED:
                self._handle_failed()
            elif self.current_state == self.STATE_RECOVERING:
                self._handle_recovering(rect)
            elif self.current_state == self.STATE_BUYING_BAIT:
                self._handle_buying_bait(rect)
                
            # 控制基础循环帧率
            if not self._sleep_interruptible(0.01, step=0.01):
                break
            
        self.sc.close()

    def _handle_idle(self, rect, roi):
        if self._should_stop():
            return
        self._log("[待机] 正在检测右下角抛竿图标...")
            
        # DEBUG 计数器
        if not hasattr(self, '_debug_count'): self._debug_count = 0
        self._debug_count += 1

        ready_info = self._detect_ready_to_cast(rect, allow_heavy=(self._debug_count % 6 == 0))
        if self._should_stop():
            return
        
        if ready_info and ready_info.get("location"):
            if not self._send_cast_input(ready_info, "待机"):
                return
            if self._should_stop():
                return
            cast_delay = max(1, min(int(self.config.get("cast_animation_delay", 2)), 5))
            self._log(f"[待机] > 发送完成，等待 {cast_delay} 秒抛竿动画...")
            self.current_state = self.STATE_WAITING
            self._wait_after_cast_or_bait_shortage(rect, cast_delay)
            return
        else:
            now = time.time()
            if now - getattr(self, "_idle_result_check_last", 0) >= 1.20:
                self._idle_result_check_last = now
                success_info = self._detect_fast_success_result(rect, fast_only=True)
                if success_info and success_info.get("location"):
                    self._log("[待机] 检测到成功结算界面仍未关闭，优先处理结算。")
                    self._finish_fast_success_result(rect, success_info, source_label="待机")
                    return
                failed_info = self._detect_fast_failed_result(rect)
                if self._maybe_finish_failed_result(rect, failed_info, source_label="待机"):
                    self._log("[待机] 检测到失败提示仍未恢复，进入失败恢复流程。")
                    return
            if self._debug_count % 10 == 0 and self._debug_count <= 30:
                btn_img = self.sc.capture_relative(rect, *roi)
                if btn_img is not None:
                    cv2.imwrite("debug_f_btn_roi.png", btn_img)
                conf = ready_info.get("confidence") if ready_info else 0.0
                self._log(f"[排错] 抛竿图标匹配失败，最高置信度: {conf:.2f}。已保存当前截图至根目录 debug_f_btn_roi.png")
            self._sleep_interruptible(0.18)

    def _handle_waiting(self, rect, roi):
        # 每隔一小段时间检测一次即可，不需要过高频率
        if not self._sleep_interruptible(0.1):
            return
        if self._should_stop():
            return
        if getattr(self, '_waiting_start_time', 0) == 0:
            self._waiting_start_time = time.time()
        if getattr(self, '_last_cast_time', 0) == 0:
            self._last_cast_time = self._waiting_start_time

        now = time.time()
        text_img = self.sc.capture_relative(rect, *roi)
        if text_img is None: return
        
        # 每次重新抛竿后，重置 PID 控制器状态
        self.pid.reset()
        
        loc, conf, matched_path = self.vis.find_best_template(
            text_img,
            self._hook_text_templates(),
            threshold=0.68,
            use_edge=False,
            use_binary=False,
            scale_range=self._template_scale_range(rect, 0.62, 1.55),
            scale_steps=11,
        )
        
        if loc:
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            self._log(f"[等待] 识别到上钩提示 (置信度: {conf:.2f}，模板: {matched_name})，迅速按F！")
            if not self._tap_key_if_running('F'):
                return
            self._prepare_fishing_round_state(time.time())
            self._waiting_start_time = 0
            self._last_cast_time = 0
            self._waiting_recast_count = 0
            self._waiting_ready_recheck_last = 0
            self.current_state = self.STATE_FISHING
            # 移除了硬编码的 1.5 秒 sleep，改为在 _handle_fishing 中动态等待耐力条出现，
            # 这样对于出现极快的稀有鱼可以做到零延迟响应。
            return

        since_cast = now - getattr(self, "_last_cast_time", now)
        if 0.25 <= since_cast <= 2.20 and now - getattr(self, "_bait_shortage_check_last", 0) >= 1.0:
            self._bait_shortage_check_last = now
            shortage_info = self._detect_bait_shortage_prompt(rect, require_visual_hint=True, allow_visual_fallback=True)
            if shortage_info and self._bait_shortage_context_allows_purchase(rect):
                self._start_bait_purchase_flow(rect, shortage_info)
                return

        wait_timeout = max(20, min(int(self.config.get("hook_wait_timeout", 90)), 300))
        if now - self._waiting_start_time > wait_timeout:
            self._log(f"[等待] 超过 {wait_timeout} 秒未识别到上钩提示，释放按键并回到待机重新检测。")
            self._enter_recovering("抛竿后长时间未识别到上钩提示", record_empty=True, press_esc=True)
            return

        cast_retry_delay = max(6.0, min(float(self.config.get("cast_retry_delay", 8)), 30.0))
        if now - self._last_cast_time >= cast_retry_delay and now - getattr(self, '_waiting_ready_recheck_last', 0) >= 1.0:
            self._waiting_ready_recheck_last = now
            ready_info = self._detect_ready_to_cast(
                rect,
                allow_heavy=(now - self._last_cast_time >= cast_retry_delay + 4.0),
                require_initial_controls=False,
                include_f=False,
            )
            if ready_info and ready_info.get("location"):
                if self._check_bait_shortage_after_cast(rect):
                    return
                retry_count = int(getattr(self, '_waiting_recast_count', 0))
                max_retries = 2
                if retry_count < max_retries:
                    self._waiting_recast_count = retry_count + 1
                    self._log(f"[等待] 抛竿后仍检测到{ready_info.get('kind') or '初始钓鱼界面'}，判定可能未进入等待上钩流程，重试抛竿 ({self._waiting_recast_count}/{max_retries})。")
                    if not self._send_cast_input(ready_info, "等待"):
                        return
                    self._wait_after_cast_or_bait_shortage(rect, 1.40)
                    return
                self._log("[等待] 多次重试后仍停留在初始钓鱼界面，进入恢复流程。")
                self._enter_recovering("多次重发 F 后仍未进入抛竿流程", record_empty=False, press_esc=False)
                return


    def _handle_fishing(self, rect, roi):
        if self._should_stop():
            return
        # 记录进入溜鱼状态的时间，用于防卡死
        if getattr(self, '_fishing_start_time', 0) == 0:
            self._prepare_fishing_round_state(time.time())
            
        elapsed = time.time() - self._fishing_start_time
        if elapsed > self.fishing_timeout:
            self._log("[防卡死] 溜鱼超时，强制结束当前回合。")
            self._fishing_start_time = 0
            self.current_state = self.STATE_RESULT
            return

        recent_bar_seen = getattr(self, '_last_bar_seen_time', 0) and (time.time() - getattr(self, '_last_bar_seen_time', 0) <= 0.35)
        if elapsed >= 1.0 and not getattr(self, '_confirmed_fishing_bar', False) and not recent_bar_seen and self._check_terminal_result_before_bar(rect, elapsed):
            return

        target_x, cursor_x, target_w, debug_img, bar_confidence = self._select_fishing_bar_detection(rect, roi)
        
        # 性能优化：限制 Debug 图像的发送频率（一秒最多 10 帧），防止撑爆队列导致主线程阻塞
        if self.config.get("debug_mode", False) and debug_img is not None:
            now = time.time()
            if getattr(self, '_last_debug_time', 0) == 0 or (now - self._last_debug_time) >= 0.25:
                if self.debug_queue is not None and self.debug_queue.qsize() < 2:
                    self.debug_queue.put(debug_img)
                self._last_debug_time = now

        # 判断是否结束 (无论是成功还是鱼儿溜走，耐力条都会消失)
        if target_x is None or cursor_x is None:
            if getattr(self, "_last_bar_capture_failed", False):
                if getattr(self, "_capture_missing_start_time", 0) == 0:
                    self._capture_missing_start_time = time.time()
                capture_missing_elapsed = time.time() - self._capture_missing_start_time
                if capture_missing_elapsed <= 0.55:
                    if not self._hold_recent_fishing_control_on_gap():
                        self.ctrl.release_all()
                        self._fish_control_direction = 0
                        self._fish_control_min_hold_until = 0
                    return
                self._capture_missing_start_time = 0

            if self._hold_recent_fishing_control_on_gap():
                return

            # 安全保护：如果丢失目标，立刻释放所有按键，防止游标因为惯性飞出界
            self.ctrl.release_all()
            self._fish_control_direction = 0
            self._fish_control_min_hold_until = 0
            
            if not getattr(self, '_fishing_control_started', False):
                last_seen_time = getattr(self, '_last_bar_seen_time', 0)
                if last_seen_time and time.time() - last_seen_time > 0.55:
                    self._bar_seen_streak = 0
                    self._seen_fishing_bar = False
                    self._confirmed_fishing_bar = False
                    self._fishing_bar_confirmed_time = 0

                transition_elapsed = time.time() - self._fishing_start_time
                if transition_elapsed >= 1.0 and self._check_terminal_result_before_bar(rect, transition_elapsed):
                    return
                pre_control_timeout = max(10.0, min(float(self.config.get("pre_control_timeout", 14)), 30.0))
                if transition_elapsed > pre_control_timeout:
                    self._log(f"[溜鱼] 上钩后 {pre_control_timeout:.0f} 秒仍未进入有效溜鱼控制，进入恢复流程。")
                    self._enter_recovering("上钩后长时间未进入有效溜鱼控制", record_empty=True, press_esc=True)
                return

            if not getattr(self, '_confirmed_fishing_bar', False):
                last_seen_time = getattr(self, '_last_bar_seen_time', 0)
                if last_seen_time and time.time() - last_seen_time > 0.55:
                    self._bar_seen_streak = 0
                    self._seen_fishing_bar = False
                # 还没看到过耐力条，说明还在播放上钩的过渡动画
                # 增加一个初始等待超时，比如 5 秒
                transition_elapsed = time.time() - self._fishing_start_time
                if transition_elapsed >= 1.0 and self._check_terminal_result_before_bar(rect, transition_elapsed):
                    return
                if transition_elapsed > 5.0:
                    self._log("[溜鱼] 长时间未检测到耐力条，进入结果判定...")
                    self._enter_recovering("上钩后长时间未出现耐力条", record_empty=True, press_esc=True)
                return

            # 引入容错：偶尔一帧没识别到不算结束，连续丢失超过用户设定才算结束
            if getattr(self, '_missing_start_time', 0) == 0:
                self._missing_start_time = time.time()
                self._result_quick_check_last = 0
                self._result_full_check_last = self._missing_start_time

            missing_elapsed = time.time() - self._missing_start_time
            if missing_elapsed >= 0.12 and self._check_result_signals_after_bar_missing(rect, missing_elapsed):
                return

            missing_timeout = max(0.8, min(float(self.config.get("bar_missing_timeout", 2)), 5.0))
            if missing_elapsed > missing_timeout:
                self._log("[溜鱼] 耐力条消失，停止溜鱼，进入结果判定...")
                self.ctrl.release_all()
                self._fishing_start_time = 0
                self._missing_start_time = 0
                self._result_quick_check_last = 0
                self._last_cursor_x = None
                self._seen_fishing_bar = False
                self._last_target_time = 0  # 重置测速时间戳
                self._target_velocity = 0   # 重置速度历史
                self.current_state = self.STATE_RESULT
            return
        
        # 识别到了，重置丢失计时器，并标记已经看到过耐力条
        self._missing_start_time = 0
        self._capture_missing_start_time = 0
        self._result_quick_check_last = 0
        self._result_full_check_last = 0
        self._clear_result_ready_candidate()

        now = time.time()
        last_seen_time = getattr(self, '_last_bar_seen_time', 0)
        if last_seen_time and now - last_seen_time <= 0.55:
            self._bar_seen_streak = int(getattr(self, '_bar_seen_streak', 0)) + 1
        else:
            self._bar_seen_streak = 1
            self._bar_first_seen_time = now
        self._last_bar_seen_time = now
        self._seen_fishing_bar = True
        if not getattr(self, '_confirmed_fishing_bar', False) and self._bar_seen_streak >= 2:
            self._confirmed_fishing_bar = True
            self._fishing_bar_confirmed_time = now

        # === 核心追踪算法：直接误差 + 滞回保持 ===
        # A/D 是离散按键，不是连续舵量；真实游戏里视觉速度噪声较大，
        # 因此控制方向只使用当前可靠位置，避免速度预测把方向带偏。
        error = target_x - cursor_x
        abs_error = abs(error)
        self._last_control_error = error
        self._last_control_target_w = target_w

        now = time.time()
        if getattr(self, '_last_target_time', 0) == 0:
            self._last_target_x = target_x
            self._last_target_time = now
            target_velocity = 0
        else:
            dt = now - self._last_target_time
            if dt > 0.001:
                raw_velocity = (target_x - self._last_target_x) / dt
                old_velocity = getattr(self, '_target_velocity', 0)
                target_velocity = old_velocity * 0.70 + raw_velocity * 0.30
            else:
                target_velocity = getattr(self, '_target_velocity', 0)
            self._last_target_x = target_x
            self._last_target_time = now
            self._target_velocity = target_velocity
            
        # 动态安全区：低级鱼竿容错更小，默认更积极追赶。
        safe_zone_ratio = self._normalize_ratio_config("safe_zone_ratio", 0.08, 0.04, 0.28)
        safe_zone = target_w * safe_zone_ratio if target_w else 8
        
        # PID 控制器计算基础偏差修正力
        tracking_strength = self._normalize_tracking_strength()
        control_signal = self.pid.update(error) * tracking_strength
        
        ff_gain = self._normalize_ratio_config("feed_forward_gain", 0.18, 0.0, 0.45) * tracking_strength
        total_signal = control_signal + target_velocity * ff_gain

        # --- 纯非阻塞高频按键控制 ---
        # 动态阈值：
        # 如果游标在安全区内且目标没有高速移动，我们提高触发阈值，释放按键让游标自然滑动，避免左右鬼畜抽搐
        # 如果游标偏离或者目标正在高速逃离，我们降低阈值，要求立即按键追赶
        is_safe = (abs_error <= safe_zone) and (abs(target_velocity) < 90)
        hold_threshold = max(2, min(int(self.config.get("t_hold", 5)), 60))
        deadzone_threshold = max(1, min(int(self.config.get("t_deadzone", 1)), 30))
        threshold = hold_threshold if is_safe else deadzone_threshold

        direction = self._choose_fishing_control_direction(
            error,
            target_w,
            target_velocity,
            total_signal,
            threshold,
        )
        if direction:
            self._fishing_control_frame_count = int(getattr(self, "_fishing_control_frame_count", 0)) + 1
            if not getattr(self, "_fishing_control_started", False):
                self._fishing_control_started = True
                self._fishing_control_started_time = time.time()
            self._round_had_fishing_bar = True
        self._apply_fishing_control_direction(direction)

    def _detect_failed_result(self, rect):
        failed_templates = self._failed_text_templates()
        if not failed_templates:
            return None

        rois = (
            (0.18, 0.38, 0.64, 0.22),
            (0.20, 0.45, 0.60, 0.12),
            (0.12, 0.32, 0.76, 0.34),
        )
        strategies = (
            {"name": "failed-edge", "threshold": 0.60, "use_edge": True},
            {"name": "failed-plain", "threshold": 0.66},
        )
        best = None
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            loc, conf, matched_path, strategy = self.vis.find_best_template_multi_strategy(
                image,
                failed_templates,
                strategies,
                threshold=0.64,
                scale_range=self._template_scale_range(rect, 0.68, 1.42),
                scale_steps=7,
            )
            if best is None or conf > best["confidence"]:
                best = {"location": loc, "confidence": conf, "template": matched_path, "strategy": strategy, "roi": roi}
            if loc is not None:
                return best
        return None

    def _match_result_signal(self, rect, kind, templates, rois, strategies, threshold, low_factor, high_factor, scale_steps):
        if not templates:
            return None

        best = None
        for roi in rois:
            image = self.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            loc, conf, matched_path, strategy = self.vis.find_best_template_multi_strategy(
                image,
                templates,
                strategies,
                threshold=threshold,
                scale_range=self._template_scale_range(rect, low_factor, high_factor),
                scale_steps=scale_steps,
            )
            if best is None or conf > best["confidence"]:
                best = {
                    "kind": kind,
                    "location": loc,
                    "confidence": conf,
                    "template": matched_path,
                    "strategy": strategy,
                    "roi": roi,
                }
            if loc is not None:
                return best

        return None

    def _build_success_result_info(self, success_signals):
        best = max(success_signals, key=lambda item: item["confidence"])
        return {
            "location": best.get("location"),
            "confidence": min(0.99, sum(item["confidence"] for item in success_signals) / len(success_signals)),
            "template": best.get("template"),
            "strategy": best.get("strategy"),
            "signals": success_signals,
        }

    def _detect_ultrafast_success_result(self, rect):
        close_info = self._match_result_signal(
            rect,
            "click close prompt",
            self._success_close_prompt_templates(),
            (
                (0.22, 0.76, 0.56, 0.20),
            ),
            (
                {"name": "close-ultra-edge", "threshold": 0.70, "use_edge": True, "early_accept": 0.92},
            ),
            threshold=0.70,
            low_factor=0.82,
            high_factor=1.24,
            scale_steps=5,
        )
        if close_info and close_info.get("location") and close_info.get("confidence", 0.0) >= 0.84:
            return self._build_success_result_info([close_info])

        exp_info = self._match_result_signal(
            rect,
            "fishing exp prompt",
            self._success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.25),
            ),
            (
                {"name": "exp-ultra-edge", "threshold": 0.64, "use_edge": True, "early_accept": 0.90},
            ),
            threshold=0.64,
            low_factor=0.82,
            high_factor=1.24,
            scale_steps=5,
        )
        if exp_info and exp_info.get("location") and exp_info.get("confidence", 0.0) >= 0.92:
            weight_info = self._match_result_signal(
                rect,
                "重量单位 g",
                self._weight_unit_templates(),
                (
                    (0.33, 0.58, 0.34, 0.18),
                ),
                (
                    {"name": "g-ultra-plain", "threshold": 0.70},
                ),
                threshold=0.70,
                low_factor=0.82,
                high_factor=1.24,
                scale_steps=5,
            )
            if weight_info and weight_info.get("location"):
                return self._build_success_result_info([exp_info, weight_info])

        return None

    def _detect_fast_success_result(self, rect, fast_only=False):
        if self._detect_initial_f_prompt_quick(rect, threshold=0.88):
            return None
        ultra_info = self._detect_ultrafast_success_result(rect)
        if ultra_info and ultra_info.get("location"):
            return ultra_info
        if fast_only:
            return None

        close_info = self._match_result_signal(
            rect,
            "点击关闭提示",
            self._success_close_prompt_templates(),
            (
                (0.22, 0.76, 0.56, 0.20),
                (0.18, 0.74, 0.64, 0.24),
            ),
            (
                {"name": "close-fast-edge", "threshold": 0.66, "use_edge": True},
                {"name": "close-fast-plain", "threshold": 0.74},
            ),
            threshold=0.66,
            low_factor=0.62,
            high_factor=1.50,
            scale_steps=11,
        )
        if not close_info or not close_info.get("location"):
            return None

        success_signals = [close_info]
        if close_info.get("confidence", 0.0) >= 0.92:
            return self._build_success_result_info(success_signals)

        weight_info = self._match_result_signal(
            rect,
            "重量单位 g",
            self._weight_unit_templates(),
            (
                (0.33, 0.58, 0.34, 0.18),
                (0.30, 0.56, 0.42, 0.22),
            ),
            (
                {"name": "g-fast-plain", "threshold": 0.64},
            ),
            threshold=0.64,
            low_factor=0.70,
            high_factor=1.35,
            scale_steps=9,
        )
        if weight_info and weight_info.get("location"):
            success_signals.append(weight_info)
            return self._build_success_result_info(success_signals)

        exp_info = self._match_result_signal(
            rect,
            "获得钓鱼经验",
            self._success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.25),
                (0.18, 0.42, 0.64, 0.32),
            ),
            (
                {"name": "exp-fast-edge", "threshold": 0.58, "use_edge": True},
            ),
            threshold=0.58,
            low_factor=0.70,
            high_factor=1.35,
            scale_steps=9,
        )
        if exp_info and exp_info.get("location"):
            success_signals.append(exp_info)
            return self._build_success_result_info(success_signals)

        return None

    def _detect_fast_failed_result(self, rect):
        return self._match_result_signal(
            rect,
            "鱼儿溜走了",
            self._failed_text_templates(),
            (
                (0.18, 0.38, 0.64, 0.22),
            ),
            (
                {"name": "failed-fast-edge", "threshold": 0.64, "use_edge": True},
                {"name": "failed-fast-plain", "threshold": 0.70},
            ),
            threshold=0.68,
            low_factor=0.78,
            high_factor=1.26,
            scale_steps=5,
        )

    def _detect_success_result(self, rect):
        if self._detect_initial_f_prompt_quick(rect, threshold=0.88):
            return None

        success_signals = []

        weight_info = self._match_result_signal(
            rect,
            "重量单位 g",
            self._weight_unit_templates(),
            (
                (0.30, 0.58, 0.42, 0.22),
                (0.33, 0.60, 0.34, 0.18),
                (0.36, 0.62, 0.28, 0.16),
                (0.25, 0.54, 0.50, 0.30),
            ),
            (
                {"name": "g-edge", "threshold": 0.50, "use_edge": True},
                {"name": "g-plain", "threshold": 0.62},
            ),
            threshold=0.58,
            low_factor=0.45,
            high_factor=1.95,
            scale_steps=17,
        )
        if weight_info and weight_info.get("location"):
            success_signals.append(weight_info)

        close_info = self._match_result_signal(
            rect,
            "点击关闭提示",
            self._success_close_prompt_templates(),
            (
                (0.24, 0.76, 0.52, 0.20),
                (0.20, 0.80, 0.60, 0.16),
                (0.30, 0.82, 0.40, 0.14),
            ),
            (
                {"name": "close-edge", "threshold": 0.50, "use_edge": True},
                {"name": "close-plain", "threshold": 0.62},
            ),
            threshold=0.60,
            low_factor=0.52,
            high_factor=1.80,
            scale_steps=17,
        )
        if close_info and close_info.get("location"):
            success_signals.append(close_info)
            if len(success_signals) >= 2:
                return self._build_success_result_info(success_signals)

        exp_info = self._match_result_signal(
            rect,
            "获得钓鱼经验",
            self._success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.30),
                (0.30, 0.52, 0.40, 0.24),
                (0.18, 0.42, 0.64, 0.38),
            ),
            (
                {"name": "exp-edge", "threshold": 0.50, "use_edge": True},
                {"name": "exp-plain", "threshold": 0.60},
            ),
            threshold=0.58,
            low_factor=0.52,
            high_factor=1.80,
            scale_steps=17,
        )
        if exp_info and exp_info.get("location"):
            success_signals.append(exp_info)

        if len(success_signals) < 2:
            return None

        return self._build_success_result_info(success_signals)

    def _format_success_signals(self, success_info):
        parts = []
        for item in (success_info or {}).get("signals", []):
            matched_path = item.get("template")
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            parts.append(f"{item.get('kind') or '成功特征'}:{item.get('confidence', 0):.2f}/{matched_name}/{item.get('strategy') or '默认'}")
        return "；".join(parts) if parts else "无"

    def _record_empty_result_once(self, reason):
        if getattr(self, "_result_empty_recorded", False):
            return
        self.record_mgr.add_empty_catch()
        self._result_empty_recorded = True
        self._log(f"[结算] {reason}，已记录一次失败/空杆尝试。")

    def _finish_fast_success_result(self, rect, success_info, source_label="溜鱼"):
        self._clear_failed_result_candidate()
        self.ctrl.release_all()
        self._finish_success_result(rect, success_info, attempt=1, max_attempts=1, source_label=source_label)

    def _clear_failed_result_candidate(self):
        self._failed_result_candidate_seen_time = 0
        self._failed_result_candidate_count = 0
        self._failed_result_candidate_signature = ""

    def _is_strong_failed_result(self, failed_info):
        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = ((failed_info or {}).get("strategy") or "").lower()
        if "edge" in strategy:
            return confidence >= 0.70
        return confidence >= 0.76

    def _maybe_finish_failed_result(self, rect, failed_info, source_label="结算"):
        if not failed_info or not failed_info.get("location"):
            return False

        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = ((failed_info or {}).get("strategy") or "").lower()

        # 失败横幅只有一张文字模板。低置信度 plain 匹配容易在成功结算/过渡动画中误报，
        # 因此只允许高置信度立即判失败；其余候选必须连续出现并在确认前再次排除成功结算。
        if self._is_strong_failed_result(failed_info):
            self._clear_failed_result_candidate()
            self._finish_failed_result(failed_info, source_label=source_label)
            return True

        min_candidate = 0.64 if "edge" in strategy else 0.70
        if confidence < min_candidate:
            self._clear_failed_result_candidate()
            return False

        now = time.time()
        matched_path = failed_info.get("template") or ""
        roi = failed_info.get("roi") or ()
        signature = f"{matched_path}|{strategy}|{roi}"
        if signature != getattr(self, "_failed_result_candidate_signature", ""):
            self._failed_result_candidate_signature = signature
            self._failed_result_candidate_seen_time = now
            self._failed_result_candidate_count = 1
            return False

        self._failed_result_candidate_count = int(getattr(self, "_failed_result_candidate_count", 0)) + 1
        seen_time = float(getattr(self, "_failed_result_candidate_seen_time", 0) or now)
        if now - seen_time < 0.35 or self._failed_result_candidate_count < 2:
            return False

        success_info = self._detect_fast_success_result(rect, fast_only=False)
        if success_info and success_info.get("location"):
            self._clear_failed_result_candidate()
            self._finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        self._clear_failed_result_candidate()
        self._finish_failed_result(failed_info, source_label=source_label)
        return True

    def _check_result_signals_during_fishing(self, rect, elapsed):
        now = time.time()
        interval = self._normalize_ratio_config("fishing_result_check_interval", 0.65, 0.35, 1.50)
        if now - getattr(self, "_fishing_result_check_last", 0) < interval:
            return False
        self._fishing_result_check_last = now

        success_info = self._detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self._finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        failed_interval = self._normalize_ratio_config("fishing_failed_check_interval", 1.25, 0.70, 3.00)
        if elapsed >= 1.5 and now - getattr(self, "_fishing_failed_check_last", 0) >= failed_interval:
            self._fishing_failed_check_last = now
            failed_info = self._detect_fast_failed_result(rect)
            if self._maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        return False

    def _check_terminal_result_before_bar(self, rect, elapsed):
        now = time.time()
        interval = 0.25 if elapsed < 3.0 else 0.20
        if now - getattr(self, "_result_quick_check_last", 0) < interval:
            return False
        self._result_quick_check_last = now

        success_info = self._detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self._finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        if elapsed >= 2.0:
            failed_info = self._detect_fast_failed_result(rect)
            if self._maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        return False

    def _finish_failed_result(self, failed_info, source_label="结算"):
        matched_path = failed_info.get("template") if failed_info else None
        matched_name = Path(matched_path).name if matched_path else "未知模板"
        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = (failed_info or {}).get("strategy") or "默认"
        self._log(f"[{source_label}] 识别到“鱼儿溜走了”横幅 (置信度: {confidence:.2f}，模板: {matched_name}，策略: {strategy})！判定为钓鱼失败。")
        self.ctrl.release_all()
        self._enter_recovering("识别到鱼儿溜走失败提示", record_empty=True, press_esc=False)

    def _finish_empty_ready_result(self, ready_info, source_label="结算"):
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        if getattr(self, "_success_recorded_pending_close", False):
            self._log(f"[{source_label}] 已检测到{kind}，确认成功结算界面已关闭。当前累计钓获: {self.fish_count} 条。等待抛竿...")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return
        if getattr(self, "_round_had_fishing_bar", False):
            self._record_empty_result_once(f"未检测到成功结算或失败横幅，但已回到{kind}，判定本轮失败/空杆")
        self._log(f"[{source_label}] 已回到{kind}，直接进入待机。")
        self._reset_round_state()
        self.current_state = self.STATE_IDLE

    def _save_empty_ready_debug(self, rect, ready_info, source_label):
        if getattr(self, "_result_ready_debug_saved", False):
            return
        self._result_ready_debug_saved = True
        if not self.config.get("debug_mode", False) or self.sc is None:
            return
        image = self.sc.capture_relative(rect, 0, 0, 1, 1)
        if image is None or image.size <= 0:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = f"debug_result_empty_ready_{timestamp}.png"
        cv2.imwrite(path, image)
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        self._log(f"[排错] {source_label} 未识别到成功/失败但准备判定为空杆，已保存画面: {path}，检测到: {kind}")

    def _clear_result_ready_candidate(self):
        self._result_ready_seen_time = 0
        self._result_ready_confirm_count = 0
        self._result_ready_last_kind = ""

    def _round_fishing_elapsed(self):
        start_time = (
            getattr(self, "_fishing_control_started_time", 0)
            or getattr(self, "_fishing_bar_confirmed_time", 0)
            or getattr(self, "fishing_start_time", 0)
            or getattr(self, "_fishing_start_time", 0)
        )
        if not start_time:
            return 0.0
        return max(0.0, time.time() - start_time)

    def _is_known_settlement_name(self, fish_name):
        fish_name = (fish_name or "").strip()
        if not fish_name or fish_name in {"未知鱼类", "未识别鱼类"}:
            return False
        return fish_name in self.record_mgr.get_encyclopedia()

    def _try_finish_success_by_settlement_probe(self, rect, source_label="结算"):
        if getattr(self, "_result_text_probe_done", False):
            return False
        self._result_text_probe_done = True
        fish_name, weight_g = self._read_settlement_info(rect, save_unknown_debug=False)
        if not self._is_known_settlement_name(fish_name):
            return False
        success_info = {
            "confidence": 0.62,
            "signals": [
                {
                    "kind": "结算文字",
                    "confidence": 0.62,
                    "template": None,
                    "strategy": "settlement-text",
                }
            ],
        }
        self._finish_success_result(
            rect,
            success_info,
            attempt=1,
            max_attempts=1,
            source_label=source_label,
            settlement_info=(fish_name, weight_g),
        )
        return True

    def _confirm_empty_ready_result(self, rect, ready_info, source_label="结算"):
        if not ready_info or not ready_info.get("location"):
            return False
        if getattr(self, "_success_recorded_pending_close", False):
            self._finish_empty_ready_result(ready_info, source_label=source_label)
            return True

        success_info = self._detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self._finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        failed_info = self._detect_fast_failed_result(rect)
        if self._maybe_finish_failed_result(rect, failed_info, source_label=source_label):
            return True

        if not getattr(self, "_round_had_fishing_bar", False):
            self._finish_empty_ready_result(ready_info, source_label=source_label)
            return True

        now = time.time()
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        if self._result_ready_last_kind != kind or getattr(self, "_result_ready_seen_time", 0) == 0:
            self._result_ready_seen_time = now
            self._result_ready_confirm_count = 1
            self._result_ready_last_kind = kind
            elapsed = self._round_fishing_elapsed()
            self._log(f"[{source_label}] 本轮溜鱼耗时 {elapsed:.1f}s，已检测到{kind}；继续短暂确认成功/失败结算，避免误记空杆。")
            return False

        self._result_ready_confirm_count += 1
        confirm_delay = self._normalize_ratio_config("empty_ready_confirm_delay", 0.45, 0.25, 3.0)
        if getattr(self, "_round_had_fishing_bar", False):
            confirm_delay = max(confirm_delay, 3.0)
        min_confirm_count = 4 if getattr(self, "_round_had_fishing_bar", False) else 2
        if now - self._result_ready_seen_time < confirm_delay or self._result_ready_confirm_count < min_confirm_count:
            return False

        success_info = self._detect_success_result(rect)
        if success_info and success_info.get("location"):
            self._finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        failed_info = self._detect_failed_result(rect)
        if self._maybe_finish_failed_result(rect, failed_info, source_label=source_label):
            return True

        if getattr(self, "_round_had_fishing_bar", False):
            if self._try_finish_success_by_settlement_probe(rect, source_label=source_label):
                return True

        if getattr(self, "_round_had_fishing_bar", False):
            last_full_check = getattr(self, "_result_full_check_last", 0)
            if not last_full_check or now - last_full_check > 0.75:
                return False

        self._save_empty_ready_debug(rect, ready_info, source_label)
        self._finish_empty_ready_result(ready_info, source_label=source_label)
        return True

    def _wait_after_settlement_close(self, rect, max_delay):
        if getattr(self, "_stop_requested", False):
            return False
        deadline = time.time() + max_delay
        if not self._sleep_interruptible(min(0.18, max_delay)):
            return False
        while time.time() < deadline:
            if getattr(self, "_stop_requested", False):
                return False
            current_rect = self.wm.get_client_rect() or rect
            ready_info = self._detect_cast_prompt_after_settlement(current_rect)
            if not (ready_info and ready_info.get("location")):
                ready_info = self._detect_ready_to_cast(current_rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                self._log(f"[结算] 已检测到{ready_info.get('kind') or '可抛钩界面'}，提前进入下一轮。")
                return True
            if not self._sleep_interruptible(0.10):
                return False
        return False

    def _finish_success_result(self, rect, success_info, attempt=1, max_attempts=1, source_label="结算", settlement_info=None):
        if getattr(self, "_stop_requested", False):
            return
        self._clear_failed_result_candidate()
        if not getattr(self, "_success_recorded_pending_close", False):
            self._log(f"[{source_label}] 识别到成功结算组合特征 (综合置信度: {success_info['confidence']:.2f}，{self._format_success_signals(success_info)})，开始识别鱼类信息...")

            if settlement_info is None:
                fish_name, weight_g = self._read_settlement_info(rect)
            else:
                fish_name, weight_g = settlement_info
            if getattr(self, "_stop_requested", False):
                return
            self.record_mgr.add_catch(fish_name, weight_g)
            self.fish_count += 1
            self._success_recorded_pending_close = True
            if getattr(self, "_stop_requested", False):
                return

            self._log(f"[结算] 捕获: {fish_name}, 重量: {weight_g}g。尝试 ESC 关闭结算界面 (尝试 {attempt}/{max_attempts})...")
        else:
            self._log(f"[结算] 本次成功结算已记录，继续尝试 ESC 关闭结算界面 (尝试 {attempt}/{max_attempts})...")

        if not self._tap_key_if_running('esc', duration=0.15):
            return
        self._success_close_retry_count = max(int(getattr(self, "_success_close_retry_count", 0)), int(attempt))
        self._success_close_last_esc = time.time()
        if getattr(self, "_stop_requested", False):
            return

        close_delay = max(0.4, min(float(self.config.get("settlement_close_delay", 1)), 5.0))
        closed = self._wait_after_settlement_close(rect, close_delay)
        if getattr(self, "_stop_requested", False):
            return
        if closed:
            self._log(f"[结算] 成功关闭结算界面。当前累计钓获: {self.fish_count} 条。等待抛竿...")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        self._log("[结算] 已记录本次钓获，但尚未确认结算界面关闭，继续停留在结算状态重试 ESC。")
        self.current_state = self.STATE_RESULT

    def _check_result_signals_after_bar_missing(self, rect, missing_elapsed):
        now = time.time()
        interval = 0.12 if missing_elapsed < 1.5 else 0.22
        if now - getattr(self, "_result_quick_check_last", 0) < interval:
            return False
        self._result_quick_check_last = now

        success_info = self._detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self._finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        failed_info = self._detect_fast_failed_result(rect)
        if self._maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
            return True

        full_interval = 0.35 if missing_elapsed < 2.0 else 0.55
        if now - getattr(self, "_result_full_check_last", 0) >= full_interval:
            self._result_full_check_last = now
            success_info = self._detect_success_result(rect)
            if success_info and success_info.get("location"):
                self._finish_fast_success_result(rect, success_info, source_label="溜鱼")
                return True

            failed_info = self._detect_failed_result(rect)
            if self._maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        # STATE_FISHING must not use F/Q/E/R ready UI as a terminal signal.
        # Those translucent templates can false-positive on the fishing HUD/background
        # and stop reel control before settlement is actually reached.
        return False

    def _handle_result(self, rect):
        if self._should_stop():
            return
        self._log("[结算] 正在检测钓鱼结果...")

        max_attempts = 10 # 增加循环次数，但缩短每次的等待时间，实现更敏捷的响应
        if getattr(self, "_success_recorded_pending_close", False):
            ready_info = self._detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                self._finish_empty_ready_result(ready_info)
                return

            now = time.time()
            close_delay = max(0.4, min(float(self.config.get("settlement_close_delay", 1)), 5.0))
            retry_count = int(getattr(self, "_success_close_retry_count", 0))
            if now - getattr(self, "_success_close_last_esc", 0) >= max(0.75, close_delay):
                if retry_count < max_attempts:
                    self._finish_success_result(
                        rect,
                        {"confidence": 0.0, "signals": []},
                        attempt=retry_count + 1,
                        max_attempts=max_attempts,
                    )
                    return
                self._log("[结算] 成功结算界面多次 ESC 后仍未确认关闭，进入恢复流程继续处理。")
                self._enter_recovering("成功结算界面关闭未确认", record_empty=False, press_esc=True)
                return

            self._sleep_interruptible(0.15)
            return

        result_start = time.time()
        result_timeout = max(6.0, min(float(self.config.get("result_detect_timeout", 9.0)), 18.0))
        full_interval = 0.70
        for attempt in range(max_attempts):
            success_info = self._detect_fast_success_result(rect, fast_only=False)
            if success_info and success_info.get("location"):
                self._finish_success_result(rect, success_info, attempt=attempt + 1, max_attempts=max_attempts)
                return

            failed_info = self._detect_fast_failed_result(rect)
            if self._maybe_finish_failed_result(rect, failed_info):
                return

            now = time.time()
            if attempt == 0 or now - getattr(self, "_result_full_check_last", 0) >= full_interval:
                full_checked_at = time.time()
                success_info = self._detect_success_result(rect)
                if success_info and success_info.get("location"):
                    self._finish_success_result(rect, success_info, attempt=attempt + 1, max_attempts=max_attempts)
                    return
                failed_info = self._detect_failed_result(rect)
                if self._maybe_finish_failed_result(rect, failed_info):
                    return
                self._result_full_check_last = full_checked_at

            if getattr(self, "_round_had_fishing_bar", False) and (attempt >= 1 or time.time() - result_start >= 0.75):
                if self._try_finish_success_by_settlement_probe(rect, source_label="结算"):
                    return

            ready_info = self._detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                if self._confirm_empty_ready_result(rect, ready_info):
                    return
            else:
                self._clear_result_ready_candidate()
                    
            # 如果既没有 F 键，也没有底部文字，说明可能还在播放动画，稍微等一下继续循环
            if not self._sleep_interruptible(0.25):
                return
            if time.time() - result_start >= result_timeout:
                break

        # 如果试了多次还是不行，就强行重置，避免脚本卡死在这个状态
        self._log("[警告] 结算超时，强制返回待机状态。")
        self._enter_recovering("结算判定超时", record_empty=False, press_esc=True)

    def _handle_buying_bait(self, rect):
        if self._should_stop():
            return
        if getattr(self, "_bait_purchase_in_progress", False):
            self._sleep_interruptible(0.10)
            return
        self._bait_purchase_in_progress = True
        try:
            if self._run_bait_purchase_flow(rect):
                self._reset_round_state()
                self.current_state = self.STATE_IDLE
                return
            if not self._should_stop():
                press_esc = not bool(getattr(self, "_bait_purchase_exit_shop_sent", False))
                self._enter_recovering("自动购买鱼饵未完成", record_empty=False, press_esc=press_esc)
        finally:
            self._set_floating_hidden_for_capture(False)
            self._bait_purchase_in_progress = False

    def _handle_recovering(self, rect):
        if self._should_stop():
            return
        if getattr(self, "_recovery_start_time", 0) == 0:
            self._recovery_start_time = time.time()

        now = time.time()
        elapsed = now - self._recovery_start_time

        if getattr(self, "_recovery_esc_requested", False) and not getattr(self, "_recovery_esc_sent", False):
            self.ctrl.release_all()
            if not self._tap_key_if_running('esc', duration=0.15):
                return
            self._recovery_esc_sent = True
            self._sleep_interruptible(0.35)
            return

        ready_info = self._detect_ready_to_cast(rect, allow_heavy=(elapsed >= 2.0), require_initial_controls=True)
        if ready_info and ready_info.get("location"):
            self._log(f"[恢复] 已检测到{ready_info.get('kind') or '可抛钩提示'}，恢复到待机流程。")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        if getattr(self, "_recovery_esc_requested", False) and elapsed >= 3.0 and not getattr(self, "_recovery_second_esc_sent", False):
            self._log("[恢复] 暂未看到可抛钩提示，执行一次轻量 ESC 复位。")
            self.ctrl.release_all()
            if not self._tap_key_if_running('esc', duration=0.12):
                return
            self._recovery_second_esc_sent = True
            self._sleep_interruptible(0.35)
            return

        recovery_timeout = max(4, min(int(self.config.get("recovery_timeout", 8)), 20))
        if elapsed > recovery_timeout:
            reason = getattr(self, "_recovery_reason", "未知异常")
            self._log(f"[恢复] {reason} 后 {recovery_timeout} 秒仍未确认可抛钩界面，退回待机继续扫描；如画面已被用户接管，请手动停止脚本。")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        self._sleep_interruptible(0.2)

    def _handle_failed(self):
        # 注意: 这里的“溜走了”如果用户提供了图片，建议也走 find_template
        # 目前暂时作为占位或使用超时跳出
        self._log("[失败/结束] 释放按键，等待复位。")
        self._enter_recovering("进入失败兜底状态", record_empty=True, press_esc=False)
