from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Callable


DropCallback = Callable[[list[Path]], None]

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
DRAG_QUERY_ALL = 0xFFFFFFFF
LRESULT = ctypes.c_ssize_t


class FileDropUnavailable(RuntimeError):
    pass


class WindowsFileDrop:
    def __init__(self, root, on_drop: DropCallback) -> None:
        if os.name != "nt":
            raise FileDropUnavailable("文件拖放当前只支持 Windows。")

        self.root = root
        self.on_drop = on_drop
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        self.hwnd = wintypes.HWND(root.winfo_id())

        self._configure_api()
        self._wnd_proc = self.WNDPROC(self._handle_message)
        self._old_proc = self.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._wnd_proc)
        if not self._old_proc:
            raise FileDropUnavailable("无法注册窗口拖放处理函数。")

        self.shell32.DragAcceptFiles(self.hwnd, True)
        self.root.bind("<Destroy>", self._on_destroy, add="+")

    def _configure_api(self) -> None:
        # Python 3.14 can crash if a Tk window procedure callback created by
        # WINFUNCTYPE re-enters Python while the GIL is released. On 64-bit
        # Windows PYFUNCTYPE uses the same ABI and keeps the GIL held.
        self.WNDPROC = ctypes.PYFUNCTYPE(
            LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.SetWindowLongPtrW = self.user32.SetWindowLongPtrW
        else:
            self.SetWindowLongPtrW = self.user32.SetWindowLongW

        self.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        self.SetWindowLongPtrW.restype = ctypes.c_void_p

        self.user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.CallWindowProcW.restype = LRESULT

        self.shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.shell32.DragAcceptFiles.restype = None
        self.shell32.DragFinish.argtypes = [wintypes.HANDLE]
        self.shell32.DragFinish.restype = None
        self.shell32.DragQueryFileW.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        self.shell32.DragQueryFileW.restype = wintypes.UINT

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            paths: list[Path] = []
            try:
                paths = self._query_drop(wparam)
            finally:
                self.shell32.DragFinish(wparam)
            try:
                self.root.after(0, lambda dropped=paths: self.on_drop(dropped))
            except Exception:
                pass
            return 0

        return self.user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def _query_drop(self, hdrop) -> list[Path]:
        count = self.shell32.DragQueryFileW(hdrop, DRAG_QUERY_ALL, None, 0)
        paths: list[Path] = []
        for index in range(count):
            length = self.shell32.DragQueryFileW(hdrop, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.shell32.DragQueryFileW(hdrop, index, buffer, length + 1)
            paths.append(Path(buffer.value))
        return paths

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self.dispose()

    def dispose(self) -> None:
        if getattr(self, "_old_proc", None):
            self.shell32.DragAcceptFiles(self.hwnd, False)
            self.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_proc)
            self._old_proc = None
