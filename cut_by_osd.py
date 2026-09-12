#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cut_by_osd.py — 按画面 OSD 叠加时间截取 DVR 导出的视频(MP4 / H264 / H265)

背景
----
DVR/NVR 导出的文件名中的时间是"导出时间",与画面内容无关。
画面左上角叠加的 OSD 时间(YYYY-MM-DD HH:MM:SS)才是真实录制时间。
本脚本用 OCR 读取各文件 OSD 时间校准时间轴,再把时间段文件中的
每个时间段映射到对应文件的相对偏移,最后调用 ffmpeg 精确截取。
裸码流(.h264/.264/.h265/.265)会自动复制封装为临时 MP4 后再处理。

依赖
----
- ffmpeg (PATH 中可用,或用 --ffmpeg 指定)
- Python 3.9+ ; pip install rapidocr-onnxruntime pillow numpy

时间段文件格式(每行一条)
----
YYYY-HHMMSS-HHMMSS
例:2026-144516-144615  -> 2026年 14:45:16 ~ 14:46:15

用法
----
python cut_by_osd.py --dir <视频目录> --txt <时间段文件> [--out <输出目录>]
                     [--ch CH5] [--ffmpeg <路径>] [--check] [--cache]

--dir    存放视频文件的目录(MP4 / H264 / H265 裸码流)
--txt    时间段文件(默认:目录下"新建 文本文档.txt")
--out    输出目录(默认:目录下"截取")
--ch     只使用文件名包含该通道(如 CH5)的文件;默认全部
--ffmpeg 手动指定 ffmpeg.exe 路径
--check  只校准时间轴并打印映射,不执行截取
--cache  保存/复用 OSD 校准结果(cache.json),加速重复运行
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

VIDEO_EXTS = (".mp4", ".h264", ".264", ".h265", ".265")
RAW_EXTS = (".h264", ".264", ".h265", ".265")


def is_raw_stream(path):
    """裸码流(h264/h265,无容器):无时长元数据且输入定位会失败,需特殊处理"""
    return path.lower().endswith(RAW_EXTS)


def _raw_format(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".h265", ".265"):
        return "hevc"
    if ext in (".h264", ".264"):
        return "h264"
    return None

DEFAULT_FFMPEG_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe"),
    "ffmpeg",
    r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe",
]

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None

# 解析 OSD 文本中的录制时间
# DVR 画面格式:"2026-07-31 14:45:16" 但 OCR 可能粘连/残缺,如
# "2026-07-31714:53"(日期时间粘连、秒缺失)、"026-07-31:14:57:30"(年份缺位)
# 返回 (hh, mm, ss) 或 None;ss=0 表示只识别到分钟级
def _split_hms(text):
    """从文本中解析时间 "HH:MM[:SS]",允许小时前 1~2 位杂散数字(如 "714:53")
    返回 (hh, mm, ss):ss=None 表示秒缺失(只读到分钟级)"""
    m = re.search(r"(\d{1,3}):(\d{2})(?::(\d{2}))?", text)
    if not m:
        return None
    hh_s = m.group(1)
    hh = int(hh_s) if len(hh_s) <= 2 else int(hh_s[-2:])
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else None
    if 0 <= hh <= 23 and 0 <= mm <= 59 and (ss is None or 0 <= ss <= 59):
        return hh, mm, ss
    return None


def parse_osd_time(text):
    m = re.search(r"(\d{3,4})[^\d]{0,3}(\d{2})[^\d]{0,3}(\d{2})", text)
    if not m:
        return None
    tail = text[m.end():]
    r = _split_hms(tail)
    if r:
        return r
    # 无冒号粘连串(如 "7145300" = 7杂散+14:53:00):滑动窗口找有效 HH:MM:SS
    m2 = re.match(r"[^\d]{0,6}(\d{1,9})", tail)
    if m2:
        ds = m2.group(1)
        for i in range(0, len(ds) - 5):
            hh, mm, ss = int(ds[i:i + 2]), int(ds[i + 2:i + 4]), int(ds[i + 4:i + 6])
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return hh, mm, ss
        for i in range(0, len(ds) - 3):
            hh, mm = int(ds[i:i + 2]), int(ds[i + 2:i + 4])
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm, None
    return None


