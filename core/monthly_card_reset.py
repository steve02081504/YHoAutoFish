import time as time_module
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone


CONFIG_KEY_ENABLED = "monthly_card_daily_reset_enabled"
CONFIG_KEY_LAST_DATE = "monthly_card_daily_reset_last_date"

DEFAULT_CONFIG = {
    CONFIG_KEY_ENABLED: False,
    CONFIG_KEY_LAST_DATE: "",
}

BEIJING_TZ = timezone(timedelta(hours=8), "UTC+08:00")


class MonthlyCardDailyResetScheduler:
    """Decides whether the monthly-card daily reset should run."""

    def __init__(self, hour=5, minute=2, trigger_window_seconds=60, tz=BEIJING_TZ):
        self.hour = int(hour)
        self.minute = int(minute)
        self.trigger_window_seconds = max(1, int(trigger_window_seconds))
        self.tz = tz

    def beijing_now(self, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            return now.replace(tzinfo=self.tz)
        return now.astimezone(self.tz)

    def date_key(self, now=None):
        return self.beijing_now(now).date().isoformat()

    def should_trigger(self, enabled, last_triggered_date="", now=None):
        if not bool(enabled):
            return False

        current = self.beijing_now(now)
        if str(last_triggered_date or "") == current.date().isoformat():
            return False

        if current.hour != self.hour or current.minute != self.minute:
            return False

        return 0 <= current.second < self.trigger_window_seconds


def _call_bool(obj, method_name, default=False):
    method = getattr(obj, method_name, None)
    if method is None:
        return default
    try:
        return bool(method())
    except Exception:
        return default


def _ensure_game_window_ready(window_manager):
    if window_manager is None:
        return True

    is_alive = _call_bool(window_manager, "is_window_alive", default=True)
    if not is_alive and not _call_bool(window_manager, "find_window", default=False):
        return False

    set_foreground = getattr(window_manager, "set_foreground", None)
    if set_foreground is None:
        return True

    try:
        if set_foreground():
            return True
    except Exception:
        pass

    return _call_bool(window_manager, "is_foreground", default=False)


def _note_program_input(user_activity, keys, duration):
    if user_activity is None:
        return
    note = getattr(user_activity, "note_program_input", None)
    if note is None:
        return
    try:
        note(keys, duration=duration)
    except Exception:
        pass


def perform_double_escape_reset(
    controller,
    window_manager=None,
    user_activity=None,
    input_lock=None,
    delay_seconds=2.0,
    tap_duration=0.12,
    sleeper=None,
):
    """Focus the game and perform ESC, wait, ESC using the existing controller."""

    if controller is None:
        return False

    sleep = sleeper or time_module.sleep
    lock = input_lock if input_lock is not None else nullcontext()
    delay_seconds = max(0.0, float(delay_seconds))
    tap_duration = max(0.01, float(tap_duration))

    with lock:
        if not _ensure_game_window_ready(window_manager):
            return False

        controller.release_all()
        _note_program_input(user_activity, ("esc",), duration=delay_seconds + tap_duration * 2 + 0.60)
        controller.key_tap("esc", duration=tap_duration)
        sleep(delay_seconds)
        _note_program_input(user_activity, ("esc",), duration=tap_duration + 0.60)
        controller.key_tap("esc", duration=tap_duration)
        controller.release_all()
        return True
