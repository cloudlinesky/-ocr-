#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcher.py — 任意文本模板识别(第 1 步:框选区域 + 正则)

在用户框选的画面区域上,对指定时刻(或前后若干帧)做 OCR,
再用用户提供的正则/关键词匹配,输出所有命中文本及其出现帧。

与 cut_by_osd.py 的关系
-----------------------
- 复用 OSDReader(抽帧 / 区域裁剪 / OCR / cache.json)
- --region 参数格式与 cut_by_osd.py 一致(JSON:{目录: {x,y,w,h}} 比例)
- 本机路径 D:\\脚本库\\osd_region.json 可由 GUI“框选OSD区域”生成

用法
----
# 单帧识别
python matcher.py --file D:\\视频\\a.mp4 --t 5 --pattern "\\d{2}:\\d{2}"
# 前后 ±2 秒共 5 帧识别(抗单帧 OCR 噪声)
python matcher.py --file D:\\视频\\a.mp4 --t 5 --pattern "车牌\\w+" --frames 5
# 用 GUI 框选保存的区域
python matcher.py --file D:\\视频\\a.mp4 --region D:\\脚本库\\osd_region.json \\
    --pattern "30\\s*km/h"

输出
----
每帧一行: t=xx.xs  <识别文本>  |> 命中: <匹配结果>
末尾汇总去重后的全部命中。

下一步扩展(见“扩展文本图像识别技术路径.md”)
------------------------------------------
- 第 2 步:图案模板匹配(OpenCV 多尺度模板匹配 / SIFT 特征)
- 第 3 步:VLM 自然语言定位(本地部署 Qwen2.5-VL 等)
"""

import argparse
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cut_by_osd as CORE


def build_reader(ffmpeg, region_path=None, src_dir=None, cache=None):
    region = CORE.load_region(region_path, src_dir) if region_path else None
    return CORE.OSDReader(ffmpeg, tempfile.mkdtemp(prefix="matcher_"),
                          cache, region), region


def match_frames(reader, src, tsec, pattern, frames=1):
    """对 tsec 前后共 frames 帧(默认 1 帧)OCR,返回 [(t, text, hits)]"""
    results = []
    rx = re.compile(pattern)
    half = frames // 2
    for i in range(-half, half + 1):
        t = round(max(0.0, tsec + i), 2)
        text = reader.read_text(src, t)
        if not text:
            results.append((t, None, []))
            continue
        hits = list(dict.fromkeys(m.group(0) for m in rx.finditer(text)))
        results.append((t, text, hits))
    return results


def main():
    ap = argparse.ArgumentParser(description="框选区域 + 正则的文本模板识别")
    ap.add_argument("--file", required=True, help="视频文件")
    ap.add_argument("--t", type=float, default=0.0, help="识别时刻(秒)")
    ap.add_argument("--pattern", required=True, help="正则表达式")
    ap.add_argument("--frames", type=int, default=1,
                    help="前后共识别多少帧(默认1,建议3~5 抗单帧噪声)")
    ap.add_argument("--region", default=None,
                    help="区域 JSON(与 cut_by_osd.py 相同);缺省用默认左上角裁剪")
    ap.add_argument("--cache", default=None, help="cache.json 路径(可选)")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg.exe 路径")
    ap.add_argument("--raw", action="store_true", help="同时打印未裁剪 OCR 全文")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print("错误:文件不存在:", args.file)
        sys.exit(1)
    ffmpeg = CORE.find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        print("错误:未找到 ffmpeg")
        sys.exit(1)
    src = CORE.prepare_source(ffmpeg, args.file)
    reader, region = build_reader(ffmpeg, args.region, os.path.dirname(args.file),
                                  args.cache)
    if region:
        print("区域(手动框选): x=%.3f y=%.3f w=%.3f h=%.3f" % region)

    results = match_frames(reader, src, args.t, args.pattern, args.frames)
    all_hits = []
    for t, text, hits in results:
        if text is None:
            print("  t=%5.2fs  无 OCR 结果" % t)
            continue
        if args.raw:
            print("  t=%5.2fs  [OCR] %s" % (t, text[:160]))
        if hits:
            print("  t=%5.2fs  命中: %s" % (t, " | ".join(hits)))
        all_hits += hits
    all_hits = list(dict.fromkeys(all_hits))
    print("\n=== 汇总 ===")
    if all_hits:
        print("命中 %d 种文本:" % len(all_hits))
        for h in all_hits:
            print("  - %s" % h)
    else:
        print("未命中(可尝试 --frames 3~5、--raw 查看 OCR 原文)")


if __name__ == "__main__":
    main()
