import ctypes
import time
import pydirectinput  # 引入更成熟的底层模拟库


class Controller:
    """键盘控制器，使用 pydirectinput 解决 3D 游戏屏蔽按键问题"""

    def __init__(self):
        self.pressed_keys = set()
        # 【极度关键】：pydirectinput 默认每次操作后会强制 sleep 0.01 秒
        # 这是导致整个主循环变慢、程序“慢半拍”的罪魁祸首！必须设为 0
        pydirectinput.PAUSE = 0.0

    def key_down(self, key_char):
        """按下并保持某键"""
        try:
            key = key_char.lower()
            if key not in self.pressed_keys:
                # 使用最底层的 ctypes 直连，绕过 pydirectinput 内部的封装逻辑
                pydirectinput.keyDown(key)
                self.pressed_keys.add(key)
        except Exception as e:
            print(f"[Controller] KeyDown error: {e}")

    def key_up(self, key_char):
        """释放某键"""
        try:
            key = key_char.lower()
            if key in self.pressed_keys:
                pydirectinput.keyUp(key)
                self.pressed_keys.remove(key)
        except Exception as e:
            print(f"[Controller] KeyUp error: {e}")

    def key_tap(self, key_char, duration=0.01):
        """
        短促点击某键
        注意：这会造成当前线程阻塞 duration 秒。
        但在微操时，我们已经将 duration 压到了极限，且 PAUSE 已经是 0。
        """
        try:
            self.key_down(key_char)
            if duration > 0:
                time.sleep(duration)
            self.key_up(key_char)
        except Exception:
            pass

    def mouse_click(self, x, y, duration=0.05):
        """移动到屏幕坐标后执行一次左键点击。"""
        try:
            x = int(round(x))
            y = int(round(y))
            try:
                ctypes.windll.user32.SetCursorPos(x, y)
            except Exception:
                pass
            time.sleep(0.02)
            pydirectinput.mouseDown(x=x, y=y, button="left")
            if duration > 0:
                time.sleep(duration)
            pydirectinput.mouseUp(x=x, y=y, button="left")
            return True
        except Exception as e:
            print(f"[Controller] MouseClick error: {e}")
            return False

    def release_all(self):
        """释放所有记录在案的被按下的键 (安全保护)"""
        for key in list(self.pressed_keys):
            self.key_up(key)
