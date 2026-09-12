#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cut_by_osd_gui.py — cut_by_osd.py 的简易桌面窗口界面(tkinter)

功能
----
标签页 1"截取":
- 选择视频目录 / 时间段文件 / 输出目录 / 通道 / ffmpeg 路径
- 运行"仅预览"(--check)或正式截取,实时滚动显示日志
- 可中途停止;重复运行建议勾选"使用缓存"

标签页 2"校准":
- 列出目录下每个视频文件的 OSD 校准结果(起点、锚点数)
- 双击任意文件可手动修改 OSD 起点(校准失败/误读时修正)
- 保存后写入 osd_override.json,截取时自动生效(--override)

用法
----
python cut_by_osd_gui.py
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    import cut_by_osd as CORE
except ImportError:
    CORE = None

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cut_by_osd.py")
OVERRIDE_NAME = "osd_override.json"
REGION_NAME = "osd_region.json"
PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
DEFAULT_TXT = "新建 文本文档.txt"
MAX_LOG_LINES = 3000
VIDEO_EXTS = (".mp4", ".h264", ".264", ".h265", ".265")


def pick_python():
    """子进程优先使用自带 runtime 的 Python(含 rapidocr),避免系统 Python 缺依赖"""
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("python.exe", "pythonw.exe"):
        p = os.path.join(base, "runtime", name)
        if os.path.isfile(p):
            return p
    return sys.executable