def find_ffmpeg(explicit=None):
    if explicit and os.path.isfile(explicit):
        return explicit
    for c in DEFAULT_FFMPEG_CANDIDATES:
        if os.path.sep in c and os.path.isfile(c):
            return c
        if not os.path.sep in c:
            try:
                r = subprocess.run([c, "-version"], capture_output=True)
                if r.returncode == 0:
                    return c
            except OSError:
                pass
    return None


class OSDReader:
    def __init__(self, ffmpeg, tmpdir, cache_path=None, region=None):
        self.ffmpeg = ffmpeg
        self.tmpdir = tmpdir
        self.cache_path = cache_path
        self.region = region  # (x, y, w, h) 归一化比例;None=自动左上角
        self.cache = {}
        if cache_path and os.path.isfile(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
        if RapidOCR is None:
            raise RuntimeError(
                "缺少 rapidocr-onnxruntime,请先执行: pip install rapidocr-onnxruntime pillow numpy"
            )
        self.engine = RapidOCR()

    def _extract(self, src, tsec):
        base = os.path.join(self.tmpdir, "osd_frame.png")
        if self.region:
            # 手动框选区域:只裁该区域(更快更准)
            x, y, w, h = self.region
            crop = ("crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,"
                    "scale=iw*4:ih*4") % (w, h, x, y)
            r = subprocess.run(
                [self.ffmpeg, "-y", "-v", "error"] + seek_args(src, tsec) +
                ["-vf", crop, "-frames:v", "1", base],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not os.path.isfile(base):
                return None
            return self._ocr_file(base)
        r = subprocess.run(
            [self.ffmpeg, "-y", "-v", "error"] + seek_args(src, tsec) +
            ["-frames:v", "1", base],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not os.path.isfile(base):
            return None
        text = self._ocr_file(base)
        if text:
            return text
        crop = os.path.join(self.tmpdir, "osd_crop.png")
        subprocess.run(
            [
                self.ffmpeg, "-y", "-v", "error", "-i", base,
                "-vf", "crop=iw*0.45:ih*0.14:0:0,scale=iw*4:ih*4",
                "-frames:v", "1", crop,
            ],
            capture_output=True, text=True,
        )
        if os.path.isfile(crop):
            return self._ocr_file(crop)
        return None

    def _ocr_file(self, path):
        try:
            res, _ = self.engine(path)
        except Exception:
            return None
        if not res:
            return None
        return " ".join(item[1] for item in res)

    def read_osd(self, src, tsec, force=False):
        """读取某时刻帧的 OSD 秒数(距当日 0 点的秒),无法识别返回 None
        返回 (val, precise):precise=True 表示读到完整 HH:MM:SS"""
        key = "%s@%.0f" % (src, tsec)
        if not force and key in self.cache:
            return tuple(self.cache[key])
        text = self._extract(src, tsec)
        val, precise = None, False
        if text:
            hms = parse_osd_time(text)
            if hms:
                hh, mm, ss = hms
                val = hh * 3600 + mm * 60 + (ss if ss is not None else 0)
                precise = ss is not None
        self.cache[key] = [val, precise]
        return val, precise

    def read_text(self, src, tsec, force=False):
        """读取某时刻帧的完整 OCR 文本(供文本模板识别用),失败返回 None"""
        key = "%s@%.0f:text" % (src, tsec)
        if not force and key in self.cache:
            return self.cache[key]
        text = self._extract(src, tsec)
        self.cache[key] = text
        return text

    def save_cache(self):
        if self.cache_path:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)


def probe_raw_duration(ffprobe, src):
    """裸码流无时长元数据:解码统计帧数,再按帧率估算时长"""
    if not is_raw_stream(src):
        return None
    args = [ffprobe, "-v", "error", "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,avg_frame_rate",
            "-of", "default=noprint_wrappers=1"]
    fmt = _raw_format(src)
    for extra in ([], ["-f", fmt] if fmt else []):
        r = subprocess.run(args + extra + [src], capture_output=True, text=True)
        frames = fps = None
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            if k == "nb_read_frames":
                try:
                    frames = int(v)
                except ValueError:
                    pass
            elif k == "avg_frame_rate":
                try:
                    num, den = v.split("/")
                    fps = float(num) / float(den) if float(den) else 0.0
                except (ValueError, ZeroDivisionError):
                    pass
        if frames and fps:
            return frames / fps
    return None


def probe_duration(ffprobe, src):
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True,
    )
    try:
        d = float(r.stdout.strip())
        if d > 0:
            return d
    except Exception:
        pass
    return probe_raw_duration(ffprobe, src)


def remux_path(src):
    """裸码流的临时封装 MP4 路径(按绝对路径+大小+修改时间取哈希,跨运行稳定以复用缓存)"""
    st = os.stat(src)
    key = "%s|%d|%d" % (os.path.abspath(src), st.st_size, int(st.st_mtime))
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "cutbyosd_remux", h + ".mp4")


