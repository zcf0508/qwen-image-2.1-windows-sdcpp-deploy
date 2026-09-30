#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen-Image-2.1 图形界面（文生图 / 图像编辑）

启动：
    uv run --no-project gui.py

设计要点：
    - 后端用 sd-server.exe 常驻进程，模型只加载一次，连续出图不再重复读盘
    - 界面可实时查看显存与内存占用，可随时停止服务释放资源
    - 只用 Python 标准库 + tkinter，无需安装任何第三方包
"""

import base64
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
SD_SERVER = ROOT / "bin" / "sd-server.exe"
OUTPUTS = ROOT / "outputs"

# 扩散模型用 HQv3 混合精度（注意力层 q8_0、MLP 输出层 Q8_0）。它由 ComfyUI-GGUF 转换，
# img_in.weight 的形状声明与 sd.cpp 的推断方式冲突，需先用 patch_gguf_img_in.py 修正，
# 详见 README「量化版本选择」。换回更快的普通量化只改这一行即可。
DIFFUSION_MODEL = ROOT / "models" / "Qwen-Image-2.1-Q4_K_M-HQv3.gguf"
VAE_MODEL = ROOT / "models" / "vae" / "qwen_image_2.1_vae_bf16.safetensors"

# 优先使用 Heretic GGUF；不存在时回退官方编码器
TEXT_ENCODERS = (
    ROOT / "models" / "text_encoders" / "qwen3vl_8b_heretic-Q4_K_M.gguf",
    ROOT / "models" / "text_encoders" / "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
    ROOT / "models" / "text_encoders" / "qwen3vl_8b_int8_convrot.safetensors",
)
# 视觉塔只在图像编辑时需要，加载它要多占约 1.1 GB 内存
VISION_MODELS = (
    ROOT / "models" / "text_encoders" / "mmproj-Qwen3VL-8B-Instruct-F16.gguf",
    ROOT / "models" / "text_encoders" / "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf",
)

PORT = 1234
BASE_URL = f"http://127.0.0.1:{PORT}"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def first_existing(candidates):
    for path in candidates:
        if path.is_file():
            return path
    return None


# ---------------------------------------------------------------- HTTP

def http_json(url, payload=None, method=None, timeout=10):
    """极简 JSON 请求，失败时抛异常由调用方处理"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else {}


def server_alive():
    try:
        http_json(f"{BASE_URL}/sdcpp/v1/capabilities", timeout=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 资源监控

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def ram_usage():
    """返回 (已用GB, 总量GB)"""
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    total = status.ullTotalPhys / 1024 ** 3
    used = (status.ullTotalPhys - status.ullAvailPhys) / 1024 ** 3
    return used, total


def vram_usage(pid=None):
    """返回 (已用MB, 总量MB)；没有 nvidia-smi 时返回 None"""
    query = "memory.used,memory.total"
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW,
        )
        used, total = result.stdout.strip().splitlines()[0].split(",")
        return int(used), int(total)
    except Exception:
        return None


# ---------------------------------------------------------------- 文件拖放

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4


class FileDropTarget:
    """接管窗口过程，让窗口接收系统投递的文件拖放（WM_DROPFILES）

    只用 ctypes 调 user32 / shell32，不引入 tkinterdnd2 这类第三方包。
    文件落到窗口上时系统会把路径打包成 HDROP 投递给窗口过程，这里先取走路径，
    再把消息交回 Tk 原来的窗口过程。

    Tk 在 Windows 上把整个顶层窗口画在一个包装窗口（类名 TkTopLevel）里，Tk 自己的
    窗口是它的子窗口，所以要把拖放注册在包装窗口上，窗口内任意位置都能收到。
    包装窗口要等窗口显示出来才存在，拿不到时直接报错由调用方稍后重试。
    """

    def __init__(self, window, on_drop):
        self.on_drop = on_drop
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 句柄与函数指针都是指针宽度，64 位下必须显式声明，否则会被截断
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        set_long.restype = ctypes.c_void_p

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_wchar_p, ctypes.c_uint]
        shell32.DragQueryFileW.restype = ctypes.c_uint
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        self._shell32 = shell32

        self.hwnd = user32.GetParent(window.winfo_id())
        if not self.hwnd:
            raise OSError("窗口还没显示出顶层 HWND")
        self._proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_ssize_t)(self._handle)
        # 回调必须留引用，否则被回收后窗口过程会跳到野指针
        self._previous = set_long(self.hwnd, GWLP_WNDPROC,
                                  ctypes.cast(self._proc, ctypes.c_void_p))
        if not self._previous:
            raise OSError("接管窗口过程失败")
        try:
            # 注册后系统才会在有文件落到窗口上时投递 WM_DROPFILES
            shell32.DragAcceptFiles(self.hwnd, True)
        except Exception:
            # 注册失败就把窗口过程还原，别留下会跳野指针的回调
            set_long(self.hwnd, GWLP_WNDPROC, self._previous)
            raise

    def _handle(self, hwnd, message, wparam, lparam):
        if message != WM_DROPFILES:
            return self.user32.CallWindowProcW(self._previous, hwnd, message, wparam, lparam)
        try:
            paths = self._take_paths(wparam)
        except Exception:
            paths = []
        if paths:
            self.on_drop(paths)
        # 已消费，不再往下传
        return 0

    def _take_paths(self, hdrop):
        """读出 HDROP 里的全部路径并释放它（传 0xFFFFFFFF 取文件个数）"""
        count = self._shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            need = self._shell32.DragQueryFileW(hdrop, index, None, 0)
            buffer = ctypes.create_unicode_buffer(need + 1)
            self._shell32.DragQueryFileW(hdrop, index,
                                         ctypes.cast(buffer, ctypes.c_wchar_p), need + 1)
            paths.append(Path(buffer.value))
        self._shell32.DragFinish(hdrop)
        return paths


def enable_file_drop(window, on_drop):
    """给窗口挂上文件拖放，成功返回接管对象，系统不支持或失败返回 None"""
    if sys.platform != "win32":
        return None
    try:
        return FileDropTarget(window, on_drop)
    except Exception:
        return None


# ---------------------------------------------------------------- 服务进程

def bind_child_lifetime(process):
    """把子进程绑进 Job 对象：本进程一旦结束（含崩溃或被强杀），子进程随之终止。

    否则 GUI 被强制结束时会残留 sd-server.exe，显存与内存都不释放。
    失败时返回 None，不影响正常流程。
    """
    if sys.platform != "win32":
        return None
    try:
        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                     ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(process._handle))):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


