#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发票合并小助手 (Invoice Merge Assistant)

把多张发票 PDF 与图片（手机拍的照片、扫描件）合并成一个 PDF，并自动处理横向 /
纵向页面的方向与尺寸问题。图片一律按「其他材料」对待：同样参与分类排序、拼版、
转扫描件，一张图片算一页。

两种用法
--------
图形界面：  python invoice_merge.py
命令行：    python invoice_merge.py --folder ./发票 -o 合并结果.pdf
报销场景：  python invoice_merge.py --folder ./发票 --size a4-rotate --sort pinyin

核心保证
--------
1. 内部按"保真"方式逐页复制（PyMuPDF insert_pdf），不改动原始内容，
   不重新压缩图像、不丢失文字层，因此不存在旋转或裁切导致的失真。
2. 尺寸策略只有两种：默认「统一 A4 纵向（横向页旋转填满）」，或「保持原始尺寸」
   逐页沿用原方向原大小。
3. 若页面被 CropBox 裁掉了内容，自动把裁剪框扩回内容范围（不超出 MediaBox）。
4. 若内容在页面里是横躺的，按需旋转页面扶正。
5. 缩放一律用 contain 方式居中放置，绝不拉伸变形、绝不裁切。
6. "统一 A4 纵向（横向页旋转填满）"模式把横向材料整体转 90° 后填满纸面，
   与人工粘贴报销凭证的习惯一致；纵向材料不受影响。
7. 默认在写盘前把整份输出转成扫描件（每页一张 300dpi 整页图片，无文字层），
   等同打印再扫回去；要保留矢量与文字层用 --no-scan 或取消界面上的勾选。
"""

from __future__ import annotations

import argparse
import atexit
import json
import locale
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__version__ = "1.2.3"

# --------------------------------------------------------------------------
# 依赖
# --------------------------------------------------------------------------

try:
    import pymupdf as fitz
except ImportError:  # 兼容旧版本的导入名
    try:
        import fitz  # type: ignore
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "缺少依赖 PyMuPDF。请先安装（Windows 的解释器名是 python，其余是 python3）：\n"
            "    python -m pip install pymupdf\n"
        )
        raise SystemExit(2)

# PyMuPDF 2.0 起捆绑了 onnx 布局模型，PyInstaller 不会打包那批资源，
# 打出来的 app 会一启动就 FileNotFoundError。1.x 才是本工具验证过的版本。
_PYMUPDF_MAJOR = 1
try:
    _pu_ver = getattr(fitz, "version", None)
    _pu_major = int(str(_pu_ver[0]).split(".")[0]) if _pu_ver else 0
except Exception:  # pragma: no cover - 版本号读不出来就别拦
    _pu_major = 0
if _pu_major and _pu_major > _PYMUPDF_MAJOR:
    sys.stderr.write(
        "PyMuPDF 版本过新（%s）：本工具按 1.x 验证，2.x 的 onnx 布局模型会让打包版启动失败。\n"
        "请改用 1.x：\n    python -m pip install 'pymupdf<2'\n" % (_pu_ver[0],)
    )
    raise SystemExit(2)

try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy 只是加速项
    _np = None


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

A4_W, A4_H = 595.276, 841.89          # A4 纵向，单位 pt
MM = 72.0 / 25.4                       # 1 毫米对应的 pt

# 可作为报销材料的文件：PDF，以及手机拍照 / 扫描得到的图片。
# 图片在打开时先转成「一页 PDF」，之后拼版、扫描件、打印全走同一条流水线，
# 下游代码不需要区分「这是 PDF 还是图片」。
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
                  ".webp", ".gif", ".heic", ".heif")
SOURCE_SUFFIXES = (".pdf",) + IMAGE_SUFFIXES
# 这些格式 PyMuPDF 解不了（没编进解码器），macOS 上借系统 sips 先转 PNG。
SYSTEM_DECODE_SUFFIXES = (".heic", ".heif", ".webp")

SIZE_KEEP = "keep"                     # 保持每页原始尺寸与方向
SIZE_A4_ROTATE = "a4-rotate"           # 统一 A4 纵向，横向页内容旋转 90° 填满

SIZE_LABELS = {
    SIZE_KEEP: "保持原始尺寸",
    SIZE_A4_ROTATE: "统一 A4 纵向（横向页旋转填满）（推荐）",
}

# 尺寸策略的排列顺序，GUI 与命令行共用
SIZE_ORDER = (SIZE_KEEP, SIZE_A4_ROTATE)

# 横向页在"旋转填满"模式下统一转到的文字角度：内容顶部朝右，与拼版路径
# 的 BAND_ROTATE 摆向一致 —— 整本材料转着看的方向才是统一的。
# 注意这是「文本书写角度」，和 show_pdf_page 的 rotate 参数不是同一套记法，
# 两者数值相同不代表摆向相同，别顺手把它们合并成一个常量。
PORTRAIT_TEXT_ANGLE = 90

SORT_NATURAL = "natural"               # 自然排序：发票2 排在 发票10 前面
SORT_PINYIN = "pinyin"                 # 中文按拼音，拉丁 / 数字开头的排在中文之后
SORT_NAME = "name"                     # 纯文件名字典序
SORT_MTIME = "mtime"                   # 按修改时间

SORT_LABELS = {
    SORT_NATURAL: "自然排序（数字按大小）",
    SORT_PINYIN: "拼音顺序（中文习惯）",
    SORT_NAME: "文件名顺序",
    SORT_MTIME: "修改时间顺序",
}

SORT_ORDER = (SORT_NATURAL, SORT_PINYIN, SORT_NAME, SORT_MTIME)

# 报销材料的业务归类。启用"整理材料顺序"后按 CATEGORY_ORDER 排列：
# 酒店发票在最前（要拼版），差旅费报销单次之，票据粘贴单封面垫底。
CATEGORY_HOTEL = "hotel"            # 酒店发票（酒店结账单归"其他材料"，见 SETTLEMENT_HINTS）
CATEGORY_REIMBURSE = "reimburse"    # 差旅费报销单
CATEGORY_TRAVEL = "travel"          # 火车票、机票、登机凭证、行程单
CATEGORY_OTHER = "other"            # 其余材料
CATEGORY_COVER = "cover"            # 票据粘贴单封面

CATEGORY_ORDER = (CATEGORY_HOTEL, CATEGORY_REIMBURSE, CATEGORY_TRAVEL,
                  CATEGORY_OTHER, CATEGORY_COVER)

CATEGORY_LABELS = {
    CATEGORY_HOTEL: "酒店发票",
    CATEGORY_REIMBURSE: "差旅费报销单",
    CATEGORY_TRAVEL: "火车 / 飞机票据",
    CATEGORY_OTHER: "其他材料",
    CATEGORY_COVER: "票据粘贴单封面",
}

# 判定关键词，按先后顺序匹配，越靠前优先级越高。
# 封面关键词最具体故最先判定，避免"票据粘贴单封面"被别的规则抢走。
CATEGORY_KEYWORDS = (
    (CATEGORY_COVER, ("票据粘贴单", "粘贴单", "凭证封面", "报销封面", "封面")),
    (CATEGORY_REIMBURSE, ("差旅费报销单", "差旅报销单", "差旅费报销", "报销单", "报销审批单")),
    (CATEGORY_HOTEL, ("酒店", "宾馆", "旅店", "旅馆", "民宿", "客房", "住宿",
                      "入住", "房费", "饭店")),
    (CATEGORY_TRAVEL, ("机票", "飞机票", "登机", "航班", "行程单", "客票",
                       "火车票", "火车", "高铁", "动车", "铁路", "12306")),
)

# 本工具的输出文件名特征。文件夹模式下必须排除，否则"把整个文件夹合并"
# 会把上一次的合并结果再合并一遍。
OUTPUT_MARKERS = ("_合并_", "发票合并_", "_报告")

# 酒店出具的消费明细，不是税务发票 —— 归「其他材料」，不按酒店那样固定两张一页。
# 必须优先判定：这类单据正文里通常写着酒店名称，否则会被酒店关键词命中。
# 「水单」是携程 / 酒店给的宾客水单（住宿明细），和结账单同性质，一并归入。
SETTLEMENT_HINTS = ("结账单", "结帐单", "消费明细", "账单明细", "水单")

# 同样归「其他材料」的单据：电子登机凭证只是乘机时打出来的 A4 纸，
# 出差申请审批单是流程单据，都不是票据本身。必须早于火车 / 飞机关键词判定，
# 否则"登机"会被交通关键词抢走。
OTHER_DOC_HINTS = ("登机凭证", "登机牌", "boarding pass", "出差申请", "出差审批")

# 航旅纵横等平台出的「行程校验单」只是验真用的行程信息单（票价、航段、票号），
# 不是税务发票 —— 归「其他材料」。真正的机票发票长这样：
# 「电子发票（航空运输电子客票行程单）」，带"行程单"三个字但不带"校验单"。
# 它往往和校验单同一次行程各占一份，早先因为都含"机票"被并成一类，
# 结果去程发票配了回程校验单。同样排除带"发票"字样的文件，
# 避免把「发票+校验单」合并在一起的 PDF 误判成非发票。
VERIFY_HINTS = ("行程校验", "校验单")

# 拼版（band 排版）策略：把小材料各占纸张的一条横向区域，多张合排到一张纸上。
# 取值 = (最少条带, 最多条带, 允许旋转)。
# 酒店发票与机票 / 火车票固定两张一页；其他材料三张一页，且竖版单据转 90°
# 横躺进带子里成像更大 —— 同样一份 A4 竖版单据，横过来比硬塞着不转大约一成半。
BAND_POLICIES: Dict[str, Tuple[int, int, bool]] = {
    CATEGORY_HOTEL: (2, 2, False),
    CATEGORY_TRAVEL: (2, 2, False),
    CATEGORY_OTHER: (1, 3, True),
}

# 内容转 90° 后「顶部朝右」，与人工把竖版单据横贴在报销单上的方向一致
BAND_ROTATE = 270

# 每条带四周的呼吸空间：内容不贴边、不压虚线。相邻两份材料之间因此空出
# 2 倍的距离，裁切虚线正好走在这条通道的正中，下剪子不会碰到字。
BAND_INSET_MM = 6.0
BAND_LINE_COLOR = (0.42, 0.42, 0.42)
BAND_LINE_WIDTH = 0.7
BAND_LINE_DASHES = "[4 3] 0"

INK_THRESHOLD = 245                    # 灰度低于该值视为有内容
EDGE_TOL = 2.0                         # 内容距页边小于该值(pt)视为贴边
CLIP_TOL = 1.0                         # 内容超出页面超过该值(pt)判定为被裁切

# 内容框里剔除「孤立小碎片」的门槛。典型场景：整页 A4 单据的正文只占上半页，
# 底部单独一行「第 1 页 / 共 1 页」。若不剔除，墨迹框会被这行 8pt 小字撑满整页，
# 拼到半页带子里只能缩到 49%，正文小到看不清 —— 用户反馈"下面一小点可以忽略"。
# 判据必须保守：行距大的稀疏单据（正文各行间也隔着四五十 pt）不能被当成碎片。
# 所以要求「离主体极远」+「自身又薄又少墨」同时成立，宁可少剔不要误伤。
INK_ISOLATE_GAP_RATIO = 0.45           # 与主体的垂直间隔超过页高的该比例即视为孤立
INK_ISOLATE_GAP_MIN = 120.0            # 且至少有这么多 pt 的空白
INK_TINY_BAND_RATIO = 0.02             # 自身高度不足页高的该比例即视为薄片
INK_TINY_BAND_MAX = 16.0               # 且绝对高度不超过这么多 pt
INK_TINY_KEEP_SHARE = 0.15             # 薄片墨量超过主体的该比例就不动它（可能真是内容）


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class AnalyzeError(Exception):
    """分析阶段的可跳过错误。"""


class PdfLockedError(AnalyzeError):
    pass


class PdfEmptyError(AnalyzeError):
    pass


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class PageInfo:
    """单个源页面的分析结果。坐标均为「页面视觉坐标」，单位为 pt。"""

    source: Path
    page_no: int                       # 源文件内从 0 开始的页序号
    vis_w: float = 0.0                 # 视觉宽（已计入 rotation 属性）
    vis_h: float = 0.0
    rotation: int = 0                  # 源页 rotation 属性
    text_angle: Optional[int] = None    # 文本书写方向量化的角度 0/90/180/270
    text_share: float = 0.0            # 该方向的字符占比
    content_box: Optional[Tuple[float, float, float, float]] = None
    clipped: bool = False              # 内容是否被 CropBox 裁掉
    new_crop: Optional[Tuple[float, float, float, float]] = None
    fix_rotation: Optional[int] = None  # 输出时应设置的旋转角（None 表示保持原样）
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def landscape(self) -> bool:
        return self.vis_w > self.vis_h

    @property
    def orientation(self) -> str:
        return "横向" if self.landscape else "纵向"

    @property
    def unrotated_size(self) -> Tuple[float, float]:
        """去掉 rotation 属性后的原始页面尺寸。"""
        if self.rotation % 180:
            return (self.vis_h, self.vis_w)
        return (self.vis_w, self.vis_h)

    @property
    def final_size(self) -> Tuple[float, float]:
        """应用扶正旋转后，该页在输出中的视觉尺寸。"""
        rot = self.rotation if self.fix_rotation is None else self.fix_rotation
        uw, uh = self.unrotated_size
        return (uh, uw) if rot % 180 else (uw, uh)

    @property
    def final_landscape(self) -> bool:
        w, h = self.final_size
        return w > h

    @property
    def text_display_angle(self) -> Optional[int]:
        """文字在屏幕上实际呈现的角度：0 为水平可读。"""
        if self.text_angle is None:
            return None
        rot = self.rotation if self.fix_rotation is None else self.fix_rotation
        return (self.text_angle + rot) % 360

    @property
    def size_mm(self) -> Tuple[float, float]:
        return (self.vis_w / MM, self.vis_h / MM)


@dataclass
class SourceInfo:
    """单个输入文件的分析结果。"""

    path: Path
    page_count: int = 0
    pages: List[PageInfo] = field(default_factory=list)
    error: str = ""
    category: str = CATEGORY_OTHER
    category_hit: str = ""              # 命中的关键词，便于人工核对分类
    is_image: bool = False              # 输入是图片（一张图 = 一页，归「其他材料」）

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def kind_label(self) -> str:
        return "图片" if self.is_image else "PDF"

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def orientation_summary(self) -> str:
        if not self.pages:
            return "-"
        land = sum(1 for p in self.pages if p.landscape)
        port = len(self.pages) - land
        if land and port:
            return "纵横混排"
        return "横向" if land else "纵向"


@dataclass
class Options:
    """合并选项。"""

    size_mode: str = SIZE_A4_ROTATE       # 默认：统一 A4 纵向，横向页旋转填满
    sort_mode: str = SORT_NATURAL
    auto_rotate: bool = True            # 识别到内容横躺时自动扶正
    fix_clipping: bool = True           # 自动恢复被 CropBox 裁掉的内容
    recursive: bool = False             # 文件夹模式是否含子目录
    margin_mm: float = 0.0              # 四周留白（毫米）
    analysis_dpi: int = 72              # 内容检测的渲染精度
    output: Optional[Path] = None
    write_report: bool = True
    organize: bool = True               # 按报销业务顺序整理材料
    band_merge: bool = True             # 拼版：酒店 / 机票两张一纸，其他材料三张一转一纸
    scan_output: bool = True            # 输出为扫描件：整页转图片，不留文字层
    scan_dpi: int = 300                 # 扫描件分辨率，300 对应 A4 上 2480x3508
    auto_print: bool = False            # 合并完成后直接送打印
    printer: Optional[str] = None       # 目标打印机，None 表示系统默认
    print_copies: int = 1


@dataclass
class MergeReport:
    """合并结果。"""

    output: Optional[Path] = None
    total_files: int = 0
    ok_files: int = 0
    image_files: int = 0                                             # 其中图片材料数
    total_pages: int = 0
    written_pages: int = 0
    skipped: List[Tuple[str, str]] = field(default_factory=list)     # (文件, 原因)
    warnings: List[Tuple[str, str]] = field(default_factory=list)    # (文件, 说明)
    actions: List[str] = field(default_factory=list)                 # 处理动作明细
    report_path: Optional[Path] = None
    elapsed: float = 0.0
    output_size: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.output is not None


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"(\d+)")

# 拼音排序依赖系统的中文 collation，初始化时探测一次
PINYIN_AVAILABLE = False
for _loc in ("zh_CN.UTF-8", "zh_CN.utf8", "zh_CN", "zh_CN.GB18030"):
    try:
        locale.setlocale(locale.LC_COLLATE, _loc)
        PINYIN_AVAILABLE = True
        break
    except Exception:
        continue


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x3000 <= o <= 0x303F)


def natural_key(text: str):
    """自然排序键：让 "发票2" 排在 "发票10" 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM_RE.split(text)]