def ensure_remux(ffmpeg, src):
    """把裸码流复制封装为 MP4(不重编码),以便快速定位;成功返回新路径,失败返回 None"""
    dst = remux_path(src)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError:
        return None
    tmp = dst + ".tmp.mp4"
    fmt = _raw_format(src)
    for extra in ([], ["-f", fmt] if fmt else []):
        r = subprocess.run([ffmpeg, "-y", "-v", "error"] + extra +
                           ["-i", src, "-c", "copy", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            try:
                os.replace(tmp, dst)
            except OSError:
                return None
            return dst
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


def prepare_source(ffmpeg, src):
    """裸码流先复制封装为 MP4(临时缓存)以支持快速定位;MP4 或封装失败时返回原路径"""
    if not is_raw_stream(src):
        return src
    remuxed = ensure_remux(ffmpeg, src)
    return remuxed if remuxed else src


def seek_args(src, tsec):
    """裸码流无时间戳,输入定位(-ss 在 -i 前)会失败,改用输出定位"""
    if is_raw_stream(src):
        return ["-i", src, "-ss", str(tsec)]
    return ["-ss", str(tsec), "-i", src]


def extract_frame(ffmpeg, src, tsec, out_path):
    """抽取指定时刻一帧到 out_path(兼容裸码流);成功返回 True"""
    r = subprocess.run([ffmpeg, "-y", "-v", "error"] + seek_args(src, tsec) +
                       ["-frames:v", "1", out_path],
                       capture_output=True, text=True)
    return r.returncode == 0 and os.path.isfile(out_path)


def find_ffprobe(ffmpeg):
    if ffmpeg.endswith("ffmpeg.exe"):
        return ffmpeg.replace("ffmpeg.exe", "ffprobe.exe")
    return "ffprobe"


def load_region(region_path, src_dir):
    """读取手动框选的 OSD 区域配置(JSON:{目录: {x,y,w,h}} 比例 0~1)
    优先精确匹配目录,否则回退到第一个条目;无配置返回 None"""
    if not region_path or not os.path.isfile(region_path):
        return None
    try:
        with open(region_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    d = os.path.normcase(os.path.abspath(src_dir))
    if d in data:
        r = data[d]
    elif data:
        r = next(iter(data.values()))
    else:
        return None
    try:
        x, y, w, h = float(r["x"]), float(r["y"]), float(r["w"]), float(r["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        return None
    return x, y, w, h


def hms(h, m, s):
    return h * 3600 + m * 60 + s


def parse_clips(txt_path):
    clips = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d{4})-(\d{6})-(\d{6})$", line)
            if not m:
                continue
            s, e = m.group(2), m.group(3)
            s0 = hms(int(s[0:2]), int(s[2:4]), int(s[4:6]))
            e0 = hms(int(e[0:2]), int(e[2:4]), int(e[4:6]))
            clips.append((line, s0, e0))
    return clips


def calibrate(reader, src, duration, verbose=False):
    """校准:返回 (osd_start, slope, precise_ok) 或 None
    osd_start:文件 t=0 时刻的 OSD 秒(众数外推,逐秒录制 slope≈1)
    precise_ok:是否有足够秒级锚点"""
    offsets = [0, 1, 2, 5, 10, 20, 40, 60, 100, 200, 400, 600, 800, 1000]
    for tail in (50, 20, 5):
        if duration - tail > 0 and duration - tail not in offsets:
            offsets.append(duration - tail)
    offsets = sorted(o for o in offsets if 0 <= o < duration)
    pts = []
    for off in offsets:
        v, precise = reader.read_osd(src, off)
        if v is None:
            continue
        pts.append((off, v, precise))
    if not pts:
        return None
    sec_pts = [p for p in pts if p[2]]
    # 每个秒级锚点给出候选起点 osd_start = v - t(斜率 1.0)
    candidates = sorted((v - t) % 86400 for t, v, _ in sec_pts)
    if len(candidates) >= 2:
        # 众数聚类:60s 桶,取最大桶平均
        best = []
        cur = [candidates[0]]
        for c in candidates[1:]:
            if c - cur[0] < 60:
                cur.append(c)
            else:
                if len(cur) > len(best):
                    best = cur
                cur = [c]
        if len(cur) > len(best):
            best = cur
        osd_start = int(round(sum(best) / len(best)))
    else:
        # 无秒级锚点:取第一个分钟级锚点
        t0, v0, _ = pts[0]
        osd_start = int(round((v0 - t0) % 86400))
    if verbose:
        osd0i = osd_start
        print("  calibrate %s: osd@0s=%02d:%02d:%02d slope=1.0 pts=%s" % (
            os.path.basename(src), osd0i // 3600, osd0i % 3600 // 60, osd0i % 60,
            [(p[0], p[1], p[2]) for p in pts]))
    return osd_start, 1.0, len(sec_pts) >= 2


def main():
    ap = argparse.ArgumentParser(description="按画面 OSD 时间截取 DVR 导出 MP4")
    ap.add_argument("--dir", required=True, help="视频所在目录")
    ap.add_argument("--txt", default=None, help="时间段文件(默认:目录/新建 文本文档.txt)")
    ap.add_argument("--out", default=None, help="输出目录(默认:目录/截取)")
    ap.add_argument("--ch", default=None, help="仅使用该通道文件,如 CH5")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg.exe 路径")
    ap.add_argument("--check", action="store_true", help="只校准打印,不截取")
    ap.add_argument("--cache", action="store_true", help="使用/保存校准缓存 cache.json")
    ap.add_argument("--override", default=None,
                    help="手动校准 JSON 文件:{文件名: OSD起点秒数},覆盖自动校准")
    ap.add_argument("--region", default=None,
                    help="手动框选的 OSD 区域 JSON:{目录: {x,y,w,h}},比例 0~1")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    folder = args.dir
    if not os.path.isdir(folder):
        print("错误:目录不存在:", folder)
        sys.exit(1)
    txt_path = args.txt or os.path.join(folder, "新建 文本文档.txt")
    if not os.path.isfile(txt_path):
        print("错误:时间段文件不存在:", txt_path)
        sys.exit(1)
    outdir = args.out or os.path.join(folder, "截取")
    if not args.check:
        os.makedirs(outdir, exist_ok=True)

    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        print("错误:未找到 ffmpeg,请用 --ffmpeg 指定路径")
        sys.exit(1)
    ffprobe = find_ffprobe(ffmpeg)
    print("ffmpeg:", ffmpeg)

    tmpdir = tempfile.mkdtemp(prefix="osd_cut_")
    cache_path = os.path.join(folder, "cache.json") if args.cache else None
    region = load_region(args.region, folder)
    if region:
        print("OSD 区域(手动框选): x=%.3f y=%.3f w=%.3f h=%.3f" % region)
    try:
        reader = OSDReader(ffmpeg, tmpdir, cache_path, region)
    except RuntimeError as e:
        print("错误:", e)
        sys.exit(1)

    clips = parse_clips(txt_path)
    if not clips:
        print("错误:时间段文件无有效条目:", txt_path)
        sys.exit(1)
    print("时间段条目:", len(clips))

    mp4s = [f for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXTS)]
    if not mp4s:
        print("错误:目录中无 MP4")
        sys.exit(1)
    if args.ch:
        mp4s = [f for f in mp4s if args.ch in f]
        if not mp4s:
            print("错误:无匹配通道 %s 的文件" % args.ch)
            sys.exit(1)

    # ---- 校准:每个文件 OSD 起点 ----
    print("== 校准时间轴 ==")
    timeline = []
    for f in sorted(mp4s):
        src = os.path.join(folder, f)
        use_src = prepare_source(ffmpeg, src)
        if use_src != src and args.verbose:
            print("  裸码流已封装:", f)
        dur = probe_duration(ffprobe, use_src)
        if dur is None or dur <= 0:
            print("  跳过(无法读取时长):", f)
            continue
        cal = calibrate(reader, use_src, dur, args.verbose)
        if cal is None:
            print("  跳过(无法识别 OSD):", f)
            continue
        osd0, slope, precise_ok = cal
        timeline.append({"file": f, "src": use_src, "dur": dur,
                         "osd0": osd0, "slope": slope, "precise_ok": precise_ok})
    if args.cache:
        reader.save_cache()

    if not timeline:
        print("错误:所有文件均无法校准 OSD")
        sys.exit(1)

    # ---- 手动校准覆盖 ----
    if args.override and os.path.isfile(args.override):
        try:
            with open(args.override, "r", encoding="utf-8") as f:
                over = json.load(f)
        except Exception as e:
            print("警告:无法读取 --override 文件(%s): %s" % (args.override, e))
            over = {}
        for seg in timeline:
            if seg["file"] in over and over[seg["file"]] is not None:
                seg["osd0"] = int(over[seg["file"]])
                print("  手动校准 %s: osd@0s=%02d:%02d:%02d" % (
                    seg["file"], seg["osd0"] // 3600,
                    seg["osd0"] % 3600 // 60, seg["osd0"] % 60))

    timeline.sort(key=lambda x: x["osd0"])
    merged = []
    for seg in timeline:
        if merged and seg["osd0"] < merged[-1]["osd0"] + merged[-1]["dur"] - 5:
            print("  忽略重叠文件:", seg["file"])
            continue
        merged.append(seg)
    timeline = merged
    print("有效时间轴段数:", len(timeline))
    for seg in timeline:
        e = seg["osd0"] + seg["dur"]
        print("  %s | %02d:%02d:%02d -> %02d:%02d:%02d (%.1fs)" % (
            seg["file"][-40:],
            seg["osd0"] // 3600, seg["osd0"] % 3600 // 60, seg["osd0"] % 60,
            e // 3600, e % 3600 // 60, e % 60, seg["dur"]))

    # ---- 映射并截取 ----
    print("== 截取 ==")
    failed = []
    for name, s0, e0 in clips:
        seg = None
        for s in timeline:
            if s["osd0"] <= s0 < s["osd0"] + s["dur"]:
                seg = s
                break
        if seg is None:
            print("  !! %s: 起点 %02d:%02d:%02d 不在任何时间段内" % (
                name, s0 // 3600, s0 % 3600 // 60, s0 % 60))
            failed.append(name)
            continue
        # 线性映射:rel = (osd_target - osd0) / slope
        rel_start = (s0 - seg["osd0"]) / seg["slope"]
        rel_end = (e0 - seg["osd0"]) / seg["slope"]
        if seg["precise_ok"] and seg["slope"] != 1.0:
            rel_start = min(max(0.0, rel_start), seg["dur"])
            rel_end = min(max(0.0, rel_end), seg["dur"])
        duration = rel_end - rel_start
        if duration <= 0.3:
            print("  !! %s: 时长过短 (%.2fs)" % (name, duration))
            failed.append(name)
            continue
        outfile = os.path.join(outdir, name + ".mp4")
        print("  %s | %s | %06.2fs -> %06.2fs (%.1fs)" % (
            name, seg["file"][-30:], rel_start, rel_end, duration))
        if args.check:
            continue
        cmd = ([ffmpeg, "-y", "-v", "error"] + seek_args(seg["src"], "%.3f" % rel_start) +
               ["-t", "%.3f" % duration,
                "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-an", "-avoid_negative_ts", "make_zero", outfile])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("     !! ffmpeg 失败: %s" % r.stderr[-300:])
            failed.append(name)
        else:
            print("     OK")

    print("\n=== 完成 ===")
    if failed:
        print("失败条目:", failed)
    else:
        print("全部成功")


if __name__ == "__main__":
    main()
