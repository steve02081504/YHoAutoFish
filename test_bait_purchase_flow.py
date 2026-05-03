import os
import unittest

from core.state_machine import StateMachine


class RemovedBaitPurchaseStateMachineTest(unittest.TestCase):
    def test_bait_purchase_state_and_config_are_removed(self):
        machine = StateMachine()

        self.assertFalse(hasattr(StateMachine, "STATE_BUYING_BAIT"))
        self.assertNotIn("auto_buy_bait_amount", machine.config)
        self.assertNotIn("bait_shop_debug_mode", machine.config)

    def test_wait_after_cast_does_not_probe_bait_shortage(self):
        class Machine(StateMachine):
            def __init__(self):
                super().__init__()
                self.sleep_calls = []

            def _sleep_interruptible(self, seconds, step=0.05):
                self.sleep_calls.append((seconds, step))
                return True

            def _check_bait_shortage_after_cast(self, rect):
                raise AssertionError("bait shortage probe must not run after cast")

        machine = Machine()

        interrupted = machine._wait_after_cast((0, 0, 1920, 1080), 1.4)

        self.assertFalse(interrupted)
        self.assertEqual(machine.sleep_calls, [(1.4, 0.04)])


class RemovedBaitPurchaseSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_app_config_and_settings_do_not_expose_bait_purchase(self):
        from gui.app import AppWindow

        window = AppWindow()
        try:
            snapshot_keys = window._settings_snapshot_keys()
            category_titles = [button.text() for button in getattr(window, "_settings_category_buttons", [])]

            self.assertNotIn("auto_buy_bait_amount", window.default_config)
            self.assertNotIn("bait_shop_debug_mode", window.default_config)
            self.assertNotIn("auto_buy_bait_amount", snapshot_keys)
            self.assertNotIn("bait_shop_debug_mode", snapshot_keys)
            self.assertNotIn("鱼饵补给", category_titles)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