class QwenServer:
    """管理 sd-server.exe 的启动、停止与任务提交"""

    def __init__(self, emit):
        self.emit = emit
        self.process = None
        self.with_vision = False
        self.job_id = None
        self.adopted = False
        self._reader = None
        # Job 对象句柄。不能命名为 job，否则会遮蔽下面的 job() 方法
        self.job_object = None

    def running(self):
        """服务是否可用（含复用外部实例的情况）"""
        return self.adopted or (self.process is not None and self.process.poll() is None)

    def stoppable(self):
        """服务是否由本窗口启动，可被停止"""
        return self.process is not None and self.process.poll() is None

    def start(self, with_vision, timeout=300):
        """启动服务并等待模型加载完成。返回 (成功, 消息)"""
        if server_alive():
            self.adopted = True
            self.with_vision = with_vision
            self.emit("log", "检测到 127.0.0.1:1234 已有服务，直接复用；该服务非本窗口启动，无法在此停止。")
            return True, "已复用运行中的服务"

        encoder = first_existing(TEXT_ENCODERS)
        missing = [p for p in (SD_SERVER, DIFFUSION_MODEL, VAE_MODEL, encoder) if not p or not Path(p).is_file()]
        if missing:
            return False, "缺少必需文件：\n" + "\n".join(str(p) for p in missing)

        command = [
            str(SD_SERVER),
            "--diffusion-model", str(DIFFUSION_MODEL),
            "--vae", str(VAE_MODEL),
            "--llm", str(encoder),
            # 文本编码器放 CPU，显存留给扩散模型
            "--backend", "te=cpu",
            "--diffusion-fa",
            "--listen-ip", "127.0.0.1",
            "--listen-port", str(PORT),
        ]
        vision = first_existing(VISION_MODELS)
        if with_vision:
            if vision is None:
                return False, "图像编辑需要视觉塔：请把 mmproj-Qwen3VL-8B-Instruct-F16.gguf 放到 models\\text_encoders\\ 下。"
            command += ["--llm_vision", str(vision)]

        self.emit("log", "启动服务：" + ("已加载视觉塔" if with_vision else "未加载视觉塔"))
        self.process = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW,
        )
        self.with_vision = with_vision
        # 绑定生命周期：本进程结束时子进程一并退出，避免残留占用显存
        self.job_object = bind_child_lifetime(self.process)
        self._reader = threading.Thread(target=self._pump, args=(self.process,), daemon=True)
        self._reader.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return False, "服务进程意外退出，请查看下方日志。"
            if server_alive():
                self.emit("log", "服务已就绪；模型在首次出图时载入，之后常驻。")
                return True, "服务已就绪"
            time.sleep(0.8)
        return False, "等待服务就绪超时。"

    def _pump(self, process):
        """把服务端日志转发到界面（绑定进程对象，避免 stop() 后取到 None）"""
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.emit("log", line)

    def stop(self):
        """停止服务并等待进程真正退出。进程退出即释放显存与内存。"""
        self.job_id = None
        if self.adopted:
            self.emit("log", "该服务由其他窗口启动，本窗口无法停止。")
            return
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.emit("log", "警告：服务进程未能在超时内退出，可能仍在占用资源。")
                return
        except Exception as exc:
            self.emit("log", f"停止服务时出错：{exc}")
            try:
                process.kill()
            except Exception:
                pass
        self.emit("log", "服务已停止，显存与内存已释放。")

    def submit(self, payload):
        result = http_json(f"{BASE_URL}/sdcpp/v1/img_gen", payload, timeout=30)
        self.job_id = result.get("id")
        return self.job_id

    def job(self, job_id):
        return http_json(f"{BASE_URL}/sdcpp/v1/jobs/{job_id}", timeout=15)

    def cancel(self, job_id):
        return http_json(f"{BASE_URL}/sdcpp/v1/jobs/{job_id}/cancel", {}, method="POST", timeout=15)


# ---------------------------------------------------------------- 视觉规范

# 暖中性灰底 + 单一铜色强调。数字用等宽字体，多行读数才能对齐。
BG = "#F4F1EE"
PANEL = "#FFFFFF"
BORDER = "#E2DCD7"
FIELD_BG = "#FFFEFC"
FIELD_BORDER = "#C8BFB7"
CONTROL_BG = "#E5DFD9"
CONTROL_HOVER = "#D8CFC7"
CONTROL_ACTIVE = "#CCC1B8"
DISABLED_BG = "#EEEAE6"
DISABLED_TEXT = "#9B9189"
TEXT = "#1F1B18"
MUTED = "#665D57"
ACCENT = "#A8520A"
ACCENT_ACTIVE = "#8F4508"
ACCENT_SOFT = "#FBEEE1"
ACCENT_TINT = "#F1D4BC"
LOG_BG = "#1C1917"
LOG_FG = "#D8D2CB"
STATE_COLORS = {"idle": "#8B5E3C", "busy": "#B45309", "ready": "#15803D", "error": "#B91C1C"}

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

# 所有内外边距取同一个值，避免反复调数字还调不平
PAD = 8

# 七种宽高比统一按 128 的网格取值。
# 尺寸必须是 128 的整数倍：VAE 分块解码的接缝落在瓦片边界，非 128 倍数会在接缝处出现亮度台阶（实测 1280x864 跳变 9.39 灰度级，128 倍数尺寸仅 0.15~0.28）
SIZE_PRESETS = (
    ("1:1", 1024, 1024),
    ("4:3", 1024, 768),
    ("3:4", 768, 1024),
    ("3:2", 1152, 768),
    ("2:3", 768, 1152),
    ("16:9", 1152, 640),
    ("9:16", 640, 1152),
)

# 高清档：边长约为标准档的 1.5 倍，像素量 1.4~2.4 MP，单张约 2~4 分钟。
# 同样落在 128 网格上；16:9 与 9:16 取 2048 × 1152，比例正好 1.778。
SIZE_PRESETS_HD = (
    ("1:1", 1536, 1536),
    ("4:3", 1536, 1152),
    ("3:4", 1152, 1536),
    ("3:2", 1536, 1024),
    ("2:3", 1024, 1536),
    ("16:9", 2048, 1152),
    ("9:16", 1152, 2048),
)

# 单张耗时粗估系数，秒/百万像素。取自三次实测：1024x1024（1.05 MP / 89 秒）、
# 1536x1024（1.57 MP / 136 秒）、1408x2048（2.88 MP / 296 秒）
SECONDS_PER_MP = 95


def parse_generation_parameters(width_text, height_text, steps_text, cfg_text, seed_text):
    """解析并校验生成参数，返回 ((宽, 高, 步数, 引导, 种子), 错误信息)。"""
    try:
        values = (
            int(width_text), int(height_text), int(steps_text),
            float(cfg_text), int(seed_text),
        )
    except ValueError:
        return None, "宽、高、步数、引导和种子都必须是数字"

    width, height, steps, cfg, _seed = values
    if width <= 0 or height <= 0:
        return None, "宽和高必须大于 0"
    if width % 128 or height % 128:
        return None, "宽和高必须是 128 的倍数，避免分块接缝"
    if steps <= 0:
        return None, "步数必须大于 0"
    if not math.isfinite(cfg) or cfg < 0:
        return None, "引导强度必须是大于等于 0 的有限数字"
    return values, None


