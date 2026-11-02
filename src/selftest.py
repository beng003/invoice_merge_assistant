#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发票合并小助手 — 自检脚本。

自动构造各种发票样本（含异常文件），跑完整合并流程，再用像素与文本方向
逐页校验输出结果。用于验证程序在真实场景下的方向、尺寸与容错行为。

用法：  python selftest.py            # 全部用例
        python selftest.py --keep-dir # 保留样本目录便于人工查看
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf as fitz  # noqa: E402

import invoice_merge as im  # noqa: E402

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

A4_W, A4_H = im.A4_W, im.A4_H
INK = im.INK_THRESHOLD

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "  PASS" if ok else "**FAIL**"
    print("%s  %s%s" % (mark, name, ("  | " + detail) if detail else ""))


# --------------------------------------------------------------------------
# 样本构造
# --------------------------------------------------------------------------

def make_samples(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)

    # 1. 正常的纵向电子发票（有文本层）
    d = fitz.open()
    p = d.new_page(width=A4_W, height=A4_H)
    p.draw_rect(fitz.Rect(40, 40, A4_W - 40, A4_H - 40), color=(0.2, 0.2, 0.2), width=1)
    p.insert_text((70, 110), "电子发票（普通发票）", fontsize=22, fontname="china-s")
    p.insert_text((70, 160), "价税合计 1234.00 元", fontsize=16, fontname="china-s")
    d.save(str(dirpath / "01_纵向电子发票.pdf"))
    d.close()

    # 2. 页面本身就是横向的发票
    d = fitz.open()
    p = d.new_page(width=A4_H, height=A4_W)
    p.draw_rect(fitz.Rect(40, 40, A4_H - 40, A4_W - 40), color=(0.2, 0.2, 0.2), width=1)
    p.insert_text((70, 110), "横向版式发票 价税合计 2500.00 元", fontsize=22, fontname="china-s")
    d.save(str(dirpath / "02_横向发票.pdf"))
    d.close()

    # 3. rotation 属性被误设：页面声明纵向、rotation=90，文字因此竖躺
    d = fitz.open()
    p = d.new_page(width=A4_W, height=A4_H)
    p.draw_rect(fitz.Rect(40, 40, A4_W - 40, A4_H - 40), color=(0.2, 0.2, 0.2), width=1)
    p.insert_text((70, 110), "旋转属性异常的发票 价税合计 800.00 元", fontsize=20, fontname="china-s")
    p.set_rotation(90)
    d.save(str(dirpath / "03_旋转属性异常.pdf"))
    d.close()

    # 4. 内容横躺：纵向页面里，文字被逆时针转了 90 度
    d = fitz.open()
    p = d.new_page(width=A4_W, height=A4_H)
    p.insert_text((90, 780), "内容横躺的发票 价税合计 666.00 元", fontsize=24, fontname="china-s", rotate=90)
    p.draw_rect(fitz.Rect(60, 60, 150, 810), color=(0.2, 0.2, 0.2), width=1)
    d.save(str(dirpath / "04_内容横躺.pdf"))
    d.close()

    # 5. 小尺寸票据（不应用 A4 强行放大）
    d = fitz.open()
    p = d.new_page(width=300, height=500)
    p.insert_text((30, 60), "收据 120.00", fontsize=16, fontname="china-s")
    d.save(str(dirpath / "05_小票据.pdf"))
    d.close()

    # 6. 被 CropBox 裁掉内容的发票
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    p.draw_rect(fitz.Rect(20, 20, 575, 822), color=(0.2, 0.2, 0.2), fill=(0.95, 0.95, 0.95), width=2)
    p.insert_text((80, 120), "被裁切的发票 价税合计 999.00 元", fontsize=20, fontname="china-s")
    p.set_cropbox(fitz.Rect(120, 200, 470, 640))     # 故意裁小
    d.save(str(dirpath / "06_被裁切.pdf"))
    d.close()

    # 7. 损坏文件
    (dirpath / "07_损坏文件.pdf").write_bytes(b"%PDF-1.4\ngarbage body not a real pdf\n%%EOF\n")

    # 8. 加密文件
    d = fitz.open()
    p = d.new_page(width=200, height=200)
    p.insert_text((20, 40), "secret")
    d.save(str(dirpath / "08_加密文件.pdf"), encryption=fitz.PDF_ENCRYPT_AES_256,
           owner_pw="owner", user_pw="userpw")
    d.close()

    # 9. 零页文档
    try:
        d = fitz.open()
        d.save(str(dirpath / "09_空文档.pdf"))
        d.close()
    except Exception:
        (dirpath / "09_空文档.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    # 10/11. 自然排序样本
    for name in ("发票10.pdf", "发票2.pdf"):
        d = fitz.open()
        p = d.new_page(width=A4_W, height=A4_H)
        p.insert_text((60, 100), name.replace(".pdf", ""), fontsize=20, fontname="china-s")
        d.save(str(dirpath / name))
        d.close()

    # 非 PDF 干扰文件
    (dirpath / "说明.txt").write_text("这不是 PDF", encoding="utf-8")
    (dirpath / "~$临时.pdf").write_bytes(b"not a pdf")


# --------------------------------------------------------------------------
# 输出校验
# --------------------------------------------------------------------------

def ink_box(page, dpi: int = 60) -> Optional[Tuple[float, float, float, float]]:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    scale = 72.0 / dpi
    if np is not None:
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
        m = a < INK
        if not m.any():
            return None
        ys, xs = np.where(m)
        return (float(xs.min()) * scale, float(ys.min()) * scale,
                float(xs.max() + 1) * scale, float(ys.max() + 1) * scale)
    return None


def display_angle(page) -> Optional[int]:
    """页面文字在屏幕上呈现的角度，0 表示水平可读。"""
    votes: Dict[int, int] = {}
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            txt = "".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
            if not txt:
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            ang = (math.degrees(math.atan2(dy, dx)) + page.rotation) % 360.0
            bucket = int(round(ang / 90.0)) % 4
            votes[bucket] = votes.get(bucket, 0) + len(txt)
    if not votes:
        return None
    return max(votes, key=lambda k: votes[k]) * 90


def inspect(path: Path) -> List[dict]:
    doc = fitz.open(str(path))
    rows = []
    for i in range(doc.page_count):
        p = doc[i]
        r = p.rect
        rows.append({
            "no": i + 1,
            "w": round(r.width, 1),
            "h": round(r.height, 1),
            "rot": p.rotation,
            "landscape": r.width > r.height,
            "angle": display_angle(p),
            "ink": ink_box(p),
        })
    doc.close()
    return rows


def describe(rows: List[dict]) -> str:
    parts = []
    for r in rows:
        parts.append("P%d %sx%s rot=%s %s" % (
            r["no"], r["w"], r["h"], r["rot"], "横" if r["landscape"] else "纵"))
    return " | ".join(parts)


# --------------------------------------------------------------------------
# 图片样本与像素工具
# --------------------------------------------------------------------------

IMG_DPI = 150                      # 造图片样本用的渲染精度


def make_image_sample(path: Path, w: float, h: float, label: str,
                      rgb: Tuple[float, float, float] = (0.85, 0.12, 0.12)) -> None:
    """造一张「照片型」报销材料：先按 w×h（pt）画一页，再渲染成图片。

    环境里没有 PIL，图片一律用 fitz 的渲染器产出；渲染精度固定 IMG_DPI，
    于是图片按 DPI 换算回原始尺寸正好是 w×h，便于「保持原始尺寸」用例核对。
    顶部留一条纯色色带，合并后用像素颜色判断这张图有没有真的进来。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    pg = doc.new_page(width=w, height=h)
    pg.draw_rect(fitz.Rect(0, 0, w, h * 0.12), color=rgb, fill=rgb, width=0)
    pg.draw_rect(fitz.Rect(2, 2, w - 2, h - 2), color=(0.2, 0.2, 0.2), width=2)
    pg.insert_text((w * 0.08, h * 0.32), label,
                   fontsize=max(10.0, min(26.0, h * 0.05)), fontname="china-s")
    pg.get_pixmap(dpi=IMG_DPI).save(str(path))
    doc.close()


def sips_convert(src: Path, dst: Path, fmt: str) -> bool:
    """用 macOS 自带的 sips 转图片格式（bmp / tiff / heic 这些 fitz 不直接输出）。"""
    try:
        done = subprocess.run(["sips", "-s", "format", fmt, str(src), "--out", str(dst)],
                              capture_output=True, timeout=60)
    except Exception:
        return False
    return done.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def add_exif_orientation(path: Path, orientation: int = 6) -> bool:
    """给 JPEG 插一段 APP1 Exif，写进 Orientation 标签（MM 字节序）。

    EXIF 方向是判断「照片有没有被转正」的唯一信号：同一张竖图，只多一个
    orientation=6，正确实现应当把它横过来显示（内容顶部转到右侧）。
    """
    raw = path.read_bytes()
    if raw[:2] != b"\xff\xd8":
        return False
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8)
    ifd = (struct.pack(">H", 1)
           + struct.pack(">HHI", 0x0112, 3, 1)      # 1 个条目：Orientation，SHORT
           + struct.pack(">HH", orientation, 0)     # 值 6（右转 90°），内联存放
           + struct.pack(">I", 0))                  # 没有下一个 IFD
    payload = b"Exif\x00\x00" + tiff + ifd
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    path.write_bytes(raw[:2] + app1 + raw[2:])
    return True


def image_page_size(path: Path) -> Tuple[float, float]:
    """图片按 DPI 换算回 PDF 后应有的原始尺寸（pt）。"""
    doc = fitz.open(str(path))
    try:
        r = doc[0].rect
        return (round(r.width, 1), round(r.height, 1))
    finally:
        doc.close()


def color_share(page, rgb: Tuple[float, float, float],
                dpi: int = 60, tol: int = 60) -> Optional[float]:
    """页面上接近某个颜色的像素占比；对应的那张图被丢掉时会掉到 0。"""
    if np is None:
        return None
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
    a = a[:, : pix.width * 3].reshape(pix.height, pix.width, 3).astype(np.int16)
    d = np.abs(a - np.array([int(round(c * 255)) for c in rgb], dtype=np.int16)).max(axis=2)
    return float((d <= tol).sum()) / float(pix.width * pix.height)


def color_center(page, rgb: Tuple[float, float, float],
                 dpi: int = 50, tol: int = 60) -> Optional[Tuple[float, float]]:
    """指定颜色的重心（归一化到 0~1），用来判断图片方向转得对不对。"""
    if np is None:
        return None
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
    a = a[:, : pix.width * 3].reshape(pix.height, pix.width, 3).astype(np.int16)
    d = np.abs(a - np.array([int(round(c * 255)) for c in rgb], dtype=np.int16)).max(axis=2)
    ys, xs = np.where(d <= tol)
    if len(xs) == 0:
        return None
    return (float(xs.mean()) / pix.width, float(ys.mean()) / pix.height)


def band_ink_count(path: Path, page_no: int, bands: int, dpi: int = 50) -> int:
    """数一页里有几条带子有内容 —— 拼版页应当每条带都有图，少一条就是漏图。

    每条带上下各让出 8%，避开骑在分界线上的裁切虚线，否则虚线自己就把
    空带子算成有内容了。
    """
    doc = fitz.open(str(path))
    try:
        pix = doc[page_no].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        h = pix.height
        if np is None:
            return -1
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, pix.stride)[:, : pix.width]
        filled = 0
        for i in range(bands):
            top = int(h * (i + 0.08) / bands)
            bottom = int(h * (i + 1 - 0.08) / bands)
            seg = a[top:max(top + 1, bottom), :]
            if seg.size and seg.min() < INK:
                filled += 1
        return filled
    finally:
        doc.close()


def keep_disables_band_note(win) -> bool:
    """选「保持原始尺寸」后，表格笔记列不该再出现「拼版」。用完还原界面。"""
    path = Path("酒店发票笔记测试.pdf")
    si = im.SourceInfo(path=path, page_count=1,
                       pages=[im.PageInfo(source=path, page_no=0,
                                          vis_w=A4_W, vis_h=A4_H)],
                       category=im.CATEGORY_HOTEL)
    win.files, win.infos = [path], {path: si}
    try:
        win.chk_band.setChecked(True)
        win.size_buttons[im.SIZE_A4_ROTATE].setChecked(True)
        win._refresh_table()
        banded = "拼版" in win.table.item(0, 5).text()
        win.size_buttons[im.SIZE_KEEP].setChecked(True)
        win._refresh_table()
        return banded and "拼版" not in win.table.item(0, 5).text()
    finally:
        win.size_buttons[im.SIZE_A4_ROTATE].setChecked(True)
        win.files, win.infos = [], {}
        win._refresh_table()


def output_path_follows_folder(workdir: Path, select, get_path, set_path) -> List[bool]:
    """换文件夹后输出路径要跟着走；用户手输过的路径要保留。

    select(文件列表) 只驱动"选文件 → 刷新输出路径"这一段，不起分析线程。
    目录必须真实存在 —— default_output_path 会把不存在的父目录退回主目录。
    """
    files = []
    for name in ("甲", "乙", "丙"):
        d = workdir / ("输出路径" + name)
        d.mkdir(exist_ok=True)
        files.append(d / ("出差%s.pdf" % name))
    select([files[0]])
    first = get_path().startswith(str(files[0].parent))
    select([files[1]])
    second = get_path().startswith(str(files[1].parent))
    manual = str(workdir / "手工指定.pdf")
    set_path(manual)
    select([files[2]])
    return [first, second, get_path() == manual]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def main() -> int:
    keep = "--keep-dir" in sys.argv
    workdir = Path(tempfile.mkdtemp(prefix="inv_selftest_"))
    samples = workdir / "样本"
    print("样本目录：%s\n" % samples)
    make_samples(samples)
    print("样本文件：")
    for f in sorted(samples.iterdir()):
        print("   %s" % f.name)
    print()

    # ================= 用例 A：保真模式，文件夹输入 =================
    print("-" * 70)
    print("用例 A：保真模式（默认），文件夹输入，自然排序")
    print("-" * 70)
    opts = im.Options(scan_output=False, size_mode=im.SIZE_KEEP, sort_mode=im.SORT_NATURAL,
                      auto_rotate=True, fix_clipping=True,
                      output=workdir / "A_保真模式.pdf", write_report=True)
    files = im.sort_paths(im.list_pdfs(samples), opts.sort_mode)
    print("排序结果：%s" % ", ".join(f.name for f in files))
    rep = im.merge_pdfs(files, opts, log=lambda m: print("   " + m))
    check("A1 合并成功生成文件", rep.ok, str(rep.output))
    rows = inspect(rep.output) if rep.ok else []
    print("   输出：%s" % describe(rows))

    check("A2 输出页数为 8（01-06 共 6 页 + 发票2/发票10 各 1 页）",
          len(rows) == 8, "实际 %d" % len(rows))
    check("A3 01 纵向页保持纵向", rows[0]["landscape"] is False, describe(rows[:1]))
    check("A4 02 横向页保持横向", rows[1]["landscape"] is True, describe(rows[1:2]))
    check("A5 03 旋转属性异常页被扶正为纵向且文字水平",
          rows[2]["landscape"] is False and rows[2]["angle"] == 0,
          "方向=%s 文字角度=%s" % ("横" if rows[2]["landscape"] else "纵", rows[2]["angle"]))
    check("A6 04 内容横躺页被扶正为横向且文字水平",
          rows[3]["landscape"] is True and rows[3]["angle"] == 0,
          "方向=%s 文字角度=%s" % ("横" if rows[3]["landscape"] else "纵", rows[3]["angle"]))
    check("A7 05 小票据保持原始尺寸",
          abs(rows[4]["w"] - 300) < 2 and abs(rows[4]["h"] - 500) < 2, describe(rows[4:5]))
    check("A8 06 被裁切页恢复内容（高度明显大于裁剪框 440pt）",
          rows[5]["h"] > 600, "实际高 %.0fpt" % rows[5]["h"])
    check("A9 07 损坏文件被跳过", any("07_损坏文件" in w for w, _ in rep.skipped),
          str(rep.skipped[:1]))
    check("A10 08 加密文件被跳过", any("08_加密文件" in w for w, _ in rep.skipped))
    check("A11 09 空文档被跳过", any("09_空文档" in w for w, _ in rep.skipped))
    check("A12 非 PDF 文件未被收进列表",
          not any(f.suffix != ".pdf" for f in files))
    check("A13 临时文件 ~$ 未被收进列表",
          not any(f.name.startswith("~$") for f in files))
    check("A14 自然排序：发票2 在 发票10 之前",
          [f.name for f in files].index("发票2.pdf") < [f.name for f in files].index("发票10.pdf"))
    check("A15 生成了合并报告", rep.report_path is not None and rep.report_path.exists())

    # ================= 用例 C：单独指定文件 + 关闭自动扶正 =================
    print()
    print("-" * 70)
    print("用例 C：手动指定文件列表，关闭自动扶正（验证选项生效）")
    print("-" * 70)
    picked = [samples / "01_纵向电子发票.pdf", samples / "04_内容横躺.pdf",
              samples / "02_横向发票.pdf"]
    opts_c = im.Options(scan_output=False, size_mode=im.SIZE_KEEP, sort_mode=im.SORT_NAME,
                        auto_rotate=False, fix_clipping=True,
                        output=workdir / "C_不扶正.pdf")
    rep_c = im.merge_pdfs(picked, opts_c, log=lambda m: print("   " + m))
    rows_c = inspect(rep_c.output) if rep_c.ok else []
    print("   输出：%s" % describe(rows_c))
    check("C1 合并成功", rep_c.ok)
    check("C2 按给定顺序输出 3 页", len(rows_c) == 3)
    check("C3 关闭扶正后，横躺内容保持横躺（文字角度=270）",
          rows_c[1]["angle"] == 270, "角度=%s" % rows_c[1]["angle"])

    # ================= 用例 D：单文件多页 =================
    print()
    print("-" * 70)
    print("用例 D：把 A 的结果再合并（多页文件作为输入）")
    print("-" * 70)
    opts_d = im.Options(size_mode=im.SIZE_KEEP, output=workdir / "D_二次合并.pdf")
    rep_d = im.merge_pdfs([rep.output], opts_d, log=lambda m: print("   " + m))
    check("D1 多页文件可再次合并", rep_d.ok and rep_d.written_pages == 8,
          "页数=%d" % rep_d.written_pages)

    # ================= 用例 E：异常输入 =================
    print()
    print("-" * 70)
    print("用例 E：异常输入处理")
    print("-" * 70)
    opts_e = im.Options(output=workdir / "E_异常.pdf")
    rep_e = im.merge_pdfs([samples / "07_损坏文件.pdf"], opts_e, log=lambda m: print("   " + m))
    check("E1 全部文件不可用时明确报错且不生成文件",
          (not rep_e.ok) and rep_e.output is None, rep_e.error)

    rep_e2 = im.merge_pdfs([samples / "不存在的文件.pdf"], opts_e,
                           log=lambda m: print("   " + m))
    check("E2 文件不存在时报错", not rep_e2.ok, rep_e2.error)

    rep_e3 = im.merge_pdfs([], opts_e, log=lambda m: print("   " + m))
    check("E3 空列表时报错", not rep_e3.ok, rep_e3.error)

    # ================= 用例 F：统一 A4 纵向 + 横向内容旋转 =================
    print()
    print("-" * 70)
    print("用例 F：统一 A4 纵向 + 横向内容旋转（横向材料转 90° 填满纸面）")
    print("-" * 70)
    opts_f = im.Options(scan_output=False, size_mode=im.SIZE_A4_ROTATE, sort_mode=im.SORT_NATURAL,
                        fix_clipping=True, band_merge=False,
                        output=workdir / "F_纵向旋转.pdf")
    rep_f = im.merge_pdfs(files, opts_f, log=lambda m: print("   " + m))
    rows_f = inspect(rep_f.output) if rep_f.ok else []
    print("   输出：%s" % describe(rows_f))
    check("F1 合并成功", rep_f.ok)
    check("F2 每页均为 A4 纵向",
          bool(rows_f) and all(abs(r["w"] - A4_W) < 2 and abs(r["h"] - A4_H) < 2 for r in rows_f),
          describe(rows_f))
    check("F3 横向源页（02）转成竖躺姿态，文字角度 90（内容顶部朝右）",
          len(rows_f) > 1 and rows_f[1]["angle"] == 90, "角度=%s" % (rows_f[1]["angle"] if len(rows_f) > 1 else "N/A"))
    check("F4 rotation 属性异常的横向页（03）同样转成竖躺",
          len(rows_f) > 2 and rows_f[2]["angle"] == 90, "角度=%s" % (rows_f[2]["angle"] if len(rows_f) > 2 else "N/A"))
    check("F5 纵向源页（01）保持文字水平",
          len(rows_f) > 0 and rows_f[0]["angle"] == 0)
    check("F6 内容横躺页（04）在该模式下被扶正为水平",
          len(rows_f) > 3 and rows_f[3]["angle"] == 0, "角度=%s" % (rows_f[3]["angle"] if len(rows_f) > 3 else "N/A"))

    # 关掉扫描件开关时，内容应当完整落在 A4 纸面内、不被裁掉
    inside = [r["no"] for r in rows_f
              if r["ink"] is not None and (
                  r["ink"][0] < -1 or r["ink"][1] < -1
                  or r["ink"][2] > r["w"] + 1 or r["ink"][3] > r["h"] + 1)]
    check("F7 内容完整落在 A4 页面内（无裁切）", not inside, "越界页 %s" % inside)

    # ================= 用例 G：拼音排序 =================
    print()
    print("-" * 70)
    print("用例 G：拼音排序")
    print("-" * 70)
    pool = [samples / "报销单.pdf", samples / "电子发票.pdf", samples / "aaa凭证.pdf",
            samples / "出差申请.pdf"]
    for p in pool:
        d = fitz.open()
        pg = d.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 100), p.stem, fontsize=20, fontname="china-s")
        d.save(str(p))
        d.close()
    ordered = [p.name for p in im.sort_paths(pool, im.SORT_PINYIN)]
    print("   拼音排序结果：%s" % " < ".join(ordered))
    check("G1 拼音排序可用", im.PINYIN_AVAILABLE, "locale 未提供中文 collation 时该项会失败")
    check("G2 中文按拼音：报销单 < 出差申请 < 电子发票",
          ordered.index("报销单.pdf") < ordered.index("出差申请.pdf") < ordered.index("电子发票.pdf"))
    check("G3 拉丁字母开头的排在中文之后", ordered[-1] == "aaa凭证.pdf")

    check("G4 自然排序仍把 发票2 排在 发票10 之前",
          [f.name for f in im.sort_paths(files, im.SORT_NATURAL)].index("发票2.pdf")
          < [f.name for f in im.sort_paths(files, im.SORT_NATURAL)].index("发票10.pdf"))

    # ================= 用例 H：报销顺序整理 + 多材料拼版 =================
    print()
    print("-" * 70)
    print("用例 H：报销顺序整理与酒店 / 机票 / 其他材料拼版")
    print("-" * 70)
    biz = workdir / "报销"
    biz.mkdir(exist_ok=True)

    def make_biz(name: str, lines: List[str]) -> None:
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        y = 90.0
        for ln in lines:
            pg.insert_text((60, y), ln, fontsize=18, fontname="china-s")
            y += 40
        doc.save(str(biz / name))
        doc.close()

    # 文件名故意取成拼音序与业务序都不一致
    make_biz("A差旅费报销单.pdf", ["差旅费报销单", "业务编码 6502CL202600414"])
    make_biz("B1桔子水晶酒店发票.pdf", ["桔子水晶酒店", "住宿费 2 晚", "价税合计 854.04 元"])
    make_biz("B2海滨酒店住宿发票.pdf", ["海滨酒店", "住宿费 1 晚", "价税合计 420.00 元"])
    make_biz("C希尔顿酒店结账单.pdf", ["希尔顿酒店 结账单", "住宿 2 晚", "合计 1200.00 元"])
    make_biz("D机票行程单.pdf", ["航空运输电子客票行程单", "机票 汕头 - 北京"])
    make_biz("E火车票.pdf", ["中国铁路 电子客票", "高铁 北京南 - 汕头"])
    make_biz("F票据粘贴单封面.pdf", ["票据粘贴单", "报销凭证封面"])
    make_biz("G出差说明.pdf", ["出差情况说明材料"])

    def page_texts(path) -> List[str]:
        doc = fitz.open(str(path))
        texts = [doc[i].get_text().strip().replace("\n", " ") for i in range(doc.page_count)]
        doc.close()
        return texts

    biz_files = im.sort_paths(im.list_pdfs(biz), im.SORT_PINYIN)
    opts_h = im.Options(scan_output=False, size_mode=im.SIZE_A4_ROTATE, sort_mode=im.SORT_PINYIN,
                        organize=True, band_merge=True,
                        output=workdir / "H_报销整理.pdf")
    rep_h = im.merge_pdfs(biz_files, opts_h, log=lambda m: print("   " + m))
    check("H1 合并成功", rep_h.ok)
    texts = page_texts(rep_h.output) if rep_h.ok else []
    print("   输出页序：")
    for i, t in enumerate(texts, 1):
        print("     P%d  %s" % (i, t[:46]))

    def cut_lines(path, page_no: int) -> List[float]:
        """取某页上所有裁切虚线的 y 坐标（横向、贯穿大半个纸面）。"""
        doc = fitz.open(str(path))
        out_y: List[float] = []
        try:
            for it in doc[page_no].get_drawings():
                r = it["rect"]
                if not it.get("dashes") or it["items"][0][0] != "l":
                    continue
                if r.height < 1 and r.width > doc[page_no].rect.width * 0.6:
                    out_y.append(round(r.y0, 1))
        finally:
            doc.close()
        return sorted(out_y)

    check("H2 酒店 / 机票 / 其他材料各自拼版，总页数 5", len(texts) == 5,
          "实际 %d 页" % len(texts))
    check("H3 首位是酒店发票拼版页（含两张，且不含结账单）",
          len(texts) > 0 and "桔子水晶酒店" in texts[0] and "海滨酒店" in texts[0]
          and "结账单" not in texts[0],
          texts[0][:44] if texts else "")
    check("H4 第二位是差旅费报销单（不拼版，独占一页）",
          len(texts) > 1 and "差旅费报销单" in texts[1] and "铁路" not in texts[1])
    check("H5 机票行程单与火车票拼在同一页",
          len(texts) > 2 and "行程单" in texts[2] and ("铁路" in texts[2] or "火车" in texts[2]),
          texts[2][:44] if len(texts) > 2 else "")
    check("H6 酒店结账单与出差说明同属其他材料，拼在同一页",
          len(texts) > 3 and "结账单" in texts[3] and "出差情况说明" in texts[3],
          texts[3][:44] if len(texts) > 3 else "")
    check("H7 票据粘贴单封面排在最后",
          len(texts) > 0 and "票据粘贴单" in texts[-1])
    check("H8 酒店发票只占半页（页面高度不变，内容各占上下半区）",
          rep_h.ok and all(abs(r["h"] - A4_H) < 2 for r in inspect(rep_h.output)))
    check("H9 每张拼版页都画了裁切虚线，两拼一页只有一条",
          rep_h.ok and all(len(cut_lines(rep_h.output, i)) == 1 for i in (0, 2, 3)),
          str([len(cut_lines(rep_h.output, i)) for i in (0, 2, 3)] if rep_h.ok else "无输出"))
    check("H10 不拼版的整页材料上没有虚线",
          rep_h.ok and not cut_lines(rep_h.output, 1) and not cut_lines(rep_h.output, 4))

    # 关掉拼版 → 页数应恢复为 8
    opts_h2 = im.Options(scan_output=False, size_mode=im.SIZE_KEEP, organize=True, band_merge=False,
                         output=workdir / "H2_不拼版.pdf")
    rep_h2 = im.merge_pdfs(biz_files, opts_h2, log=lambda m: None)
    check("H11 关闭拼版后各材料各占一页，共 8 页",
          rep_h2.ok and rep_h2.written_pages == 8, "实际 %d" % rep_h2.written_pages)

    # 保持原始尺寸 → 拼版让位于保真，页数不变
    opts_hk = im.Options(scan_output=False, size_mode=im.SIZE_KEEP, organize=True, band_merge=True,
                         output=workdir / "H4_保真不拼版.pdf")
    rep_hk = im.merge_pdfs(biz_files, opts_hk, log=lambda m: None)
    check("H11b 保持原始尺寸下不拼版，仍是 8 页",
          rep_hk.ok and rep_hk.written_pages == 8, "实际 %d" % rep_hk.written_pages)

    # 关掉整理 → 顺序应回到拼音序
    opts_h3 = im.Options(scan_output=False, size_mode=im.SIZE_KEEP, organize=False, band_merge=True,
                         sort_mode=im.SORT_PINYIN, output=workdir / "H3_不整理.pdf")
    rep_h3 = im.merge_pdfs(biz_files, opts_h3, log=lambda m: None)
    texts3 = page_texts(rep_h3.output) if rep_h3.ok else []
    check("H12 关闭整理后按拼音序排列（首页不是酒店）",
          bool(texts3) and "酒店" not in texts3[0], texts3[0][:30] if texts3 else "")

    # ================= 用例 T：其他材料三合一旋转 =================
    print()
    print("-" * 70)
    print("用例 T：其他材料三张一转拼一页，第四张单独成页")
    print("-" * 70)
    tiny = workdir / "其他材料三合一"
    tiny.mkdir(exist_ok=True)
    for i in range(1, 5):
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        y = 70.0
        while y < 270.0:                      # 铺成竖长一条，才谈得上"转过来更划算"
            pg.insert_text((60, y), "结账单 明细行 项目 %d-%.0f" % (i, y),
                           fontsize=12, fontname="china-s")
            y += 22.0
        pg.insert_text((60, 300), "编号 NO-000%d" % i, fontsize=12, fontname="china-s")
        doc.save(str(tiny / ("结账单%d.pdf" % i)))
        doc.close()

    def vertical_share(path, page_no: int) -> float:
        """该页上竖排（旋转过的）文字占多少比例。"""
        doc = fitz.open(str(path))
        tot = vert = 0
        try:
            for blk in doc[page_no].get_text("dict").get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for line in blk.get("lines", []):
                    n = len("".join(s.get("text", "") for s in line["spans"]).strip())
                    if not n:
                        continue
                    tot += n
                    if abs(line["dir"][0]) < 0.1:
                        vert += n
        finally:
            doc.close()
        return vert / float(tot) if tot else 0.0

    opts_t = im.Options(scan_output=False, size_mode=im.SIZE_A4_ROTATE, organize=True, band_merge=True,
                        output=workdir / "T_三合一.pdf")
    tiny_files = im.sort_paths(im.list_pdfs(tiny), im.SORT_NATURAL)
    rep_t = im.merge_pdfs(tiny_files[:3], opts_t, log=lambda m: None)
    check("T1 三份其他材料合成一页", rep_t.ok and rep_t.written_pages == 1,
          "实际 %s" % (rep_t.written_pages if rep_t.ok else rep_t.error))
    if rep_t.ok:
        ys = cut_lines(rep_t.output, 0)
        check("T2 三等分产生两条裁切虚线", len(ys) == 2, str(ys))
        check("T3 虚线等距落在 1/3、2/3 处",
              all(abs(y - A4_H * k / 3) < 2 for k, y in zip((1, 2), ys)), str(ys))
        check("T4 竖版内容被转成横躺（顶部朝右）",
              vertical_share(rep_t.output, 0) > 0.9,
              "竖排占比 %.0f%%" % (vertical_share(rep_t.output, 0) * 100))
        _dt = fitz.open(str(rep_t.output))
        _txt = _dt[0].get_text()
        _dt.close()
        check("T5 三份内容都在", all(("NO-000%d" % i) in _txt for i in (1, 2, 3)))

    rep_t4 = im.merge_pdfs(tiny_files, im.Options(scan_output=False,
        size_mode=im.SIZE_A4_ROTATE, organize=True, band_merge=True,
        output=workdir / "T4_四份.pdf"), log=lambda m: None)
    check("T6 第四份不硬塞，单独占一页（3+1）",
          rep_t4.ok and rep_t4.written_pages == 2, "实际 %s" % rep_t4.written_pages)
    check("T7 落单那份不缩小（竖排占比为 0）",
          rep_t4.ok and vertical_share(rep_t4.output, 1) == 0.0)

    check("T8 电子登机凭证归其他材料而非交通票据",
          im.classify_document("个人登机凭证 (1).pdf")[0] == im.CATEGORY_OTHER)
    check("T9 出差申请审批单归其他材料",
          im.classify_document("汕头出差.pdf", "国核信息-出差申请审批单")[0]
          == im.CATEGORY_OTHER)
    check("T10 真机票仍归交通票据，不被登机凭证规则误伤",
          im.classify_document("机票行程单.pdf")[0] == im.CATEGORY_TRAVEL)

    # 单页旋转路径（差旅费报销单 / 封面）与拼版路径（其他材料）的摆向必须一致，
    # 否则整本材料翻着看时方向会跳。
    rep_l = im.merge_pdfs([samples / "02_横向发票.pdf"], im.Options(scan_output=False,
        size_mode=im.SIZE_A4_ROTATE, band_merge=False,
        output=workdir / "T_横版单页.pdf"), log=lambda m: None)
    ang_l = ang_b = None
    if rep_l.ok:
        _dl = fitz.open(str(rep_l.output)); ang_l = display_angle(_dl[0]); _dl.close()
    _db = fitz.open(str(rep_t.output)); ang_b = display_angle(_db[0]); _db.close()
    check("T11 单页旋转与拼版旋转摆向一致（内容顶部同侧朝右）",
          rep_l.ok and ang_l == ang_b == 90, "单页=%s 拼版=%s" % (ang_l, ang_b))

    # ================= 用例 I：打印接口 =================
    print()
    print("-" * 70)
    print("用例 I：打印接口（只验证接口健壮性，不真的出纸）")
    print("-" * 70)
    printers = im.list_printers()
    print("   系统打印机：%s" % (printers if printers else "未检测到（属正常）"))
    check("I1 打印机枚举返回列表且不抛异常", isinstance(printers, list))
    ok_p, msg_p = im.print_pdf(workdir / "不存在的文件.pdf")
    check("I2 文件不存在时返回失败而非抛异常", ok_p is False and bool(msg_p), msg_p)

    # ================= 用例 J：图形界面冒烟 =================
    print()
    print("-" * 70)
    print("用例 J：图形界面冒烟测试（构建界面 + 打开打印对话框）")
    print("-" * 70)
    built = None
    try:
        built = im._build_gui()
    except Exception as exc:
        print("   构建界面失败：%s" % exc)
    if built is None:
        print("   [跳过] 当前环境没有可用的图形界面（无显示器或 Tk 不可用）")
    else:
        import tkinter as tk
        root, app = built
        root.update()
        check("J1 图形界面可正常构建", True)

        gui_pdf = workdir / "J_打印样本.pdf"
        _d = fitz.open()
        _p = _d.new_page(width=A4_W, height=A4_H)
        _p.insert_text((60, 100), "打印测试样本", fontsize=20, fontname="china-s")
        _d.save(str(gui_pdf))
        _d.close()

        app.last_report = im.MergeReport(output=gui_pdf)
        app.btn_print.configure(state="normal")

        before = set(root.winfo_children())
        err = ""
        try:
            app.print_output()          # 这里曾因 _ttk 作用域错误抛 NameError
            root.update()
        except Exception as exc:
            err = "%s: %s" % (exc.__class__.__name__, exc)
        dialogs = [w for w in root.winfo_children()
                   if w not in before and isinstance(w, tk.Toplevel)]
        check("J2 打印对话框能正常打开（不抛异常）",
              (not err) and bool(dialogs),
              err or ("已弹出「%s」" % dialogs[0].title() if dialogs else "对话框未出现"))

        combos: List[list] = []

        def _walk(widget) -> None:
            for child in widget.winfo_children():
                if child.winfo_class() == "TCombobox":
                    combos.append(list(child["values"]))
                _walk(child)

        if dialogs:
            _walk(dialogs[0])
            dialogs[0].destroy()
        check("J3 打印机下拉框已填充设备名",
              bool(combos) and bool(combos[0]),
              str(combos[0]) if combos else "未找到下拉框")

        check("J4 主界面所有按钮均已创建",
              all(hasattr(app, n) for n in
                  ("btn_run", "btn_stop", "btn_open", "btn_print", "progress")))

        # 点一次「打印」按钮走完整个链路。真实打印用桩替换掉，
        # 免得跑个自检就往打印机塞纸；弹窗也要压住，否则会卡在模态框上。
        from tkinter import messagebox as _mb
        _old_info, _old_err = _mb.showinfo, _mb.showerror
        _mb.showinfo = lambda *a, **k: None
        _mb.showerror = lambda *a, **k: None
        real_print_pdf = im.print_pdf
        sent: List[tuple] = []

        def _stub_print(path, printer=None, copies=1):
            sent.append((Path(path).name, printer, copies))
            return True, "自检桩：未真实打印"

        im.print_pdf = _stub_print

        def _find_button(widget, label: str):
            for child in widget.winfo_children():
                if child.winfo_class() == "TButton" and child["text"] == label:
                    return child
                found = _find_button(child, label)
                if found is not None:
                    return found
            return None

        try:
            before2 = set(root.winfo_children())
            app.print_output()
            root.update()
            dialogs2 = [w for w in root.winfo_children()
                        if w not in before2 and isinstance(w, tk.Toplevel)]
            clicked = False
            if dialogs2:
                btn = _find_button(dialogs2[0], "打印")
                if btn is not None:
                    btn.invoke()
                    root.update()
                    clicked = True
                if dialogs2[0].winfo_exists():
                    dialogs2[0].destroy()
            check("J5 点「打印」按钮能走完整个打印链路",
                  clicked and bool(sent),
                  "调用参数：%s" % (str(sent[0]) if sent else "未触发打印"))
        finally:
            im.print_pdf = real_print_pdf
            _mb.showinfo, _mb.showerror = _old_info, _old_err
        root.destroy()

    # ================= 用例 K：对话框记忆上次目录 =================
    print()
    print("-" * 70)
    print("用例 K：文件对话框记忆上次用过的目录")
    print("-" * 70)
    cfg = im.settings_path()
    backup = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    try:
        if cfg.exists():
            cfg.unlink()
        built_k = None
        try:
            built_k = im._build_gui()
        except Exception as exc:
            print("   构建界面失败：%s" % exc)
        if built_k is None:
            print("   [跳过] 当前环境没有可用的图形界面")
        else:
            root_k, app_k = built_k
            root_k.update()
            check("K1 没有历史记录时回退到可用目录",
                  Path(app_k._dialog_dir()).is_dir(), app_k._dialog_dir())

            target = biz
            app_k._remember_dir(target)
            check("K2 用过之后写入配置文件",
                  cfg.exists() and str(target) in cfg.read_text(encoding="utf-8"),
                  str(cfg))
            root_k.destroy()

            # 重建界面 = 模拟下次启动
            root_k2, app_k2 = im._build_gui()
            root_k2.update()
            check("K3 下次启动仍停在上次目录",
                  app_k2.last_dir == target, str(app_k2.last_dir))

            app_k2._remember_dir(target / "报销单.pdf")
            check("K4 传文件路径时记的是它所在的目录",
                  app_k2.last_dir == target, str(app_k2.last_dir))

            app_k2.last_dir = Path("/不存在的目录/xyz")
            check("K5 记录失效时安全回退到可用目录",
                  Path(app_k2._dialog_dir()).is_dir(), app_k2._dialog_dir())

            def tk_select(fs, app=app_k2):
                app.files = fs
                app.base_dir = None
                app._sync_output_path()

            got_k = output_path_follows_folder(
                workdir, tk_select, lambda: app_k2.var_output.get(),
                lambda v: app_k2.var_output.set(v))
            check("K6 换文件夹后输出路径跟着换，不会写回上一个文件夹",
                  got_k[0] and got_k[1], str(got_k))
            check("K7 手输过的输出路径不被新材料覆盖", got_k[2], str(got_k))
            root_k2.destroy()
    finally:
        if backup is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_text(backup, encoding="utf-8")

    # ================= 用例 L：PySide6 界面 =================
    print()
    print("-" * 70)
    print("用例 L：PySide6 界面冒烟测试")
    print("-" * 70)
    try:
        import gui_qt
    except Exception as exc:
        print("   [跳过] 未安装 PySide6 或界面模块导入失败：%s" % exc)
    else:
        try:
            win = gui_qt.build_window()
        except Exception as exc:
            check("L1 主窗口可构建", False, "%s: %s" % (exc.__class__.__name__, exc))
            win = None
        if win is not None:
            check("L1 主窗口可构建", True)

            check("L2 界面上默认选中「统一 A4 纵向（横向页旋转填满）」",
                  win.size_buttons[im.SIZE_A4_ROTATE].isChecked(),
                  im.SIZE_LABELS[im.SIZE_A4_ROTATE])
            check("L2b 输出尺寸只剩两项，「推荐」落在 a4-rotate 上",
                  list(win.size_buttons) == [im.SIZE_KEEP, im.SIZE_A4_ROTATE]
                  and "推荐" in im.SIZE_LABELS[im.SIZE_A4_ROTATE]
                  and "推荐" not in im.SIZE_LABELS[im.SIZE_KEEP],
                  " / ".join(im.SIZE_LABELS[m] for m in win.size_buttons))
            check("L3 界面选项映射到 Options 时尺寸正确",
                  win._options().size_mode == im.SIZE_A4_ROTATE,
                  win._options().size_mode)
            check("L4 关键控件齐备",
                  all(hasattr(win, n) for n in
                      ("table", "btn_run", "btn_cancel", "btn_reveal", "btn_print",
                       "progress", "log", "sort_combo", "path_edit", "count_label",
                       "chk_band")))
            check("L4b 拼版开关默认开且映射到 Options",
                  win.chk_band.isChecked() and win._options().band_merge is True)
            check("L4c 切到「保持原始尺寸」时表格不再标注拼版",
                  keep_disables_band_note(win),
                  "选中保持原始尺寸后笔记列仍出现「拼版」")

            def qt_select(fs, w=win):
                w.files = fs
                w._sync_output_path()

            got_l = output_path_follows_folder(
                workdir, qt_select, lambda: win.path_edit.text(),
                lambda v: win.path_edit.setText(v))
            check("L4d 换文件夹后输出路径跟着换，不会写回上一个文件夹",
                  got_l[0] and got_l[1], str(got_l))
            check("L4e 手输过的输出路径不被新材料覆盖", got_l[2], str(got_l))
            win.files, win.infos = [], {}
            win.path_edit.setText("")
            check("L5 Lucide 图标可加载并着色",
                  not gui_qt.icon("play", gui_qt.C.ICON).isNull(),
                  "assets/icons/play.svg")
            dark = gui_qt.THEMES["dark"]
            check("L6 深色主题配色符合规范（低饱和蓝灰底 + 深灰卡片）",
                  dark["BG"] == "#0f1114" and dark["SURFACE"] == "#1c1f26"
                  and dark["TEXT_DIM"] == "#8a8f98",
                  "BG=%s SURFACE=%s TEXT_DIM=%s"
                  % (dark["BG"], dark["SURFACE"], dark["TEXT_DIM"]))

            # 打印对话框（用桩替换真实打印，避免自检出纸）
            gpdf = workdir / "L_打印样本.pdf"
            _d = fitz.open()
            _p = _d.new_page(width=A4_W, height=A4_H)
            _p.insert_text((60, 100), "打印测试样本", fontsize=20, fontname="china-s")
            _d.save(str(gpdf))
            _d.close()
            win.last_report = im.MergeReport(output=gpdf)
            try:
                dlg = gui_qt.PrintDialog(gpdf, im.list_printers() or [("测试打印机", True)], win)
                check("L7 打印对话框可构建并读到打印机列表",
                      dlg.combo.count() >= 1, "打印机数 %d" % dlg.combo.count())
                dlg.close()
            except Exception as exc:
                check("L7 打印对话框可构建并读到打印机列表", False,
                      "%s: %s" % (exc.__class__.__name__, exc))

            # ---------- 拖放添加：模拟一次「文件 + 文件夹 + 非 PDF + 重复项」 ----------
            try:
                from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
                from PySide6.QtGui import QDropEvent
            except Exception as exc:
                print("   [跳过] PySide6 事件类不可用：%s" % exc)
            else:
                import time as _time
                drop_dir = workdir / "拖放样本"
                drop_dir.mkdir(exist_ok=True)
                for nm in ("拖放甲.pdf", "拖放乙.pdf"):
                    _dd = fitz.open()
                    _dd.new_page(width=A4_W, height=A4_H)
                    _dd.save(str(drop_dir / nm))
                    _dd.close()
                (drop_dir / "说明.txt").write_text("非 PDF", encoding="utf-8")

                # QDropEvent 只保存 QMimeData 的裸指针，Python 侧必须自己保住
                # 引用，否则临时对象被回收后事件里访问的是悬垂指针（段错误）。
                mimes: List[object] = []

                def _drop_event() -> "QDropEvent":
                    mime = QMimeData()
                    mime.setUrls([
                        QUrl.fromLocalFile(str(drop_dir / "拖放甲.pdf")),
                        QUrl.fromLocalFile(str(drop_dir)),
                        QUrl.fromLocalFile(str(drop_dir / "说明.txt")),
                        QUrl.fromLocalFile(str(drop_dir / "拖放甲.pdf")),   # 重复项
                    ])
                    mimes.append(mime)
                    return QDropEvent(QPointF(10, 10), Qt.CopyAction, mime,
                                      Qt.LeftButton, Qt.NoModifier)

                win.files, win.infos = [], {}
                win.dropEvent(_drop_event())
                check("L8 拖入一次可添加多个 PDF（文件 + 文件夹），去重并跳过非 PDF",
                      {p.name for p in win.files} == {"拖放甲.pdf", "拖放乙.pdf"},
                      str([p.name for p in win.files]))

                n_before = len(win.files)
                win.dropEvent(_drop_event())
                check("L9 重复拖入自动去重，不产生重复项",
                      len(win.files) == n_before,
                      str([p.name for p in win.files]))

                # 拖入后自动开始分析，等后台线程跑完确认结果齐全
                if win.analyzer is not None:
                    win.analyzer.wait(15000)
                    _qapp = gui_qt.QApplication.instance()
                    for _ in range(100):
                        _qapp.processEvents()
                        if win.infos and all(f in win.infos for f in win.files):
                            break
                        _time.sleep(0.05)
                    check("L10 拖入的文件自动开始分析",
                          bool(win.infos) and all(
                              win.infos.get(f) is not None and win.infos[f].ok
                              for f in win.files),
                          str([p.name for p in win.files]))
                    check("L11 窗口已开启拖放接收",
                          win.acceptDrops() and not win.table.acceptDrops()
                          and not win.log.acceptDrops()
                          and not win.path_edit.acceptDrops(),
                          "window=%s table=%s log=%s path=%s" % (
                              win.acceptDrops(), win.table.acceptDrops(),
                              win.log.acceptDrops(), win.path_edit.acceptDrops()))

            win.close()

    # ================= 用例 M：界面不会被压到裁字 =================
    print()
    print("-" * 70)
    print("用例 M：界面高度分配（防止控件被压扁导致文字裁切）")
    print("-" * 70)
    try:
        import gui_qt
        from PySide6.QtWidgets import QApplication, QCheckBox, QRadioButton, QFrame
    except Exception as exc:
        print("   [跳过] 未安装 PySide6：%s" % exc)
    else:
        import sys as _sys
        _app = QApplication.instance() or QApplication(_sys.argv[:1])
        win = gui_qt.build_window()
        # 按用户实际反馈的场景取值：窗口高度不足以放下全部内容
        win.resize(1084, 984)
        win.show()
        for _ in range(14):
            _app.processEvents()

        cards = [f for f in win.findChildren(QFrame) if f.objectName() == "card"]
        squeezed = [c for c in cards if c.height() < c.minimumSizeHint().height()]
        check("M1 没有卡片被压到低于内容所需高度",
              bool(cards) and not squeezed,
              "共 %d 个卡片，其中 %d 个被压缩" % (len(cards), len(squeezed)))

        controls = win.findChildren(QRadioButton) + win.findChildren(QCheckBox)
        clipped = [c for c in controls if c.height() < c.fontMetrics().height()]
        check("M2 单选 / 复选文字都能完整显示",
              bool(controls) and not clipped,
              "共 %d 个控件，其中 %d 个高度不足" % (len(controls), len(clipped)))

        need = sum(win.table.columnWidth(i) for i in range(win.table.columnCount()))
        avail = win.table.viewport().width()
        check("M3 表格列宽不溢出（不出现横向滚动条）",
              need <= avail, "列宽合计 %d / 可视 %d" % (need, avail))

        win.close()

    # ================= 用例 N：酒店票内容完整性 =================
    print()
    print("-" * 70)
    print("用例 N：横向小票型酒店发票拼版后下半截不被切掉")
    print("-" * 70)
    hotel_dir = workdir / "酒店横向小票"
    hotel_dir.mkdir(exist_ok=True)
    KEY = "价税合计"

    def make_landscape_hotel(name: str, number: str) -> None:
        """造 600x400 的横向小票，关键信息贴在页面最底部。

        这类票正文占满整页，早先按页高 50% 硬切的做法必然把底部切掉。
        """
        doc = fitz.open()
        pg = doc.new_page(width=600, height=400)
        pg.insert_text((30, 60), "电子发票（增值税专用发票）", fontsize=15, fontname="china-s")
        pg.insert_text((30, 115), "发票号码：%s" % number, fontsize=11, fontname="china-s")
        pg.insert_text((30, 175), "项目名称：*生产生活服务*住宿费", fontsize=11, fontname="china-s")
        pg.insert_text((30, 235), "购买方：国核信息科技有限公司", fontsize=11, fontname="china-s")
        pg.insert_text((30, 385), "%s（小写）￥2678.31" % KEY, fontsize=12, fontname="china-s")
        doc.save(str(hotel_dir / name))
        doc.close()

    make_landscape_hotel("酒店A.pdf", "26112000003833356171")
    make_landscape_hotel("酒店B.pdf", "26112000003869656831")

    opts_n = im.Options(scan_output=False, size_mode=im.SIZE_A4_ROTATE, organize=True, band_merge=True,
                        output=workdir / "N_酒店完整.pdf")
    rep_n = im.merge_pdfs(im.sort_paths(im.list_pdfs(hotel_dir), im.SORT_PINYIN),
                          opts_n, log=lambda m: print("   " + m))
    check("N1 合并成功", rep_n.ok)

    merged_text = ""
    if rep_n.ok:
        _dn = fitz.open(str(rep_n.output))
        merged_text = "\n".join(_dn[i].get_text() for i in range(_dn.page_count))
        page_count_n = _dn.page_count
        _dn.close()
    else:
        page_count_n = 0

    check("N2 两张横向酒店票拼成一页", page_count_n == 1, "实际 %d 页" % page_count_n)
    check("N3 两张票的关键信息都在（下半截没被切）",
          merged_text.count(KEY) == 2,
          "命中「%s」%d 次" % (KEY, merged_text.count(KEY)))
    check("N4 两张票的号码都在",
          "26112000003833356171" in merged_text and "26112000003869656831" in merged_text,
          "号码缺失" if merged_text else "无文本")

    # ================= 用例 P：输出为扫描件 =================
    print()
    print("-" * 70)
    print("用例 P：默认输出为扫描件（整页图片、无文字层）")
    print("-" * 70)

    def scan_profile(path):
        """返回 (页数, 每页图片数, 首图宽, 首图高, 首页文字数, 首页尺寸)。"""
        doc = fitz.open(str(path))
        pg = doc[0]
        imgs = pg.get_images(full=True)
        w = h = 0
        if imgs:
            info = doc.extract_image(imgs[0][0])
            w, h = info["width"], info["height"]
        prof = (doc.page_count, len(imgs), w, h, len(pg.get_text().strip()),
                (round(pg.rect.width, 1), round(pg.rect.height, 1)))
        doc.close()
        return prof

    opts_p = im.Options(size_mode=im.SIZE_A4_ROTATE, organize=True,
                        output=workdir / "P_扫描件.pdf")
    check("P1 默认就是扫描件（Options 默认值）",
          opts_p.scan_output is True and opts_p.scan_dpi == 300)
    rep_p = im.merge_pdfs(biz_files, opts_p, log=lambda m: None)
    prof = scan_profile(rep_p.output) if rep_p.ok else None
    check("P2 合并成功且页数不变", rep_p.ok and prof and prof[0] == rep_h.written_pages,
          str(prof[0]) if prof else rep_p.error)
    check("P3 每页就是一张整页图片", bool(prof) and prof[1] == 1, "每页图片数 %s" % (prof[1] if prof else "-"))
    check("P4 没有文字层", bool(prof) and prof[4] == 0, "文字 %d 字" % (prof[4] if prof else -1))
    check("P5 分辨率约 300dpi（A4 宽 2480 上下）",
          bool(prof) and abs(prof[2] - A4_W / 72 * 300) <= 3, "%dx%d" % (prof[2], prof[3]) if prof else "-")
    check("P6 纸张尺寸仍是 A4 纵向，没被改小",
          bool(prof) and abs(prof[5][0] - A4_W) < 1 and abs(prof[5][1] - A4_H) < 1,
          str(prof[5]) if prof else "-")

    rep_v = im.merge_pdfs(biz_files, im.Options(
        size_mode=im.SIZE_A4_ROTATE, organize=True, scan_output=False,
        output=workdir / "P_矢量.pdf"), log=lambda m: None)
    prof_v = scan_profile(rep_v.output) if rep_v.ok else None
    check("P7 关掉开关则保留文字层的矢量输出",
          bool(prof_v) and prof_v[4] > 0 and prof_v[1] == 0,
          "文字 %d 字 / 图片 %d 张" % (prof_v[4], prof_v[1]) if prof_v else rep_v.error)

    rep_low = im.merge_pdfs(biz_files, im.Options(
        size_mode=im.SIZE_A4_ROTATE, organize=True, scan_dpi=150,
        output=workdir / "P_150dpi.pdf"), log=lambda m: None)
    prof_low = scan_profile(rep_low.output) if rep_low.ok else None
    check("P8 分辨率可调（150dpi 约 1240 宽）",
          bool(prof_low) and abs(prof_low[2] - A4_W / 72 * 150) <= 3,
          "%dx%d" % (prof_low[2], prof_low[3]) if prof_low else "-")
    size_300 = rep_p.output.stat().st_size if rep_p.ok else 0
    size_150 = rep_low.output.stat().st_size if rep_low.ok else 0
    check("P9 低分辨率确实更小（给体积留了退路）", 0 < size_150 < size_300,
          "150dpi %s / 300dpi %s" % (im.human_size(size_150), im.human_size(size_300)))

    # ================= 用例 O：深色 / 浅色主题 =================
    print()
    print("-" * 70)
    print("用例 O：深色 / 浅色主题切换")
    print("-" * 70)
    try:
        import gui_qt
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QColor
    except Exception as exc:
        print("   [跳过] 未安装 PySide6：%s" % exc)
    else:
        import sys as _sys2
        _app2 = QApplication.instance() or QApplication(_sys2.argv[:1])
        cfg2 = im.settings_path()
        backup2 = cfg2.read_text(encoding="utf-8") if cfg2.exists() else None
        orig_theme = gui_qt.C.name          # 跑完要还原，别污染后续用例
        try:
            win2 = gui_qt.build_window()
            win2.resize(1000, 880)
            win2.show()
            for _ in range(10):
                _app2.processEvents()

            check("O1 深色与浅色两套主题都已定义",
                  set(gui_qt.THEMES) == {"dark", "light"},
                  str(sorted(gui_qt.THEMES)))

            win2.apply_theme("dark")
            dark_bg, dark_text = gui_qt.C.BG, gui_qt.C.TEXT
            check("O2 深色主题：底深字浅",
                  QColor(dark_bg).lightness() < 60 and QColor(dark_text).lightness() > 180,
                  "BG=%s TEXT=%s" % (dark_bg, dark_text))

            win2.apply_theme("light")
            light_bg, light_text = gui_qt.C.BG, gui_qt.C.TEXT
            check("O3 浅色主题：底浅字深",
                  QColor(light_bg).lightness() > 200 and QColor(light_text).lightness() < 60,
                  "BG=%s TEXT=%s" % (light_bg, light_text))

            check("O4 两套主题的底色确实不同", dark_bg != light_bg,
                  "%s vs %s" % (dark_bg, light_bg))
            check("O5 避开纯黑纯白",
                  not any(c.lower() in ("#000000", "#ffffff")
                          for c in (dark_bg, light_bg, dark_text, light_text)),
                  "已检查 4 个色值")

            check("O6 主题选择写入设置以便下次沿用",
                  im.load_settings().get("theme") == "light",
                  "settings.theme=%s" % im.load_settings().get("theme"))

            for _ in range(3):
                win2.toggle_theme()
            check("O7 反复来回切换不抛异常",
                  gui_qt.C.name in gui_qt.THEMES, gui_qt.C.name)
            win2.close()
        finally:
            gui_qt.C.apply(orig_theme)
            if backup2 is None:
                cfg2.unlink(missing_ok=True)
            else:
                cfg2.write_text(backup2, encoding="utf-8")

    # ================= 用例 IM：图片作为报销材料 =================
    print()
    print("-" * 70)
    print("用例 IM：图片作为报销材料（图片一律归「其他材料」）")
    print("-" * 70)

    api_names = ("IMAGE_SUFFIXES", "is_image_path", "is_source_path", "list_sources")
    missing_api = [n for n in api_names if not hasattr(im, n)]
    check("IM0 核心已提供图片接口（%s）" % " / ".join(api_names),
          not missing_api, "缺少 %s" % missing_api)
    if missing_api:
        print("   [跳过] 图片用例要等核心接口落地后再跑")
    else:
        img_dir = workdir / "图片样本"
        img_dir.mkdir(exist_ok=True)
        photo_png = img_dir / "01_照片凭证.png"
        photo_jpg = img_dir / "02_照片凭证.jpg"
        make_image_sample(photo_png, 600, 400, "报销凭证照片 A", (0.85, 0.12, 0.12))
        make_image_sample(photo_jpg, 400, 600, "报销凭证照片 B", (0.12, 0.2, 0.85))
        (img_dir / "说明.txt").write_text("这不是图片", encoding="utf-8")
        mixed_pdf = img_dir / "09_混入的其他材料.pdf"
        _d = fitz.open()
        _p = _d.new_page(width=A4_W, height=A4_H)
        _p.insert_text((60, 100), "混在图片文件夹里的其他材料", fontsize=18, fontname="china-s")
        _d.save(str(mixed_pdf))
        _d.close()

        suffixes = tuple(str(s).lower() for s in im.IMAGE_SUFFIXES)
        check("IM1 图片后缀表覆盖常见拍照 / 扫描格式",
              all(s in suffixes for s in (".jpg", ".jpeg", ".png", ".bmp", ".tif",
                                          ".tiff", ".webp", ".gif", ".heic", ".heif")),
              str(suffixes))
        check("IM2 图片判定与材料判定分开：图片为真、PDF 为假，两者都算可用材料",
              im.is_image_path(photo_png) and im.is_image_path(photo_jpg)
              and not im.is_image_path(mixed_pdf)
              and im.is_source_path(photo_png) and im.is_source_path(photo_jpg)
              and im.is_source_path(mixed_pdf),
              "is_image png=%s jpg=%s pdf=%s / is_source png=%s pdf=%s" % (
                  im.is_image_path(photo_png), im.is_image_path(photo_jpg),
                  im.is_image_path(mixed_pdf), im.is_source_path(photo_png),
                  im.is_source_path(mixed_pdf)))

        opts_i = im.Options(scan_output=False, output=workdir / "IM_图片分析.pdf")
        si_png = im.analyze_source(photo_png, opts_i)
        si_jpg = im.analyze_source(photo_jpg, opts_i)
        print("   图片分析：%s" % " / ".join(
            "%s → %d 页 is_image=%s %s" % (s.path.name, s.page_count,
                                           getattr(s, "is_image", None), s.category_label)
            for s in (si_png, si_jpg)))
        check("IM3 图片可被分析：1 页、is_image=True、页面尺寸可用",
              si_png.ok and si_png.page_count == 1
              and getattr(si_png, "is_image", None) is True
              and bool(si_png.pages) and si_png.pages[0].vis_w > 0 and si_png.pages[0].vis_h > 0
              and si_jpg.ok and si_jpg.page_count == 1
              and getattr(si_jpg, "is_image", None) is True,
              "png：ok=%s 页数=%s is_image=%s / jpg：ok=%s 页数=%s is_image=%s" % (
                  si_png.ok, si_png.page_count, getattr(si_png, "is_image", None),
                  si_jpg.ok, si_jpg.page_count, getattr(si_jpg, "is_image", None)))
        check("IM4 图片一律归「其他材料」，且带一条命中说明",
              si_png.category == im.CATEGORY_OTHER and bool(si_png.category_hit)
              and si_jpg.category == im.CATEGORY_OTHER and bool(si_jpg.category_hit),
              "png=%s/%s jpg=%s/%s" % (si_png.category, si_png.category_hit or "-",
                                       si_jpg.category, si_jpg.category_hit or "-"))
        si_pdf = im.analyze_source(mixed_pdf, opts_i)
        check("IM5 PDF 的分析结果不带图片标记（is_image=False）",
              si_pdf.ok and getattr(si_pdf, "is_image", None) is False,
              "ok=%s is_image=%s 页数=%s" % (si_pdf.ok, getattr(si_pdf, "is_image", None),
                                             si_pdf.page_count))

        listed = im.list_sources(img_dir)
        listed_names = {p.name for p in listed}
        only_pdf = im.list_pdfs(img_dir)
        check("IM6 文件夹模式：list_sources 收到图片与 PDF，list_pdfs 仍只收 PDF",
              {"01_照片凭证.png", "02_照片凭证.jpg", "09_混入的其他材料.pdf"} <= listed_names
              and [p.name for p in only_pdf] == ["09_混入的其他材料.pdf"],
              "list_sources=%s / list_pdfs=%s" % (sorted(listed_names),
                                                  [p.name for p in only_pdf]))
        check("IM7 非图片文件（.txt）不进材料列表",
              "说明.txt" not in listed_names
              and not any(p.suffix.lower() == ".txt" for p in listed),
              str(sorted(listed_names)))

        # ---- 两张图片：默认选项下应当拼版成一页 ----
        band_dir = workdir / "图片拼版"
        band_a = band_dir / "照片甲.png"
        band_b = band_dir / "照片乙.png"
        make_image_sample(band_a, 600, 400, "照片 甲", (0.85, 0.12, 0.12))
        make_image_sample(band_b, 600, 400, "照片 乙", (0.12, 0.2, 0.85))
        band_files = im.sort_paths(im.list_sources(band_dir), im.SORT_NATURAL)
        logs_band: List[str] = []
        opts_band = im.Options(scan_output=False, size_mode=im.SIZE_A4_ROTATE,
                               organize=True, band_merge=True,
                               output=workdir / "IM_图片拼版.pdf")
        rep_band = im.merge_pdfs(band_files, opts_band, log=logs_band.append)
        rows_band = inspect(rep_band.output) if rep_band.ok else []
        print("   输出：%s" % describe(rows_band))
        check("IM8 两张图片默认拼版成一页（a4-rotate + band_merge）",
              rep_band.ok and len(rows_band) == 1,
              "页数=%s" % (len(rows_band) if rep_band.ok else rep_band.error))
        cut_band = cut_lines(rep_band.output, 0) if rep_band.ok else []
        check("IM9 两图拼版页画了一条裁切虚线", rep_band.ok and len(cut_band) == 1,
              str(cut_band))
        red_share = blue_share = None
        if rep_band.ok and np is not None:
            _db = fitz.open(str(rep_band.output))
            red_share = color_share(_db[0], (0.85, 0.12, 0.12))
            blue_share = color_share(_db[0], (0.12, 0.2, 0.85))
            _db.close()
        if np is None:
            print("   [跳过] 没有 numpy，做不了像素级判定（IM10 / IM11）")
        else:
            check("IM10 两张图片的内容都在同一页上（红色带与蓝色带同时出现）",
                  red_share is not None and blue_share is not None
                  and red_share > 0.002 and blue_share > 0.002,
                  "红 %s / 蓝 %s" % (red_share, blue_share))
            check("IM11 拼版页的两条带都有内容（没有哪张图被挤没）",
                  rep_band.ok and band_ink_count(rep_band.output, 0, 2) == 2,
                  "有内容的带数 %s" % (band_ink_count(rep_band.output, 0, 2)
                                       if rep_band.ok else "-"))

        # ---- 三张图片：一页三条带 ----
        band3_dir = workdir / "图片拼版三张"
        for _i, _rgb in enumerate(((0.85, 0.12, 0.12), (0.12, 0.6, 0.2),
                                   (0.12, 0.2, 0.85)), 1):
            make_image_sample(band3_dir / ("照片%d.png" % _i), 600, 400,
                              "照片 %d" % _i, _rgb)
        band3_files = im.sort_paths(im.list_sources(band3_dir), im.SORT_NATURAL)
        rep_b3 = im.merge_pdfs(band3_files, im.Options(
            scan_output=False, output=workdir / "IM_图片三合一.pdf"))
        cut_b3 = cut_lines(rep_b3.output, 0) if rep_b3.ok else []
        check("IM12 三张图片合排一页、两条裁切虚线（拼版页数正确）",
              rep_b3.ok and rep_b3.written_pages == 1 and len(cut_b3) == 2,
              "页数=%s 虚线=%s" % (rep_b3.written_pages if rep_b3.ok else rep_b3.error,
                                   cut_b3))

        # ---- 保持原始尺寸：每张独占一页、尺寸按 DPI 换算 ----
        keep_dir = workdir / "图片保真"
        keep_big = keep_dir / "保真甲.png"
        keep_small = keep_dir / "保真乙.png"
        make_image_sample(keep_big, 600, 400, "保真 甲", (0.85, 0.12, 0.12))
        make_image_sample(keep_small, 300, 500, "保真 乙", (0.12, 0.2, 0.85))
        opts_ikeep = im.Options(scan_output=False, size_mode=im.SIZE_KEEP,
                                band_merge=True, organize=True,
                                output=workdir / "IM_图片保真.pdf")
        rep_keep = im.merge_pdfs([keep_big, keep_small], opts_ikeep)
        rows_keep = inspect(rep_keep.output) if rep_keep.ok else []
        print("   输出：%s" % describe(rows_keep))
        exp_big = image_page_size(keep_big)
        exp_small = image_page_size(keep_small)
        check("IM13 保持原始尺寸：每张图片独占一页（不参与拼版）",
              rep_keep.ok and len(rows_keep) == 2,
              "页数=%s" % (len(rows_keep) if rep_keep.ok else rep_keep.error))
        got_big = (rows_keep[0]["w"], rows_keep[0]["h"]) if len(rows_keep) > 0 else None
        got_small = (rows_keep[1]["w"], rows_keep[1]["h"]) if len(rows_keep) > 1 else None
        check("IM14 保持原始尺寸：页面尺寸=图片原始 pt（按 DPI 换算，容差 2pt）",
              got_big is not None and got_small is not None
              and abs(got_big[0] - exp_big[0]) < 2 and abs(got_big[1] - exp_big[1]) < 2
              and abs(got_small[0] - exp_small[0]) < 2
              and abs(got_small[1] - exp_small[1]) < 2,
              "期望 %s / %s，实际 %s / %s" % (exp_big, exp_small, got_big, got_small))
        check("IM15 保持原始尺寸：不同尺寸的图片不被统一成 A4",
              len(rows_keep) == 2
              and not any(abs(r["w"] - A4_W) < 2 and abs(r["h"] - A4_H) < 2
                          for r in rows_keep)
              and abs(rows_keep[0]["w"] - rows_keep[1]["w"]) > 5,
              describe(rows_keep))

        # ---- 坏图片不能拖垮整批 ----
        bad_dir = workdir / "图片容错"
        make_image_sample(bad_dir / "好照片1.png", 600, 400, "好照片 1", (0.85, 0.12, 0.12))
        make_image_sample(bad_dir / "好照片2.png", 600, 400, "好照片 2", (0.12, 0.2, 0.85))
        (bad_dir / "坏照片.jpg").write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"not a real jpeg body" * 8)
        bad_files = im.sort_paths(im.list_sources(bad_dir), im.SORT_NATURAL)
        rep_bad = im.merge_pdfs(bad_files, im.Options(
            scan_output=False, size_mode=im.SIZE_KEEP,
            output=workdir / "IM_图片容错.pdf"))
        check("IM16 坏图片被跳过，其余图片仍合并成功（不拖垮整批）",
              rep_bad.ok and rep_bad.written_pages == 2
              and any("坏照片" in w for w, _ in rep_bad.skipped),
              "页数=%s 跳过=%s" % (rep_bad.written_pages if rep_bad.ok else rep_bad.error,
                                   rep_bad.skipped))

        # ---- 图片强制归其他材料，不被文件名关键词抢走 ----
        force_dir = workdir / "图片分类"
        hotel_img = force_dir / "酒店发票.png"
        flight_img = force_dir / "机票行程单.jpg"
        make_image_sample(hotel_img, 600, 400, "酒店 住宿费 854.04", (0.85, 0.12, 0.12))
        make_image_sample(flight_img, 600, 400, "航空运输电子客票行程单",
                          (0.12, 0.2, 0.85))
        si_hotel = im.analyze_source(hotel_img, opts_i)
        si_flight = im.analyze_source(flight_img, opts_i)
        check("IM17 文件名带「酒店」「机票」的图片仍强制归其他材料（分类不被关键词抢走）",
              getattr(si_hotel, "is_image", None) is True
              and si_hotel.category == im.CATEGORY_OTHER
              and getattr(si_flight, "is_image", None) is True
              and si_flight.category == im.CATEGORY_OTHER,
              "酒店图=%s/%s 机票图=%s/%s" % (
                  si_hotel.category, si_hotel.category_hit or "-",
                  si_flight.category, si_flight.category_hit or "-"))

        # ---- bmp / tiff / heic：需要真实格式，macOS 用 sips 生成 ----
        if sys.platform != "darwin":
            print("   [跳过] 非 macOS 环境造不出真实的 bmp / tiff / heic 样本")
        else:
            src_png = img_dir / "_临时转格式.png"
            make_image_sample(src_png, 500, 350, "票据照片", (0.2, 0.6, 0.2))
            fmt_paths = []
            for _fmt, _name in (("bmp", "03_票据照片.bmp"), ("tiff", "04_票据照片.tiff")):
                _dst = img_dir / _name
                if sips_convert(src_png, _dst, _fmt):
                    fmt_paths.append(_dst)
            src_png.unlink(missing_ok=True)
            fmt_ok = []
            for _p in fmt_paths:
                _si = im.analyze_source(_p, opts_i)
                fmt_ok.append(_si.ok and _si.page_count == 1
                              and getattr(_si, "is_image", None) is True
                              and _si.category == im.CATEGORY_OTHER)
            check("IM18 bmp / tiff 图片同样可分析与归类（macOS 用 sips 生成真实格式）",
                  len(fmt_paths) == 2 and all(fmt_ok),
                  "生成 %s / 分析 %s" % ([p.name for p in fmt_paths], fmt_ok))
            if fmt_paths:
                rep_fmt = im.merge_pdfs(fmt_paths, im.Options(
                    scan_output=False, size_mode=im.SIZE_KEEP,
                    output=workdir / "IM_bmp_tiff.pdf"))
                check("IM19 bmp / tiff 图片可正常合并",
                      rep_fmt.ok and rep_fmt.written_pages == len(fmt_paths),
                      "页数=%s" % (rep_fmt.written_pages if rep_fmt.ok else rep_fmt.error))

            heic = img_dir / "05_手机照片.heic"
            src_png2 = img_dir / "_临时heic.png"
            make_image_sample(src_png2, 500, 350, "HEIC 手机照片", (0.2, 0.2, 0.6))
            got_heic = sips_convert(src_png2, heic, "heic")
            src_png2.unlink(missing_ok=True)
            if not got_heic:
                print("   [跳过] sips 生成不出 .heic 样本")
            else:
                si_heic = im.analyze_source(heic, opts_i)
                check("IM20 HEIC 在 macOS 上自动转 PNG 后可分析（1 页 / 图片 / 其他材料）",
                      si_heic.ok and si_heic.page_count == 1
                      and getattr(si_heic, "is_image", None) is True
                      and si_heic.category == im.CATEGORY_OTHER,
                      "ok=%s 错误=%s is_image=%s 页数=%s 类别=%s" % (
                          si_heic.ok, si_heic.error or "-",
                          getattr(si_heic, "is_image", None), si_heic.page_count,
                          si_heic.category))
                rep_heic = im.merge_pdfs([heic], im.Options(
                    scan_output=False, size_mode=im.SIZE_KEEP,
                    output=workdir / "IM_heic.pdf"))
                check("IM21 HEIC 图片可正常合并出一页",
                      rep_heic.ok and rep_heic.written_pages == 1,
                      "页数=%s" % (rep_heic.written_pages if rep_heic.ok
                                   else rep_heic.error))

        # ---- EXIF 方向：同一张竖图，只差一个 orientation=6 ----
        exif_dir = workdir / "图片方向"
        ctrl_jpg = exif_dir / "方向对照.jpg"
        rot_jpg = exif_dir / "方向旋转.jpg"
        make_image_sample(ctrl_jpg, 300, 500, "方向测试", (0.85, 0.12, 0.12))
        shutil.copyfile(ctrl_jpg, rot_jpg)
        exif_done = add_exif_orientation(rot_jpg, 6)
        rep_exif = im.merge_pdfs([ctrl_jpg, rot_jpg], im.Options(
            scan_output=False, size_mode=im.SIZE_KEEP,
            output=workdir / "IM_图片方向.pdf"))
        rows_exif = inspect(rep_exif.output) if rep_exif.ok else []
        print("   输出：%s" % describe(rows_exif))
        check("IM22 EXIF orientation=6 的 JPEG 已按方向转正（对照组竖、该组横）",
              exif_done and rep_exif.ok and len(rows_exif) == 2
              and rows_exif[0]["landscape"] is False and rows_exif[1]["landscape"] is True,
              "对照=%s 旋转=%s" % (
                  ("横" if rows_exif[0]["landscape"] else "纵") if len(rows_exif) > 0 else "-",
                  ("横" if rows_exif[1]["landscape"] else "纵") if len(rows_exif) > 1 else "-"))
        center_red = None
        if rep_exif.ok and len(rows_exif) == 2 and np is not None:
            _dx = fitz.open(str(rep_exif.output))
            center_red = color_center(_dx[1], (0.85, 0.12, 0.12))
            _dx.close()
        if np is None:
            print("   [跳过] 没有 numpy，做不了色带位置判定（IM23）")
        else:
            check("IM23 转正后原本在顶部的色带落到页面右侧（像素真的转了，不只是换尺寸）",
                  center_red is not None and center_red[0] > 0.5,
                  "色带重心 x=%s" % (round(center_red[0], 2) if center_red else "-"))

        # ---- 图片与同类 PDF 混排 ----
        mix_dir = workdir / "图片与PDF混排"
        make_image_sample(mix_dir / "照片1.png", 600, 400, "混排甲", (0.85, 0.12, 0.12))
        make_image_sample(mix_dir / "照片2.png", 600, 400, "混排乙", (0.12, 0.2, 0.85))
        _d = fitz.open()
        _p = _d.new_page(width=A4_W, height=A4_H)
        _p.insert_text((60, 100), "出差情况说明材料", fontsize=18, fontname="china-s")
        _d.save(str(mix_dir / "出差说明.pdf"))
        _d.close()
        mix_files = im.sort_paths(im.list_sources(mix_dir), im.SORT_NATURAL)
        rep_mix = im.merge_pdfs(mix_files, im.Options(
            scan_output=False, output=workdir / "IM_混排.pdf"))
        cut_mix = cut_lines(rep_mix.output, 0) if rep_mix.ok else []
        check("IM24 图片与同类 PDF（其他材料）混排：三张仍合成一页",
              rep_mix.ok and rep_mix.written_pages == 1 and len(cut_mix) == 2,
              "页数=%s 虚线=%s" % (rep_mix.written_pages if rep_mix.ok else rep_mix.error,
                                   cut_mix))
        check("IM25 合并报告的动作明细里写明了图片材料",
              rep_band.ok and any("照片甲" in a for a in rep_band.actions),
              str(rep_band.actions[:3]))
        check("IM26 日志里说明了图片按「其他材料」拼版",
              any("拼版" in ln and "其他材料" in ln for ln in logs_band),
              str(logs_band[:3]))

        # ---- 新增字段与报告文本：图片要能被人和程序都认出来 ----
        check("IM27 材料类型标签：图片标「图片」、PDF 标「PDF」",
              getattr(si_png, "kind_label", None) == "图片"
              and getattr(si_pdf, "kind_label", None) == "PDF",
              "png=%s pdf=%s" % (getattr(si_png, "kind_label", None),
                                 getattr(si_pdf, "kind_label", None)))
        check("IM28 合并报告统计了可用图片数",
              rep_band.ok and getattr(rep_band, "image_files", None) == 2
              and rep_keep.ok and getattr(rep_keep, "image_files", None) == 2,
              "拼版=%s 保真=%s" % (getattr(rep_band, "image_files", None),
                                   getattr(rep_keep, "image_files", None)))
        report_text = ""
        if rep_band.ok and rep_band.report_path:
            report_text = Path(rep_band.report_path).read_text(encoding="utf-8")
        check("IM29 报告正文写了图片材料数量与处理口径",
              "图片材料：2 个" in report_text,
              " / ".join(ln for ln in report_text.splitlines() if "图片材料" in ln)
              or "报告里没有「图片材料」一行")
        check("IM30 单张图片页的动作行标注「图片按其他材料处理」",
              rep_keep.ok and any("图片按其他材料处理" in a for a in rep_keep.actions),
              str(rep_keep.actions))

        # ---- WebP：sips 能读不能写，造样本要用 cwebp；没有就跳过 ----
        webp_tool = shutil.which("cwebp")
        if not webp_tool:
            print("   [跳过] 本机没有 cwebp，造不出真实的 .webp 样本")
        else:
            webp_png = img_dir / "_临时webp.png"
            webp_path = img_dir / "06_网页截图.webp"
            make_image_sample(webp_png, 500, 350, "网页截图", (0.6, 0.2, 0.5))
            made_webp = subprocess.run([webp_tool, "-quiet", str(webp_png), "-o", str(webp_path)],
                                       capture_output=True, timeout=60)
            webp_png.unlink(missing_ok=True)
            if made_webp.returncode != 0 or not webp_path.exists():
                print("   [跳过] cwebp 生成不出 .webp 样本")
            else:
                si_webp = im.analyze_source(webp_path, opts_i)
                check("IM31 WebP 图片经系统转换后可分析（1 页 / 图片 / 其他材料）",
                      si_webp.ok and si_webp.page_count == 1
                      and getattr(si_webp, "is_image", None) is True
                      and si_webp.category == im.CATEGORY_OTHER,
                      "ok=%s 错误=%s 页数=%s" % (si_webp.ok, si_webp.error or "-",
                                                 si_webp.page_count))
                rep_webp = im.merge_pdfs([webp_path], im.Options(
                    scan_output=False, size_mode=im.SIZE_KEEP,
                    output=workdir / "IM_webp.pdf"))
                check("IM32 WebP 图片可正常合并出一页",
                      rep_webp.ok and rep_webp.written_pages == 1,
                      "页数=%s" % (rep_webp.written_pages if rep_webp.ok
                                   else rep_webp.error))

        # ---- 转换器链路：没有 sips 时兜底，一个都没有时给可照做的提示 ----
        probe = img_dir / "_临时链路.png"
        make_image_sample(probe, 400, 300, "链路探测")
        probe_webp = img_dir / "_临时链路.webp"
        made = shutil.which("cwebp") and subprocess.run(
            [shutil.which("cwebp"), "-quiet", str(probe), "-o", str(probe_webp)],
            capture_output=True, timeout=60).returncode == 0
        if not made:
            print("   [跳过] 造不出 .webp，跳过转换器链路用例")
        else:
            # 这台机器上到底有没有「后备」转换器（Pillow 或命令行工具），
            # 一个都没有就别拿 IM35 去冤枉代码 —— 那是环境问题，不是缺陷。
            try:
                import PIL  # noqa: F401
                has_fallback = True
            except Exception:
                has_fallback = any(shutil.which(n) for n in
                                   ("magick", "convert", "heif-convert", "dwebp", "ffmpeg"))
            real_sips, real_which = im._sips_path, im.shutil.which
            try:
                # 假装不是 mac：没有 sips，只剩 Pillow / 命令行工具链
                im._sips_path = lambda: None
                im._CONVERT_CACHE.clear()
                if not has_fallback:
                    print("   [跳过] 本机既没 Pillow 也没命令行转换器，无法验证后备链路")
                else:
                    si_chain = im.analyze_source(probe_webp, opts_i)
                    check("IM35 非 macOS 上仍能靠后备转换器处理 WebP（不再直接判死）",
                          si_chain.ok and si_chain.page_count == 1,
                          "ok=%s 错误=%s" % (si_chain.ok, si_chain.error or "-"))

                # 连后备转换器也没有：必须是能照着做的提示，而不是原始异常
                im.shutil.which = lambda name: None
                im._CONVERT_CACHE.clear()
                si_none = im.analyze_source(probe_webp, opts_i)
                check("IM36 一个转换器都没有时给出可照做的提示（提示装 Pillow / ImageMagick）",
                      (not si_none.ok) and "Pillow" in (si_none.error or "")
                      and "JPG" in (si_none.error or ""),
                      si_none.error or "-")
            finally:
                im._sips_path, im.shutil.which = real_sips, real_which
                im._CONVERT_CACHE.clear()
            probe_webp.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)

        # ---- PyMuPDF 版本守卫：2.x 打的包会崩，得在源码层面就拦住 ----
        import subprocess as _sp
        guard = _sp.run(
            [sys.executable, "-c",
             "import sys,types,runpy;"
             "f=types.ModuleType('pymupdf');f.version=('2.0.0','2.0.0',None);"
             "sys.modules['pymupdf']=f;sys.argv=['x','--version'];"
             "runpy.run_path(%r, run_name='__main__')" % str(Path(__file__).with_name("invoice_merge.py")),
             ],
            capture_output=True, text=True, timeout=60)
        check("IM37 PyMuPDF 2.x 被明确拦下并提示改回 1.x（避免打包版启动即崩）",
              guard.returncode == 2 and "pymupdf<2" in (guard.stderr or ""),
              "退出码=%s 提示=%s" % (guard.returncode,
                                   (guard.stderr or "").strip().splitlines()[:1]))

        # ---- 超 A4 的图片：keep 模式下不能产出「1.4 米宽的纸」 ----
        huge_dir = workdir / "超大图片"
        huge = huge_dir / "手机原图.png"
        make_image_sample(huge, 1600, 1200, "手机原图 超 A4", (0.2, 0.55, 0.3))
        si_huge = im.analyze_source(huge, opts_i)
        huge_pt = image_page_size(huge)
        rep_huge = im.merge_pdfs([huge], im.Options(
            scan_output=False, size_mode=im.SIZE_KEEP,
            output=workdir / "IM_超大图片.pdf"))
        rows_huge = inspect(rep_huge.output) if rep_huge.ok else []
        check("IM33 超 A4 的图片在 keep 模式下被等比缩到 A4 以内（不产出 1.4 米宽的页）",
              rep_huge.ok and len(rows_huge) == 1
              and rows_huge[0]["w"] <= A4_H + 2 and rows_huge[0]["h"] <= A4_W + 2
              and rows_huge[0]["w"] > A4_H * 0.9,
              "原始 %s pt → 输出 %s" % (
                  tuple(round(v) for v in huge_pt),
                  (round(rows_huge[0]["w"]), round(rows_huge[0]["h"])) if rows_huge else "-"))
        ratio_in = huge_pt[0] / huge_pt[1] if huge_pt[1] else 0
        ratio_out = (rows_huge[0]["w"] / rows_huge[0]["h"]) if rows_huge else 0
        check("IM34 缩小后的页面保持原图长宽比，且动作行写明了缩放比例",
              abs(ratio_in - ratio_out) < 0.02
              and any("等比缩小" in a for a in rep_huge.actions),
              "长宽比 %0.3f → %0.3f；%s" % (ratio_in, ratio_out, rep_huge.actions))

    # ================= 用例 Q：类别分页与页脚剔除 =================
    print()
    print("-" * 70)
    print("用例 Q：酒店水单归其他材料、余下带子留白、页脚小字不算内容")
    print("-" * 70)
    qdir = workdir / "Q_分页与页脚"
    qdir.mkdir(exist_ok=True)

    def make_q_hotel(name: str, number: str) -> None:
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), "电子发票（增值税普通发票）", fontsize=16, fontname="china-s")
        pg.insert_text((60, 130), "发票号码：%s" % number, fontsize=11, fontname="china-s")
        pg.insert_text((60, 170), "项目名称：住宿费  维也纳国际酒店", fontsize=11, fontname="china-s")
        pg.insert_text((60, 210), "价税合计：1366.00 元", fontsize=11, fontname="china-s")
        doc.save(str(qdir / name))
        doc.close()

    def make_q_folio(name: str) -> None:
        """宾客水单：正文写着酒店名，容易被「酒店」关键词抢进酒店发票类。"""
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), "宾客水单", fontsize=16, fontname="china-s")
        pg.insert_text((60, 130), "酒店名称：桔子酒店乌鲁木齐二宫体育馆店", fontsize=11, fontname="china-s")
        pg.insert_text((60, 170), "总金额 623.76", fontsize=11, fontname="china-s")
        doc.save(str(qdir / name))
        doc.close()

    def make_q_footer(name: str) -> None:
        """正文只占上半页，页脚一行小字贴在最底 —— 早先这行把内容框撑满整页。"""
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), "电子发票（航空运输电子客票行程单）", fontsize=15, fontname="china-s")
        pg.insert_text((60, 130), "票价 2410.00  航段 济南 - 乌鲁木齐", fontsize=11, fontname="china-s")
        pg.insert_text((60, 165), "备注：本单由航旅纵横出具，可扫码验真", fontsize=11, fontname="china-s")
        pg.insert_text((A4_W / 2 - 34, A4_H - 16), "第 1 页/共 1 页", fontsize=8, fontname="china-s")
        doc.save(str(qdir / name))
        doc.close()

    make_q_folio("桔子酒店宾客水单.pdf")
    folio = im.analyze_source(qdir / "桔子酒店宾客水单.pdf", im.Options())
    check("Q1 宾客水单不算酒店发票，归其他材料（与结账单同口径）",
          folio.ok and folio.category == im.CATEGORY_OTHER,
          "类别=%s 命中=%s" % (folio.category, folio.category_hit))

    for i in (1, 2, 3):
        make_q_hotel("酒店发票%d.pdf" % i, "26112000003833356%03d" % i)
    rep_q2 = im.merge_pdfs(
        im.sort_paths([qdir / ("酒店发票%d.pdf" % i) for i in (1, 2, 3)], im.SORT_NATURAL),
        im.Options(scan_output=False, output=qdir / "Q_三张酒店发票.pdf"),
        log=lambda _m: None)
    inks_q2 = []
    if rep_q2.ok:
        d_q2 = fitz.open(str(rep_q2.output))
        inks_q2 = [im._ink_box(pg, 72) for pg in d_q2]
        d_q2.close()
    check("Q2 三张酒店发票排两页：第三张独占半页，下半页留白不塞别的材料",
          len(inks_q2) == 2 and inks_q2[1] is not None
          and inks_q2[1].y1 < A4_H * 0.55,
          "页数=%d 第2页内容底边=%s（页高 %.0f）"
          % (len(inks_q2),
             "%.0f" % inks_q2[1].y1 if len(inks_q2) > 1 and inks_q2[1] else "-", A4_H))

    make_q_footer("机票发票_带页脚.pdf")
    doc_q3 = im._open_source(qdir / "机票发票_带页脚.pdf")
    raw_q3 = im._ink_box(doc_q3[0], 72)
    main_q3 = im._main_ink_box(doc_q3[0], 72)
    doc_q3.close()
    check("Q3 孤立页脚不计入内容框（框明显变矮，上边界不动）",
          raw_q3 is not None and main_q3 is not None
          and main_q3.height < raw_q3.height * 0.6
          and abs(main_q3.y0 - raw_q3.y0) < 2,
          "原框高 %.0f → 新框高 %.0f"
          % (raw_q3.height if raw_q3 else -1, main_q3.height if main_q3 else -1))

    make_q_footer("机票发票_带页脚2.pdf")
    rep_q = im.merge_pdfs(
        [qdir / "机票发票_带页脚.pdf", qdir / "机票发票_带页脚2.pdf"],
        im.Options(scan_output=False, output=qdir / "Q_页脚拼版.pdf"),
        log=lambda _m: None)
    shrunk = [w for _s, w in rep_q.warnings if "内容放不下" in w]
    ink_w = 0.0
    if rep_q.ok:
        d_q = fitz.open(str(rep_q.output))
        ink_q = im._ink_box(d_q[0], 72)
        ink_w = ink_q.width if ink_q else 0.0
        d_q.close()
    band_w = A4_W - 2 * im.BAND_INSET_MM * im.MM
    check("Q4 带页脚的整页单据拼进半页不再被压到 49%（按正文取框、铺满带子）",
          rep_q.ok and not shrunk and ink_w >= band_w * 0.85,
          "缩放告警=%s 内容宽 %.0fpt / 带子宽 %.0fpt"
          % (shrunk or "无", ink_w, band_w))

    def make_q_verify(name: str) -> None:
        """平台出的行程校验单：正文有"校验单"、没有"发票"，文件名里还带着"机票"。"""
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), "航旅纵横 - 行程校验单", fontsize=15, fontname="china-s")
        pg.insert_text((60, 130), "姓名：王新龙    类别：机票    总金额 2410.0",
                       fontsize=11, fontname="china-s")
        pg.insert_text((60, 165), "2026-10-12  SC8715  济南遥墙 / 乌鲁木齐天山",
                       fontsize=11, fontname="china-s")
        doc.save(str(qdir / name))
        doc.close()

    def make_q_eticket(name: str) -> None:
        """真正的机票发票：电子发票（航空运输电子客票行程单）。"""
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), "电子发票（航空运输电子客票行程单）",
                       fontsize=15, fontname="china-s")
        pg.insert_text((60, 130), "发票号码：26378324211083729554",
                       fontsize=11, fontname="china-s")
        pg.insert_text((60, 165), "票价 1807.34  合计 2110.00  航班 SC8716",
                       fontsize=11, fontname="china-s")
        doc.save(str(qdir / name))
        doc.close()

    make_q_verify("05_机票行程校验单_20261012_济南至乌鲁木齐_SC8715.pdf")
    ver_q = im.analyze_source(
        qdir / "05_机票行程校验单_20261012_济南至乌鲁木齐_SC8715.pdf", im.Options())
    check("Q5 机票行程校验单不是机票，归其他材料（文件名里的\"机票\"不作数）",
          ver_q.ok and ver_q.category == im.CATEGORY_OTHER,
          "类别=%s 命中=%s" % (ver_q.category, ver_q.category_hit))

    make_q_eticket("04_机票行程单电子发票_20261012_济南至乌鲁木齐_SC8715.pdf")
    tic_q = im.analyze_source(
        qdir / "04_机票行程单电子发票_20261012_济南至乌鲁木齐_SC8715.pdf", im.Options())
    check("Q6 电子发票（航空运输电子客票行程单）仍算机票 / 火车票据",
          tic_q.ok and tic_q.category == im.CATEGORY_TRAVEL,
          "类别=%s 命中=%s" % (tic_q.category, tic_q.category_hit))

    # 发票与校验单合订成一份时，主体是发票 → 仍按票据算，别被"校验单"抢走
    doc_q7 = fitz.open()
    for i in (0, 1):
        pg = doc_q7.new_page(width=A4_W, height=A4_H)
        if i == 0:
            pg.insert_text((60, 90), "电子发票（航空运输电子客票行程单）",
                           fontsize=15, fontname="china-s")
            pg.insert_text((60, 130), "发票号码：26378324211083729554",
                           fontsize=11, fontname="china-s")
        else:
            pg.insert_text((60, 90), "航旅纵横 - 行程校验单",
                           fontsize=15, fontname="china-s")
            pg.insert_text((60, 130), "机票 总金额 2410.0",
                           fontsize=11, fontname="china-s")
    doc_q7.save(str(qdir / "04_机票发票与校验单合订.pdf"))
    doc_q7.close()
    mix_q = im.analyze_source(qdir / "04_机票发票与校验单合订.pdf", im.Options())
    check("Q7 发票与校验单合订的文件仍算票据（带\"发票\"字样时不信\"校验单\"）",
          mix_q.ok and mix_q.category == im.CATEGORY_TRAVEL,
          "类别=%s 命中=%s" % (mix_q.category, mix_q.category_hit))

    # ================= 用例 R：其他材料内部同类型归拢 =================
    print()
    print("-" * 70)
    print("用例 R：其他材料里同类型的挨在一起，一摞不被拆到两页")
    print("-" * 70)
    rdir = workdir / "R_同类归拢"
    rdir.mkdir(exist_ok=True)

    def make_r_sheet(name: str, title: str, body: str) -> None:
        doc = fitz.open()
        pg = doc.new_page(width=A4_W, height=A4_H)
        pg.insert_text((60, 90), title, fontsize=15, fontname="china-s")
        pg.insert_text((60, 130), body, fontsize=11, fontname="china-s")
        doc.save(str(rdir / name))
        doc.close()

    make_r_sheet("05_行程校验单_A.pdf", "航旅纵横 - 行程校验单", "机票 总金额 2410.0")
    make_r_sheet("06_订单截图_A.pdf", "订单详情", "待支付 2410.0")
    make_r_sheet("15_行程校验单_B.pdf", "航旅纵横 - 行程校验单", "机票 总金额 2110.0")
    make_r_sheet("01_出差审批单_X.pdf", "出差审批单", "五彩湾 20261012-20261018")
    make_r_sheet("02_出差审批单_Y.pdf", "出差审批单", "五彩湾 20261019-20261021")
    make_r_sheet("16_订单截图_B.pdf", "订单详情", "已支付 2110.0")
    make_r_sheet("09_住宿水单_A.pdf", "宾客水单", "酒店名称：桔子酒店 623.76")

    # 故意交错着给：校验单、截图、校验单、审批单、审批单、水单
    r_order = ["05_行程校验单_A.pdf", "06_订单截图_A.pdf", "15_行程校验单_B.pdf",
               "01_出差审批单_X.pdf", "02_出差审批单_Y.pdf", "09_住宿水单_A.pdf"]
    r_srcs = [im.analyze_source(rdir / n, im.Options()) for n in r_order]
    r_org = im.organize_sources([s for s in r_srcs if s.ok])
    r_subs = [im.other_subtype(s) for s in r_org]
    check("R1 其他材料内部按小类归拢（校验单挨着校验单，审批单挨着审批单）",
          r_subs == ["行程校验单", "行程校验单", "订单截图",
                     "出差审批单", "出差审批单", "消费明细"],
          "顺序=%s" % " / ".join(r_subs))

    def r_units(names: Sequence[str]) -> List[List[str]]:
        srcs = [im.analyze_source(rdir / n, im.Options()) for n in names]
        org = im.organize_sources(srcs)
        plan = [(s, pi) for s in org for pi in s.pages if not pi.error]
        return [[it[0].path.name for it in unit]
                for unit, _bands, _rot in im.build_units(plan, im.Options())]

    u_mix = r_units(["05_行程校验单_A.pdf", "06_订单截图_A.pdf", "15_行程校验单_B.pdf"])
    check("R2 一摞放进剩下的条带里放得下时，顺着排、不多占一页",
          len(u_mix) == 1 and len(u_mix[0]) == 3,
          "分页=%s" % u_mix)

    u_split = r_units(["05_行程校验单_A.pdf", "06_订单截图_A.pdf",
                       "15_行程校验单_B.pdf", "16_订单截图_B.pdf"])
    check("R3 一摞放不下时整摞挪到下一页，不拆成两页",
          len(u_split) == 2
          and sorted(u_split[0]) == ["05_行程校验单_A.pdf", "15_行程校验单_B.pdf"]
          and sorted(u_split[1]) == ["06_订单截图_A.pdf", "16_订单截图_B.pdf"],
          "分页=%s" % u_split)

    # ================= 结果 =================
    print()
    print("=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   - %s" % f)
    print("=" * 70)

    if keep:
        print("\n样本与输出保留在：%s" % workdir)
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n临时文件已清理（加 --keep-dir 可保留）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
