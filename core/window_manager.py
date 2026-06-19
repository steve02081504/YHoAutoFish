import win32gui
import win32process
import win32api
import ctypes
from core.dpi import dpi_scale_for_window


class WindowManager:
    def __init__(self, process_name="HTGame.exe"):
        self.process_name = process_name.lower()
        self.hwnd = None

    def _enum_windows_callback(self, hwnd, hwnds):
        """枚举窗口的回调函数，寻找符合要求的游戏窗口"""
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                # 首先尝试通过进程模块名匹配 (更严谨)
                # 使用较低权限 PROCESS_QUERY_LIMITED_INFORMATION 替代 PROCESS_QUERY_INFORMATION
                # 以防游戏自带保护机制拒绝访问 (Access Denied)
                process_handle = win32api.OpenProcess(0x1000, False, pid)
                if process_handle:
                    exe_path = win32process.GetModuleFileNameEx(process_handle, 0)
                    win32api.CloseHandle(process_handle)
                    if self.process_name in exe_path.lower():
                        hwnds.append(hwnd)
                        return True
            except Exception as e:
                pass

            # 如果上面因为权限问题获取不到进程名，退而求其次通过窗口类名或标题匹配
            # 《异环》通常使用虚幻引擎 (UnrealEngine) 开发，类名可能是 UnrealWindow
            # 标题一般就是 "异环"
            window_title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)

            # 兼容标题名为 "异环" 或类名为 "UnrealWindow" 的可见窗口
            if window_title == "异环" or class_name == "UnrealWindow":
                hwnds.append(hwnd)

        return True

    def find_window(self):
        """查找游戏窗口句柄 (通过进程名查找)"""
        hwnds = []
        win32gui.EnumWindows(self._enum_windows_callback, hwnds)
        if hwnds:
            # 默认取第一个匹配的窗口句柄
            self.hwnd = hwnds[0]
            return True
        if self.hwnd and not self.is_window_alive():
            self.hwnd = None
        return False

    def is_window_alive(self):
        """当前缓存的游戏窗口句柄是否仍然有效。"""
        if not self.hwnd:
            return False
        try:
            return bool(win32gui.IsWindow(self.hwnd))
        except Exception:
            self.hwnd = None
            return False

    def is_window_visible_and_restored(self):
        """游戏窗口可见且未最小化时，才适合绑定悬浮窗和截图。"""
        if not self.is_window_alive():
            return False
        try:
            return bool(win32gui.IsWindowVisible(self.hwnd)) and not bool(win32gui.IsIconic(self.hwnd))
        except Exception:
            return False

    def get_client_rect(self):
        """
        获取纯游戏画面(Client Area)在屏幕上的绝对坐标和宽高。
        去除窗口标题栏和边框的影响。
        返回: (left, top, width, height)
        """
        if not self.is_window_visible_and_restored():
            return None

        try:
            # 获取客户区大小 (0, 0, width, height)
            client_rect = win32gui.GetClientRect(self.hwnd)
            width = client_rect[2] - client_rect[0]
            height = client_rect[3] - client_rect[1]

            # 将客户区左上角坐标(0,0)转换为屏幕绝对坐标
            point = win32gui.ClientToScreen(self.hwnd, (0, 0))
            left, top = point[0], point[1]

            if width <= 0 or height <= 0:
                return None

            return (left, top, width, height)
        except Exception as e:
            print(f"[WindowManager] 获取客户区坐标失败: {e}")
            return None

    def get_dpi_scale(self):
        """返回当前游戏窗口相对 96 DPI 的缩放倍率，用于调试和模板缩放参考。"""
        if not self.is_window_alive():
            return 1.0
        return dpi_scale_for_window(self.hwnd)

    def is_foreground(self):
        """检查游戏窗口是否在最前面（获得焦点），用于防误触保护"""
        if not self.is_window_alive():
            return False
        return win32gui.GetForegroundWindow() == self.hwnd

    def set_foreground(self):
        """尝试将窗口置顶并获取焦点"""
        if self.is_window_alive():
            try:
                # 若窗口最小化，先恢复
                if win32gui.IsIconic(self.hwnd):
                    win32gui.ShowWindow(self.hwnd, 9)  # SW_RESTORE
                win32gui.SetForegroundWindow(self.hwnd)
                return True
            except Exception:
                return False
        return False