class SizePresetStrip(tk.Canvas):
    """宽高比预设条：每格画出该比例的缩略矩形，点一下切换尺寸"""

    THUMB_BOX = 30

    def __init__(self, master, presets, on_click, **kwargs):
        super().__init__(master, height=72, highlightthickness=1,
                         highlightbackground=BG, highlightcolor=ACCENT,
                         background=BG, takefocus=1, **kwargs)
        self.presets = presets
        self.on_click = on_click
        self.selected = None
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Left>", lambda _event: self._move_selection(-1))
        self.bind("<Right>", lambda _event: self._move_selection(1))
        self.bind("<Home>", lambda _event: self._choose(0))
        self.bind("<End>", lambda _event: self._choose(len(self.presets) - 1))
        self.bind("<Return>", lambda _event: self._activate_selected())
        self.bind("<space>", lambda _event: self._activate_selected())
        self.redraw()

    def _choose(self, index):
        self.focus_set()
        self.on_click(*self.presets[index][1:])
        return "break"

    def _move_selection(self, delta):
        index = self.selected if self.selected is not None else 0
        return self._choose(max(0, min(len(self.presets) - 1, index + delta)))

    def _activate_selected(self):
        return self._choose(self.selected if self.selected is not None else 0)

    def set_selected(self, index):
        """只刷新高亮，不触发回调"""
        if index != self.selected:
            self.selected = index
            self.redraw()

    def set_presets(self, presets):
        """整套换用另一组预设（标准档 / 高清档）"""
        self.presets = presets
        self.selected = None
        self.redraw()

    def redraw(self):
        self.delete("all")
        width = self.winfo_width() or 360
        cell = width / len(self.presets)
        for index, (label, w, h) in enumerate(self.presets):
            center = cell * (index + 0.5)
            active = index == self.selected
            tag = f"tile{index}"

            if active:
                self.create_rectangle(cell * index + 2, 2, cell * (index + 1) - 2, 58,
                                      fill=ACCENT_SOFT, outline=ACCENT, width=1, tags=tag)

            scale = min(self.THUMB_BOX / w, self.THUMB_BOX / h)
            thumb_w = max(7, w * scale)
            thumb_h = max(7, h * scale)
            self.create_rectangle(center - thumb_w / 2, 30 - thumb_h / 2,
                                  center + thumb_w / 2, 30 + thumb_h / 2,
                                  outline=ACCENT if active else MUTED,
                                  fill=ACCENT if active else PANEL, width=1, tags=tag)

            self.create_text(center, 66, text=label,
                             fill=ACCENT if active else MUTED,
                             font=(UI_FONT, 8), tags=tag)

            self.tag_bind(tag, "<Button-1>", lambda _event, i=index: self._choose(i))
            self.tag_bind(tag, "<Enter>", lambda _event: self.configure(cursor="hand2"))
            self.tag_bind(tag, "<Leave>", lambda _event: self.configure(cursor=""))