def pinyin_key(text: str):
    """中文按拼音排序，数字按大小，拉丁 / 数字开头的名字排在中文之后。

    排序结果与 Windows 资源管理器、多数国产办公软件一致：中文材料按拼音
    先后排列，`dzfp_xxx.pdf` 这类英文开头的文件落在最后一档。
    """
    key = [0 if (text and _is_cjk(text[0])) else 1]
    for i, part in enumerate(_NUM_RE.split(text)):
        if i % 2 == 1:                      # 数字段按数值比较
            key.append((1, int(part)))
        elif part:
            try:
                key.append((0, locale.strxfrm(part)))
            except Exception:
                key.append((0, part))
    return key


def sort_paths(paths: Sequence[Path], mode: str) -> List[Path]:
    items = list(paths)
    if mode == SORT_MTIME:
        return sorted(items, key=lambda p: (p.stat().st_mtime if p.exists() else 0, natural_key(p.name)))
    if mode == SORT_PINYIN and PINYIN_AVAILABLE:
        return sorted(items, key=lambda p: pinyin_key(p.name))
    if mode == SORT_NAME:
        return sorted(items, key=lambda p: p.name.lower())
    return sorted(items, key=lambda p: natural_key(p.name))


def _document_digest(doc, max_pages: int = 3, max_chars: int = 4000) -> str:
    """抽取前几页的文字，用于判断材料类别。只取少量页与字符，避免拖慢分析。"""
    parts: List[str] = []
    total = 0
    for i in range(min(doc.page_count, max_pages)):
        try:
            text = doc[i].get_text()
        except Exception:
            continue
        if text:
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
    return "\n".join(parts)[:max_chars]


def classify_document(name: str, text: str = "") -> Tuple[str, str]:
    """按文件名与正文判断材料类别，返回 (类别, 命中的关键词)。

    文件名和正文一起看：像"结账单20260731.pdf"这种文件名看不出用途的，
    靠正文里的"酒店"字样才能认出来。

    两条前置规则：

    1. **结账单是酒店出具的消费明细，不是发票**，归入「其他材料」。判断时把
       "发票"字样排除在外，避免误伤文件名里带"结账单"三个字的真发票。
    2. **电子登机凭证、出差申请审批单也只是 A4 单据**，同样归「其他材料」，
       以便和结账单一起三张一转拼到一页。这条必须走在交通关键词前面，
       否则"登机"会被火车 / 飞机那组抢走。
    3. **行程校验单不是机票**，是平台给的验真行程信息单，同样归「其他材料」。
       机票发票是「电子发票（航空运输电子客票行程单）」。这条也必须走在前头：
       文件名里通常带着"机票"两个字，否则会被交通关键词抢走。
    """
    haystack = ("%s\n%s" % (name, text)).lower()

    if any(hint in haystack for hint in SETTLEMENT_HINTS) and "发票" not in haystack:
        return CATEGORY_OTHER, "结账单（非发票）"

    if any(hint in haystack for hint in VERIFY_HINTS) and "发票" not in haystack:
        return CATEGORY_OTHER, "行程校验单（非发票）"

    for hint in OTHER_DOC_HINTS:
        if hint in haystack:
            return CATEGORY_OTHER, hint

    for category, words in CATEGORY_KEYWORDS:
        for word in words:
            if word.lower() in haystack:
                return category, word
    return CATEGORY_OTHER, ""


# 「其他材料」里东西很杂，但性质并不一样：水单、校验单、审批单、订单截图各是一摞。
# 若按用户选的文件顺序直接排，同一摞会被别的材料隔开（05 校验单、06 截图、15 校验单…），
# 贴票时得来回翻。这里再切一层小类，让同类的挨在一起。
# 顺序有意义：先长串的、更具识别度的关键词放前面，免得"住宿账单"被"订单"之类抢走。
OTHER_SUBTYPE_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("消费明细", ("水单", "结账单", "结帐单", "消费明细", "账单", "明细")),
    ("行程校验单", ("校验单", "行程校验")),
    ("出差审批单", ("出差审批", "出差申请", "审批单")),
    ("登机凭证", ("登机凭证", "登机牌", "boarding")),
    ("订单截图", ("订单", "截图", "待支付", "购票")),
)


def other_subtype(si: "SourceInfo") -> str:
    """把「其他材料」再分小类，用于排序时把同类拢在一起。

    先看文件名（截图、水单这类图片没有文本层，只能靠文件名），再看分类时命中的关键词，
    都没有就落到小类叫「其他材料」，也就是彼此还能挨着。
    """
    if si.category != CATEGORY_OTHER:
        return ""
    hay = ("%s\n%s" % (si.path.name, si.category_hit or "")).lower()
    for label, hints in OTHER_SUBTYPE_HINTS:
        if any(h in hay for h in hints):
            return label
    return "其他材料"


def organize_sources(sources: Sequence["SourceInfo"]) -> List["SourceInfo"]:
    """按报销业务顺序排列材料：酒店发票 → 差旅费报销单 → 火车/飞机 → 其他 → 封面。

    组内保持调用方传入的先后（即用户选择的排序方式），不做二次排序；
    唯一的例外是「其他材料」：它内部按小类（水单 / 校验单 / 审批单 / 截图 / 登机牌…）
    再归拢一次，同类的挨在一起，小类之间按首次出现的先后排。
    """
    ordered: List["SourceInfo"] = []
    for category in CATEGORY_ORDER:
        group = [si for si in sources if si.category == category]
        if category == CATEGORY_OTHER:
            buckets: Dict[str, List["SourceInfo"]] = {}
            order_seen: List[str] = []
            for si in group:
                key = other_subtype(si)
                if key not in buckets:
                    buckets[key] = []
                    order_seen.append(key)
                buckets[key].append(si)
            group = [si for key in order_seen for si in buckets[key]]
        ordered.extend(group)
    # 兜底：万一有类别不在预设顺序里，也不能丢材料
    seen = {id(si) for si in ordered}
    ordered.extend(si for si in sources if id(si) not in seen)
    return ordered


def is_tool_output(path: Path) -> bool:
    """判断是否为本工具先前生成的产物（合并结果或报告），用于避免重复合并。"""
    stem = path.stem
    return any(marker in stem for marker in OUTPUT_MARKERS)


def is_image_path(path: Path) -> bool:
    """是不是图片材料（按后缀判断，大小写不敏感）。"""
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def is_source_path(path: Path) -> bool:
    """能不能作为材料：PDF 或图片。"""
    return Path(path).suffix.lower() in SOURCE_SUFFIXES


def list_sources(folder: Path, recursive: bool = False) -> List[Path]:
    """列出文件夹内可作为材料的文件：PDF 与图片。

    忽略隐藏文件、Office 临时文件（~$）与本工具自己的输出。
    """
    folder = Path(folder)
    found: List[Path] = []
    pattern = "**/*" if recursive else "*"
    for p in folder.glob(pattern):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith("~$"):
            continue
        if not is_source_path(p):
            continue
        if is_tool_output(p):
            continue
        found.append(p)
    return found


def list_pdfs(folder: Path, recursive: bool = False) -> List[Path]:
    """只列出 PDF（保留旧语义：只认 PDF，不把图片混进来）。"""
    return [p for p in list_sources(folder, recursive) if not is_image_path(p)]


def human_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return "%.0f %s" % (v, unit) if unit == "B" else "%.1f %s" % (v, unit)
        v /= 1024
    return "%.1f GB" % v


