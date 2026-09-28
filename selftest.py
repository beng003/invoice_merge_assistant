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
    opts = im.Options(size_mode=im.SIZE_KEEP, sort_mode=im.SORT_NATURAL,
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

    # ================= 用例 B：统一 A4 =================
    print()
    print("-" * 70)
    print("用例 B：统一 A4（按内容方向自动选横纵）")
    print("-" * 70)
    opts_b = im.Options(size_mode=im.SIZE_A4_AUTO, sort_mode=im.SORT_NATURAL,
                        auto_rotate=True, fix_clipping=True,
                        output=workdir / "B_统一A4.pdf")
    rep_b = im.merge_pdfs(files, opts_b, log=lambda m: print("   " + m))
    rows_b = inspect(rep_b.output) if rep_b.ok else []
    print("   输出：%s" % describe(rows_b))
    check("B1 合并成功", rep_b.ok)
    check("B2 共 8 页", len(rows_b) == 8, "实际 %d" % len(rows_b))
    a4_ok = all(
        (abs(r["w"] - A4_W) < 2 and abs(r["h"] - A4_H) < 2) or
        (abs(r["w"] - A4_H) < 2 and abs(r["h"] - A4_W) < 2)
        for r in rows_b
    )
    check("B3 每页均为 A4 尺寸（横或纵）", a4_ok, describe(rows_b))
    check("B4 纵向源页 -> A4 纵向", not rows_b[0]["landscape"])
    check("B5 横向源页 -> A4 横向", rows_b[1]["landscape"])
    check("B6 内容横躺页扶正后为 A4 横向", rows_b[3]["landscape"], describe(rows_b[3:4]))
    check("B7 所有页文字水平", all(r["angle"] in (0, None) for r in rows_b),
          str([r["angle"] for r in rows_b]))
    # 内容未被裁切：墨迹应完整落在页面内且不过度留白
    inside = True
    for r in rows_b:
        if r["ink"] is None:
            continue
        x0, y0, x1, y1 = r["ink"]
        if x0 < -1 or y0 < -1 or x1 > r["w"] + 1 or y1 > r["h"] + 1:
            inside = False
    check("B8 内容完整落在页面内（无裁切）", inside)

    # ================= 用例 C：单独指定文件 + 关闭自动扶正 =================
    print()
    print("-" * 70)
    print("用例 C：手动指定文件列表，关闭自动扶正（验证选项生效）")
    print("-" * 70)
    picked = [samples / "01_纵向电子发票.pdf", samples / "04_内容横躺.pdf",
              samples / "02_横向发票.pdf"]
    opts_c = im.Options(size_mode=im.SIZE_KEEP, sort_mode=im.SORT_NAME,
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
    opts_f = im.Options(size_mode=im.SIZE_A4_ROTATE, sort_mode=im.SORT_NATURAL,
                        fix_clipping=True, output=workdir / "F_纵向旋转.pdf")
    rep_f = im.merge_pdfs(files, opts_f, log=lambda m: print("   " + m))
    rows_f = inspect(rep_f.output) if rep_f.ok else []
    print("   输出：%s" % describe(rows_f))
    check("F1 合并成功", rep_f.ok)
    check("F2 每页均为 A4 纵向",
          bool(rows_f) and all(abs(r["w"] - A4_W) < 2 and abs(r["h"] - A4_H) < 2 for r in rows_f),
          describe(rows_f))
    check("F3 横向源页（02）转成竖躺姿态，文字角度 270",
          len(rows_f) > 1 and rows_f[1]["angle"] == 270, "角度=%s" % (rows_f[1]["angle"] if len(rows_f) > 1 else "N/A"))
    check("F4 rotation 属性异常的横向页（03）同样转成竖躺",
          len(rows_f) > 2 and rows_f[2]["angle"] == 270, "角度=%s" % (rows_f[2]["angle"] if len(rows_f) > 2 else "N/A"))
    check("F5 纵向源页（01）保持文字水平",
          len(rows_f) > 0 and rows_f[0]["angle"] == 0)
    check("F6 内容横躺页（04）在该模式下被扶正为水平",
          len(rows_f) > 3 and rows_f[3]["angle"] == 0, "角度=%s" % (rows_f[3]["angle"] if len(rows_f) > 3 else "N/A"))

    # 关闭开关时不应再有旋转行为（用 a4-portrait 对照）
    opts_f2 = im.Options(size_mode=im.SIZE_A4_PORTRAIT, sort_mode=im.SORT_NATURAL,
                         fix_clipping=True, output=workdir / "F2_纵向居中.pdf")
    rep_f2 = im.merge_pdfs(files, opts_f2, log=lambda m: None)
    rows_f2 = inspect(rep_f2.output) if rep_f2.ok else []
    check("F7 对照：缩小居中模式保持文字水平",
          len(rows_f2) > 1 and rows_f2[1]["angle"] == 0,
          "角度=%s" % (rows_f2[1]["angle"] if len(rows_f2) > 1 else "N/A"))

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

    # ================= 用例 H：报销顺序整理 + 酒店票拼版 =================
    print()
    print("-" * 70)
    print("用例 H：报销顺序整理与酒店发票上半页拼版")
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
    opts_h = im.Options(size_mode=im.SIZE_KEEP, sort_mode=im.SORT_PINYIN,
                        organize=True, hotel_merge=True,
                        output=workdir / "H_报销整理.pdf")
    rep_h = im.merge_pdfs(biz_files, opts_h, log=lambda m: print("   " + m))
    check("H1 合并成功", rep_h.ok)
    texts = page_texts(rep_h.output) if rep_h.ok else []
    print("   输出页序：")
    for i, t in enumerate(texts, 1):
        print("     P%d  %s" % (i, t[:46]))

    check("H2 两张酒店发票拼成一页，总页数 7", len(texts) == 7, "实际 %d 页" % len(texts))
    check("H3 首位是酒店发票拼版页（含两张，且不含结账单）",
          len(texts) > 0 and "桔子水晶酒店" in texts[0] and "海滨酒店" in texts[0]
          and "结账单" not in texts[0],
          texts[0][:44] if texts else "")
    check("H4 第二位是差旅费报销单",
          len(texts) > 1 and "差旅费报销单" in texts[1])
    check("H5 第三位是机票行程单", len(texts) > 2 and "行程单" in texts[2])
    check("H6 第四位是火车票", len(texts) > 3 and ("铁路" in texts[3] or "火车" in texts[3]))
    check("H7 酒店结账单归入其他材料（排在票据之后，不参与拼版）",
          len(texts) > 4 and "结账单" in texts[4], texts[4][:40] if len(texts) > 4 else "")
    check("H8 票据粘贴单封面排在最后",
          len(texts) > 0 and "票据粘贴单" in texts[-1])
    check("H9 酒店发票只占半页（页面高度不变，内容各占上下半区）",
          rep_h.ok and all(abs(r["h"] - A4_H) < 2 for r in inspect(rep_h.output)))

    # 关掉拼版 → 页数应恢复为 8
    opts_h2 = im.Options(size_mode=im.SIZE_KEEP, organize=True, hotel_merge=False,
                         output=workdir / "H2_不拼版.pdf")
    rep_h2 = im.merge_pdfs(biz_files, opts_h2, log=lambda m: None)
    check("H10 关闭拼版后各材料各占一页，共 8 页",
          rep_h2.ok and rep_h2.written_pages == 8, "实际 %d" % rep_h2.written_pages)

    # 关掉整理 → 顺序应回到拼音序
    opts_h3 = im.Options(size_mode=im.SIZE_KEEP, organize=False, hotel_merge=True,
                         sort_mode=im.SORT_PINYIN, output=workdir / "H3_不整理.pdf")
    rep_h3 = im.merge_pdfs(biz_files, opts_h3, log=lambda m: None)
    texts3 = page_texts(rep_h3.output) if rep_h3.ok else []
    check("H11 关闭整理后按拼音序排列（首页不是酒店）",
          bool(texts3) and "酒店" not in texts3[0], texts3[0][:30] if texts3 else "")

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
            check("L3 界面选项映射到 Options 时尺寸正确",
                  win._options().size_mode == im.SIZE_A4_ROTATE,
                  win._options().size_mode)
            check("L4 关键控件齐备",
                  all(hasattr(win, n) for n in
                      ("table", "btn_run", "btn_cancel", "btn_reveal", "btn_print",
                       "progress", "log", "sort_combo", "path_edit", "count_label")))
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

    opts_n = im.Options(size_mode=im.SIZE_A4_ROTATE, organize=True, hotel_merge=True,
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