# ---------------------------------------------------------------- 界面

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Qwen-Image-2.1")
        width = min(1280, self.winfo_screenwidth() - 80)
        height = min(800, self.winfo_screenheight() - 80)
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(1040, 680)
        self._setup_style()

        self.events = queue.Queue()
        self.server = QwenServer(self.emit)
        self.busy = False
        self.cancel_pending = False
        self.force_stopping = False
        self.service_starting = False
        self.preview = None
        self.preview_source = None
        self.preview_resize_job = None
        self.result_path = None
        self.start_time = 0.0
        self._params_valid = True
        self.log_visible = False
        self.drop = None
        self._drop_giveup = None

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-Return>", self._shortcut_generate)
        # 拖放要等窗口映射后才装得上：Tk 到那时才建好真正的顶层 HWND
        self.bind("<Map>", self._enable_drop, add="+")
        self.after(100, self._drain)
        self.after_idle(self.txt_prompt.focus_set)
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.log("就绪。填写画面描述后按 Ctrl+Enter；服务未启动时会自动加载。")

    # ------------------------------------------------------------ 样式与布局

    def _setup_style(self):
        """统一配色与控件样式。系统默认的 vista 主题不接受自定义配色，改用 clam。"""
        style = ttk.Style(self)
        style.theme_use("clam")
        self.style = style

        style.configure(".", background=BG, foreground=TEXT, font=(UI_FONT, 10),
                        bordercolor=BORDER, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER,
                        relief="flat", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=(UI_FONT, 9, "bold"))
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=(UI_FONT, 15, "bold"))
        style.configure("Section.TLabel", background=BG, foreground=TEXT,
                        font=(UI_FONT, 10, "bold"))
        style.configure("Error.TLabel", background=BG, foreground=STATE_COLORS["error"])
        style.configure("Metric.TLabel", background=BG, foreground=MUTED, font=(MONO_FONT, 10))
        style.configure("State.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 10, "bold"))
        style.configure("TPanedwindow", background=BG, sashrelief="flat", sashwidth=6)

        base_button = dict(
            borderwidth=0, relief="flat", padding=(13, 8), font=(UI_FONT, 10),
            focusthickness=1, focuscolor=ACCENT,
        )
        style.configure(
            "TButton", background=CONTROL_BG, foreground=TEXT,
            bordercolor=CONTROL_BG, lightcolor=CONTROL_BG, darkcolor=CONTROL_BG,
            **base_button,
        )
        style.map("TButton",
                  background=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                              ("active", CONTROL_HOVER)],
                  bordercolor=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                               ("active", CONTROL_HOVER)],
                  lightcolor=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                              ("active", CONTROL_HOVER)],
                  darkcolor=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                             ("active", CONTROL_HOVER)],
                  foreground=[("disabled", DISABLED_TEXT)],
                  relief=[("pressed", "flat")])
        style.configure("Compact.TButton", padding=(10, 6), font=(UI_FONT, 9))
        style.configure(
            "Primary.TButton", background=ACCENT, foreground="#FFFFFF",
            bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
            **base_button,
        )
        style.map("Primary.TButton",
                  background=[("disabled", "#DCC7B2"), ("pressed", ACCENT_ACTIVE),
                              ("active", ACCENT_ACTIVE)],
                  bordercolor=[("disabled", "#DCC7B2"), ("pressed", ACCENT_ACTIVE),
                               ("active", ACCENT_ACTIVE)],
                  lightcolor=[("disabled", "#DCC7B2"), ("pressed", ACCENT_ACTIVE),
                              ("active", ACCENT_ACTIVE)],
                  darkcolor=[("disabled", "#DCC7B2"), ("pressed", ACCENT_ACTIVE),
                             ("active", ACCENT_ACTIVE)],
                  foreground=[("disabled", "#FFF9F4")],
                  relief=[("pressed", "flat")])
        style.configure(
            "Secondary.TButton", background=CONTROL_BG, foreground=TEXT,
            bordercolor=CONTROL_BG, lightcolor=CONTROL_BG, darkcolor=CONTROL_BG,
            **base_button,
        )
        style.map("Secondary.TButton",
                  background=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                              ("active", CONTROL_HOVER)],
                  bordercolor=[("disabled", DISABLED_BG), ("pressed", CONTROL_ACTIVE),
                               ("active", CONTROL_HOVER)],
                  foreground=[("disabled", DISABLED_TEXT)],
                  relief=[("pressed", "flat")])
        style.configure(
            "TScrollbar", background=CONTROL_BG, troughcolor=BG,
            bordercolor=BG, lightcolor=CONTROL_BG, darkcolor=CONTROL_BG,
            relief="flat", borderwidth=0, arrowsize=12,
        )
        style.map("TScrollbar", background=[("active", CONTROL_HOVER),
                                             ("pressed", CONTROL_ACTIVE)])
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=BORDER,
                        background=ACCENT, bordercolor=BG, lightcolor=ACCENT,
                        darkcolor=ACCENT, relief="flat", borderwidth=0)

    def _build(self):
        self.configure(background=BG)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        flat_field = {
            "background": FIELD_BG,
            "foreground": TEXT,
            "selectbackground": ACCENT_SOFT,
            "selectforeground": TEXT,
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 1,
            "highlightbackground": FIELD_BORDER,
            "highlightcolor": ACCENT,
        }
        flat_toggle = {
            "indicatoron": False,
            "font": (UI_FONT, 9),
            "background": CONTROL_BG,
            "foreground": TEXT,
            "selectcolor": ACCENT_TINT,
            "activebackground": CONTROL_HOVER,
            "activeforeground": TEXT,
            "disabledforeground": MUTED,
            "relief": "flat",
            "offrelief": "flat",
            "overrelief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "padx": 10,
            "pady": 5,
        }

        def flat_entry(master, variable, font, justify="left"):
            shell = tk.Frame(
                master, background=FIELD_BG, relief="flat", borderwidth=0,
                highlightthickness=1, highlightbackground=FIELD_BORDER,
            )
            entry = tk.Entry(
                shell, textvariable=variable, font=font, justify=justify,
                background=FIELD_BG, foreground=TEXT, insertbackground=TEXT,
                selectbackground=ACCENT_SOFT, selectforeground=TEXT,
                relief="flat", borderwidth=0, highlightthickness=0,
            )
            entry.pack(fill="both", expand=True, padx=7, pady=4)
            entry.bind("<FocusIn>", lambda _event: shell.configure(
                highlightbackground=ACCENT))
            entry.bind("<FocusOut>", lambda _event: shell.configure(
                highlightbackground=FIELD_BORDER))
            return shell

        # 顶部工作区：产品、服务状态、资源读数与服务控制保持在同一工具栏。
        status = ttk.Frame(self, padding=(PAD + 4, PAD))
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.var_state = tk.StringVar(value="● 服务未启动")
        self.var_vram = tk.StringVar(value="显存 —")
        self.var_ram = tk.StringVar(value="内存 —")
        brand = ttk.Frame(status)
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="Qwen Image", style="Title.TLabel").pack(side="left")
        ttk.Label(brand, text="本地创作台", style="Muted.TLabel").pack(
            side="left", padx=(10, 18))
        self.lbl_state = ttk.Label(brand, textvariable=self.var_state, style="State.TLabel")
        self.lbl_state.pack(side="left")

        metrics = ttk.Frame(status)
        metrics.grid(row=0, column=1, padx=(12, 16))
        ttk.Label(metrics, textvariable=self.var_vram, style="Metric.TLabel").pack(side="left")
        ttk.Label(metrics, textvariable=self.var_ram, style="Metric.TLabel").pack(
            side="left", padx=(14, 0))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=PAD + 4,
                  pady=(0, PAD + 4))

        # 左侧控制面板：放进可滚动容器，窗口再矮也不会把按钮挤没
        left_outer = ttk.Frame(body, padding=(0, 0, PAD, 0))
        body.add(left_outer, weight=0)
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, highlightthickness=0, background=BG,
                                width=356, height=560, takefocus=0)
        left_canvas.grid(row=0, column=0, sticky="nsew", pady=(0, 48))
        left_scroll = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_scroll.grid(row=0, column=1, sticky="ns", pady=(0, 48))
        left_canvas.configure(yscrollcommand=left_scroll.set)

        left = ttk.Frame(left_canvas)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
        def _sync_scroll(_event=None):
            """内容装得下就收起滚动条，装不下才显示"""
            box = left_canvas.bbox("all")
            left_canvas.configure(scrollregion=box)
            if box and box[3] > left_canvas.winfo_height():
                left_scroll.grid()
            else:
                left_scroll.grid_remove()

        left.bind("<Configure>", _sync_scroll)
        left_canvas.bind("<Configure>", lambda e: (
            left_canvas.itemconfigure(left_window, width=e.width), _sync_scroll()))

        def _on_wheel(event):
            left_canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        left_canvas.bind("<Enter>", lambda _e: left_canvas.bind_all("<MouseWheel>", _on_wheel))
        left_canvas.bind("<Leave>", lambda _e: left_canvas.unbind_all("<MouseWheel>"))

        service_toolbar = ttk.Frame(left)
        service_toolbar.pack(fill="x", pady=(0, PAD))
        ttk.Label(service_toolbar, text="推理服务", style="Muted.TLabel").pack(side="left")
        self.btn_stop = ttk.Button(
            service_toolbar, text="停止", command=self.on_stop,
            style="Compact.TButton", state="disabled",
        )
        self.btn_stop.pack(side="right")
        self.btn_start = ttk.Button(
            service_toolbar, text="启动服务", command=self.on_start,
            style="Compact.TButton",
        )
        self.btn_start.pack(side="right", padx=(0, 6))

        input_box = ttk.LabelFrame(left, text=" 输入 ", padding=PAD)
        input_box.pack(fill="both", expand=True, pady=(0, PAD))

        mode_row = ttk.Frame(input_box)
        mode_row.pack(fill="x")
        self.var_mode = tk.StringVar(value="txt2img")
        mode_options = (("文生图", "txt2img"), ("图像编辑", "edit"))
        for index, (text, value) in enumerate(mode_options):
            button = tk.Radiobutton(
                mode_row, text=text, value=value, variable=self.var_mode,
                command=self.on_mode, indicatoron=False, font=(UI_FONT, 10),
                background=CONTROL_BG, foreground=TEXT, selectcolor=ACCENT_TINT,
                activebackground=CONTROL_HOVER, activeforeground=TEXT,
                relief="flat", offrelief="flat", overrelief="flat",
                borderwidth=0, highlightthickness=0,
                padx=16, pady=7,
            )
            button.pack(side="left", fill="x", expand=True,
                        padx=((0 if index == 0 else 4), (4 if index == 0 else 0)))
        self.var_mode_hint = tk.StringVar()
        self.lbl_mode_hint = ttk.Label(
            input_box, textvariable=self.var_mode_hint, style="Muted.TLabel", wraplength=360,
        )
        self.lbl_mode_hint.pack(fill="x", pady=(6, 0))

        # 参考图是有序列表，顺序即提示词里的「图一 / 图二」
        self.ref_paths = []
        self.ref_panel = ttk.Frame(input_box)
        self.ref_panel.pack(fill="x", pady=(PAD, 0))
        ref_row = ttk.Frame(self.ref_panel)
        ref_row.pack(fill="x")
        self.btn_ref = ttk.Button(ref_row, text="选择参考图…", command=self.on_pick_ref)
        self.btn_ref.pack(side="left")
        self.btn_clear_ref = ttk.Button(ref_row, text="全部清除", command=self.on_clear_ref)
        self.btn_clear_ref.pack(side="left", padx=(8, 0))
        self.var_ref = tk.StringVar(value="未选择")
        ttk.Label(ref_row, textvariable=self.var_ref, style="Muted.TLabel").pack(side="left", padx=(10, 0))

        list_row = ttk.Frame(self.ref_panel)
        list_row.pack(fill="x", pady=(6, 0))
        self.list_ref = tk.Listbox(
            list_row, height=3, activestyle="none", font=(UI_FONT, 9),
            exportselection=False, **flat_field)
        self.list_ref.pack(fill="x", expand=True)
        ref_btns = ttk.Frame(self.ref_panel)
        ref_btns.pack(fill="x", pady=(5, 0))
        self.btn_ref_up = ttk.Button(ref_btns, text="上移", width=5,
                                     command=lambda: self.on_move_ref(-1),
                                     style="Compact.TButton")
        self.btn_ref_up.pack(side="left")
        self.btn_ref_down = ttk.Button(ref_btns, text="下移", width=5,
                                       command=lambda: self.on_move_ref(1),
                                       style="Compact.TButton")
        self.btn_ref_down.pack(side="left", padx=(5, 0))
        self.btn_ref_remove = ttk.Button(ref_btns, text="移除", width=6,
                                         command=self.on_remove_ref,
                                         style="Compact.TButton")
        self.btn_ref_remove.pack(side="left", padx=(5, 0))
        self.list_ref.bind("<<ListboxSelect>>", lambda _event: self._sync_ref_buttons())
        self.list_ref.bind("<Delete>", lambda _event: self.on_remove_ref())

        prompt_header = ttk.Frame(input_box)
        prompt_header.pack(fill="x", pady=(PAD, 0))
        ttk.Label(prompt_header, text="画面描述", style="Section.TLabel").pack(side="left")
        self.var_prompt_count = tk.StringVar(value="0 字")
        ttk.Label(prompt_header, textvariable=self.var_prompt_count, style="Muted.TLabel").pack(side="right")
        self.txt_prompt = tk.Text(input_box, height=4, width=44, wrap="word", font=(UI_FONT, 10),
                                  insertbackground=TEXT, padx=8, pady=6, undo=True,
                                  **flat_field)
        self.txt_prompt.pack(fill="both", expand=True, pady=(4, 0))
        self.txt_prompt.bind("<<Modified>>", self._on_prompt_changed)
        self.txt_prompt.bind("<Control-Return>", self._shortcut_generate, add="+")

        ttk.Label(input_box, text="负面提示词（可留空）", style="Muted.TLabel").pack(
            anchor="w", pady=(PAD, 4))
        self.var_negative = tk.StringVar()
        flat_entry(input_box, self.var_negative, (UI_FONT, 10)).pack(fill="x")

        # 尺寸预设：一格一个宽高比，点一下即换
        size_box = ttk.LabelFrame(left, text=" 尺寸预设 ", padding=PAD)
        size_box.pack(fill="x", pady=(0, PAD))
        self.var_hd = tk.BooleanVar(value=False)
        tk.Checkbutton(
            size_box, text="高清档（边长 1.5 倍，单张大 2~4 分钟）",
            variable=self.var_hd, command=self.on_toggle_tier, anchor="w",
            **flat_toggle,
        ).pack(anchor="w", pady=(0, 6))
        self.strip = SizePresetStrip(size_box, SIZE_PRESETS, self.on_preset)
        self.strip.pack(fill="x")
        self.var_size = tk.StringVar(value="")
        ttk.Label(size_box, textvariable=self.var_size, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0))

        params = ttk.LabelFrame(left, text=" 参数 ", padding=PAD)
        params.pack(fill="x", pady=(0, PAD))
        grid = ttk.Frame(params)
        grid.pack(fill="x")
        for column in range(3):
            grid.columnconfigure(column, weight=1)

        self.var_width = tk.StringVar(value="1024")
        self.var_height = tk.StringVar(value="1024")
        self.var_steps = tk.StringVar(value="40")
        self.var_cfg = tk.StringVar(value="1.0")
        self.var_seed = tk.StringVar(value="-1")

        fields = (("宽", self.var_width), ("高", self.var_height), ("步数", self.var_steps),
                  ("引导", self.var_cfg), ("种子", self.var_seed))
        for index, (label, var) in enumerate(fields):
            column = index % 3
            row = index // 3
            cell = ttk.Frame(grid)
            cell.grid(row=row, column=column, sticky="ew", padx=(0, 8),
                      pady=(0 if row == 0 else 6, 0))
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=label, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
            flat_entry(cell, var, (MONO_FONT, 10), justify="right").grid(
                row=0, column=1, sticky="ew", padx=(6, 0))
        for var in (self.var_width, self.var_height, self.var_steps, self.var_cfg, self.var_seed):
            var.trace_add("write", self._on_size_var_changed)
        ttk.Label(params, text="宽高需为 128 的倍数 · 种子 -1 为随机", style="Muted.TLabel").pack(
            anchor="w", pady=(PAD, 0))
        self.var_param_error = tk.StringVar()
        self.lbl_param_error = ttk.Label(
            params, textvariable=self.var_param_error, style="Error.TLabel",
        )

        actions = ttk.Frame(left_outer, padding=(0, PAD, 0, 0))
        actions.place(relx=0, rely=1, anchor="sw", relwidth=1)
        self.btn_generate = ttk.Button(actions, text="生成图像", command=self.on_generate,
                                       style="Primary.TButton")
        self.btn_generate.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(actions, text="取消任务", command=self.on_cancel,
                                     style="Secondary.TButton", state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))

        # 右侧预览台
        right = ttk.Frame(body)
        body.add(right, weight=1)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        preview_stage = tk.Frame(
            right, background=FIELD_BG, relief="flat", borderwidth=0,
            highlightbackground=FIELD_BORDER, highlightthickness=1,
        )
        preview_stage.grid(row=0, column=0, sticky="nsew")
        preview_stage.rowconfigure(0, weight=1)
        preview_stage.columnconfigure(0, weight=1)
        self.canvas = tk.Label(preview_stage, background=FIELD_BG, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.canvas.bind("<Configure>", self._schedule_preview_resize)

        self.empty_preview = tk.Frame(preview_stage, background=FIELD_BG)
        self.empty_preview.place(relx=0.5, rely=0.47, anchor="center")
        tk.Label(self.empty_preview, text="◇", background=FIELD_BG, foreground=ACCENT,
                 font=(UI_FONT, 28)).pack()
        tk.Label(self.empty_preview, text="等待第一张图像", background=FIELD_BG,
                 foreground=TEXT, font=(UI_FONT, 13, "bold")).pack(pady=(6, 0))
        tk.Label(self.empty_preview, text="填写左侧画面描述，按 Ctrl+Enter 开始",
                 background=FIELD_BG, foreground=MUTED,
                 font=(UI_FONT, 10)).pack(pady=(6, 0))

        preview_actions = ttk.Frame(right)
        preview_actions.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        preview_actions.columnconfigure(0, weight=1)
        self.var_progress = tk.StringVar(value="")
        ttk.Label(preview_actions, textvariable=self.var_progress, style="Metric.TLabel").grid(
            row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(
            preview_actions, mode="indeterminate", style="Accent.Horizontal.TProgressbar",
        )
        self.progress.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.progress.grid_remove()
        preview_buttons = ttk.Frame(preview_actions)
        preview_buttons.grid(row=0, column=1, rowspan=2, sticky="e", padx=(PAD, 0))
        self.btn_close_preview = ttk.Button(
            preview_buttons, text="关闭预览", command=self.on_close_preview, state="disabled",
            style="Compact.TButton",
        )
        self.btn_close_preview.pack(side="left", padx=(0, 8))
        self.btn_open = ttk.Button(
            preview_buttons, text="打开图片", command=self.on_open_result, state="disabled",
            style="Compact.TButton",
        )
        self.btn_open.pack(side="left")
        self.btn_save = ttk.Button(
            preview_buttons, text="另存为…", command=self.on_save, state="disabled",
            style="Compact.TButton",
        )
        self.btn_save.pack(side="left", padx=(8, 0))
        self.btn_log = ttk.Button(
            preview_buttons, text="运行记录", command=self._toggle_log,
            style="Compact.TButton",
        )
        self.btn_log.pack(side="left", padx=(8, 0))
        self.btn_output = ttk.Button(
            preview_buttons, text="输出目录", command=self.on_open_output_folder,
            style="Compact.TButton",
        )
        self.btn_output.pack(side="left", padx=(8, 0))

        # 底部日志
        self.log_box = ttk.LabelFrame(self, text=" 运行记录 ", padding=PAD // 2)
        self.log_box.grid(row=2, column=0, sticky="ew", padx=PAD + 4, pady=(PAD, PAD + 4))
        self.txt_log = tk.Text(self.log_box, height=4, wrap="word", state="disabled",
                               font=(MONO_FONT, 9), background=LOG_BG, foreground=LOG_FG,
                               relief="flat", borderwidth=0, padx=8, pady=6)
        scroll = ttk.Scrollbar(self.log_box, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)
        self.log_box.grid_remove()

        self._on_size_var_changed()
        self.on_mode()
        self._sync_ref_buttons()

    # ------------------------------------------------------------ 事件通道

    def emit(self, kind, payload):
        self.events.put((kind, payload))

    def _drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "monitor":
                    self._update_monitor(*payload)
                elif kind == "drop":
                    self.on_files_dropped(payload)
                elif kind == "progress":
                    self.var_progress.set(payload)
                elif kind == "start_done":
                    self._on_start_done(payload)
                elif kind == "cancel_done":
                    self._on_cancel_done(payload)
                elif kind == "cancelled":
                    self._on_cancelled(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "fail":
                    self._on_fail(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n")
        if int(self.txt_log.index("end-1c").split(".")[0]) > 600:
            self.txt_log.delete("1.0", "100.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _set_log_visible(self, visible):
        self.log_visible = visible
        if visible:
            self.log_box.grid()
            self.btn_log.configure(text="收起记录")
        else:
            self.log_box.grid_remove()
            self.btn_log.configure(text="运行记录")

    def _toggle_log(self):
        self._set_log_visible(not self.log_visible)

    def _on_prompt_changed(self, _event=None):
        if not self.txt_prompt.edit_modified():
            return
        count = len(self.txt_prompt.get("1.0", "end-1c"))
        self.var_prompt_count.set(f"{count} 字")
        self.txt_prompt.edit_modified(False)

    def _shortcut_generate(self, _event=None):
        if str(self.btn_generate.cget("state")) != "disabled":
            self.on_generate()
        return "break"

    # ------------------------------------------------------------ 监控

    def _monitor_loop(self):
        while True:
            vram = vram_usage()
            ram = ram_usage()
            self.emit("monitor", (vram, ram))
            time.sleep(2)

    def _update_monitor(self, vram, ram):
        if vram:
            self.var_vram.set(f"显存 {vram[0]} / {vram[1]} MB")
        else:
            self.var_vram.set("显存 —")
        self.var_ram.set(f"内存 {ram[0]:.1f} / {ram[1]:.1f} GB")

    def _set_state(self, text, kind="idle"):
        """状态语义交给颜色，文案保持稳定"""
        self.var_state.set(text)
        self.lbl_state.configure(foreground=STATE_COLORS.get(kind, TEXT))

    def _sync_buttons(self):
        """按当前忙闲与服务状态刷新按钮可用性"""
        blocked = self.busy or self.service_starting
        if self.force_stopping:
            generate_text = "正在停止…"
        elif self.busy:
            generate_text = "正在生成…"
        else:
            generate_text = "开始编辑" if self.var_mode.get() == "edit" else "生成图像"
        self.btn_generate.configure(
            text=generate_text,
            state="disabled" if (blocked or not self._params_valid) else "normal",
        )
        cancel_text = "停止中…" if self.force_stopping else (
            "请求中…" if self.cancel_pending else "取消任务"
        )
        self.btn_cancel.configure(
            text=cancel_text,
            state="normal"
            if (self.busy and not self.cancel_pending and not self.force_stopping) else "disabled",
        )
        self.btn_start.configure(
            text="启动中…" if self.service_starting else "启动服务",
            state="disabled" if (blocked or self.server.running()) else "normal",
        )
        self.btn_stop.configure(
            state="normal" if (self.server.stoppable() and not self.service_starting) else "disabled",
        )

    def _set_progress_active(self, active):
        if active:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def _schedule_preview_resize(self, _event=None):
        if self.preview_source is None:
            return
        if self.preview_resize_job is not None:
            self.after_cancel(self.preview_resize_job)
        self.preview_resize_job = self.after(80, self._resize_preview)

    def _resize_preview(self):
        self.preview_resize_job = None
        if self.preview_source is None:
            return
        available_width = max(1, self.canvas.winfo_width() - 24)
        available_height = max(1, self.canvas.winfo_height() - 24)
        factor = max(
            1,
            math.ceil(self.preview_source.width() / available_width),
            math.ceil(self.preview_source.height() / available_height),
        )
        self.preview = (
            self.preview_source if factor == 1
            else self.preview_source.subsample(factor, factor)
        )
        self.canvas.configure(image=self.preview, text="")

    # ------------------------------------------------------------ 交互

    def on_preset(self, width, height):
        """点预设块：写入宽高，高亮由变量的 trace 统一同步"""
        self.var_width.set(str(width))
        self.var_height.set(str(height))

    def active_presets(self):
        """当前生效的预设集"""
        return SIZE_PRESETS_HD if self.var_hd.get() else SIZE_PRESETS

    def on_toggle_tier(self):
        """标准档 / 高清档切换：换一套预设并刷新高亮"""
        self.strip.set_presets(self.active_presets())
        self._on_size_var_changed()

    def _on_size_var_changed(self, *_args):
        """宽高变化时同步预设高亮与尺寸提示"""
        values, error = parse_generation_parameters(
            self.var_width.get(), self.var_height.get(), self.var_steps.get(),
            self.var_cfg.get(), self.var_seed.get(),
        )
        self._params_valid = values is not None
        self.var_param_error.set(error or "")
        if error:
            self.lbl_param_error.pack(anchor="w", pady=(3, 0))
        else:
            self.lbl_param_error.pack_forget()

        try:
            current = (int(self.var_width.get()), int(self.var_height.get()))
        except ValueError:
            self.var_size.set("宽高需要是整数")
            self.strip.set_selected(None)
            self._sync_buttons()
            return
        megapixels = current[0] * current[1] / 1e6
        self.var_size.set(
            f"当前 {current[0]} × {current[1]}（约 {megapixels:.2f} MP，"
            f"预计 {megapixels * SECONDS_PER_MP:.0f} 秒）"
        )
        self.strip.set_selected(None)
        for index, (_, width, height) in enumerate(self.active_presets()):
            if (width, height) == current:
                self.strip.set_selected(index)
                break
        self._sync_buttons()

    def on_mode(self):
        editing = self.var_mode.get() == "edit"
        if editing:
            self.ref_panel.pack(fill="x", pady=(PAD, 0), after=self.lbl_mode_hint)
            if self.server.running() and not self.server.with_vision:
                self.var_mode_hint.set("当前服务未加载编辑能力，生成时会自动重启并加载。")
                self.lbl_mode_hint.configure(style="Error.TLabel")
                self.log("提示：当前服务未加载视觉塔，编辑模式生成时会自动重启服务。")
            else:
                self.var_mode_hint.set("添加参考图并描述修改（图片也可直接拖进窗口）；编辑能力会自动加载。")
                self.lbl_mode_hint.configure(style="Muted.TLabel")
        else:
            self.ref_panel.pack_forget()
            self.var_mode_hint.set("直接描述想要的画面；无需参考图。")
            self.lbl_mode_hint.configure(style="Muted.TLabel")
        self._sync_buttons()

    def _sync_ref_buttons(self):
        selection = self.list_ref.curselection()
        index = selection[0] if selection else None
        self.btn_clear_ref.configure(state="normal" if self.ref_paths else "disabled")
        self.btn_ref_remove.configure(state="normal" if index is not None else "disabled")
        self.btn_ref_up.configure(
            state="normal" if index is not None and index > 0 else "disabled",
        )
        self.btn_ref_down.configure(
            state="normal"
            if index is not None and index < len(self.ref_paths) - 1 else "disabled",
        )

    def _refresh_ref_list(self):
        """按当前顺序重画参考图列表，行首序号即提示词里的图一、图二"""
        self.list_ref.delete(0, "end")
        for index, path in enumerate(self.ref_paths, start=1):
            self.list_ref.insert("end", f"{index}. {path.name}")
        if self.ref_paths:
            self.var_ref.set(f"已选 {len(self.ref_paths)} 张")
        else:
            self.var_ref.set("未选择")
        self._sync_ref_buttons()

    def on_pick_ref(self):
        paths = filedialog.askopenfilenames(
            title="选择参考图（可多选，按住 Ctrl 或 Shift）",
            initialdir=str(OUTPUTS if OUTPUTS.is_dir() else ROOT),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        self._add_refs(paths)

    def _add_refs(self, paths):
        """把图片追加到参考图列表（重复的跳过），有新增就切到图像编辑模式"""
        added = 0
        for raw in paths:
            candidate = Path(raw)
            if candidate in self.ref_paths:
                continue
            self.ref_paths.append(candidate)
            added += 1
        if added:
            self._refresh_ref_list()
            self.var_mode.set("edit")
            self.on_mode()
        return added

    def _enable_drop(self, _event=None):
        """把拖放挂到顶层窗口上。装成之前每个子控件显示出来都会触发一次 <Map>，
        而顶层窗口要等窗口真正显示才有，所以按时间给 3 秒预算一直重试，别按次数算。"""
        if self.drop is not None:
            return
        if self._drop_giveup is None:
            self._drop_giveup = time.monotonic() + 3
        self.drop = enable_file_drop(self, self._queue_drop)
        if self.drop is not None:
            self.log("图片可直接拖进窗口，按顺序加进参考图列表。")
        elif time.monotonic() < self._drop_giveup:
            self.after(50, self._enable_drop)
        else:
            self.log("提示：本机未能启用拖放，请用「选择参考图…」添加图片。")

    def _queue_drop(self, paths):
        """窗口过程里只入队：在那里动控件会撞上系统同步消息的重入，Tk 操作交给 _drain"""
        self.events.put(("drop", paths))

    def on_files_dropped(self, paths):
        """拖进窗口的文件：图片加进参考图列表，其余只记一条日志"""
        images = [path for path in paths
                  if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
        others = [path.name for path in paths if path not in images]
        if others:
            self.log("已忽略非图片内容：" + "，".join(others))
        if not images:
            return
        if self._add_refs(images):
            self.log(f"拖入 {len(images)} 张图片，参考图共 {len(self.ref_paths)} 张。")
        else:
            self.log("拖入的图片已在参考图里，未重复添加。")

    def on_clear_ref(self):
        self.ref_paths = []
        self._refresh_ref_list()

    def on_move_ref(self, delta):
        """移动选中的参考图，用来调整它与提示词的对应关系"""
        selection = self.list_ref.curselection()
        if not selection:
            self.log("先在列表里点一张参考图，再调顺序。")
            return
        index = selection[0]
        target = index + delta
        if not 0 <= target < len(self.ref_paths):
            return
        self.ref_paths[index], self.ref_paths[target] = self.ref_paths[target], self.ref_paths[index]
        self._refresh_ref_list()
        self.list_ref.selection_set(target)
        self._sync_ref_buttons()

    def on_remove_ref(self):
        selection = self.list_ref.curselection()
        if not selection:
            self.log("先在列表里点一张参考图，再移除。")
            return
        index = selection[0]
        del self.ref_paths[index]
        self._refresh_ref_list()
        if index < len(self.ref_paths):
            self.list_ref.selection_set(index)
        elif self.ref_paths:
            self.list_ref.selection_set(len(self.ref_paths) - 1)
        self._sync_ref_buttons()

    def on_start(self):
        if self.server.running() or self.service_starting:
            return
        self.service_starting = True
        self._set_state("● 正在加载模型…", "busy")
        self._sync_buttons()
        with_vision = self.var_mode.get() == "edit"
        threading.Thread(target=self._start_worker, args=(with_vision,), daemon=True).start()

    def _start_worker(self, with_vision):
        try:
            ok, message = self.server.start(with_vision)
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.emit("log", message)
        self.emit("start_done", {"ok": ok, "message": message})

    def _on_start_done(self, payload):
        self.service_starting = False
        self._sync_buttons()
        if payload["ok"]:
            self._set_state("● 服务就绪", "ready")
        else:
            self._set_state("● 服务未启动", "error")
            self._set_log_visible(True)
            messagebox.showerror("启动失败", payload["message"])
        self.on_mode()

    def on_stop(self, force=False):
        if self.busy and not force:
            messagebox.showinfo("提示", "正在生成，请先取消任务；若无法取消可强制停止服务。")
            return
        if force:
            self.force_stopping = True
            self.var_progress.set("正在强制停止服务…")
        self.server.stop()
        self._set_state("● 服务未启动")
        self.on_mode()
        self._sync_buttons()

    def on_cancel(self):
        job = self.server.job_id
        if not job:
            self.log("任务仍在准备中，暂时没有可取消的任务 ID。")
            return
        self.cancel_pending = True
        self._sync_buttons()
        threading.Thread(target=self._cancel_worker, args=(job,), daemon=True).start()

    def _cancel_worker(self, job):
        try:
            self.server.cancel(job)
            self.emit("cancel_done", ("ok", "已请求取消任务。"))
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                self.emit("cancel_done", ("unsupported", None))
            else:
                self.emit("cancel_done", ("error", f"取消失败：HTTP {exc.code}"))
        except Exception as exc:
            self.emit("cancel_done", ("error", f"取消失败：{exc}"))

    def _on_cancel_done(self, payload):
        outcome, message = payload
        self.cancel_pending = False
        self._sync_buttons()
        if outcome == "ok":
            self.log(message)
        elif outcome == "unsupported":
            self.log("服务端不支持中断正在生成的画面（cancel_generating=false）。")
            if self.busy and messagebox.askyesno(
                "无法取消",
                "当前版本无法中断正在进行的生成。\n是否强制停止服务？这会中断本次生成并立即释放显存与内存。",
            ):
                self.on_stop(force=True)
        else:
            self.log(message)

    def on_close_preview(self):
        if self.preview_resize_job is not None:
            self.after_cancel(self.preview_resize_job)
            self.preview_resize_job = None
        self.canvas.configure(image="", text="")
        self.preview = None
        self.preview_source = None
        self.empty_preview.place(relx=0.5, rely=0.47, anchor="center")
        self.btn_close_preview.configure(state="disabled")

    def on_save(self):
        if not self.result_path or not Path(self.result_path).is_file():
            return
        target = filedialog.asksaveasfilename(
            title="另存为", defaultextension=".png", initialfile=Path(self.result_path).name,
            filetypes=[("PNG 图片", "*.png")],
        )
        if target:
            Path(target).write_bytes(Path(self.result_path).read_bytes())
            self.log(f"已保存到 {target}")

    def on_open_result(self):
        if self.result_path and Path(self.result_path).is_file():
            try:
                os.startfile(str(self.result_path))
            except OSError as exc:
                messagebox.showerror("无法打开图片", str(exc))

    def on_open_output_folder(self):
        try:
            OUTPUTS.mkdir(parents=True, exist_ok=True)
            os.startfile(str(OUTPUTS))
        except OSError as exc:
            messagebox.showerror("无法打开输出目录", str(exc))

    # ------------------------------------------------------------ 生成

    def on_generate(self):
        if self.busy or self.service_starting:
            return
        prompt = self.txt_prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("缺少画面描述", "请先描述想要生成的画面。")
            self.txt_prompt.focus_set()
            return

        editing = self.var_mode.get() == "edit"
        if editing and not self.ref_paths:
            messagebox.showwarning("缺少参考图", "图像编辑模式需要至少一张参考图。")
            return

        params, error = parse_generation_parameters(
            self.var_width.get(), self.var_height.get(), self.var_steps.get(),
            self.var_cfg.get(), self.var_seed.get(),
        )
        if error:
            messagebox.showwarning("参数有误", error)
            return
        width, height, steps, cfg, seed = params
        request = {
            "prompt": prompt,
            "negative_prompt": self.var_negative.get().strip(),
            "editing": editing,
            "ref_paths": tuple(self.ref_paths),
            "with_vision": editing,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
        }

        self.busy = True
        self.server.job_id = None
        estimate = width * height / 1e6 * SECONDS_PER_MP
        estimate_text = (
            f"基础生成约 {estimate:.0f} 秒，图像编辑会更久"
            if editing else f"预计约 {estimate:.0f} 秒"
        )
        self.var_progress.set(f"准备中 · {estimate_text}")
        self._set_state("● 生成中", "busy")
        self._set_progress_active(True)
        self._sync_buttons()

        threading.Thread(
            target=self._generate_worker,
            args=(request,),
            daemon=True,
        ).start()

    def _generate_worker(self, request):
        prompt = request["prompt"]
        editing = request["editing"]
        width = request["width"]
        height = request["height"]
        steps = request["steps"]
        cfg = request["cfg"]
        seed = request["seed"]
        try:
            if not server_alive():
                self.emit("log", "服务未运行，正在启动…")
                ok, message = self.server.start(request["with_vision"])
                if not ok:
                    self.emit("fail", message)
                    return
            if editing and not self.server.with_vision:
                self.emit("log", "当前服务未加载视觉塔，正在重启服务…")
                self.server.stop()
                ok, message = self.server.start(True)
                if not ok:
                    self.emit("fail", message)
                    return

            self.emit("log", f"提交任务：{width}x{height}（{width * height / 1e6:.2f} MP）"
                            f"步数 {steps} 引导 {cfg}")
            payload = {
                "prompt": prompt,
                "negative_prompt": request["negative_prompt"],
                "width": width,
                "height": height,
                "seed": seed if seed >= 0 else -1,
                "batch_count": 1,
                "sample_params": {
                    "sample_method": "euler",
                    "sample_steps": steps,
                    "guidance": {"txt_cfg": cfg},
                },
                "output_format": "png",
                # 解码分块显式开启。服务端不会像 sd-cli 那样在解码失败后自动降级，
                # 尺寸超过约 1 MP 就必须自己开，否则整张判失败。实测代价接近于零
                # （1024 尺寸下分块 5.72 秒，整体解码 5.74 秒）。
                "vae_tiling_params": {"enabled": True},
            }
            if editing:
                # 顺序即提示词里的图一、图二：服务端按数组顺序逐张编码，
                # increase_ref_index 让每张参考图拿到递增的位置索引，模型据此区分它们
                images = []
                for path in request["ref_paths"]:
                    data = base64.b64encode(path.read_bytes()).decode("ascii")
                    images.append(f"data:image/png;base64,{data}")
                payload["ref_images"] = images
                payload["increase_ref_index"] = True
                self.emit("log", "参考图顺序：" + "，".join(
                    f"图{i} {p.name}" for i, p in enumerate(request["ref_paths"], start=1)))

            job_id = self.server.submit(payload)
            if not job_id:
                self.emit("fail", "服务未返回任务 ID。")
                return

            started = time.time()
            while True:
                time.sleep(1.0)
                info = self.server.job(job_id)
                status = info.get("status")
                elapsed = time.time() - started
                if status in ("queued", "generating"):
                    self.emit("progress", f"{'排队中' if status == 'queued' else '生成中'}… 已用 {elapsed:.0f} 秒")
                    continue
                break

            if status == "cancelled":
                self.emit("cancelled", "任务已取消。")
                return
            if status != "completed":
                error = (info.get("error") or {}).get("message") or "未知错误"
                self.emit("fail", f"生成失败：{error}")
                return

            images = ((info.get("result") or {}).get("images")) or []
            if not images:
                self.emit("fail", "服务未返回图片。")
                return

            OUTPUTS.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = OUTPUTS / f"{'edit' if editing else 'qwen'}-{width}x{height}-{stamp}.png"
            path.write_bytes(base64.b64decode(images[0]["b64_json"]))
            self.emit("done", {"path": path, "elapsed": elapsed})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            self.emit("fail", f"服务返回 HTTP {exc.code}：{detail}")
        except Exception as exc:
            self.emit("fail", f"{type(exc).__name__}: {exc}")

    def _on_cancelled(self, message):
        self.busy = False
        self.cancel_pending = False
        self.server.job_id = None
        self._set_progress_active(False)
        self._sync_buttons()
        self.var_progress.set(message)
        self._set_state("● 服务就绪", "ready")
        self.log(message)

    def _on_done(self, payload):
        self.busy = False
        self.cancel_pending = False
        self.server.job_id = None
        self._set_progress_active(False)
        self._sync_buttons()

        path = Path(payload["path"])
        self.result_path = path
        self._show_image(path)
        self.btn_save.configure(state="normal")
        self.btn_open.configure(state="normal")
        self.var_progress.set(f"{path.name} · 耗时 {payload['elapsed']:.1f} 秒")
        self._set_state("● 服务就绪", "ready")
        self.on_mode()
        self.log(f"完成：{path}")

    def _on_fail(self, message):
        intentionally_stopped = self.force_stopping
        self.force_stopping = False
        self.busy = False
        self.cancel_pending = False
        self.server.job_id = None
        self._set_progress_active(False)
        self._sync_buttons()
        self.var_progress.set("已停止")
        if self.server.running():
            self._set_state("● 服务就绪", "ready")
        else:
            self._set_state("● 服务未启动")
        self.on_mode()
        if intentionally_stopped:
            self.var_progress.set("已强制停止")
            self.log("任务已因服务停止而中断。")
            return
        self.log(f"[失败] {message}")
        self._set_log_visible(True)
        messagebox.showerror("生成失败", message)

    def _show_image(self, path):
        try:
            image = tk.PhotoImage(file=str(path))
        except Exception as exc:
            self.log(f"预览失败：{exc}")
            return
        self.preview_source = image
        self.empty_preview.place_forget()
        self._resize_preview()
        self.btn_close_preview.configure(state="normal")

    # ------------------------------------------------------------ 退出

    def on_close(self):
        if self.busy and not messagebox.askyesno("确认", "正在生成，退出会中断本次生成。确定退出吗？"):
            return
        if self.server.adopted:
            messagebox.showinfo(
                "提示",
                "1234 端口上的服务并非本窗口启动，退出后它仍会占用显存与内存。\n"
                "如需释放，请结束该 sd-server.exe 进程。",
            )
        self._release_and_exit()

    def _release_and_exit(self):
        """退出前释放资源：先结束服务进程，再销毁窗口"""
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    app = App()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        # 无论正��关闭还是异常退出，都确保服务进程被结束
        try:
            app.server.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