def ensure_runtime_python():
    """当前解释器缺少 rapidocr 时,改用自带 runtime 重启 GUI;False=已转交"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        pass
    base = os.path.dirname(os.path.abspath(__file__))
    rt = os.path.join(base, "runtime", "pythonw.exe")
    rt_check = os.path.join(base, "runtime", "python.exe")
    if not (os.path.isfile(rt) and os.path.isfile(rt_check)):
        return True
    if os.path.normcase(os.path.abspath(sys.executable)) == \
            os.path.normcase(os.path.abspath(rt)):
        return True
    r = subprocess.run([rt_check, "-c", "import rapidocr_onnxruntime"],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return True
    subprocess.Popen([rt, os.path.abspath(__file__)], close_fds=True)
    return False


def region_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), REGION_NAME)


def list_profiles():
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(f for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))


PRESETS = [
    ("自定义…", "", ""),
    ("时间 HH:MM:SS", r"\d{1,2}:\d{2}:\d{2}", "示例: 14:53:00"),
    ("时间 分钟级", r"\d{1,2}:\d{2}", "示例: 14:53"),
    ("日期 YYYY-MM-DD", r"\d{4}[-\s/]\d{1,2}[-\s/]\d{1,2}", "示例: 2026-07-31"),
    ("日期+时间(容忍粘连)", r"\d{4}[^\d]{0,3}\d{2}[^\d]{0,3}\d{2}[^\d]{0,3}\d{1,2}:\d{2}(?::\d{2})?", "示例: 2026-07-31 14:53:00 / 2026-07-31714:53"),
    ("车速 km/h", r"\d{1,3}\s*km/h", "示例: 30km/h / 105 km/h"),
    ("GPS 坐标", r"\d{1,3}\.\d{4,}[NS]\s*\d{1,3}\.\d{4,}[EW]", "示例: 33.4795649S 150.5271889E"),
    ("车牌(常规)", r"[A-Z]{1,3}[- ]?[A-Z0-9]{2,6}", "示例: ABC-1234 / NZ123"),
    ("中文车牌", r"[\u4e00-\u9fa5][A-Z][A-Z0-9]{4,6}", "示例: 京A12345"),
    ("纯数字编号", r"\d{5,15}", "示例: 20510092212"),
    ("任意数字(含小数/负)", r"-?\d+(\.\d+)?", "示例: 33.47 / -12"),
    ("经纬度原始", r"\d{1,3}\.\d+", "示例: 150.5271889"),
]


def sec2hms(s):
    s = int(round(s)) % 86400
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def hms2sec(txt):
    txt = txt.strip()
    if not txt:
        raise ValueError("为空")
    parts = txt.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("格式应为 HH:MM:SS")
    try:
        hh, mm = int(parts[0]), int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise ValueError("包含非数字字符")
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("数值超出范围")
    return hh * 3600 + mm * 60 + ss


class RegionDialog(tk.Toplevel):
    """抓拍帧上手动框选 OSD 区域,支持实时预览识别结果"""

    def __init__(self, root, frame_path, src_dir):
        super().__init__(root)
        self.title("框选 OSD 时间区域(拖拽画矩形)")
        self.resizable(False, False)
        self.src_dir = src_dir
        self.geom = None
        self.rect = None  # 归一化 (x, y, w, h)

        if Image is None:
            messagebox.showerror("错误", "缺少 pillow,请先: pip install pillow")
            self.destroy()
            return

        img = Image.open(frame_path)
        self.img = img
        self.scale = min(1.0, 900 / img.width, 520 / img.height)
        disp = img.resize((int(img.width * self.scale),
                           int(img.height * self.scale)))
        self.photo = ImageTk.PhotoImage(disp)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="在画面上框住 OSD 时间(左上角日期时间区域),松开鼠标完成。"
                            "可反复框选,").pack(side="left")
        ttk.Label(top, text="点“测试识别”确认识别到时间后保存。").pack(side="left")

        self.canvas = tk.Canvas(self, width=disp.width, height=disp.height,
                                cursor="crosshair")
        self.canvas.pack(padx=8, pady=4)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        self.coord_var = tk.StringVar(value="未框选")
        ttk.Label(self, textvariable=self.coord_var, foreground="#333").pack()

        self.ocr_var = tk.StringVar(value="")
        self.ocr_lbl = ttk.Label(self, textvariable=self.ocr_var,
                                 foreground="#060", wraplength=880)
        self.ocr_lbl.pack(padx=8)

        btns = ttk.Frame(self, padding=8)
        btns.pack()
        ttk.Button(btns, text="测试识别", command=self.test_ocr).pack(side="left", padx=4)
        ttk.Button(btns, text="保存并应用", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=4)

        self._drag_start = None
        self._rect_id = None
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, e):
        self._drag_start = (e.x, e.y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#f00", width=2)

    def _on_drag(self, e):
        if self._rect_id is None:
            return
        self.canvas.coords(self._rect_id, self._drag_start[0],
                           self._drag_start[1], e.x, e.y)

    def _on_release(self, e):
        if self._rect_id is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = e.x, e.y
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if x1 - x0 < 5 or y1 - y0 < 5:
            self.coord_var.set("框选太小,请重新框选")
            return
        iw, ih = self.img.size
        self.rect = (x0 / self.scale / iw, y0 / self.scale / ih,
                     (x1 - x0) / self.scale / iw, (y1 - y0) / self.scale / ih)
        self.coord_var.set("区域: x=%.3f y=%.3f w=%.3f h=%.3f" % self.rect)
        self.ocr_var.set("")

    def test_ocr(self):
        if self.rect is None:
            self.ocr_var.set("请先框选区域")
            return
        self.ocr_var.set("识别中…")
        self.update_idletasks()
        try:
            x, y, w, h = self.rect
            iw, ih = self.img.size
            crop = self.img.crop((int(x * iw), int(y * ih),
                                  int((x + w) * iw), int((y + h) * ih)))
            s = 4
            crop = crop.resize((crop.width * s, crop.height * s), Image.LANCZOS)
            tmp = os.path.join(tempfile.gettempdir(), "osd_region_test.png")
            crop.save(tmp)
            from rapidocr_onnxruntime import RapidOCR
            res, _ = RapidOCR()(tmp)
            txt = " ".join(i[1] for i in res) if res else "(无文本)"
            self.ocr_var.set("识别结果: %s" % txt[:200])
        except Exception as e:
            self.ocr_var.set("识别失败: %s" % e)

    def save(self):
        if self.rect is None:
            messagebox.showinfo("提示", "请先在画面上框选区域")
            return
        p = region_path()
        data = {}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[os.path.normcase(os.path.abspath(self.src_dir))] = {
            "x": self.rect[0], "y": self.rect[1],
            "w": self.rect[2], "h": self.rect[3]}
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("错误", "保存失败: %s" % e)
            return
        self.destroy()
        messagebox.showinfo("完成", "OSD 区域已保存到:\n%s\n\n截取/校准时自动生效。" % p)


class App:
    def __init__(self, root):
        self.root = root
        root.title("cut_by_osd — 按画面 OSD 时间截取 DVR 视频")
        root.geometry("860x680")
        root.minsize(680, 540)

        self.proc = None
        self.q = queue.Queue()
        self.cal_rows = {}   # 文件名 -> {dur, osd0, precise, manual}

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True)
        self.tab_cut = ttk.Frame(nb)
        self.tab_cal = ttk.Frame(nb)
        self.tab_match = ttk.Frame(nb)
        nb.add(self.tab_cut, text="  截取  ")
        nb.add(self.tab_cal, text="  校准  ")
        nb.add(self.tab_match, text="  文本识别  ")
        self._build_cut_tab()
        self._build_cal_tab()
        self._build_match_tab()

        self.root.after(100, self._poll)

    # ================= 截取页 =================
    def _build_cut_tab(self):
        t = self.tab_cut
        pad = {"padx": 8, "pady": 3}
        frm = ttk.Frame(t, padding=8)
        frm.pack(fill="x")

        def browses():
            d = filedialog.askdirectory()
            if d:
                self.dir_var.set(d)
                self.auto_txt(d)
                self._load_overrides(d)
                self._refresh_profiles()
                self._match_refresh_files()

        def browsedir(v):
            def _do():
                d = filedialog.askdirectory()
                if d:
                    v.set(d)
            return _do

        def browsefile(v):
            def _do():
                f = filedialog.askopenfilename(
                    filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
                if f:
                    v.set(f)
            return _do

        self.dir_var = tk.StringVar()
        self.txt_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.ch_var = tk.StringVar()
        self.ff_var = tk.StringVar()

        self.ck_check = tk.BooleanVar(value=True)
        self.ck_cache = tk.BooleanVar(value=True)
        self.ck_verb = tk.BooleanVar(value=False)

        ttk.Label(frm, text="视频目录").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self.dir_var, width=50).grid(
            row=0, column=1, sticky="we", **pad)
        ttk.Button(frm, text="浏览…", command=browses).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="时间段文件").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self.txt_var, width=50).grid(
            row=1, column=1, sticky="we", **pad)
        ttk.Button(frm, text="浏览…", command=browsefile(self.txt_var)).grid(
            row=1, column=2, **pad)

        ttk.Label(frm, text="输出目录").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self.out_var, width=50).grid(
            row=2, column=1, sticky="we", **pad)
        ttk.Button(frm, text="浏览…", command=browsedir(self.out_var)).grid(
            row=2, column=2, **pad)

        ttk.Label(frm, text="通道(可选)").grid(row=3, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self.ch_var, width=12).grid(
            row=3, column=1, sticky="w", **pad)
        ttk.Label(frm, text="ffmpeg(可选)").grid(row=3, column=1, sticky="e",
                                                 padx=(220, 0), pady=3)
        ttk.Entry(frm, textvariable=self.ff_var, width=30).grid(
            row=3, column=1, sticky="e", padx=(250, 8), pady=3)

        opt = ttk.Frame(frm)
        opt.grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(opt, text="仅预览(--check)",
                        variable=self.ck_check).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="使用缓存(--cache)",
                        variable=self.ck_cache).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="详细日志(--verbose)",
                        variable=self.ck_verb).pack(side="left", padx=4)

        btns = ttk.Frame(t, padding=(8, 0, 8, 4))
        btns.pack(fill="x")
        self.btn_region = ttk.Button(btns, text="框选OSD区域", command=self.region_set)
        self.btn_region.pack(side="left", padx=4)
        self.btn_check = ttk.Button(btns, text="预览映射", command=lambda: self.run(True))
        self.btn_check.pack(side="left", padx=4)
        self.btn_cut = ttk.Button(btns, text="开始截取", command=lambda: self.run(False))
        self.btn_cut.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.status = ttk.Label(btns, text="就绪", foreground="#666")
        self.status.pack(side="right")

        self.log = scrolledtext.ScrolledText(t, wrap="none", state="disabled",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ================= 校准页 =================
    def _build_cal_tab(self):
        t = self.tab_cal
        top = ttk.Frame(t, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="说明:双击一行可修改该文件的 OSD 起点(校准失败或误读时用)。"
                            "修改后点“保存修改”,截取时自动生效。").pack(side="left")

        btns = ttk.Frame(t, padding=(8, 0, 8, 4))
        btns.pack(fill="x")
        self.btn_cal_refresh = ttk.Button(btns, text="重新校准", command=self.cal_run)
        self.btn_cal_refresh.pack(side="left", padx=4)
        self.btn_cal_edit = ttk.Button(btns, text="编辑选中行", command=self.cal_edit)
        self.btn_cal_edit.pack(side="left", padx=4)
        self.btn_cal_save = ttk.Button(btns, text="保存修改", command=self.cal_save)
        self.btn_cal_save.pack(side="left", padx=4)
        self.btn_cal_clear = ttk.Button(btns, text="清除全部修改",
                                        command=self.cal_clear).pack(side="left", padx=4)
        self.cal_status = ttk.Label(btns, text="", foreground="#666")
        self.cal_status.pack(side="right")

        # ---- 校准档案区:命名保存 / 加载复用 / 删除 ----
        arc = ttk.Frame(t, padding=(8, 0, 8, 4))
        arc.pack(fill="x")
        ttk.Label(arc, text="校准档案:").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(arc, textvariable=self.profile_var,
                                       width=26, state="readonly")
        self.profile_cb.pack(side="left", padx=4)
        ttk.Button(arc, text="保存为档案", command=self.profile_save).pack(side="left", padx=2)
        ttk.Button(arc, text="加载档案", command=self.profile_load).pack(side="left", padx=2)
        ttk.Button(arc, text="删除档案", command=self.profile_delete).pack(side="left", padx=2)
        ttk.Button(arc, text="刷新列表", command=self._refresh_profiles).pack(side="left", padx=2)
        self._refresh_profiles()

        cols = ("file", "dur", "osd0", "anchors", "state")
        self.tree = ttk.Treeview(t, columns=cols, show="headings",
                                 selectmode="browse")
        heads = {"file": ("文件名", 340), "dur": ("时长(s)", 80),
                 "osd0": ("OSD起点", 90), "anchors": ("秒级锚点数", 90),
                 "state": ("状态", 110)}
        for c, (text, w) in heads.items():
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.bind("<Double-1>", lambda e: self.cal_edit())
        sb = ttk.Scrollbar(t, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=4)

    def _override_path(self):
        d = self.dir_var.get().strip()
        return os.path.join(d, OVERRIDE_NAME) if d else None

    def _load_overrides(self, d=None):
        """把 osd_override.json 中的手动起点并入 cal_rows(首次加载/换目录时)"""
        p = self._override_path()
        if not p or not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                over = json.load(f)
        except Exception:
            return
        for name, v in over.items():
            if name in self.cal_rows and v is not None:
                self.cal_rows[name]["osd0"] = int(v)
                self.cal_rows[name]["manual"] = True

    # ---- 校准:子线程跑 cut_by_osd 校准逻辑 ----
    def cal_run(self):
        d = self.dir_var.get().strip()
        if not d:
            messagebox.showerror("错误", "请先在“截取”页选择视频目录")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("提示", "已有任务在运行")
            return
        if CORE is None:
            messagebox.showerror("错误", "无法导入 cut_by_osd 模块")
            return
        self.btn_cal_refresh.configure(state="disabled")
        self.cal_status.configure(text="校准中…(OCR 较慢,请等待)", foreground="#c33")
        threading.Thread(target=self._cal_worker, args=(d,), daemon=True).start()

    def _cal_worker(self, d):
        try:
            ffmpeg = CORE.find_ffmpeg(self.ff_var.get().strip() or None)
            if not ffmpeg:
                raise RuntimeError("未找到 ffmpeg")
            ffprobe = CORE.find_ffprobe(ffmpeg)
            tmpdir = tempfile.mkdtemp(prefix="osd_cal_")
            cache_path = os.path.join(d, "cache.json")
            reader = CORE.OSDReader(ffmpeg, tmpdir, cache_path)
            rows = {}
            for f in sorted(os.listdir(d)):
                if not f.lower().endswith(VIDEO_EXTS):
                    continue
                src = CORE.prepare_source(ffmpeg, os.path.join(d, f))
                dur = CORE.probe_duration(ffprobe, src)
                if dur is None or dur <= 0:
                    rows[f] = {"dur": None, "osd0": None, "anchors": 0,
                               "error": "无法读取时长"}
                    continue
                cal = CORE.calibrate(reader, src, dur)
                if cal is None:
                    rows[f] = {"dur": dur, "osd0": None, "anchors": 0,
                               "error": "无法识别 OSD"}
                else:
                    rows[f] = {"dur": dur, "osd0": cal[0], "anchors": 0,
                               "error": None}
            reader.save_cache()
        except Exception as e:
            self.root.after(0, lambda: self._cal_done(None, str(e)))
            return
        self.root.after(0, lambda: self._cal_done(d, None, rows))

    def _cal_done(self, d, error, rows=None):
        self.btn_cal_refresh.configure(state="normal")
        if error:
            self.cal_status.configure(text="校准失败: %s" % error, foreground="#c33")
            messagebox.showerror("校准失败", error)
            return
        if not rows:
            self.cal_status.configure(text="目录中没有视频文件", foreground="#c33")
            return
        for name, r in rows.items():
            r["anchors"] = self._count_sec_anchors(name)
        self.cal_rows = rows
        self._load_overrides(d)
        self._fill_tree()
        self._refresh_profiles()
        self.cal_status.configure(text="校准完成: %d 个文件" % len(rows), foreground="#090")

    def _count_sec_anchors(self, name):
        """从 cache.json 统计该文件的秒级锚点数(裸码流按临时封装路径匹配)"""
        d = self.dir_var.get().strip()
        cache_path = os.path.join(d, "cache.json")
        names = [name]
        if CORE:
            src = os.path.join(d, name)
            if CORE.is_raw_stream(src) and os.path.isfile(src):
                try:
                    names.append(os.path.basename(CORE.remux_path(src)))
                except OSError:
                    pass
        n = 0
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            for k, v in cache.items():
                p = k.split("@", 1)[0]
                if v and v[1] and any(
                        p.endswith(os.path.sep + nm) for nm in names):
                    n += 1
        except Exception:
            pass
        return n

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        for name, r in sorted(self.cal_rows.items()):
            if r["osd0"] is None:
                st = r.get("error", "失败")
                self.tree.insert("", "end", values=(
                    name, "%.1f" % r["dur"] if r["dur"] else "-",
                    "-", "-", st))
                continue
            st = "已修改" if r.get("manual") else "OK"
            self.tree.insert("", "end", values=(
                name, "%.1f" % r["dur"], sec2hms(r["osd0"]),
                r.get("anchors", 0), st))

    def cal_edit(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一行")
            return
        name = self.tree.item(sel[0], "values")[0]
        r = self.cal_rows.get(name)
        if not r or r["osd0"] is None:
            messagebox.showinfo("提示", "该文件无法识别 OSD,无法编辑(检查 OSD 格式)")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("修改 OSD 起点 — %s" % name[:40])
        dlg.resizable(False, False)
        ttk.Label(dlg, text="文件 OSD 起点时间 (HH:MM:SS):").pack(
            padx=10, pady=(10, 0))
        var = tk.StringVar(value=sec2hms(r["osd0"]))
        ent = ttk.Entry(dlg, textvariable=var, width=14)
        ent.pack(padx=10, pady=6)
        ent.select_range(0, "end")
        ent.focus_set()
        err = ttk.Label(dlg, text="", foreground="#c33")
        err.pack()

        def ok():
            try:
                v = hms2sec(var.get())
            except ValueError as e:
                err.configure(text="无效时间: %s" % e)
                return
            r["osd0"] = v
            r["manual"] = True
            self._fill_tree()
            dlg.destroy()

        bf = ttk.Frame(dlg)
        bf.pack(pady=(0, 10))
        ttk.Button(bf, text="确定", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="left", padx=4)
        dlg.transient(self.root)
        dlg.grab_set()
        ent.bind("<Return>", lambda e: ok())

    def cal_save(self):
        if not self.cal_rows:
            messagebox.showinfo("提示", "没有可保存的数据")
            return
        p = self._override_path()
        if not p:
            messagebox.showerror("错误", "请先选择视频目录")
            return
        over = {}
        for name, r in self.cal_rows.items():
            if r.get("manual") and r["osd0"] is not None:
                over[name] = r["osd0"]
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(over, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("错误", "保存失败: %s" % e)
            return
        n = len(over)
        self.cal_status.configure(
            text="已保存 %d 条手动校准到 %s" % (n, OVERRIDE_NAME), foreground="#090")
        messagebox.showinfo("完成", "已保存 %d 条手动校准到:\n%s" % (n, p))

    def cal_clear(self):
        p = self._override_path()
        if not p:
            return
        try:
            os.remove(p)
        except OSError:
            pass
        for r in self.cal_rows.values():
            r["manual"] = False
        self._fill_tree()
        self.cal_status.configure(text="已清除全部手动校准", foreground="#666")

    # ================= 校准档案管理 =================
    def _refresh_profiles(self):
        names = list_profiles()
        self.profile_cb.configure(values=names)
        if self.profile_var.get() not in names and names:
            self.profile_cb.current(0)

    def _profile_path(self):
        n = self.profile_var.get().strip()
        if not n:
            return None
        if not n.endswith(".json"):
            n += ".json"
        return os.path.join(PROFILES_DIR, n)

    def profile_save(self):
        """把当前校准结果命名保存为档案,供以后复用"""
        if not self.cal_rows:
            messagebox.showinfo("提示", "当前没有校准数据,请先在“校准”页执行“重新校准”")
            return
        if not self.dir_var.get().strip():
            messagebox.showerror("错误", "请先选择视频目录")
            return
        # 输入档案名
        dlg = tk.Toplevel(self.root)
        dlg.title("保存校准档案")
        dlg.resizable(False, False)
        ttk.Label(dlg, text="档案名称:").pack(padx=10, pady=(10, 0))
        default_name = os.path.basename(self.dir_var.get().strip())
        var = tk.StringVar(value=default_name)
        ent = ttk.Entry(dlg, textvariable=var, width=30)
        ent.pack(padx=10, pady=6)
        ent.select_range(0, "end")
        ent.focus_set()
        err = ttk.Label(dlg, text="", foreground="#c33")
        err.pack()

        def ok():
            name = var.get().strip()
            if not name:
                err.configure(text="名称不能为空")
                return
            path = os.path.join(PROFILES_DIR, name if name.endswith(".json") else name + ".json")
            if os.path.isfile(path):
                if not messagebox.askyesno("覆盖?", "档案“%s”已存在,是否覆盖?" % name):
                    return
            os.makedirs(PROFILES_DIR, exist_ok=True)
            entries = {}
            for f, r in self.cal_rows.items():
                entries[f] = {"dur": r.get("dur"), "osd0": r.get("osd0"),
                              "anchors": r.get("anchors", 0),
                              "manual": bool(r.get("manual"))}
            data = {"name": name, "source_dir": self.dir_var.get().strip(),
                    "created": __import__("datetime").datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"),
                    "entries": entries}
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except OSError as e:
                messagebox.showerror("错误", "保存失败: %s" % e)
                return
            self.profile_var.set(name if not name.endswith(".json") else name[:-5])
            self._refresh_profiles()
            self.cal_status.configure(text="档案已保存: %s" % name, foreground="#090")
            dlg.destroy()

        bf = ttk.Frame(dlg)
        bf.pack(pady=(0, 10))
        ttk.Button(bf, text="保存", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="left", padx=4)
        dlg.transient(self.root)
        dlg.grab_set()
        ent.bind("<Return>", lambda e: ok())

    def profile_load(self):
        """加载所选档案:把其中与当前目录匹配文件的校准写入 osd_override.json 并显示"""
        path = self._profile_path()
        if not path or not os.path.isfile(path):
            messagebox.showinfo("提示", "请先在下拉框选择一个档案")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("错误", "档案读取失败: %s" % e)
            return
        entries = data.get("entries", {})
        if not entries:
            messagebox.showinfo("提示", "该档案没有校准数据")
            return
        if not self.cal_rows:
            # 尚未校准:用档案文件列表初始化行(时长从档案取)
            for f, r in entries.items():
                self.cal_rows[f] = {"dur": r.get("dur"), "osd0": r.get("osd0"),
                                    "anchors": r.get("anchors", 0),
                                    "error": None}
        matched = 0
        for f, r in entries.items():
            if f in self.cal_rows and r.get("osd0") is not None:
                self.cal_rows[f]["osd0"] = int(r["osd0"])
                self.cal_rows[f]["manual"] = True
                matched += 1
        self._fill_tree()
        # 直接写入 osd_override.json,截取自动生效
        p = self._override_path()
        over = {}
        for f, r in self.cal_rows.items():
            if r.get("manual") and r["osd0"] is not None:
                over[f] = r["osd0"]
        try:
            if p:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(over, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("错误", "写入 osd_override.json 失败: %s" % e)
            return
        self.cal_status.configure(
            text="已加载档案“%s”,命中 %d 个文件(已写入 osd_override.json)"
                 % (data.get("name", ""), matched), foreground="#090")
        messagebox.showinfo("加载完成",
                            "档案“%s”已加载,命中 %d 个文件。\n"
                            "若当前目录文件与档案来源不一致,请核对 OSD 起点。" % (
                                data.get("name", ""), matched))

    def profile_delete(self):
        path = self._profile_path()
        if not path or not os.path.isfile(path):
            messagebox.showinfo("提示", "请先在下拉框选择要删除的档案")
            return
        if not messagebox.askyesno("确认", "删除档案“%s”?" % os.path.basename(path)[:-5]):
            return
        try:
            os.remove(path)
        except OSError as e:
            messagebox.showerror("错误", "删除失败: %s" % e)
            return
        self._refresh_profiles()
        self.cal_status.configure(text="档案已删除", foreground="#666")

    # ================= 文本识别页(第1步:框选区域+正则) =================
    def _build_match_tab(self):
        t = self.tab_match
        frm = ttk.Frame(t, padding=8)
        frm.pack(fill="x")

        ttk.Label(frm, text="视频文件").grid(row=0, column=0, sticky="e", **{"padx": 8, "pady": 3})
        self.m_file = tk.StringVar()
        self.m_file_cb = ttk.Combobox(frm, textvariable=self.m_file, width=46)
        self.m_file_cb.grid(row=0, column=1, sticky="we", **{"padx": 8, "pady": 3})
        ttk.Button(frm, text="刷新列表", command=self._match_refresh_files).grid(
            row=0, column=2, **{"padx": 8, "pady": 3})

        ttk.Label(frm, text="识别时刻(秒)").grid(row=1, column=0, sticky="e",
                                                 **{"padx": 8, "pady": 3})
        self.m_t = tk.StringVar(value="0")
        ttk.Entry(frm, textvariable=self.m_t, width=8).grid(
            row=1, column=1, sticky="w", **{"padx": 8, "pady": 3})

        ttk.Label(frm, text="预设模板").grid(row=2, column=0, sticky="e",
                                            **{"padx": 8, "pady": 3})
        self.m_preset = tk.StringVar()
        self.m_preset_cb = ttk.Combobox(frm, textvariable=self.m_preset,
                                        values=[p[0] for p in PRESETS],
                                        state="readonly", width=24)
        self.m_preset_cb.grid(row=2, column=1, sticky="w", **{"padx": 8, "pady": 3})
        self.m_preset_cb.bind("<<ComboboxSelected>>", self._on_preset)

        ttk.Label(frm, text="正则表达式").grid(row=3, column=0, sticky="e",
                                             **{"padx": 8, "pady": 3})
        self.m_pat = tk.StringVar(value=r"\d{1,2}:\d{2}:\d{2}")
        ttk.Entry(frm, textvariable=self.m_pat, width=46).grid(
            row=3, column=1, sticky="we", **{"padx": 8, "pady": 3})
        self.m_hint_var = tk.StringVar(value=PRESETS[1][2])
        ttk.Label(frm, textvariable=self.m_hint_var,
                  foreground="#888").grid(row=4, column=1, sticky="w",
                                          **{"padx": 8, "pady": 3})

        btns = ttk.Frame(t, padding=(8, 0, 8, 4))
        btns.pack(fill="x")
        ttk.Button(btns, text="框选识别区域", command=self._match_region).pack(side="left", padx=4)
        ttk.Button(btns, text="开始识别", command=self.match_run).pack(side="left", padx=4)
        self.match_status = ttk.Label(btns, text="", foreground="#666")
        self.match_status.pack(side="right")

        self.match_log = scrolledtext.ScrolledText(t, wrap="none", state="disabled",
                                                   font=("Consolas", 9))
        self.match_log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._match_refresh_files()

    def _on_preset(self, _e=None):
        name = self.m_preset.get()
        for pname, pat, example in PRESETS:
            if pname == name:
                self.m_pat.set(pat)
                self.m_hint_var.set(example)
                return

    def _match_refresh_files(self):
        d = self.dir_var.get().strip()
        if not d or not os.path.isdir(d):
            return
        mp4s = sorted(f for f in os.listdir(d) if f.lower().endswith(VIDEO_EXTS))
        self.m_file_cb.configure(values=mp4s)
        if not self.m_file.get() and mp4s:
            self.m_file_cb.current(0)

    def _match_region(self):
        self.region_set()  # 复用截取页的框选(保存到 osd_region.json)

    def match_run(self):
        d = self.dir_var.get().strip()
        f = self.m_file.get().strip()
        if not d or not f:
            messagebox.showerror("错误", "请先选择视频目录和视频文件")
            return
        try:
            tsec = float(self.m_t.get().strip() or 0)
        except ValueError:
            messagebox.showerror("错误", "识别时刻必须是数字")
            return
        pat = self.m_pat.get().strip()
        if not pat:
            messagebox.showerror("错误", "请输入正则表达式")
            return
        src = os.path.join(d, f)
        args = [pick_python(), os.path.join(os.path.dirname(SCRIPT), "matcher.py"),
                "--file", src, "--t", str(tsec), "--pattern", pat,
                "--frames", "3"]
        rp = region_path()
        if os.path.isfile(rp):
            args += ["--region", rp]
        self.match_log.configure(state="normal")
        self.match_log.delete("1.0", "end")
        self.match_log.insert("end", "$ " + " ".join(args) + "\n\n")
        self.match_log.configure(state="disabled")
        self.match_status.configure(text="识别中…", foreground="#c33")
        self.root.update_idletasks()

        def _work():
            try:
                r = subprocess.run(args, capture_output=True,
                                   timeout=300,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
                stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
                out = stdout + ("\n[stderr] " + stderr if r.returncode != 0 else "")
                self.root.after(0, lambda: self._match_done(out, r.returncode))
            except Exception as e:
                self.root.after(0, lambda: self._match_done(str(e), 1))

        threading.Thread(target=_work, daemon=True).start()

    def _match_done(self, out, rc):
        self.match_log.configure(state="normal")
        self.match_log.insert("end", out)
        self.match_log.configure(state="disabled")
        self.match_status.configure(
            text="完成" if rc == 0 else "出错(退出码 %d)" % rc,
            foreground="#090" if rc == 0 else "#c33")

    # ---------- 框选 OSD 区域 ----------
    def region_set(self):
        d = self.dir_var.get().strip()
        if not d:
            messagebox.showerror("错误", "请先选择视频目录")
            return
        if Image is None:
            messagebox.showerror("错误", "缺少 pillow,请先: pip install pillow")
            return
        mp4s = [f for f in os.listdir(d) if f.lower().endswith(VIDEO_EXTS)]
        if not mp4s:
            messagebox.showerror("错误", "目录中没有视频文件(MP4/H264/H265)")
            return
        ffmpeg = CORE.find_ffmpeg(self.ff_var.get().strip() or None) if CORE else None
        if not ffmpeg:
            messagebox.showerror("错误", "未找到 ffmpeg")
            return
        src = CORE.prepare_source(ffmpeg, os.path.join(d, mp4s[0]))
        frame = os.path.join(tempfile.gettempdir(), "osd_grab.png")
        r = subprocess.run([ffmpeg, "-y", "-v", "error"] + CORE.seek_args(src, 5) +
                           ["-frames:v", "1", frame],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(frame):
            messagebox.showerror("错误", "抓拍失败:\n%s" % r.stderr[-300:])
            return
        RegionDialog(self.root, frame, d)

    # ---------- 自动补全 txt 默认名 ----------
    def auto_txt(self, d):
        cand = os.path.join(d, DEFAULT_TXT)
        if os.path.isfile(cand):
            self.txt_var.set(cand)

    # ---------- 日志 ----------
    def log_write(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", "%d.0" % (lines - MAX_LOG_LINES))
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 子进程 ----------
    def build_args(self, preview):
        d = self.dir_var.get().strip()
        if not d:
            raise ValueError("请选择视频目录")
        args = [pick_python(), SCRIPT, "--dir", d]
        if self.txt_var.get().strip():
            args += ["--txt", self.txt_var.get().strip()]
        if self.out_var.get().strip():
            args += ["--out", self.out_var.get().strip()]
        if self.ch_var.get().strip():
            args += ["--ch", self.ch_var.get().strip()]
        if self.ff_var.get().strip():
            args += ["--ffmpeg", self.ff_var.get().strip()]
        p = self._override_path()
        if p and os.path.isfile(p):
            args += ["--override", p]
        rp = region_path()
        if os.path.isfile(rp):
            args += ["--region", rp]
        if preview:
            args.append("--check")
        if self.ck_cache.get():
            args.append("--cache")
        if self.ck_verb.get():
            args.append("--verbose")
        return args

    def run(self, preview):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("提示", "已有任务在运行")
            return
        try:
            args = self.build_args(preview)
        except ValueError as e:
            messagebox.showerror("错误", str(e))
            return
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            messagebox.showerror("错误", "无法启动脚本: %s" % e)
            return
        self.btn_check.configure(state="disabled")
        self.btn_cut.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text="运行中…", foreground="#c33")
        self.log_write("$ " + " ".join(args) + "\n\n")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        p = self.proc
        for raw in p.stdout:
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                line = str(raw)
            self.q.put(line)
        p.wait()
        self.q.put(None)
        self.q.put(("exit", p.returncode))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log_write("\n[已请求停止]\n")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    break
                if isinstance(item, tuple) and item[0] == "exit":
                    rc = item[1]
                    self.btn_check.configure(state="normal")
                    self.btn_cut.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    if rc == 0:
                        self.status.configure(text="完成", foreground="#090")
                        self.log_write("\n=== 完成(退出码 0)===\n")
                    else:
                        self.status.configure(text="已停止/出错(退出码 %d)" % rc,
                                              foreground="#c33")
                        self.log_write("\n=== 已停止/出错(退出码 %d)===\n" % rc)
                    self.proc = None
                else:
                    self.log_write(item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def main():
    if not ensure_runtime_python():
        return
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