def unique_path(path: Path) -> Path:
    """若目标已存在，追加 (1)(2)… 避免覆盖。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        cand = parent / ("%s (%d)%s" % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def writable_fallback(filename: str) -> Optional[Path]:
    """按顺序找一个可写目录来放置输出文件。"""
    for cand in (Path.home() / "Desktop", Path.home() / "Documents", Path.home()):
        try:
            if cand.is_dir() and os.access(str(cand), os.W_OK):
                return cand / filename
        except Exception:
            continue
    return None


def default_output_path(inputs: Sequence[Path], base_name: Optional[str] = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = ("%s_合并_%s" % (base_name, ts)) if base_name else ("发票合并_%s" % ts)
    if inputs:
        first = Path(inputs[0])
        out_dir = first.parent if first.parent.exists() else Path.home()
    else:
        out_dir = Path.home() / "Desktop"
        if not out_dir.exists():
            out_dir = Path.home()
    return out_dir / (stem + ".pdf")


# --------------------------------------------------------------------------
# 用户设置：记住上次用过的目录，下次打开文件对话框直接停在那里
# --------------------------------------------------------------------------

SETTINGS_DIR_NAME = "InvoiceMergeAssistant"


def settings_path() -> Path:
    """配置文件位置 —— 用 macOS 标准的 Application Support 目录。"""
    return (Path.home() / "Library" / "Application Support"
            / SETTINGS_DIR_NAME / "settings.json")


def load_settings() -> Dict[str, object]:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: Dict[str, object]) -> None:
    """写配置。失败就静默放过 —— 记不住路径是小事，不能影响合并本身。"""
    try:
        target = settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 页面分析
# --------------------------------------------------------------------------

def _open_pdf(path: Path):
    """打开 PDF，处理加密与空文档，失败时抛出 AnalyzeError 子类。"""
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise AnalyzeError(str(exc) or exc.__class__.__name__) from exc

    if doc.is_encrypted and doc.needs_pass:
        if not doc.authenticate(""):
            doc.close()
            raise PdfLockedError("已加密，需要密码")
    try:
        n = doc.page_count
    except Exception as exc:
        doc.close()
        raise AnalyzeError("无法读取页数：%s" % exc) from exc
    if n <= 0:
        doc.close()
        raise PdfEmptyError("文档没有任何页面")
    return doc


# 需要外部解码器的图片格式（MuPDF 没编进解码器）的临时目录与缓存：
# 分析、合并各要打开一次，转好的 PNG 缓存起来别转两遍。
_TEMP_DIR: Optional[Path] = None
_CONVERT_CACHE: Dict[Path, Path] = {}


def _temp_dir() -> Path:
    global _TEMP_DIR
    if _TEMP_DIR is None or not _TEMP_DIR.exists():
        _TEMP_DIR = Path(tempfile.mkdtemp(prefix="invoice_merge_img_"))
        atexit.register(shutil.rmtree, str(_TEMP_DIR), True)
    return _TEMP_DIR


def _sips_path() -> Optional[str]:
    """macOS 上返回系统 sips 的路径，其他平台返回 None。

    单独抽成函数是为了让自检能模拟「这台机器不是 mac」——
    直接改 sys.platform 会连带影响 shutil / subprocess 的内部判断。
    """
    if sys.platform != "darwin":
        return None
    return shutil.which("sips")


def _image_dpi(img) -> Optional[Tuple[float, float]]:
    """读出图片自己声明的分辨率（每英寸像素），读不到就返回 None。

    iPhone 的 HEIC 不写 info["dpi"]，而是把分辨率放在 EXIF 的 282/283 标签里；
    只看 info 会让同一张照片在 macOS（sips，读得到）和 Windows（Pillow，读不到）
    上算出不同的页面尺寸。
    """
    pair: Optional[Tuple[float, float]] = None
    dpi = img.info.get("dpi")
    try:
        if dpi:
            pair = (float(dpi[0]), float(dpi[1]))
    except Exception:
        pair = None
    if pair is None:
        try:
            ex = img.getexif()
            x, y = ex.get(282), ex.get(283)
            unit = ex.get(296) or 2          # 2 = 英寸，3 = 厘米
            if x and y:
                pair = (float(x), float(y))
                if unit == 3:                # 每厘米 → 每英寸
                    pair = (pair[0] * 2.54, pair[1] * 2.54)
        except Exception:
            pair = None
    if pair is None or not all(1.0 <= v <= 2400.0 for v in pair):
        return None
    return pair


def _convert_with_pillow(path: Path, target: Path) -> bool:
    """有 Pillow 就用它转（Windows 上最省事的一条路；没装就跳过）。

    HEIC 还要额外装 pillow-heif，装了就在这里注册解码器；PNG 里不带 EXIF，
    所以必须先 exif_transpose，否则手机竖拍的照片会躺着出去 ——
    走 MuPDF 那条路时方向是解码器自动应用的，两条路的结果必须一致。
    """
    try:
        from PIL import Image, ImageOps  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        import pillow_heif  # type: ignore[import-not-found]

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(path) as img:
            # Pillow 存 PNG 默认写 96dpi，会让同一张图经这条路转完变大 4/3；
            # 沿用图片自己的 DPI，没有就按 72 —— 与走 MuPDF / sips 的结果对齐。
            pair = _image_dpi(img)
            opts = {"dpi": pair} if pair else {}
            ImageOps.exif_transpose(img).convert("RGB").save(target, "PNG", **opts)
        return target.exists() and target.stat().st_size > 0
    except Exception:
        return False


def _convert_with_cli(path: Path, target: Path) -> bool:
    """挨个试系统里可能装了的命令行转换器 —— 只要有任意一个能出图就算成功。

    macOS 自带 sips，能解 HEIC 与 WebP；装了 ImageMagick / libheif / ffmpeg 的
    Windows / Linux 机器也能走到这里，不必让用户先手动转格式。
    """
    sips = _sips_path()
    plans = [["sips", "-s", "format", "png", str(path), "--out", str(target)]] if sips else []
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        plans.append([magick, str(path), str(target)])
    heif = shutil.which("heif-convert")
    if heif:
        plans.append([heif, str(path), str(target)])
    dwebp = shutil.which("dwebp")
    if dwebp and path.suffix.lower() == ".webp":
        plans.append([dwebp, "-quiet", str(path), "-o", str(target)])
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        plans.append([ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
                      "-frames:v", "1", "-update", "1", str(target)])
    for cmd in plans:
        target.unlink(missing_ok=True)
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            continue
        if done.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return True
    return False


def _system_convert(path: Path) -> Path:
    """把 MuPDF 解不了的图片（HEIC / HEIF / WebP）先转成 PNG 再读。

    HEIC 是 iPhone 的默认格式，WebP 在 PyMuPDF 的多数构建里都没编进解码器。
    这里按「Pillow → 系统命令行工具」的顺序试，能转就转；全都不可用时给一句
    能直接照做的提示，而不是抛一句看不懂的异常。
    """
    cached = _CONVERT_CACHE.get(path)
    if cached is not None and cached.exists():
        return cached
    label = path.suffix.lstrip(".").upper() or "该"
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", path.stem)[:40] or "image"
    target = _temp_dir() / ("%s_%s.png" % (stem, uuid.uuid4().hex[:8]))
    if _sips_path():
        converted = _convert_with_cli(path, target) or _convert_with_pillow(path, target)
    else:
        converted = _convert_with_pillow(path, target) or _convert_with_cli(path, target)
    if not converted:
        target.unlink(missing_ok=True)
        raise AnalyzeError(
            "%s 图片需要先转成 JPG 或 PNG 再合并（本机没有可用的转换器："
            "装 Pillow 或 ImageMagick 后可自动处理）" % label)
    _CONVERT_CACHE[path] = target
    return target


def _open_source(path: Path):
    """打开材料：PDF 原样打开，图片转成「一页 PDF」再返回。

    图片转 PDF 之后，insert_pdf / show_pdf_page 这些下游代码不必区分来源类型；
    图片的 EXIF 方向由 MuPDF 解码时应用，手机竖拍的照片不会横躺。
    """
    path = Path(path)
    if not is_image_path(path):
        return _open_pdf(path)

    target = _system_convert(path) if path.suffix.lower() in SYSTEM_DECODE_SUFFIXES else path
    try:
        doc = fitz.open(str(target))
    except Exception as exc:
        raise AnalyzeError("无法读取图片（格式不受支持或文件已损坏）：%s"
                           % (str(exc) or exc.__class__.__name__)) from exc
    try:
        if doc.page_count <= 0:
            raise PdfEmptyError("图片没有可用的画面")
        pdf_bytes = doc.convert_to_pdf()
    except AnalyzeError:
        raise
    except Exception as exc:
        raise AnalyzeError("无法读取图片（格式不受支持或文件已损坏）：%s"
                           % (str(exc) or exc.__class__.__name__)) from exc
    finally:
        try:
            doc.close()
        except Exception:
            pass

    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise AnalyzeError("图片转换 PDF 失败：%s" % exc) from exc
    if pdf.page_count <= 0:
        pdf.close()
        raise PdfEmptyError("图片转换后没有页面")
    return pdf


def _ink_box(page, dpi: int) -> Optional[fitz.Rect]:
    """渲染页面后求非白像素包围盒，返回视觉坐标下的 Rect。"""
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    except Exception:
        return None
    if pix.width <= 0 or pix.height <= 0:
        return None
    scale = 72.0 / dpi

    if _np is not None:
        arr = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
        mask = arr < INK_THRESHOLD
        if not mask.any():
            return None
        ys, xs = _np.where(mask)
        return fitz.Rect(
            float(xs.min()) * scale, float(ys.min()) * scale,
            float(xs.max() + 1) * scale, float(ys.max() + 1) * scale,
        )

    # 无 numpy 时的纯 Python 回退
    samples, stride, w, h = pix.samples, pix.stride, pix.width, pix.height
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = samples[y * stride: y * stride + w]
        if min(row) >= INK_THRESHOLD:
            continue
        if y < miny:
            miny = y
        if y > maxy:
            maxy = y
        for x, v in enumerate(row):
            if v < INK_THRESHOLD:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
    if maxx < 0:
        return None
    return fitz.Rect(minx * scale, miny * scale, (maxx + 1) * scale, (maxy + 1) * scale)


def _ink_bands(page, dpi: int) -> Optional[List[Tuple[float, float, float, float, int]]]:
    """渲染页面后按行切出墨迹带，返回 [(y0, y1, x0, x1, 墨量)]，单位 pt。

    只把「上下相邻、中间没有空白行」的像素算同一条带；带与带之间的空白留给
    调用方按需合并 —— 一段正常文字的行距会产生很多条带，页脚页码则是单独一条。
    """
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    except Exception:
        return None
    if pix.width <= 0 or pix.height <= 0:
        return None
    scale = 72.0 / dpi
    w, h, stride, samples = pix.width, pix.height, pix.stride, pix.samples

    spans: List[Tuple[int, int]] = []
    if _np is not None:
        arr = _np.frombuffer(samples, dtype=_np.uint8).reshape(h, stride)[:, :w]
        mask = arr < INK_THRESHOLD
        rows = _np.where(mask.any(axis=1))[0]
        if len(rows) == 0:
            return []
        start = prev = int(rows[0])
        for r in rows[1:]:
            r = int(r)
            if r - prev > 1:
                spans.append((start, prev))
                start = r
            prev = r
        spans.append((start, prev))
        bands: List[Tuple[float, float, float, float, int]] = []
        for a, b in spans:
            sub = mask[a:b + 1]
            cols = _np.where(sub.any(axis=0))[0]
            bands.append((a * scale, (b + 1) * scale,
                          float(cols[0]) * scale, float(cols[-1] + 1) * scale,
                          int(sub.sum())))
        return bands

    # 无 numpy 时的纯 Python 回退
    for y in range(h):
        row = samples[y * stride: y * stride + w]
        if min(row) >= INK_THRESHOLD:
            continue
        if spans and y - spans[-1][1] <= 1:
            spans[-1] = (spans[-1][0], y)
        else:
            spans.append((y, y))
    bands = []
    for a, b in spans:
        minx, maxx, area = w, -1, 0
        for y in range(a, b + 1):
            row = samples[y * stride: y * stride + w]
            for x, v in enumerate(row):
                if v < INK_THRESHOLD:
                    area += 1
                    if x < minx:
                        minx = x
                    if x > maxx:
                        maxx = x
        if maxx < 0:
            continue
        bands.append((a * scale, (b + 1) * scale,
                      minx * scale, (maxx + 1) * scale, area))
    return bands


def _union_band_box(bands) -> fitz.Rect:
    """把若干条带合成一个外框。"""
    return fitz.Rect(
        min(b[2] for b in bands), min(b[0] for b in bands),
        max(b[3] for b in bands), max(b[1] for b in bands),
    )


def _main_ink_box(page, dpi: int) -> Optional[fitz.Rect]:
    """内容外框，但剔除与主体隔得很远的极小碎片（页脚页码之类）。

    和 `_ink_box` 的区别只在这一点：`_ink_box` 取全部墨迹的包围盒，适合判断
    "有没有被裁切"；这里要的是"内容到底占多大"，所以孤立的页码小字不参与。
    剔除后内容框会变小，拼版时同样的条带能放下更大的成像。
    """
    bands = _ink_bands(page, dpi)
    if bands is None:
        return None
    if not bands:
        return None

    page_h = float(page.rect.height)
    gap_thr = max(INK_ISOLATE_GAP_MIN, INK_ISOLATE_GAP_RATIO * page_h)
    tiny_thr = max(INK_TINY_BAND_MAX, INK_TINY_BAND_RATIO * page_h)

    # 按垂直间隔聚类：间隔小于门槛的算同一块内容
    clusters: List[List[Tuple[float, float, float, float, int]]] = [[bands[0]]]
    for b in bands[1:]:
        if b[0] - clusters[-1][-1][1] <= gap_thr:
            clusters[-1].append(b)
        else:
            clusters.append([b])

    if len(clusters) == 1:
        return _union_band_box(clusters[0])

    main = max(clusters, key=lambda c: sum(b[4] for b in c))
    main_area = sum(b[4] for b in main) or 1
    main_top = min(b[0] for b in main)
    main_bottom = max(b[1] for b in main)

    keep = [main]
    for c in clusters:
        if c is main:
            continue
        top = min(b[0] for b in c)
        bottom = max(b[1] for b in c)
        area = sum(b[4] for b in c)
        gap = min(abs(top - main_bottom), abs(main_top - bottom))
        if bottom - top < tiny_thr and area < INK_TINY_KEEP_SHARE * main_area and gap > gap_thr:
            continue                    # 孤立小碎片：不参与内容框
        keep.append(c)

    return _union_band_box([b for c in keep for b in c])


def _vector_extent(page) -> Optional[fitz.Rect]:
    """页面所有元素（文本 / 图像 / 矢量）在未旋转坐标系下的并集包围盒。

    坐标原点是 CropBox 左上角，与 get_drawings 的约定一致。
    """
    rects: List[fitz.Rect] = []
    try:
        for blk in page.get_text("blocks"):
            r = fitz.Rect(blk[0], blk[1], blk[2], blk[3])
            if not r.is_empty and r.is_valid:
                rects.append(r)
    except Exception:
        pass
    try:
        for item in page.get_images(full=True):
            try:
                for r in page.get_image_rects(item[0]):
                    if not r.is_empty and r.is_valid:
                        rects.append(r)
            except Exception:
                pass
    except Exception:
        pass
    if not rects:
        # 纯矢量页面才去取绘制路径，避免在复杂页面上浪费性能
        try:
            for d in page.get_drawings():
                r = d.get("rect")
                if r is not None and not r.is_empty and r.is_valid:
                    rects.append(r)
        except Exception:
            pass
    if not rects:
        return None
    union = rects[0]
    for r in rects[1:]:
        union |= r
    return union


def _text_direction(page) -> Tuple[Optional[int], float]:
    """统计文本书写方向，返回 (量化的角度, 该方向的字符占比)。

    角度含义：0 = 正常横排；90 = 文字自下而上（内容被逆时针转过 90）；
    180 = 倒置；270 = 文字自上而下。
    """
    votes: Dict[int, int] = {}
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return None, 0.0
    for blk in blocks:
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            txt = "".join(sp.get("text", "") for sp in line.get("spans", []))
            n = len(txt.strip())
            if n == 0:
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            ang = math.degrees(math.atan2(dy, dx)) % 360.0
            bucket = int(round(ang / 90.0)) % 4
            votes[bucket] = votes.get(bucket, 0) + n
    if not votes:
        return None, 0.0
    total = sum(votes.values())
    best = max(votes, key=lambda k: votes[k])
    return best * 90, votes[best] / float(total)


def _analyze_page(doc, page_no: int, source: Path, opts: Options) -> PageInfo:
    page = doc[page_no]
    vrect = page.rect                       # 视觉矩形，rotation 属性已计入
    info = PageInfo(source=source, page_no=page_no,
                    vis_w=float(vrect.width), vis_h=float(vrect.height),
                    rotation=int(page.rotation) % 360)

    # ---- 内容范围（渲染法，直接反映肉眼所见）----
    ink = _ink_box(page, opts.analysis_dpi)
    if ink is not None:
        info.content_box = (ink.x0, ink.y0, ink.x1, ink.y1)

    # ---- 文本书写方向（用于判断内容是否横躺）----
    angle, share = _text_direction(page)
    info.text_angle, info.text_share = angle, share

    # ---- 是否被 CropBox 裁掉内容 ----
    if opts.fix_clipping:
        edge_touch = ink is not None and (
            ink.x0 - vrect.x0 < EDGE_TOL or ink.y0 - vrect.y0 < EDGE_TOL
            or vrect.x1 - ink.x1 < EDGE_TOL or vrect.y1 - ink.y1 < EDGE_TOL
        )
        if edge_touch or ink is None:
            extent = _vector_extent(page)
            if extent is not None:
                vis_extent = extent * page.rotation_matrix
                over = max(
                    vrect.x0 - vis_extent.x0, vrect.y0 - vis_extent.y0,
                    vis_extent.x1 - vrect.x1, vis_extent.y1 - vrect.y1,
                )
                if over > CLIP_TOL:
                    crop = page.cropbox
                    unrot = vis_extent * page.derotation_matrix
                    abs_extent = fitz.Rect(
                        unrot.x0 + crop.x0, unrot.y0 + crop.y0,
                        unrot.x1 + crop.x0, unrot.y1 + crop.y0,
                    )
                    merged = crop | abs_extent
                    merged = merged & page.mediabox
                    info.clipped = True
                    info.new_crop = (merged.x0, merged.y0, merged.x1, merged.y1)
                    info.warnings.append(
                        "内容超出裁剪框，已自动扩展裁剪范围恢复完整内容"
                    )

    # ---- 内容横躺判定与旋转纠正 ----
    # 关键：判断依据是「文字书写方向 ⊕ 页面 rotation 属性」的合成结果，
    # 只有合成后仍不水平（0°）时才需要干预。
    # "统一 A4 纵向 + 横向内容旋转"模式有自己的一套方向规则，此处不介入。
    if opts.size_mode == SIZE_A4_ROTATE:
        if info.landscape:
            info.warnings.append("横向页，内容将旋转 90° 后填满 A4 纵向")
    elif opts.auto_rotate and angle is not None and share >= 0.6:
        display = (angle + info.rotation) % 360
        if display != 0:
            target_rot = (360 - angle) % 360
            if target_rot != info.rotation:
                info.fix_rotation = target_rot
                info.warnings.append(
                    "内容在页面中呈 %d° 摆放，已扶正为%s页" % (display, "横" if target_rot % 180 else "纵")
                )
    elif angle is None and ink is not None and not is_image_path(source):
        # 无文本层：只能用内容形状做粗判，属于低置信度，仅提示不改动。
        # 图片跳过这条：EXIF 方向已由解码器应用，照片内容本就未必填满画面，
        # 再按宽高比提示「方向可能异常」只会制造噪音。
        w = ink.x1 - ink.x0
        h = ink.y1 - ink.y0
        if h > 1 and w > 1:
            ratio = w / h
            if (not info.landscape and ratio > 1.6) or (info.landscape and ratio < 0.63):
                info.warnings.append(
                    "该页为图片内容且形状与页面方向不一致，方向可能异常，请人工确认"
                )
    return info


def analyze_source(path: Path, opts: Options) -> SourceInfo:
    """分析一个 PDF：页数、每页方向、尺寸、裁切与旋转诊断。"""
    path = Path(path)
    info = SourceInfo(path=path)
    if not path.exists():
        info.error = "文件不存在"
        return info
    if not path.is_file():
        info.error = "不是文件"
        return info
    if not is_source_path(path):
        info.error = "不是 PDF 或图片文件"
        return info
    info.is_image = is_image_path(path)
    try:
        doc = _open_source(path)
    except AnalyzeError as exc:
        info.error = str(exc)
        return info
    except Exception as exc:
        info.error = "%s: %s" % (exc.__class__.__name__, exc)
        return info

    try:
        info.page_count = doc.page_count
        if info.is_image:
            # 图片不参与关键词分类：一张照片不是「酒店发票」也不是「火车票」，
            # 用户明确要求图片一律当「其他材料」，可拼版、排在封面之前。
            info.category, info.category_hit = CATEGORY_OTHER, "图片（按其他材料）"
        else:
            # 先认类别：文件名看不出用途的（如"结账单20260731.pdf"）要靠正文里的关键词
            info.category, info.category_hit = classify_document(
                path.name, _document_digest(doc)
            )
        for pno in range(doc.page_count):
            try:
                info.pages.append(_analyze_page(doc, pno, path, opts))
            except Exception as exc:
                info.pages.append(PageInfo(
                    source=path, page_no=pno, error="%s: %s" % (exc.__class__.__name__, exc)
                ))
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return info


# --------------------------------------------------------------------------
# 合并
# --------------------------------------------------------------------------

def portrait_target_rotation(info: PageInfo) -> int:
    """在"统一 A4 纵向 + 横向内容旋转"模式下，该页应设置的 rotation。

    规律：横向页的内容统一转成 PORTRAIT_TEXT_ANGLE（内容顶部朝右），
    纵向页保持水平。这样处理与人工排版的报销材料一致 —— 横向材料
    旋转后能填满 A4 纵向纸面，打印出来把纸转 90° 看；摆向与拼版页统一。

    注意这里算的是「覆盖」用的 rotation 值（set_rotation 会替换源页的
    rotation 属性，不是叠加），因此必须基于内容流的原始角度 text_angle
    反推，而不是在页面的当前视觉角度上累加。
    """
    target = PORTRAIT_TEXT_ANGLE if info.landscape else 0
    if info.text_angle is not None:
        return (target - info.text_angle) % 360
    return target if info.landscape else 0


def _a4_cap(w: float, h: float) -> Tuple[float, float]:
    """同样摆向下 A4 的长宽（横向内容拿横版 A4 当上限）。"""
    return (A4_H, A4_W) if w > h else (A4_W, A4_H)


def image_exceeds_a4(pi: "PageInfo") -> bool:
    """图片的原始尺寸是否超过 A4 —— 手机原图按 72dpi 折算常有 4000pt 见方，
    直接照搬会印出一张 1.4 米宽的纸，那已经不是「保持原始尺寸」而是失控。"""
    if pi.vis_w <= 0 or pi.vis_h <= 0:
        return False
    cap_w, cap_h = _a4_cap(pi.vis_w, pi.vis_h)
    return pi.vis_w > cap_w + 0.5 or pi.vis_h > cap_h + 0.5


def write_image_keep_page(out, doc, pi: "PageInfo") -> float:
    """「保持原始尺寸」模式下写一张超 A4 的图片：按 A4 上限等比缩小后单独成页。

    只缩不放，且保持长宽比；返回缩放比例（1.0 表示没缩）。
    """
    w, h = pi.vis_w, pi.vis_h
    cap_w, cap_h = _a4_cap(w, h)
    scale = min(1.0, cap_w / w, cap_h / h) if w > 0 and h > 0 else 1.0
    tmp = fitz.open()
    stage = fitz.open()
    try:
        tmp.insert_pdf(doc, from_page=pi.page_no, to_page=pi.page_no)
        tp = tmp[0]
        if pi.new_crop:
            tp.set_cropbox(fitz.Rect(*pi.new_crop))
        tp.remove_rotation()
        # 先在暂存文档里把这一页排好，成功后再整体插进 out ——
        # 否则中途出错会在输出里留下半张空白页（这条坑在拼版路径上踩过）。
        newpage = stage.new_page(width=max(1.0, w * scale), height=max(1.0, h * scale))
        newpage.show_pdf_page(newpage.rect, tmp, 0, keep_proportion=True)
        out.insert_pdf(stage)
    finally:
        stage.close()
        tmp.close()
    return scale


def _open_cached(cache: Dict[Path, object], path: Path):
    doc = cache.get(path)
    if doc is None:
        doc = _open_source(path)
        cache[path] = doc
    return doc


def _band_rects(tw: float, th: float, bands: int,
                margin: float) -> List[fitz.Rect]:
    """把纸张纵向等分成 bands 条，返回每条的可用区域。

    每条四周留 BAND_INSET_MM（不小于页边留白 margin）的呼吸空间，相邻两条
    因此空出两倍距离，裁切虚线正走在这条通道中间。
    """
    pad = max(margin, BAND_INSET_MM * MM)
    band_h = th / bands
    x0, x1 = pad, max(pad + 1.0, tw - pad)
    return [fitz.Rect(x0, i * band_h + pad, x1, max(i * band_h + pad + 1.0,
                                                    (i + 1) * band_h - pad))
            for i in range(bands)]


def band_policy(category: str) -> Optional[Tuple[int, int, bool]]:
    """该类别的拼版策略 (最少条带, 最多条带, 允许旋转)；None 表示不拼版。"""
    return BAND_POLICIES.get(category)


def band_applies(category: str, opts: Options) -> bool:
    """该类别在当前选项下是否真的参与拼版。

    「保持原始尺寸」不参与：拼版必然要重绘并统一到一张纸上，与该选项
    "逐页原样复制、零质量损失"的承诺冲突。
    """
    return bool(opts.band_merge and opts.size_mode != SIZE_KEEP
                and band_policy(category) is not None)


def build_units(plan: Sequence[Tuple["SourceInfo", PageInfo]],
                opts: Options) -> List[Tuple[List[Tuple["SourceInfo", PageInfo]], int, bool]]:
    """把待写入的页编组，返回 [(组内页面, 该页分几条带, 是否允许旋转)]。

    同类材料就近凑满一张纸，不同类不混排 —— 酒店票和登机凭证对转不转的诉求
    不一样，混在一页上裁出来的尺寸就乱了。差旅费报销单、封面各占一页。
    """
    units: List[Tuple[List[Tuple["SourceInfo", PageInfo]], int, bool]] = []
    buf: List[Tuple["SourceInfo", PageInfo]] = []
    buf_cat: Optional[str] = None
    buf_pol: Optional[Tuple[int, int, bool]] = None
    buf_sub: Optional[str] = None

    def flush() -> None:
        nonlocal buf, buf_cat, buf_pol, buf_sub
        if buf:
            lo, _hi, rot = buf_pol
            units.append((buf, max(lo, len(buf)), rot))
            buf, buf_cat, buf_pol, buf_sub = [], None, None, None

    def same_subtype_run(start: int, sub: str) -> int:
        """从 start 起、同属一个小类（其他材料）的页数，用来判断换页会不会把一摞拆开。"""
        count = 0
        for j in range(start, len(plan)):
            sj = plan[j][0]
            if sj.category != CATEGORY_OTHER or other_subtype(sj) != sub:
                break
            count += 1
        return count

    for idx, item in enumerate(plan):
        si = item[0]
        pol = band_policy(si.category) if band_applies(si.category, opts) else None
        if pol is None:
            flush()
            units.append(([item], 1, False))
            continue
        if buf_cat != si.category or len(buf) >= pol[1]:
            flush()
        else:
            sub = other_subtype(si) if si.category == CATEGORY_OTHER else None
            # 换小类时，如果这一摞摆在剩下的条带里放不下，就整摞挪到下一页；
            # 放得下就顺着排，不为凑整多费一张纸。
            if (sub is not None and buf_sub is not None and sub != buf_sub
                    and len(buf) + same_subtype_run(idx, sub) > pol[1]):
                flush()
        buf.append(item)
        buf_cat, buf_pol = si.category, pol
        buf_sub = other_subtype(si) if si.category == CATEGORY_OTHER else None
    flush()
    return units


def estimate_output_pages(sources: Sequence[SourceInfo], opts: Options) -> int:
    """按当前选项预估输出页数，供界面与 --dry-run 显示。"""
    plan = [(si, pi) for si in sources if si.ok
            for pi in si.pages if not pi.error]
    return len(build_units(plan, opts)) if plan else 0


def _place_content(page, src_doc, src_page, dst: fitz.Rect,
                   allow_rotate: bool) -> Tuple[int, float]:
    """把 src_page 的内容放进 page 上的 dst 条带，返回 (旋转角, 成像比例)。

    这里曾经按页高比例硬切，结果把横向小票切残了 —— 比如 600×400 的电子发票
    正文占满整页，切一半正好把"价税合计"那几行切掉。

    正确理解是"几张材料各占纸张的几条带"，而不是"把发票截掉一半"：只去掉四周
    空白，内容一律完整保留；放不下就整张缩小，只是小一点，不丢信息。竖版单据
    横过来通常比硬塞着不转成像更大（A4 竖版进三条带约 47% 对 33%），故两种摆法取大的。
    唯一的例外是先剔除页脚页码这类孤立小碎片，理由见 _main_ink_box。
    """
    ink = _main_ink_box(src_page, 72)
    if ink is None or ink.is_empty:
        page.show_pdf_page(dst, src_doc, 0, keep_proportion=True)
        return 0, 1.0
    # 四周空白都按实际墨迹范围去掉 —— 早先只去上下、保留整页宽，
    # 结果横向留白把内容撑成"宽扁一条"，旋转该不该转就判反了。
    # 用 _main_ink_box 而不是 _ink_box：底部孤零零的「第 1 页 / 共 1 页」
    # 会把内容框撑满整页，让正文白白缩掉一半。
    band = ink & src_page.rect
    if band.is_empty or not band.width or not band.height:
        page.show_pdf_page(dst, src_doc, 0, keep_proportion=True)
        return 0, 1.0
    rot, scale = 0, min(dst.width / band.width, dst.height / band.height)
    if allow_rotate:
        turn = min(dst.width / band.height, dst.height / band.width)
        if turn > scale + 1e-6:
            rot, scale = BAND_ROTATE, turn
    page.show_pdf_page(dst, src_doc, 0, clip=band, keep_proportion=True, rotate=rot)
    return rot, scale


def _draw_cut_lines(page, bands: int, margin: float) -> None:
    """在相邻条带之间的通道正中画裁切虚线，两端离纸边留一段方便下剪子。"""
    if bands < 2:
        return
    tw, th = page.rect.width, page.rect.height
    inset = max(margin, BAND_INSET_MM * MM)
    band_h = th / bands
    for i in range(1, bands):
        y = i * band_h
        page.draw_line(fitz.Point(inset, y), fitz.Point(tw - inset, y),
                       color=BAND_LINE_COLOR, width=BAND_LINE_WIDTH,
                       dashes=BAND_LINE_DASHES)


def _write_band_unit(
    out,
    unit: Sequence[Tuple["SourceInfo", PageInfo]],
    bands: int,
    allow_rotate: bool,
    opts: Options,
    doc_cache: Dict[Path, object],
    margin: float,
) -> None:
    """把一组小材料纵向排进同一张纸的等高条带里，条带间画裁切虚线。

    allow_rotate 为假时固定按页面的自然方向取内容，不套用横向页旋转 90° 的规则：
    票据本是宽扁的一条，上下叠放正好，转成竖躺反而浪费纸。
    """
    tw, th = A4_W, A4_H        # 拼版只在统一 A4 纵向模式下发生
    newpage = out.new_page(width=tw, height=th)
    rects = _band_rects(tw, th, bands, margin)

    for idx, (si, pi) in enumerate(unit):
        doc = _open_cached(doc_cache, si.path)
        tmp = fitz.open()
        try:
            tmp.insert_pdf(doc, from_page=pi.page_no, to_page=pi.page_no)
            tp = tmp[0]
            if pi.new_crop:
                tp.set_cropbox(fitz.Rect(*pi.new_crop))
            tp.remove_rotation()             # 烘焙旋转，坐标即以视觉呈现为准

            _rot, scale = _place_content(newpage, tmp, tp, rects[idx], allow_rotate)
            if scale < 1.0 - 1e-6:
                pi.warnings.append(
                    "内容放不下 1/%d 页，已缩小到约 %d%%" % (bands, round(scale * 100)))
        finally:
            tmp.close()

    _draw_cut_lines(newpage, bands, margin)


def rasterize_pages(src, dpi: int):
    """把每页整页渲染成图片后重组成新文档 —— 出来就是一份扫描件。

    页面尺寸沿用源页原样，只换内容的表示方式：没有文字层、不能选中复制，
    和打印出去再扫回来基本等价。dpi 用 300 时 A4 上是 2480x3508 像素，
    与常见扫描仪的默认档一致。
    """
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        newpage = out.new_page(width=page.rect.width, height=page.rect.height)
        newpage.insert_image(newpage.rect, pixmap=pix)
        pix = None                      # 单页 RGB 位图约 26MB，尽早放手
    return out


def merge_pdfs(
    files: Sequence[Path],
    opts: Options,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> MergeReport:
    """执行合并。单个文件失败不会中断整体流程。"""
    started = datetime.now()
    report = MergeReport()
    say = log or (lambda _m: None)

    paths = [Path(p) for p in files]
    report.total_files = len(paths)

    def stopped() -> bool:
        return bool(should_stop and should_stop())

    # ---------- 1. 分析 ----------
    say("正在分析 %d 个文件…" % len(paths))
    sources: List[SourceInfo] = []
    for i, p in enumerate(paths, 1):
        if stopped():
            report.error = "已取消"
            return report
        si = analyze_source(p, opts)
        sources.append(si)
        if si.ok:
            report.ok_files += 1
            report.total_pages += si.page_count
            if si.is_image:
                report.image_files += 1
        else:
            report.skipped.append((p.name, si.error))
            say("  跳过 %s：%s" % (p.name, si.error))
        if progress:
            progress(i, len(paths))

    if report.ok_files == 0:
        report.error = "没有任何可用的 PDF 或图片文件"
        return report

    # 按报销业务顺序整理材料：酒店发票 → 差旅费报销单 → 火车 / 飞机 → 其他 → 封面
    if opts.organize:
        before = [si.path.name for si in sources if si.ok]
        sources = organize_sources(sources)
        after = [si.path.name for si in sources if si.ok]
        if before != after:
            say("已按报销顺序整理材料：")
            for si in sources:
                if si.ok:
                    hit = ("（命中「%s」）" % si.category_hit) if si.category_hit else ""
                    say("  [%s] %s%s" % (si.category_label, si.path.name, hit))

    # 需要复制的页序列
    plan: List[Tuple[SourceInfo, PageInfo]] = []
    for si in sources:
        if not si.ok:
            continue
        for pi in si.pages:
            if pi.error:
                report.skipped.append(("%s 第 %d 页" % (si.path.name, pi.page_no + 1), pi.error))
                continue
            plan.append((si, pi))

    if not plan:
        report.error = "所有页面都无法处理"
        return report

    # 编组：可拼版的材料多张合排一张纸，其余材料各占一页
    units = build_units(plan, opts)
    if opts.band_merge:
        banded = [u for u, bands, _r in units if bands > 1]
        if banded:
            counts: Dict[str, int] = {}
            for u in banded:
                for s, _p in u:
                    counts[s.category] = counts.get(s.category, 0) + 1
            say("拼版（带间已画裁切虚线）：%s，共 %d 页合排为 %d 张纸" % (
                "、".join("%s %d 页" % (CATEGORY_LABELS[c], n) for c, n in counts.items()),
                sum(counts.values()), len(banded)))

    # ---------- 2. 写入 ----------
    out = fitz.open()
    margin = max(0.0, opts.margin_mm) * MM
    doc_cache: Dict[Path, object] = {}
    total = len(units)

    try:
        for order, (unit, bands, allow_rotate) in enumerate(units, 1):
            if stopped():
                report.error = "已取消"
                break
            si, pi = unit[0]
            try:
                if bands > 1:
                    # 拼版页：每张材料占纸面一条等高带，带间画裁切虚线
                    _write_band_unit(out, unit, bands, allow_rotate,
                                     opts, doc_cache, margin)
                    report.written_pages += 1
                    names = " + ".join(u[0].path.name for u in unit)
                    imgs = sum(1 for u in unit if u[0].is_image)
                    report.actions.append(
                        "第%d页 拼版（%d 张合排%s%s，带间裁切虚线）：%s"
                        % (order, len(unit),
                           "、竖版转 90°" if allow_rotate else "",
                           "、含图片 %d 张" % imgs if imgs else "", names))
                    for u in unit:
                        for w in u[1].warnings:
                            report.warnings.append(
                                ("%s 第%d页" % (u[0].path.name, u[1].page_no + 1), w))
                    if progress:
                        progress(order, total)
                    continue

                doc = _open_cached(doc_cache, si.path)

                shrunk = 1.0
                if opts.size_mode == SIZE_KEEP and si.is_image and image_exceeds_a4(pi):
                    # 图片没有「原始纸张尺寸」可言：手机原图按 72dpi 折算是 1.4 米宽的纸。
                    # 按 A4 上限等比缩小，既保留整张照片，又不产出没法打印的页面。
                    shrunk = write_image_keep_page(out, doc, pi)
                elif opts.size_mode == SIZE_KEEP:
                    # 保真路径：原样复制页面对象，零重绘、零质量损失
                    out.insert_pdf(doc, from_page=pi.page_no, to_page=pi.page_no)
                    page = out[out.page_count - 1]

                    if pi.new_crop:
                        page.set_cropbox(fitz.Rect(*pi.new_crop))
                    if pi.fix_rotation is not None:
                        page.set_rotation(pi.fix_rotation)
                else:
                    # 重绘路径：先把旋转烘焙进内容，再 contain 居中放进 A4 纵向页
                    tmp = fitz.open()
                    try:
                        tmp.insert_pdf(doc, from_page=pi.page_no, to_page=pi.page_no)
                        tp = tmp[0]
                        if pi.new_crop:
                            tp.set_cropbox(fitz.Rect(*pi.new_crop))
                        # 横向内容统一转成竖躺姿态，填满 A4 纵向纸面
                        tp.set_rotation(portrait_target_rotation(pi))
                        # 走到这里必须烘焙，否则 show_pdf_page 会忽略 rotation 属性
                        tp.remove_rotation()

                        newpage = out.new_page(width=A4_W, height=A4_H)
                        dst = fitz.Rect(margin, margin,
                                        max(margin + 1.0, A4_W - margin),
                                        max(margin + 1.0, A4_H - margin))
                        newpage.show_pdf_page(dst, tmp, 0, keep_proportion=True)
                    finally:
                        tmp.close()

                report.written_pages += 1
                acts = []
                if si.is_image:
                    acts.append("图片按其他材料处理")
                if shrunk < 1.0 - 1e-9:
                    acts.append("原始尺寸超 A4，已等比缩小到约 %d%%" % round(shrunk * 100))
                if pi.new_crop:
                    acts.append("恢复被裁切内容")
                if pi.fix_rotation is not None:
                    acts.append("旋转扶正 %d°" % pi.fix_rotation)
                if opts.size_mode == SIZE_A4_ROTATE:
                    acts.append("横向内容旋转 90° 填满" if pi.landscape else "置入 A4 纵向")
                if acts:
                    report.actions.append(
                        "第%d页 %s：%s" % (order, si.path.name, "、".join(acts))
                    )
                for w in pi.warnings:
                    report.warnings.append(("%s 第%d页" % (si.path.name, pi.page_no + 1), w))
            except Exception as exc:
                where = " + ".join(u[0].path.name for u in unit)
                report.skipped.append(
                    (where, "%s: %s" % (exc.__class__.__name__, exc))
                )
                say("  第 %d 组处理失败：%s" % (order, exc))
            if progress:
                progress(order, total)

        if report.error:
            out.close()
            return report

        if out.page_count == 0:
            out.close()
            report.error = "没有成功写入任何页面"
            return report

        # ---------- 3. 转扫描件 ----------
        if opts.scan_output:
            say("正在转为扫描件（%d dpi，不留文字层）…" % opts.scan_dpi)
            scanned = rasterize_pages(out, opts.scan_dpi)
            out.close()
            out = scanned
            report.actions.append("整份输出为扫描件：%d 页 @ %d dpi"
                                 % (out.page_count, opts.scan_dpi))

        # ---------- 4. 保存 ----------
        target = Path(opts.output) if opts.output else default_output_path(paths)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if not os.access(str(target.parent), os.W_OK):
            if opts.output is not None:
                # 用户明确指定的路径不可写，直接报错，不擅自换地方
                out.close()
                report.error = "输出目录不可写：%s" % target.parent
                return report
            fallback = writable_fallback(target.name)
            if fallback is None:
                out.close()
                report.error = "找不到可写入的输出位置"
                return report
            say("目标目录不可写，改为写入 %s" % fallback.parent)
            target = fallback
        if target.exists():
            target = unique_path(target)
        say("正在写入 %s" % target)
        try:
            out.save(str(target), garbage=3, deflate=True)
        except Exception:
            try:
                out.save(str(target), garbage=0, deflate=True)
            except Exception as exc2:
                out.close()
                report.error = "无法写入输出文件：%s" % exc2
                return report

        # 回读校验，确认文件完整
        try:
            check = fitz.open(str(target))
            n = check.page_count
            check.close()
            if n != out.page_count:
                out.close()
                report.error = "输出文件校验失败：页数不一致（%d != %d）" % (n, out.page_count)
                return report
        except Exception as exc:
            out.close()
            report.error = "输出文件校验失败：%s" % exc
            return report

        report.output = target
        report.output_size = target.stat().st_size
        out.close()
    finally:
        for d in doc_cache.values():
            try:
                d.close()
            except Exception:
                pass
        if not out.is_closed:
            try:
                out.close()
            except Exception:
                pass

    report.elapsed = (datetime.now() - started).total_seconds()
    say("完成：%d 页 -> %s（%s，耗时 %.1f 秒）"
        % (report.written_pages, report.output.name if report.output else "-",
           human_size(report.output_size), report.elapsed))

    if opts.write_report and report.output:
        try:
            report.report_path = _write_report(report, opts)
        except Exception as exc:  # 报告失败不影响主结果
            say("报告写入失败：%s" % exc)

    return report


def list_printers() -> List[Tuple[str, bool]]:
    """列出系统里可用的打印机，返回 [(名称, 是否默认)]。

    macOS / Linux 走 CUPS。踩过的坑：macOS 的 lpstat 输出会随系统语言本地化，
    中文环境下打印机名与后面的说明文字之间**没有空格**（如"PDFwriter正在接受请求，…"），
    按空格切分取不到正确名字。所以名单改用 `lpstat -e`（每行只输出名字），
    默认机则从 `lpstat -d` 里连全角冒号一起用正则取。

    Windows 没有 CUPS，改用 PowerShell 的 Win32_Printer 清单，默认机看 Default 属性。
    """
    if sys.platform == "win32":
        return _list_printers_win()

    names: List[str] = []
    try:
        r = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=6)
        names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception:
        pass

    default = ""
    try:
        r = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=6)
        m = re.search(r"[：:]\s*(\S+)\s*$", (r.stdout or "").strip())
        if m:
            default = m.group(1)
    except Exception:
        pass

    return [(name, name == default) for name in names]


def _list_printers_win() -> List[Tuple[str, bool]]:
    """Windows 下的打印机清单：PowerShell 一行一条 `名称<TAB>是否默认`。"""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Printer | ForEach-Object "
          '{ "{0}`t{1}" -f $_.Name, $_.Default }')
    out = ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        out = r.stdout or ""
    except Exception:
        pass
    names: List[str] = []
    default = ""
    for ln in out.splitlines():
        if "\t" not in ln:
            continue
        name, _, flag = ln.rpartition("\t")
        name = name.strip()
        if not name:
            continue
        names.append(name)
        if not default and flag.strip().lower() == "true":
            default = name
    return [(name, name == default) for name in names]


def print_pdf(path: Path, printer: Optional[str] = None,
              copies: int = 1) -> Tuple[bool, str]:
    """把 PDF 送到打印机。返回 (是否成功, 说明)。"""
    path = Path(path)
    if not path.exists():
        return False, "文件不存在：%s" % path
    if sys.platform == "win32":
        return _print_pdf_win(path, printer, copies)
    cmd = ["lp"]
    if printer:
        cmd += ["-d", str(printer)]
    if copies and int(copies) > 1:
        cmd += ["-n", str(int(copies))]
    cmd.append(str(path))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, "系统没有 lp 命令，无法直接打印，可用预览程序手动打印"
    except Exception as exc:
        return False, "调用打印机失败：%s" % exc
    if r.returncode == 0:
        return True, (r.stdout or "").strip() or "已发送到打印机"
    return False, ((r.stderr or r.stdout) or "").strip() or "打印命令返回失败"


def _print_pdf_win(path: Path, printer: Optional[str],
                   copies: int) -> Tuple[bool, str]:
    """Windows 打印：ShellExecute 的 printto / print 动词，交给 PDF 关联程序处理。

    局限（Windows 体系决定的，如实告知用户）：这是"打开即打印"语义，份数传不进关联程序，
    部分阅读器（如 Edge）没注册打印动词，会静默失败 —— 那就提示手动打印。
    """
    import ctypes

    verb, params = ("printto", '"%s"' % printer) if printer else ("print", None)
    try:
        # SW_HIDE = 0；返回值 > 32 表示成功，否则是 SE_ERR_* 错误码。
        ret = ctypes.windll.shell32.ShellExecuteW(0, verb, str(path), params, None, 0)
    except Exception as exc:
        return False, "调用打印机失败：%s" % exc
    if ret > 32:
        note = ("；Windows 下 --copies 由打印程序决定，可能不生效"
                if copies and int(copies) > 1 else "")
        return True, "已交给系统按 %s 打印，请留意打印机输出%s" % (
            printer if printer else "默认打印机", note)
    if ret == 31:  # SE_ERR_NOASSOC：没有关联打印处理程序
        return False, ("PDF 关联程序不支持静默打印。可打开文件手动打印；"
                       "或安装 SumatraPDF 并设为默认 PDF 阅读器后重试")
    return False, "调用打印机失败（ShellExecute 错误码 %d）" % ret


def _write_report(report: MergeReport, opts: Options) -> Optional[Path]:
    if not report.output:
        return None
    rp = report.output.with_name(report.output.stem + "_报告.txt")
    lines: List[str] = []
    lines.append("发票合并报告")
    lines.append("=" * 46)
    lines.append("生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("输出文件：%s" % report.output)
    lines.append("文件大小：%s" % human_size(report.output_size))
    lines.append("尺寸策略：%s" % SIZE_LABELS.get(opts.size_mode, opts.size_mode))
    lines.append("输出形式：%s" % ("扫描件（整页图片，%d dpi，无文字层）" % opts.scan_dpi
                                  if opts.scan_output else "矢量（保留文字层）"))
    lines.append("排序方式：%s" % SORT_LABELS.get(opts.sort_mode, opts.sort_mode))
    lines.append("自动扶正：%s" % ("开" if opts.auto_rotate else "关"))
    lines.append("恢复裁切：%s" % ("开" if opts.fix_clipping else "关"))
    lines.append("")
    lines.append("输入文件：%d 个（可用 %d 个）" % (report.total_files, report.ok_files))
    if report.image_files:
        lines.append("图片材料：%d 个（按「其他材料」处理，一张一页）" % report.image_files)
    lines.append("输入页数：%d" % report.total_pages)
    lines.append("输出页数：%d" % report.written_pages)
    lines.append("处理用时：%.1f 秒" % report.elapsed)
    lines.append("")
    if report.actions:
        lines.append("处理明细")
        lines.append("-" * 46)
        lines.extend(report.actions)
        lines.append("")
    if report.warnings:
        lines.append("提示与警告")
        lines.append("-" * 46)
        for where, msg in report.warnings:
            lines.append("[%s] %s" % (where, msg))
        lines.append("")
    if report.skipped:
        lines.append("已跳过")
        lines.append("-" * 46)
        for where, msg in report.skipped:
            lines.append("[%s] %s" % (where, msg))
        lines.append("")
    rp.write_text("\n".join(lines), encoding="utf-8")
    return rp


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="invoice_merge",
        description="发票合并小助手：把多张发票 PDF / 图片合并为一个，自动处理横竖方向与尺寸。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python invoice_merge.py                                  # 打开图形界面\n"
            "  python invoice_merge.py --folder ~/Desktop/发票\n"
            "  python invoice_merge.py --files a.pdf b.pdf -o 合并.pdf\n"
            "  python invoice_merge.py --files 发票.pdf 车票.jpg -o 合并.pdf\n"
            "  python invoice_merge.py --folder ./发票 --size keep --sort mtime\n"
        ),
    )
    src = p.add_argument_group("输入（二选一）")
    src.add_argument("--files", nargs="+", metavar="FILE",
                     help="手动指定若干 PDF 或图片文件"
                          "（图片支持 jpg/jpeg/png/bmp/tif/tiff/webp/gif/heic/heif）")
    src.add_argument("--folder", metavar="DIR", help="合并该文件夹内的所有 PDF 与图片")
    src.add_argument("--recursive", action="store_true", help="文件夹模式包含子目录")

    out = p.add_argument_group("输出")
    out.add_argument("-o", "--output", metavar="FILE", help="输出 PDF 路径")
    out.add_argument("--no-report", action="store_true", help="不生成合并报告")

    opt = p.add_argument_group("选项")
    opt.add_argument("--size", choices=list(SIZE_ORDER),
                     default=SIZE_A4_ROTATE,
                     help="页面尺寸策略：a4-rotate（默认，推荐）或 keep（保持原始尺寸）")
    opt.add_argument("--sort", choices=list(SORT_ORDER),
                     default=SORT_NATURAL, help="排序方式，默认 natural")
    opt.add_argument("--margin", type=float, default=0.0, metavar="MM",
                     help="四周留白（毫米），默认 0")
    opt.add_argument("--no-auto-rotate", action="store_true",
                     help="关闭内容方向自动扶正")
    opt.add_argument("--no-fix-clipping", action="store_true",
                     help="关闭被裁切内容的自动恢复")
    opt.add_argument("--no-organize", action="store_true",
                     help="关闭报销顺序整理（默认按 酒店发票→报销单→火车/飞机→其他→封面 排列）")
    opt.add_argument("--no-band-merge", action="store_true",
                     help="关闭拼版（默认酒店发票 / 机票火车票两张一页、其他材料三张一页且竖版转 90°，"
                          "带间画裁切虚线；--size keep 下本就不生效）")
    opt.add_argument("--no-hotel-merge", action="store_true",
                     dest="no_band_merge", help=argparse.SUPPRESS)   # 旧名，等同 --no-band-merge
    opt.add_argument("--no-scan", action="store_true",
                     help="不转扫描件，输出保留文字层的矢量 PDF（默认整页转成图片的扫描件）")
    opt.add_argument("--scan-dpi", type=int, default=300, metavar="N",
                     help="扫描件分辨率，默认 300（A4 上 2480x3508）")

    prt = p.add_argument_group("打印")
    prt.add_argument("--print", dest="do_print", action="store_true",
                     help="合并完成后送打印")
    prt.add_argument("--printer", metavar="NAME", help="指定打印机，缺省用系统默认")
    prt.add_argument("--copies", type=int, default=1, metavar="N", help="打印份数，默认 1")
    prt.add_argument("--list-printers", action="store_true", help="列出可用打印机后退出")
    opt.add_argument("--dpi", type=int, default=72, help="内容检测渲染精度，默认 72")
    opt.add_argument("--dry-run", action="store_true", help="只分析并打印诊断，不生成文件")
    opt.add_argument("-v", "--version", action="version", version="发票合并小助手 %s" % __version__)
    return p


def run_cli(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(list(argv))

    if args.list_printers:
        printers = list_printers()
        if not printers:
            print("系统里没有检测到打印机。")
            print("可以在「系统设置 → 打印机与扫描仪」里添加后重试。")
            return 1
        print("可用打印机：")
        for name, is_default in printers:
            print("   %s%s" % (name, "（默认）" if is_default else ""))
        return 0

    if not args.files and not args.folder:
        build_parser().print_help()
        print("\n提示：不带参数运行将打开图形界面。")
        return 2

    files: List[Path] = []
    if args.folder:
        folder = Path(args.folder).expanduser()
        if not folder.is_dir():
            print("错误：文件夹不存在 %s" % folder, file=sys.stderr)
            return 2
        files = list_sources(folder, args.recursive)
        if not files:
            print("错误：该文件夹内没有 PDF 或图片文件", file=sys.stderr)
            return 2
    if args.files:
        files = [Path(f).expanduser() for f in args.files] + files

    opts = Options(
        size_mode=args.size,
        sort_mode=args.sort,
        auto_rotate=not args.no_auto_rotate,
        fix_clipping=not args.no_fix_clipping,
        recursive=args.recursive,
        margin_mm=args.margin,
        analysis_dpi=args.dpi,
        output=Path(args.output).expanduser() if args.output else None,
        write_report=not args.no_report,
        organize=not args.no_organize,
        band_merge=not args.no_band_merge,
        scan_output=not args.no_scan,
        scan_dpi=max(72, args.scan_dpi),
        auto_print=args.do_print,
        printer=args.printer,
        print_copies=args.copies,
    )

    files = sort_paths(files, opts.sort_mode)

    if args.dry_run:
        print("将要合并 %d 个文件：\n" % len(files))
        infos: List[SourceInfo] = [analyze_source(f, opts) for f in files]
        if opts.organize:
            infos = organize_sources(infos)
            print("按报销顺序整理后的排列：")
        total = 0
        for si in infos:
            if si.ok:
                total += si.page_count
                hit = ("  命中「%s」" % si.category_hit) if si.category_hit else ""
                kind = "（图片）" if si.is_image else ""
                print("  [%s] %s%s  共 %d 页  %s%s"
                      % (si.category_label, si.path.name, kind, si.page_count,
                         si.orientation_summary(), hit))
                for pi in si.pages:
                    flags = []
                    if pi.fix_rotation is not None:
                        flags.append("扶正%d°" % pi.fix_rotation)
                    if pi.clipped:
                        flags.append("恢复裁切")
                    if pi.error:
                        flags.append("错误:" + pi.error)
                    print("      第%2d页 %s %.0fx%.0fpt %s"
                          % (pi.page_no + 1, pi.orientation, pi.vis_w, pi.vis_h,
                             " ".join(flags)))
            else:
                print("  [跳过] %s  原因：%s" % (si.path.name, si.error))

        out_pages = estimate_output_pages(infos, opts)
        print("\n输入 %d 页" % total, end="")
        if out_pages != total:
            print("（拼版省 %d 页）" % (total - out_pages), end="")
        print(" → 预计输出 %d 页" % out_pages)
        return 0

    def say(msg: str) -> None:
        print(msg, flush=True)

    try:
        report = merge_pdfs(files, opts, log=say)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print("\n发生未预期的错误：%s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
        traceback.print_exc()
        return 1

    if report.error:
        print("\n失败：%s" % report.error, file=sys.stderr)
        return 1
    print("\n输出：%s" % report.output)
    print("页数：%d    大小：%s" % (report.written_pages, human_size(report.output_size)))
    if report.skipped:
        print("跳过 %d 项：" % len(report.skipped))
        for where, why in report.skipped:
            print("  - %s：%s" % (where, why))
    if report.report_path:
        print("报告：%s" % report.report_path)

    if opts.auto_print:
        printer = opts.printer
        if not printer:
            found = list_printers()
            printer = next((n for n, d in found if d), found[0][0] if found else None)
        if not printer:
            print("\n未检测到打印机，已跳过打印。可在系统设置里添加打印机后重试。",
                  file=sys.stderr)
        else:
            ok, msg = print_pdf(report.output, printer, opts.print_copies)
            if ok:
                print("已送打印 → %s（%d 份）" % (printer, max(1, opts.print_copies)))
            else:
                print("打印失败：%s" % msg, file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------

def _build_gui():
    """构建图形界面，返回 (root, app)；无法启动 Tk 时返回 None。

    单独抽成函数是为了让自检能真实构建界面、操作控件（比如打开打印对话框），
    而不进入事件循环 —— GUI 代码以前完全没有测试覆盖，出过一个只有在点按钮
    时才会暴露的 NameError。
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App:
        def __init__(self, root: "tk.Tk") -> None:
            self.root = root
            self.files: List[Path] = []
            self.infos: Dict[Path, SourceInfo] = {}
            self.msg_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
            self.worker: Optional[threading.Thread] = None
            self.stop_flag = threading.Event()
            self.last_report: Optional[MergeReport] = None
            self.base_dir: Optional[Path] = None
            self._print_dialog = None
            # 上次用过的目录，跨次启动保留（存在 Application Support 里）
            self.settings = load_settings()
            _last = str(self.settings.get("last_dir", "") or "")
            self.last_dir: Optional[Path] = (
                Path(_last) if _last and Path(_last).is_dir() else None)

            root.title("发票合并小助手")
            root.geometry("880x660")
            root.minsize(760, 560)

            self.var_size = tk.StringVar(value=SIZE_A4_ROTATE)
            self.var_sort = tk.StringVar(value=SORT_NATURAL)
            self.var_recursive = tk.BooleanVar(value=False)
            self.var_autorot = tk.BooleanVar(value=True)
            self.var_fixclip = tk.BooleanVar(value=True)
            self.var_report = tk.BooleanVar(value=True)
            self.var_organize = tk.BooleanVar(value=True)
            self.var_band = tk.BooleanVar(value=True)
            self.var_scan = tk.BooleanVar(value=True)
            self.var_output = tk.StringVar(value="")
            # 输出路径框里"我们自动填的那个值"，等于它或为空时才允许随材料刷新
            self._auto_output = ""
            self.var_status = tk.StringVar(value="请选择要合并的 PDF / 图片文件或文件夹")

            self._build_widgets()
            self._pump()

        # ---------- 界面 ----------
        def _build_widgets(self) -> None:

            top = ttk.Frame(self.root, padding=(12, 12, 12, 6))
            top.pack(fill="x")
            ttk.Button(top, text="选择多个 PDF / 图片文件…", command=self.pick_files).pack(side="left")
            ttk.Button(top, text="选择一个文件夹…", command=self.pick_folder).pack(side="left", padx=(8, 0))
            ttk.Button(top, text="清空列表", command=self.clear_files).pack(side="left", padx=(8, 0))
            ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side="left", padx=(8, 0))
            ttk.Button(top, text="上移", command=lambda: self.move_one(-1)).pack(side="left", padx=(8, 0))
            ttk.Button(top, text="下移", command=lambda: self.move_one(1)).pack(side="left", padx=(4, 0))
            ttk.Button(top, text="移到最前", command=lambda: self.move_selected(-1)).pack(side="left", padx=(4, 0))
            ttk.Button(top, text="移到末尾", command=lambda: self.move_selected(1)).pack(side="left", padx=(4, 0))

            mid = ttk.Frame(self.root, padding=(12, 0, 12, 6))
            mid.pack(fill="both", expand=True)
            cols = ("name", "category", "pages", "orient", "size", "note")
            self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10,
                                      selectmode="extended")
            for c, text, width, anchor in (
                ("name", "文件", 300, "w"),
                ("category", "类别", 110, "w"),
                ("pages", "页数", 55, "center"),
                ("orient", "方向", 80, "center"),
                ("size", "页面尺寸", 130, "center"),
                ("note", "诊断", 150, "w"),
            ):
                self.tree.heading(c, text=text)
                self.tree.column(c, width=width, anchor=anchor)
            vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")

            opts = ttk.LabelFrame(self.root, text="选项", padding=(12, 8))
            opts.pack(fill="x", padx=12, pady=(0, 6))

            row1 = ttk.Frame(opts)
            row1.pack(fill="x")
            ttk.Label(row1, text="输出尺寸：").grid(row=0, column=0, sticky="w")
            for idx, mode in enumerate(SIZE_ORDER):
                ttk.Radiobutton(row1, text=SIZE_LABELS[mode], value=mode,
                                 variable=self.var_size).grid(
                    row=idx // 3, column=idx % 3 + 1, sticky="w", padx=(0, 12), pady=1)

            row2 = ttk.Frame(opts)
            row2.pack(fill="x", pady=(6, 0))
            ttk.Label(row2, text="排序：").pack(side="left")
            combo = ttk.Combobox(row2, state="readonly", width=24,
                                  values=[SORT_LABELS[m] for m in SORT_ORDER])
            combo.current(0)
            combo.pack(side="left")
            self.combo_sort = combo
            ttk.Checkbutton(row2, text="包含子文件夹", variable=self.var_recursive).pack(side="left", padx=(12, 0))
            ttk.Checkbutton(row2, text="自动扶正横躺内容", variable=self.var_autorot).pack(side="left", padx=(12, 0))
            ttk.Checkbutton(row2, text="恢复被裁切内容", variable=self.var_fixclip).pack(side="left", padx=(12, 0))
            ttk.Checkbutton(row2, text="生成报告", variable=self.var_report).pack(side="left", padx=(12, 0))

            row2b = ttk.Frame(opts)
            row2b.pack(fill="x", pady=(4, 0))
            ttk.Checkbutton(
                row2b, variable=self.var_organize, command=self._apply_organize,
                text="整理报销顺序：酒店发票 → 差旅费报销单 → 火车 / 飞机 → 其他 → 票据粘贴单封面",
            ).pack(side="left")
            ttk.Checkbutton(
                row2b, variable=self.var_band, command=self._refresh_tree,
                text="拼版：酒店 / 机票两张一页，其他材料三张一转一页",
            ).pack(side="left", padx=(12, 0))
            ttk.Checkbutton(
                row2b, variable=self.var_scan,
                text="输出为扫描件（整页图片 300dpi，不留文字层）",
            ).pack(side="left", padx=(12, 0))

            row3 = ttk.Frame(opts)
            row3.pack(fill="x", pady=(6, 0))
            ttk.Label(row3, text="输出到：").pack(side="left")
            ttk.Entry(row3, textvariable=self.var_output).pack(side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Button(row3, text="另存为…", command=self.pick_output).pack(side="left")

            act = ttk.Frame(self.root, padding=(12, 0, 12, 6))
            act.pack(fill="x")
            self.btn_run = ttk.Button(act, text="开始合并", command=self.start_merge)
            self.btn_run.pack(side="left")
            self.btn_stop = ttk.Button(act, text="取消", command=self.cancel_merge, state="disabled")
            self.btn_stop.pack(side="left", padx=(8, 0))
            self.btn_open = ttk.Button(act, text="在访达中显示", command=self.reveal_output, state="disabled")
            self.btn_open.pack(side="left", padx=(8, 0))
            self.btn_print = ttk.Button(act, text="打印…", command=self.print_output, state="disabled")
            self.btn_print.pack(side="left", padx=(8, 0))
            self.progress = ttk.Progressbar(act, mode="determinate", length=280)
            self.progress.pack(side="right")

            logf = ttk.LabelFrame(self.root, text="日志", padding=(8, 6))
            logf.pack(fill="both", expand=False, padx=12, pady=(0, 4))
            self.txt = tk.Text(logf, height=9, wrap="word", state="disabled")
            lsb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
            self.txt.configure(yscrollcommand=lsb.set)
            self.txt.pack(side="left", fill="both", expand=True)
            lsb.pack(side="right", fill="y")

            status = ttk.Label(self.root, textvariable=self.var_status, anchor="w", padding=(14, 0, 14, 10))
            status.pack(fill="x")

        # ---------- 列表 ----------
        def _refresh_tree(self) -> None:
            self.tree.delete(*self.tree.get_children())
            for i, f in enumerate(self.files, 1):
                si = self.infos.get(f)
                if si is None:
                    vals = (f.name, "…", "", "", "", "")
                elif not si.ok:
                    vals = (f.name, "-", "-", "-", "-", si.error)
                else:
                    sizes = sorted({(round(p.vis_w), round(p.vis_h)) for p in si.pages})
                    size_txt = ", ".join("%dx%d" % s for s in sizes[:2])
                    if len(sizes) > 2:
                        size_txt += " 等"
                    notes = []
                    if band_applies(si.category, self._collect_options()):
                        notes.append("拼版")
                    if any(p.clipped for p in si.pages):
                        notes.append("需恢复裁切")
                    if any(p.fix_rotation for p in si.pages):
                        notes.append("需扶正")
                    warns = sum(len(p.warnings) for p in si.pages)
                    if warns and not notes:
                        notes.append("%d 条提示" % warns)
                    vals = (f.name, si.category_label, si.page_count,
                            si.orientation_summary(), size_txt, " ".join(notes))
                self.tree.insert("", "end", iid=str(i - 1), values=vals)

        def _apply_organize(self) -> None:
            """分析完成后把列表排成最终输出顺序，让界面与结果一致。"""
            if not self.var_organize.get():
                self._refresh_tree()
                return
            infos = [self.infos.get(f) for f in self.files]
            if not infos or any(si is None for si in infos):
                return
            ok_infos = [si for si in infos if si.ok]
            failed = [si for si in infos if not si.ok]
            self.files = ([si.path for si in organize_sources(ok_infos)]
                          + [si.path for si in failed])
            self._refresh_tree()

        def _set_files(self, paths: Sequence[Path], base_dir: Optional[Path] = None) -> None:
            self.files = sort_paths(paths, self._current_sort())
            self.infos.clear()
            if base_dir is not None:
                self.base_dir = base_dir
            self._sync_output_path()
            self._refresh_tree()
            self._log("已选择 %d 个文件，正在分析…" % len(self.files))
            self._start_analysis()

        def _sync_output_path(self) -> None:
            """输出路径跟着当前材料走，别留在上一次那个文件夹里。

            只在"用户没动过这个框"时刷新：内容为空、或还是上次自动填的那个值。
            """
            current = self.var_output.get().strip()
            if current and current != self._auto_output:
                return
            if not self.files:
                self._auto_output = ""
                self.var_output.set("")
                return
            self._auto_output = str(default_output_path(
                self.files, self.base_dir.name if self.base_dir else None))
            self.var_output.set(self._auto_output)

        def _dialog_dir(self) -> str:
            """文件对话框的起始目录：优先上次用过的，其次桌面。"""
            if self.last_dir and self.last_dir.is_dir():
                return str(self.last_dir)
            desktop = Path.home() / "Desktop"
            return str(desktop if desktop.is_dir() else Path.home())

        def _remember_dir(self, path) -> None:
            """记下这次用过的目录，下次打开对话框直接停在这里。"""
            if not path:
                return
            p = Path(path)
            folder = p if p.is_dir() else p.parent
            if not folder.is_dir():
                return
            self.last_dir = folder
            self.settings["last_dir"] = str(folder)
            save_settings(self.settings)

        def pick_files(self) -> None:
            paths = filedialog.askopenfilenames(
                title="选择要合并的 PDF / 图片文件",
                initialdir=self._dialog_dir(),
                filetypes=[("发票文件", " ".join("*" + ext for ext in SOURCE_SUFFIXES)),
                           ("PDF 文件", "*.pdf"),
                           ("图片", " ".join("*" + ext for ext in IMAGE_SUFFIXES)),
                           ("所有文件", "*.*")],
            )
            if not paths:
                return
            self._remember_dir(paths[0])
            self._set_files([Path(p) for p in paths])

        def pick_folder(self) -> None:
            folder = filedialog.askdirectory(
                title="选择包含发票 PDF / 图片的文件夹",
                initialdir=self._dialog_dir(),
            )
            if not folder:
                return
            fdir = Path(folder)
            self._remember_dir(fdir)
            sources = list_sources(fdir, self.var_recursive.get())
            if not sources:
                messagebox.showwarning("没有找到 PDF 或图片",
                                       "该文件夹内没有 PDF 或图片文件。\n可勾选「包含子文件夹」后重试。")
                return
            self._set_files(sources, base_dir=fdir)

        def clear_files(self) -> None:
            self.files = []
            self.infos.clear()
            self.base_dir = None
            self._sync_output_path()
            self._refresh_tree()
            self.var_status.set("请选择要合并的 PDF / 图片文件或文件夹")

        def remove_selected(self) -> None:
            sel = sorted((int(i) for i in self.tree.selection()), reverse=True)
            for i in sel:
                if 0 <= i < len(self.files):
                    self.files.pop(i)
            self._refresh_tree()
            self._log("已移除 %d 个文件" % len(sel))

        def move_one(self, delta: int) -> None:
            """把选中项上移或下移一格，用于精确排出报销材料的顺序。"""
            sel = sorted(int(i) for i in self.tree.selection())
            if not sel:
                return
            order = sel if delta < 0 else list(reversed(sel))
            moved = []
            for i in order:
                j = i + delta
                if 0 <= j < len(self.files):
                    self.files[i], self.files[j] = self.files[j], self.files[i]
                    moved.append(j)
                else:
                    moved.append(i)
            self._refresh_tree()
            self.tree.selection_set([str(i) for i in sorted(moved)])

        def move_selected(self, direction: int) -> None:
            sel = sorted(int(i) for i in self.tree.selection())
            if not sel:
                return
            if direction < 0:
                for i in sel:
                    if i > 0:
                        self.files.insert(i - 1, self.files.pop(i))
            else:
                for i in reversed(sel):
                    if i < len(self.files) - 1:
                        self.files.insert(i + 1, self.files.pop(i))
            self._refresh_tree()
            self.tree.selection_set([str(i) for i in sel])

        def pick_output(self) -> None:
            initial = Path(self.var_output.get().strip() or "发票合并.pdf")
            start = str(initial.parent) if initial.parent.is_dir() else self._dialog_dir()
            path = filedialog.asksaveasfilename(
                title="保存合并结果",
                defaultextension=".pdf",
                initialfile=initial.name,
                initialdir=start,
                filetypes=[("PDF 文件", "*.pdf")],
            )
            if path:
                self._auto_output = ""   # 手动选过，之后换材料不再自动改
                self.var_output.set(path)
                self._remember_dir(path)

        # ---------- 分析 ----------
        def _current_sort(self) -> str:
            combo = getattr(self, "combo_sort", None)
            if combo is None:
                return SORT_NATURAL
            i = combo.current()
            return SORT_ORDER[max(0, min(i, len(SORT_ORDER) - 1))]

        def _start_analysis(self) -> None:
            files = list(self.files)
            opts = self._collect_options()      # Tk 变量只在主线程读取

            def work() -> None:
                for f in files:
                    si = analyze_source(f, opts)
                    self.msg_q.put(("analyzed", (f, si)))
                self.msg_q.put(("analysis_done", None))

            threading.Thread(target=work, daemon=True).start()

        # ---------- 合并 ----------
        def _collect_options(self) -> Options:
            return Options(
                size_mode=self.var_size.get(),
                sort_mode=self._current_sort(),
                auto_rotate=self.var_autorot.get(),
                fix_clipping=self.var_fixclip.get(),
                recursive=self.var_recursive.get(),
                output=Path(self.var_output.get()).expanduser() if self.var_output.get().strip() else None,
                write_report=self.var_report.get(),
                organize=self.var_organize.get(),
                band_merge=self.var_band.get(),
                scan_output=self.var_scan.get(),
            )

        def start_merge(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            if not self.files:
                messagebox.showinfo("还没有文件", "请先选择要合并的 PDF / 图片文件或文件夹。")
                return
            opts = self._collect_options()
            # 界面上的顺序就是最终输出顺序。用户可能用「上移 / 下移」手动调过，
            # 这里不能再排一次，否则手动调整会被冲掉。
            files = list(self.files)
            self.stop_flag.clear()
            self.last_report = None
            self.btn_run.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_open.configure(state="disabled")
            self.progress.configure(value=0, maximum=100)
            self._log("=" * 40)
            self._log("开始合并 %d 个文件" % len(files))

            def work() -> None:
                try:
                    rep = merge_pdfs(
                        files, opts,
                        log=lambda m: self.msg_q.put(("log", m)),
                        progress=lambda a, b: self.msg_q.put(("progress", (a, b))),
                        should_stop=self.stop_flag.is_set,
                    )
                    self.msg_q.put(("done", rep))
                except Exception as exc:
                    self.msg_q.put(("crash", "%s: %s" % (exc.__class__.__name__, exc)
                                    + "\n" + traceback.format_exc()))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def cancel_merge(self) -> None:
            self.stop_flag.set()
            self._log("已请求取消，正在收尾…")
            self.btn_stop.configure(state="disabled")

        def reveal_output(self) -> None:
            if self.last_report and self.last_report.output:
                subprocess.run(["open", "-R", str(self.last_report.output)], check=False)

        def _open_in_preview(self) -> None:
            if self.last_report and self.last_report.output:
                subprocess.run(["open", str(self.last_report.output)], check=False)

        def print_output(self) -> None:
            """弹出打印对话框：选打印机、填份数，然后送打印。"""
            if not (self.last_report and self.last_report.output):
                return
            # 避免重复点按弹出多个对话框
            if self._print_dialog is not None and self._print_dialog.winfo_exists():
                self._print_dialog.lift()
                self._print_dialog.focus_force()
                return
            printers = list_printers()
            if not printers:
                if messagebox.askyesno(
                    "没有可用的打印机",
                    "系统里没有检测到打印机。\n\n"
                    "可以到「系统设置 → 打印机与扫描仪」添加后再打印。\n\n"
                    "要现在用预览程序打开这份 PDF 手动打印吗？",
                ):
                    self._open_in_preview()
                return

            dlg = tk.Toplevel(self.root)
            dlg.title("打印合并结果")
            dlg.transient(self.root)
            dlg.resizable(False, False)
            self._print_dialog = dlg
            body = ttk.Frame(dlg, padding=16)
            body.pack(fill="both", expand=True)

            default_idx = next((i for i, (_n, d) in enumerate(printers) if d), 0)
            labels = ["%s%s" % (n, "（默认）" if d else "") for n, d in printers]

            ttk.Label(body, text="打印机：").grid(row=0, column=0, sticky="w")
            combo = ttk.Combobox(body, state="readonly", width=34, values=labels)
            combo.current(default_idx)
            combo.grid(row=0, column=1, sticky="w", padx=(8, 0))

            ttk.Label(body, text="份数：").grid(row=1, column=0, sticky="w", pady=(10, 0))
            var_copies = tk.StringVar(value="1")
            ttk.Spinbox(body, from_=1, to=99, width=6,
                        textvariable=var_copies).grid(row=1, column=1, sticky="w",
                                                      padx=(8, 0), pady=(10, 0))

            info = ttk.Label(body, text="文件：%s" % self.last_report.output.name,
                             foreground="#666666")
            info.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

            def close_dialog() -> None:
                self._print_dialog = None
                try:
                    dlg.destroy()
                except Exception:
                    pass

            def do_print() -> None:
                idx = combo.current()
                if idx < 0:                     # 没选中任何项时退回默认打印机
                    idx = default_idx
                name = printers[idx][0]
                try:
                    copies = max(1, int(var_copies.get()))
                except Exception:
                    copies = 1
                close_dialog()
                ok, msg = print_pdf(self.last_report.output, name, copies)
                if ok:
                    self._log("已送打印 → %s（%d 份）：%s" % (name, copies, msg))
                    messagebox.showinfo(
                        "已提交打印任务",
                        "已送到「%s」，共 %d 份。\n\n"
                        "如果长时间没出纸，请确认打印机已开机、联机、有纸。"
                        % (name, copies))
                else:
                    messagebox.showerror("打印失败", msg)

            btns = ttk.Frame(body)
            btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(16, 0))
            ttk.Button(btns, text="打印", command=do_print).pack(side="left")
            ttk.Button(btns, text="取消", command=close_dialog).pack(side="left", padx=(8, 0))
            dlg.protocol("WM_DELETE_WINDOW", close_dialog)
            dlg.grab_set()

        # ---------- 消息泵 ----------
        def _pump(self) -> None:
            try:
                while True:
                    kind, payload = self.msg_q.get_nowait()
                    if kind == "log":
                        self._log(str(payload))
                    elif kind == "progress":
                        done, total = payload  # type: ignore[misc]
                        self.progress.configure(maximum=max(1, total), value=done)
                        self.var_status.set("处理中 %d / %d" % (done, total))
                    elif kind == "analyzed":
                        f, si = payload  # type: ignore[misc]
                        self.infos[f] = si
                        self._refresh_tree()
                    elif kind == "analysis_done":
                        self._apply_organize()
                    elif kind == "done":
                        self._on_done(payload)  # type: ignore[arg-type]
                    elif kind == "crash":
                        self._log("发生未预期的错误：\n" + str(payload))
                        messagebox.showerror("出错了", str(payload).splitlines()[0])
                        self.btn_run.configure(state="normal")
                        self.btn_stop.configure(state="disabled")
            except queue.Empty:
                pass
            self.root.after(120, self._pump)

        def _on_done(self, rep: MergeReport) -> None:
            self.last_report = rep
            self.btn_run.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.progress.configure(value=self.progress["maximum"])
            if rep.ok:
                self.btn_open.configure(state="normal")
                self.btn_print.configure(state="normal")
                self.var_status.set("完成：%d 页 -> %s" % (rep.written_pages, rep.output.name))
                self._log("完成：%s" % rep.output)
                if rep.skipped:
                    self._log("跳过 %d 项，详见报告" % len(rep.skipped))
                detail = "输出：%s\n页数：%d\n大小：%s\n耗时：%.1f 秒" % (
                    rep.output, rep.written_pages, human_size(rep.output_size), rep.elapsed)
                if rep.skipped:
                    detail += "\n\n跳过 %d 项：\n" % len(rep.skipped)
                    for where, why in rep.skipped[:6]:
                        detail += "  · %s：%s\n" % (where, why)
                    if len(rep.skipped) > 6:
                        detail += "  …（详见报告文件）\n"
                messagebox.showinfo("合并完成", detail)
            else:
                self.var_status.set("失败：%s" % rep.error)
                messagebox.showerror("合并失败", rep.error or "未知错误")

        def _log(self, msg: str) -> None:
            self.txt.configure(state="normal")
            self.txt.insert("end", str(msg) + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover
        print("无法启动图形界面：%s" % exc, file=sys.stderr)
        print("可改用命令行模式，例如：python invoice_merge.py --folder ./发票", file=sys.stderr)
        return None

    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    app = App(root)
    return root, app


def run_gui() -> int:
    """打开图形界面。

    优先使用 PySide6 版界面（gui_qt.py）；没装 PySide6 或启动失败时，
    自动退回内置的 Tkinter 版，保证功能始终可用。
    """
    try:
        import gui_qt
    except Exception:
        pass
    else:
        try:
            return int(gui_qt.run())
        except Exception as exc:  # pragma: no cover
            print("PySide6 界面启动失败，改用内置界面：%s" % exc, file=sys.stderr)

    built = _build_gui()
    if built is None:
        return 1
    root, _app = built
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def _setup_stdio() -> None:
    """Windows 下管道输出默认走本地编码（中文系统是 GBK），统一成 UTF-8，
    免得调用方（agent）按 UTF-8 读到乱码。macOS / Linux 上本来就已是 UTF-8，无感。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return run_gui()
    return run_cli(args)


if __name__ == "__main__":
    _setup_stdio()
    raise SystemExit(main())
