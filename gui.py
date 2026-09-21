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
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
SD_SERVER = ROOT / "bin" / "sd-server.exe"
OUTPUTS = ROOT / "outputs"

# 扩散模型用 HQv3 混合精度（注意力层 q8_0、MLP 输出层 Q8_0）。它由 ComfyUI-GGUF 转换，
# img_in.weight 的形状声明与 sd.cpp 的推断方式冲突，需先用 patch_gguf_img_in.py 修正，
# 详见 README「量化版本选择」。换回更快的普通量化只改这一行即可。
DIFFUSION_MODEL = ROOT / "models" / "Qwen-Image-2.1-Q4_K_M-HQv3.gguf"
VAE_MODEL = ROOT / "models" / "vae" / "qwen_image_2.1_vae_bf16.safetensors"

# 文本编码器优先用 GGUF（元数据完整），safetensors 作为备选
TEXT_ENCODERS = (
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
TEXT = "#1F1B18"
MUTED = "#7A716B"
ACCENT = "#A8520A"
ACCENT_ACTIVE = "#8F4508"
ACCENT_SOFT = "#FBEEE1"
LOG_BG = "#1C1917"
LOG_FG = "#D8D2CB"
STATE_COLORS = {"idle": MUTED, "busy": "#B45309", "ready": "#15803D", "error": "#B91C1C"}

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


class SizePresetStrip(tk.Canvas):
    """宽高比预设条：每格画出该比例的缩略矩形，点一下切换尺寸"""

    THUMB_BOX = 30

    def __init__(self, master, presets, on_click, **kwargs):
        super().__init__(master, height=72, highlightthickness=0, background=BG, **kwargs)
        self.presets = presets
        self.on_click = on_click
        self.selected = None
        self.bind("<Configure>", lambda _event: self.redraw())
        self.redraw()

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

            self.tag_bind(tag, "<Button-1>", lambda _event, i=index: self.on_click(*self.presets[i][1:]))
            self.tag_bind(tag, "<Enter>", lambda _event: self.configure(cursor="hand2"))
            self.tag_bind(tag, "<Leave>", lambda _event: self.configure(cursor=""))


# ---------------------------------------------------------------- 界面

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Qwen-Image-2.1")
        self.geometry("1320x940")
        self.minsize(1120, 780)
        self._setup_style()

        self.events = queue.Queue()
        self.server = QwenServer(self.emit)
        self.busy = False
        self.preview = None
        self.result_path = None
        self.start_time = 0.0

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain)
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.log("就绪。点击「启动服务」加载模型（约 10 秒），或直接点「生成」自动启动。")

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
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Metric.TLabel", background=BG, foreground=MUTED, font=(MONO_FONT, 10))
        style.configure("State.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 10, "bold"))
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TRadiobutton", background=BG, foreground=TEXT)
        style.map("TRadiobutton", background=[("active", BG)])

        base_button = dict(bordercolor=BORDER, relief="solid", borderwidth=1,
                           padding=(12, 7), font=(UI_FONT, 10))
        style.configure("TButton", background=PANEL, foreground=TEXT, **base_button)
        style.map("TButton",
                  background=[("active", ACCENT_SOFT), ("disabled", BG)],
                  foreground=[("disabled", MUTED)])
        # 同一行里的主次按钮几何完全一致，只靠填充与描边区分
        style.configure("Primary.TButton", background=ACCENT, foreground="#FFFFFF",
                        bordercolor=ACCENT, relief="solid", borderwidth=1,
                        padding=(12, 7), font=(UI_FONT, 10))
        style.map("Primary.TButton",
                  background=[("active", ACCENT_ACTIVE), ("disabled", "#DCC7B2")],
                  foreground=[("disabled", "#FFF9F4")])
        style.configure("Secondary.TButton", background=PANEL, foreground=TEXT, **base_button)
        style.map("Secondary.TButton",
                  background=[("active", ACCENT_SOFT), ("disabled", BG)],
                  foreground=[("disabled", MUTED)])
        style.configure("TScrollbar", background=BG, troughcolor=BG, bordercolor=BORDER)

    def _build(self):
        self.configure(background=BG)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # 顶部状态栏：状态语义交给颜色，读数用等宽字体
        status = ttk.Frame(self, padding=(PAD + 4, PAD))
        status.grid(row=0, column=0, sticky="ew")
        self.var_state = tk.StringVar(value="● 服务未启动")
        self.var_vram = tk.StringVar(value="显存 —")
        self.var_ram = tk.StringVar(value="内存 —")
        self.lbl_state = ttk.Label(status, textvariable=self.var_state, style="State.TLabel")
        self.lbl_state.pack(side="left")
        ttk.Label(status, textvariable=self.var_ram, style="Metric.TLabel").pack(side="right")
        ttk.Label(status, textvariable=self.var_vram, style="Metric.TLabel").pack(
            side="right", padx=(0, 18))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=PAD + 4)

        # 左侧控制面板：放进可滚动容器，窗口再矮也不会把按钮挤没
        left_outer = ttk.Frame(body, padding=(0, 0, PAD, 0))
        body.add(left_outer, weight=0)
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, highlightthickness=0, background=BG,
                                width=392, height=600, takefocus=0)
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_scroll.grid(row=0, column=1, sticky="ns")
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

        server_box = ttk.LabelFrame(left, text=" 服务 ", padding=PAD)
        server_box.pack(fill="x", pady=(0, PAD))
        server_row = ttk.Frame(server_box)
        server_row.pack(fill="x")
        self.btn_start = ttk.Button(server_row, text="启动服务", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(server_row, text="停止服务", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.var_vision = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            server_box, text="加载视觉塔（编辑需要，+1.1 GB 内存）", variable=self.var_vision
        ).pack(anchor="w", pady=(PAD, 0))

        input_box = ttk.LabelFrame(left, text=" 输入 ", padding=PAD)
        input_box.pack(fill="both", expand=True, pady=(0, PAD))

        mode_row = ttk.Frame(input_box)
        mode_row.pack(fill="x")
        self.var_mode = tk.StringVar(value="txt2img")
        ttk.Radiobutton(mode_row, text="文生图", value="txt2img", variable=self.var_mode,
                        command=self.on_mode).pack(side="left")
        ttk.Radiobutton(mode_row, text="图像编辑", value="edit", variable=self.var_mode,
                        command=self.on_mode).pack(side="left", padx=(18, 0))

        self.ref_path = None
        ref_row = ttk.Frame(input_box)
        ref_row.pack(fill="x", pady=(PAD, 0))
        self.btn_ref = ttk.Button(ref_row, text="选择参考图…", command=self.on_pick_ref)
        self.btn_ref.pack(side="left")
        ttk.Button(ref_row, text="清除", command=self.on_clear_ref).pack(side="left", padx=(8, 0))
        self.var_ref = tk.StringVar(value="未选择")
        ttk.Label(ref_row, textvariable=self.var_ref, style="Muted.TLabel").pack(side="left", padx=(10, 0))

        self.txt_prompt = tk.Text(input_box, height=4, width=44, wrap="word", font=(UI_FONT, 10),
                                  relief="solid", borderwidth=1, highlightthickness=0,
                                  background=PANEL, foreground=TEXT, insertbackground=TEXT,
                                  padx=8, pady=6)
        self.txt_prompt.pack(fill="both", expand=True, pady=(PAD, 0))

        ttk.Label(input_box, text="负面提示词（可留空）", style="Muted.TLabel").pack(
            anchor="w", pady=(PAD, 4))
        self.var_negative = tk.StringVar()
        tk.Entry(input_box, textvariable=self.var_negative, font=(UI_FONT, 10), width=44,
                 relief="solid", borderwidth=1, highlightthickness=0,
                 background=PANEL, foreground=TEXT, insertbackground=TEXT).pack(fill="x", ipady=4)

        # 尺寸预设：一格一个宽高比，点一下即换
        size_box = ttk.LabelFrame(left, text=" 尺寸预设 ", padding=PAD)
        size_box.pack(fill="x", pady=(0, PAD))
        self.var_hd = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            size_box, text="高清档（边长 1.5 倍，单张大 2~4 分钟）",
            variable=self.var_hd, command=self.on_toggle_tier,
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
            tk.Entry(cell, textvariable=var, font=(MONO_FONT, 10), width=7,
                     relief="solid", borderwidth=1, highlightthickness=0,
                     background=PANEL, foreground=TEXT, insertbackground=TEXT,
                     justify="right").grid(row=0, column=1, sticky="ew", padx=(6, 0), ipady=3)
        for var in (self.var_width, self.var_height):
            var.trace_add("write", self._on_size_var_changed)
        ttk.Label(params, text="宽高需为 32 的倍数 · 种子 -1 为随机", style="Muted.TLabel").pack(
            anchor="w", pady=(PAD, 0))

        actions = ttk.Frame(left)
        actions.pack(fill="x")
        self.btn_generate = ttk.Button(actions, text="生成", command=self.on_generate,
                                       style="Primary.TButton")
        self.btn_generate.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(actions, text="取消", command=self.on_cancel,
                                     style="Secondary.TButton", state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))

        # 右侧预览台
        right = ttk.Frame(body)
        body.add(right, weight=1)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.canvas = tk.Label(right, background=PANEL, text="生成结果将显示在这里",
                               foreground=MUTED, font=(UI_FONT, 11),
                               highlightbackground=BORDER, highlightthickness=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        preview_actions = ttk.Frame(right)
        preview_actions.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        self.var_progress = tk.StringVar(value="")
        ttk.Label(preview_actions, textvariable=self.var_progress, style="Metric.TLabel").pack(side="left")
        self.btn_save = ttk.Button(preview_actions, text="另存为…", command=self.on_save, state="disabled")
        self.btn_save.pack(side="right")

        # 底部日志
        log_box = ttk.LabelFrame(self, text=" 日志 ", padding=PAD // 2)
        log_box.grid(row=2, column=0, sticky="ew", padx=PAD + 4, pady=(PAD, PAD + 4))
        self.txt_log = tk.Text(log_box, height=5, wrap="none", state="disabled",
                               font=(MONO_FONT, 9), background=LOG_BG, foreground=LOG_FG,
                               relief="flat", borderwidth=0, padx=8, pady=6)
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)

        self._on_size_var_changed()

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
                elif kind == "progress":
                    self.var_progress.set(payload)
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
        self.btn_generate.configure(state="disabled" if self.busy else "normal")
        self.btn_cancel.configure(state="normal" if self.busy else "disabled")
        self.btn_start.configure(state="disabled" if (self.busy or self.server.running()) else "normal")
        self.btn_stop.configure(state="normal" if self.server.stoppable() else "disabled")

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
        try:
            current = (int(self.var_width.get()), int(self.var_height.get()))
        except ValueError:
            self.var_size.set("宽高需要是整数")
            return
        megapixels = current[0] * current[1] / 1e6
        self.var_size.set(
            f"当前 {current[0]} × {current[1]}（约 {megapixels:.2f} MP，"
            f"预计 {megapixels * SECONDS_PER_MP:.0f} 秒）"
        )
        if not hasattr(self, "strip"):
            return
        for index, (_, width, height) in enumerate(self.active_presets()):
            if (width, height) == current:
                self.strip.set_selected(index)
                return
        self.strip.set_selected(None)

    def on_mode(self):
        editing = self.var_mode.get() == "edit"
        if editing and self.server.running() and not self.server.with_vision:
            self.log("提示：当前服务未加载视觉塔，编辑模式需要重启服务。")

    def on_pick_ref(self):
        path = filedialog.askopenfilename(
            title="选择参考图",
            initialdir=str(OUTPUTS if OUTPUTS.is_dir() else ROOT),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.ref_path = Path(path)
        self.var_ref.set(self.ref_path.name)
        self.var_mode.set("edit")

    def on_clear_ref(self):
        self.ref_path = None
        self.var_ref.set("未选择")

    def on_start(self):
        if self.server.running():
            return
        self.btn_start.configure(state="disabled")
        self._set_state("● 正在加载模型…", "busy")
        threading.Thread(target=self._start_worker, args=(self.var_vision.get(),), daemon=True).start()

    def _start_worker(self, with_vision):
        ok, message = self.server.start(with_vision)
        self.emit("log", message)
        self.events.put(("done", {"started": True} if ok else {"error": message}))

    def on_stop(self, force=False):
        if self.busy and not force:
            messagebox.showinfo("提示", "正在生成，请先取消任务；若无法取消可强制停止服务。")
            return
        self.server.stop()
        self._set_state("● 服务未启动")
        self._sync_buttons()

    def on_cancel(self):
        job = self.server.job_id
        if not job:
            return
        try:
            self.server.cancel(job)
            self.log("已请求取消任务。")
        except urllib.error.HTTPError as exc:
            # 该版本 capabilities 里 cancel_generating=false，运行中的任务无法中断
            if exc.code == 409:
                self.log("服务端不支持中断正在生成的画面（cancel_generating=false）。")
                if messagebox.askyesno(
                    "无法取消",
                    "当前版本无法中断正在进行的生成。\n是否强制停止服务？这会中断本次生成并立即释放显存与内存。",
                ):
                    self.on_stop(force=True)
                return
            self.log(f"取消失败：HTTP {exc.code}")
        except Exception as exc:
            self.log(f"取消失败：{exc}")

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

    # ------------------------------------------------------------ 生成

    def on_generate(self):
        if self.busy:
            return
        prompt = self.txt_prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("提示", "请先填写提示词。")
            return

        editing = self.var_mode.get() == "edit"
        if editing and self.ref_path is None:
            messagebox.showwarning("提示", "图像编辑模式需要先选择参考图。")
            return

        try:
            width, height = int(self.var_width.get()), int(self.var_height.get())
            steps = int(self.var_steps.get())
            cfg = float(self.var_cfg.get())
            seed = int(self.var_seed.get())
        except ValueError:
            messagebox.showwarning("提示", "参数必须是数字。")
            return
        if width % 128 or height % 128:
            messagebox.showwarning(
                "提示",
                "宽和高必须是 128 的倍数。\n\n"
                "解码按瓦片分块进行，尺寸不在 128 网格上会在接缝处出现亮度台阶。\n"
                "常用可用值：1024、1152、1280、1408、1536、1792、2048。",
            )
            return

        self.busy = True
        self.btn_generate.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_start.configure(state="disabled")
        self.var_progress.set("准备中…")
        self._set_state("● 生成中", "busy")

        threading.Thread(
            target=self._generate_worker,
            args=(prompt, editing, width, height, steps, cfg, seed),
            daemon=True,
        ).start()

    def _generate_worker(self, prompt, editing, width, height, steps, cfg, seed):
        try:
            if not server_alive():
                self.emit("log", "服务未运行，正在启动…")
                ok, message = self.server.start(self.var_vision.get())
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
                "negative_prompt": self.var_negative.get().strip(),
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
                data = base64.b64encode(self.ref_path.read_bytes()).decode("ascii")
                payload["ref_images"] = [f"data:image/png;base64,{data}"]

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
                self.emit("fail", "任务已取消。")
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

    def _on_done(self, payload):
        self.busy = False
        self._sync_buttons()

        if payload.get("started"):
            self._set_state("● 服务就绪", "ready")
            self.log("服务已就绪。模型在首次出图时载入，之后常驻，连续出图不再重复加载。")
            return
        if "error" in payload:
            self._set_state("● 服务未启动")
            messagebox.showerror("启动失败", payload["error"])
            return

        path = Path(payload["path"])
        self.result_path = path
        self._show_image(path)
        self.btn_save.configure(state="normal")
        self.var_progress.set(f"完成，耗时 {payload['elapsed']:.1f} 秒")
        self._set_state("● 服务就绪", "ready")
        self.log(f"完成：{path}")

    def _on_fail(self, message):
        self.busy = False
        self._sync_buttons()
        self.var_progress.set("已停止")
        if self.server.running():
            self._set_state("● 服务就绪", "ready")
        else:
            self._set_state("● 服务未启动")
        self.log(f"[失败] {message}")
        messagebox.showerror("生成失败", message)

    def _show_image(self, path):
        try:
            image = tk.PhotoImage(file=str(path))
        except Exception as exc:
            self.log(f"预览失败：{exc}")
            return
        factor = max(1, -(-max(image.width(), image.height()) // 700))
        if factor > 1:
            image = image.subsample(factor, factor)
        self.preview = image  # 保持引用，否则会被回收
        self.canvas.configure(image=image, text="")

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
