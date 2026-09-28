#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发票合并小助手 — PySide6 图形界面。

设计约定（克制为主，不花哨）：

* 配色：1 个主色 + 灰阶 + 1 个强调色。深色底用 #0f1114 / #1c1f26 这类
  低饱和蓝灰，避开纯黑纯白造成的刺眼对比。
* 留白：控件间距 12~24px，卡片内边距 ≥16px，不把界面塞满。
* 圆角统一：卡片 12px，按钮 / 输入框 8px。
* 字体分级：标题 bold 21、正文 13、辅助 12 且用浅灰 #8a8f98。
* 交互反馈：按钮 hover 变色、按下有状态变化。
* 长任务：合并与分析都跑在 QThread 里，用信号回传，界面不卡死。
* 图标：Lucide SVG，运行时按主题色着色。

核心逻辑全部复用 invoice_merge，本文件只负责界面。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    from PySide6.QtCore import QByteArray, QSize, Qt, QThread, Signal
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLayout,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "图形界面需要 PySide6，请先安装：pip install PySide6-Essentials\n%s" % exc)

import invoice_merge as core


# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

# 两套调色板。都以灰阶为主，各留一个主色和一个强调色。
# 深色底用低饱和蓝灰（不用纯黑），浅色底用浅灰白（不用纯白），避免刺眼。
DARK = {
    "BG": "#0f1114",            # 窗口底
    "SURFACE": "#1c1f26",       # 卡片
    "SURFACE_ALT": "#22262f",   # 卡片内的次级面：输入框、表头、日志底
    "SURFACE_HOVER": "#2a2f3a",
    "BORDER": "#2b303b",
    "BORDER_HOVER": "#3a4150",
    "ROW_ALT": "#20242c",       # 表格隔行底色
    "PANEL": "#1c1f26",         # 对话框面板

    "TEXT": "#e6e8ec",          # 主文字
    "TEXT_DIM": "#8a8f98",      # 辅助文字
    "TEXT_MUTED": "#5f6672",    # 更弱的说明

    "PRIMARY": "#4f8ff7",       # 唯一主色
    "PRIMARY_HOVER": "#6ba1f8",
    "PRIMARY_PRESS": "#3d7ae0",
    "PRIMARY_SOFT": "#1e2c44",  # 选中行 / 主色低饱和底

    "ACCENT": "#f0a04b",        # 唯一强调色，只给打印这类"终点动作"
    "ACCENT_HOVER": "#f5b16a",
    "ACCENT_TEXT": "#201404",

    "ICON": "#c8ccd4",
    "ICON_ON_PRIMARY": "#ffffff",
    "ICON_ON_ACCENT": "#201404",
}

LIGHT = {
    "BG": "#f4f5f7",            # 浅灰白底，不用纯白
    "SURFACE": "#ffffff",       # 卡片纯白，与底色拉开层次
    "SURFACE_ALT": "#f1f3f6",
    "SURFACE_HOVER": "#e9ecf1",
    "BORDER": "#e2e5ea",
    "BORDER_HOVER": "#c9d0db",
    "ROW_ALT": "#fafbfc",
    "PANEL": "#ffffff",

    "TEXT": "#1c1f26",          # 深灰近黑，不用纯黑
    "TEXT_DIM": "#6b7280",
    "TEXT_MUTED": "#9aa1ac",

    "PRIMARY": "#2f6fe0",       # 浅底上略深一点，保证对比度
    "PRIMARY_HOVER": "#4a85e8",
    "PRIMARY_PRESS": "#2258b8",
    "PRIMARY_SOFT": "#e4edfc",

    "ACCENT": "#d97a1f",
    "ACCENT_HOVER": "#e8892f",
    "ACCENT_TEXT": "#ffffff",

    "ICON": "#5a6472",
    "ICON_ON_PRIMARY": "#ffffff",
    "ICON_ON_ACCENT": "#ffffff",
}

THEMES = {"dark": DARK, "light": LIGHT}
DEFAULT_THEME = "dark"


class C:
    """当前主题的调色板。

    切主题时整体替换属性，业务代码照常写 C.PRIMARY 即可，
    不用到处传参。
    """

    name = DEFAULT_THEME

    @classmethod
    def apply(cls, theme: str) -> None:
        cls.name = theme if theme in THEMES else DEFAULT_THEME
        for key, value in THEMES[cls.name].items():
            setattr(cls, key, value)


C.apply(DEFAULT_THEME)


FONT = "PingFang SC"
R_CARD = 12
R_CTRL = 8
GAP = 14                  # 卡片之间的间距
ROW_GAP = 12              # 卡片内部的行间距
PAD = 18                  # 卡片内边距

QSS_TEMPLATE = """
QWidget {
    background: %(BG)s;
    color: %(TEXT)s;
    font-family: "%(FONT)s";
    font-size: 13px;
}
QLabel { background: transparent; }
QLabel#title     { font-size: 21px; font-weight: 700; }
QLabel#subtitle  { font-size: 12px; color: %(TEXT_DIM)s; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; }
QLabel#hint      { font-size: 12px; color: %(TEXT_DIM)s; }
QLabel#field     { font-size: 13px; color: %(TEXT_DIM)s; }
QLabel#status    { font-size: 12px; color: %(TEXT_DIM)s; }

QFrame#card {
    background: %(SURFACE)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CARD)dpx;
}

QScrollArea#scroll, QScrollArea#scroll > QWidget > QWidget { background: %(BG)s; border: none; }

QPushButton {
    background: %(SURFACE_ALT)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CTRL)dpx;
    padding: 7px 14px;
    font-size: 13px;
}
QPushButton:hover   { background: %(SURFACE_HOVER)s; border-color: %(BORDER_HOVER)s; }
QPushButton:pressed { background: %(BORDER)s; }
QPushButton:disabled { background: %(SURFACE)s; color: %(TEXT_MUTED)s; }

QPushButton#primary {
    background: %(PRIMARY)s;
    color: #ffffff;
    border: 1px solid %(PRIMARY)s;
    font-weight: 600;
    padding: 9px 22px;
}
QPushButton#primary:hover   { background: %(PRIMARY_HOVER)s; border-color: %(PRIMARY_HOVER)s; }
QPushButton#primary:pressed { background: %(PRIMARY_PRESS)s; }
QPushButton#primary:disabled {
    background: %(PRIMARY_SOFT)s; color: %(TEXT_MUTED)s; border-color: %(PRIMARY_SOFT)s;
}

QPushButton#accent {
    background: %(ACCENT)s;
    color: %(ACCENT_TEXT)s;
    border: 1px solid %(ACCENT)s;
    font-weight: 600;
}
QPushButton#accent:hover   { background: %(ACCENT_HOVER)s; border-color: %(ACCENT_HOVER)s; }
QPushButton#accent:disabled {
    background: %(SURFACE_ALT)s; color: %(TEXT_MUTED)s; border-color: %(BORDER)s;
}

QLineEdit, QComboBox, QSpinBox {
    background: %(SURFACE_ALT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CTRL)dpx;
    padding: 6px 10px;
    selection-background-color: %(PRIMARY)s;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: %(BORDER_HOVER)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: %(PRIMARY)s; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: %(SURFACE_ALT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CTRL)dpx;
    selection-background-color: %(PRIMARY_SOFT)s;
    selection-color: %(TEXT)s;
    outline: none;
}

/* 单/复选：每条状态规则的属性都要写全。
   Qt 的 QSS 在同一子控件的不同状态间不做属性合并 —— 只取最匹配的那条，
   没写的属性就退回默认值。少写 width/height 会被拉伸成色块盖住文字，
   少写 border-radius 圆角会消失。下面每一条都是完整的。 */
QRadioButton, QCheckBox { spacing: 7px; background: transparent; font-size: 13px; }

QRadioButton::indicator {
    width: 15px; height: 15px;
    border-radius: 8px;
    border: 1px solid %(BORDER_HOVER)s;
    background: %(SURFACE_ALT)s;
}
QRadioButton::indicator:hover {
    width: 15px; height: 15px;
    border-radius: 8px;
    border: 1px solid %(PRIMARY)s;
    background: %(SURFACE_ALT)s;
}
QRadioButton::indicator:checked {
    width: 15px; height: 15px;
    border-radius: 8px;
    border: 4px solid %(PRIMARY)s;
    background: #ffffff;
}
QRadioButton::indicator:checked:hover {
    width: 15px; height: 15px;
    border-radius: 8px;
    border: 4px solid %(PRIMARY_HOVER)s;
    background: #ffffff;
}

QCheckBox::indicator {
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid %(BORDER_HOVER)s;
    background: %(SURFACE_ALT)s;
}
QCheckBox::indicator:hover {
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid %(PRIMARY)s;
    background: %(SURFACE_ALT)s;
}
QCheckBox::indicator:checked {
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid %(PRIMARY)s;
    background: %(PRIMARY)s;
    image: url("%(CHECK)s");
}
QCheckBox::indicator:checked:hover {
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid %(PRIMARY_HOVER)s;
    background: %(PRIMARY_HOVER)s;
    image: url("%(CHECK)s");
}

QTableWidget {
    background: %(SURFACE_ALT)s;
    alternate-background-color: %(ROW_ALT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CTRL)dpx;
    gridline-color: transparent;
    outline: none;
}
QTableWidget::item { padding: 6px 8px; border: none; }
QTableWidget::item:selected { background: %(PRIMARY_SOFT)s; color: %(TEXT)s; }
QHeaderView::section {
    background: %(SURFACE)s;
    color: %(TEXT_DIM)s;
    border: none;
    border-bottom: 1px solid %(BORDER)s;
    padding: 7px 8px;
    font-size: 12px;
    font-weight: 600;
}
QTableCornerButton::section { background: %(SURFACE)s; border: none; }

QProgressBar {
    background: %(SURFACE_ALT)s;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background: %(PRIMARY)s; border-radius: 5px; }

QPlainTextEdit {
    background: %(SURFACE_ALT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(R_CTRL)dpx;
    padding: 8px;
    font-family: "Menlo", monospace;
    font-size: 12px;
    color: %(TEXT_DIM)s;
}

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(BORDER_HOVER)s; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: %(TEXT_MUTED)s; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: %(BORDER_HOVER)s; border-radius: 5px; min-width: 28px; }

QToolTip {
    background: %(SURFACE_ALT)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER_HOVER)s;
    border-radius: 6px;
    padding: 5px 8px;
}
"""


def build_qss() -> str:
    """生成样式表。

    对勾图标要按真实磁盘路径引用（打包后路径会变），所以放在运行时拼。
    """
    return QSS_TEMPLATE % {
        "BG": C.BG, "SURFACE": C.SURFACE, "SURFACE_ALT": C.SURFACE_ALT,
        "SURFACE_HOVER": C.SURFACE_HOVER, "BORDER": C.BORDER,
        "BORDER_HOVER": C.BORDER_HOVER,
        "TEXT": C.TEXT, "TEXT_DIM": C.TEXT_DIM, "TEXT_MUTED": C.TEXT_MUTED,
        "ROW_ALT": C.ROW_ALT,
        "PRIMARY": C.PRIMARY, "PRIMARY_HOVER": C.PRIMARY_HOVER,
        "PRIMARY_PRESS": C.PRIMARY_PRESS, "PRIMARY_SOFT": C.PRIMARY_SOFT,
        "ACCENT": C.ACCENT, "ACCENT_HOVER": C.ACCENT_HOVER,
        "ACCENT_TEXT": C.ACCENT_TEXT,
        "FONT": FONT, "R_CARD": R_CARD, "R_CTRL": R_CTRL,
        "CHECK": (asset_dir() / "icons" / "check.svg").as_posix(),
    }


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------

def asset_dir() -> Path:
    """图标目录。打包后位于 PyInstaller 解包出的 _MEIPASS 下。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    return Path(__file__).resolve().parent / "assets"


_ICON_CACHE: dict = {}


def icon(name: str, color: Optional[str] = None, size: int = 16) -> QIcon:
    """加载 Lucide SVG 并着色。

    Lucide 的 SVG 用的是 stroke="currentColor"，Qt 不认这个关键字，
    所以要把颜色替换掉再渲染 —— 同一份 SVG 就能出不同颜色的图标。

    color 不写死默认值，运行时取当前主题色，切主题才会跟着变。
    """
    color = color or C.ICON
    key = (name, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    try:
        svg = (asset_dir() / "icons" / ("%s.svg" % name)).read_text(encoding="utf-8")
    except Exception:
        return QIcon()

    svg = svg.replace('stroke="currentColor"', 'stroke="%s"' % color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return QIcon()

    scale = 2                     # 2 倍图，Retina 下不糊
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pix.setDevicePixelRatio(scale)

    result = QIcon(pix)
    _ICON_CACHE[key] = result
    return result


# --------------------------------------------------------------------------
# 后台线程
# --------------------------------------------------------------------------

class AnalyzeWorker(QThread):
    """后台分析文件，逐个把结果发回主线程。"""

    one_done = Signal(object, object)     # (Path, SourceInfo)
    all_done = Signal()

    def __init__(self, files: Sequence[Path], opts: core.Options, parent=None) -> None:
        super().__init__(parent)
        self.files = list(files)
        self.opts = opts

    def run(self) -> None:  # noqa: D102
        for f in self.files:
            if self.isInterruptionRequested():
                return
            try:
                info = core.analyze_source(f, self.opts)
            except Exception as exc:      # 单文件异常不该拖垮整个分析
                info = core.SourceInfo(path=f, error="%s: %s" % (exc.__class__.__name__, exc))
            self.one_done.emit(f, info)
        self.all_done.emit()


class MergeWorker(QThread):
    """后台执行合并。进度、日志、结果全部走信号。"""

    progressed = Signal(int, int)
    logged = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, files: Sequence[Path], opts: core.Options, parent=None) -> None:
        super().__init__(parent)
        self.files = list(files)
        self.opts = opts
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102
        try:
            report = core.merge_pdfs(
                self.files,
                self.opts,
                log=self.logged.emit,
                progress=self.progressed.emit,
                should_stop=lambda: self._cancelled,
            )
            self.succeeded.emit(report)
        except Exception as exc:  # pragma: no cover
            self.failed.emit("%s: %s" % (exc.__class__.__name__, exc))


# --------------------------------------------------------------------------
# 构建小工具
# --------------------------------------------------------------------------

def make_card(title: str = "") -> Tuple[QFrame, QVBoxLayout]:
    """返回 (卡片, 内容布局)。给了标题才画标题行。"""
    frame = QFrame()
    frame.setObjectName("card")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(PAD, PAD - 2, PAD, PAD)
    outer.setSpacing(ROW_GAP)
    # QFrame 一旦被 QSS 设了圆角和边框，minimumSizeHint 就会失真（返回很小的值），
    # 布局于是能把卡片压得比内容还矮，文字被上下裁掉。这条约束把它钉住：
    # 卡片最小就是装得下所有子控件的高度。
    outer.setSizeConstraint(QLayout.SetMinimumSize)
    if title:
        label = QLabel(title)
        label.setObjectName("cardTitle")
        outer.addWidget(label)
    return frame, outer


def make_button(text: str, icon_name: str = "", obj: str = "",
                color: Optional[str] = None, slot=None) -> QPushButton:
    """建按钮。

    color 默认 None 而不是 C.ICON —— 默认参数只在函数定义时求值一次，
    写死的话切换主题后图标颜色不会跟着变，这里改成运行时再取。
    """
    btn = QPushButton(text)
    if icon_name:
        btn.setIcon(icon(icon_name, color or C.ICON))
        btn.setIconSize(QSize(16, 16))
    if obj:
        btn.setObjectName(obj)
    btn.setCursor(Qt.PointingHandCursor)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("field")
    label.setMinimumWidth(60)
    return label


# --------------------------------------------------------------------------
# 打印对话框
# --------------------------------------------------------------------------

class PrintDialog(QDialog):
    def __init__(self, pdf: Path, printers: Sequence[Tuple[str, bool]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("打印合并结果")
        self.setModal(True)
        self.printers = list(printers)

        root = QVBoxLayout(self)
        root.setContentsMargins(PAD, PAD, PAD, PAD)
        root.setSpacing(14)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.addWidget(field_label("打印机"), 0, 0)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(300)
        default_idx = 0
        for i, (name, is_default) in enumerate(self.printers):
            self.combo.addItem("%s%s" % (name, "（默认）" if is_default else ""))
            if is_default:
                default_idx = i
        self.combo.setCurrentIndex(default_idx)
        grid.addWidget(self.combo, 0, 1)

        grid.addWidget(field_label("份数"), 1, 0)
        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.setValue(1)
        grid.addWidget(self.copies, 1, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        hint = QLabel("文件：%s" % pdf.name)
        hint.setObjectName("hint")
        root.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(make_button("取消", slot=self.reject))
        row.addWidget(make_button("打印", "printer", "accent", C.ACCENT_TEXT, self.accept))
        root.addLayout(row)

    def selection(self) -> Tuple[Optional[str], int]:
        idx = self.combo.currentIndex()
        name = self.printers[idx][0] if 0 <= idx < len(self.printers) else None
        return name, self.copies.value()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class MainWindow(QWidget):
    # 列宽合计要留在最小窗口宽度以内，否则表格会冒出横向滚动条
    COLS = (
        ("文件", 250), ("类别", 96), ("页数", 52),
        ("方向", 70), ("页面尺寸", 116), ("诊断", 130),
    )

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("发票合并小助手")
        self.resize(1000, 860)
        self.setMinimumSize(820, 560)      # 有滚动区兜底，窗口小也不会裁字

        self.files: List[Path] = []
        self.infos: dict = {}
        self.last_report: Optional[core.MergeReport] = None
        self.analyzer: Optional[AnalyzeWorker] = None
        self.worker: Optional[MergeWorker] = None
        # 输出路径字段里"我们自动填的那个值"。字段内容等于它（或为空）才允许
        # 随文件列表刷新；用户手输 / 另存为选过的路径要原样留着。
        self._auto_output = ""
        # 记下每个按钮的图标名与配色角色，切主题时批量换色
        self._icon_bindings: List[Tuple[QPushButton, str, str]] = []

        self.settings = core.load_settings()
        # 先定主题再搭界面，图标才会用对颜色
        theme = str(self.settings.get("theme", "") or "")
        C.apply(theme if theme in THEMES else DEFAULT_THEME)
        last = str(self.settings.get("last_dir", "") or "")
        self.last_dir = Path(last) if last and Path(last).is_dir() else None

        self._build()
        self._refresh_table()
        self._set_running(False)

    # ---------- 主题 ----------

    def _role_color(self, role: str) -> str:
        """图标配色角色 → 当前主题里的实际颜色。"""
        return {
            "on_primary": C.ICON_ON_PRIMARY,
            "on_accent": C.ICON_ON_ACCENT,
        }.get(role, C.ICON)

    def _mkbtn(self, text: str, icon_name: str = "", obj: str = "",
               color_role: str = "icon", slot=None) -> QPushButton:
        """建按钮并登记图标，切主题时好统一换色。"""
        btn = make_button(text, icon_name, obj, self._role_color(color_role), slot)
        self._icon_bindings.append((btn, icon_name, color_role))
        return btn

    def apply_theme(self, name: str, persist: bool = True) -> None:
        """切换深色 / 浅色主题，并记住选择。"""
        C.apply(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss())
        for btn, icon_name, role in self._icon_bindings:
            if icon_name:
                btn.setIcon(icon(icon_name, self._role_color(role)))
        if hasattr(self, "header_icon"):
            self.header_icon.setPixmap(
                icon("file-text", C.PRIMARY, 26).pixmap(26, 26))
        if hasattr(self, "btn_theme"):
            # 按钮上显示的是"点一下会切到哪个主题"
            nxt = "sun" if C.name == "dark" else "moon"
            self.btn_theme.setIcon(icon(nxt, C.ICON))
            self.btn_theme.setToolTip(
                "切换到浅色主题" if C.name == "dark" else "切换到深色主题")
        if persist:
            self.settings["theme"] = C.name
            core.save_settings(self.settings)

    def toggle_theme(self) -> None:
        self.apply_theme("light" if C.name == "dark" else "dark")

    # ---------- 搭建界面 ----------

    def _build(self) -> None:
        # 外层只放一个滚动区：窗口再小也只是滚动，不会把卡片压到裁字。
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        content = QWidget()
        content.setObjectName("content")
        root = QVBoxLayout(content)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(GAP)
        root.addWidget(self._build_header())
        root.addWidget(self._build_materials())
        root.addWidget(self._build_options())
        root.addLayout(self._build_actions())
        root.addWidget(self._build_log())

        scroll.setWidget(content)
        shell.addWidget(scroll)

    def _build_header(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(12)

        mark = QLabel()
        mark.setPixmap(icon("file-text", C.PRIMARY, 26).pixmap(26, 26))
        mark.setFixedSize(26, 26)
        self.header_icon = mark
        row.addWidget(mark)

        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("发票合并小助手")
        title.setObjectName("title")
        sub = QLabel("把多张发票 PDF 合成一个文件，自动处理横竖方向与页面尺寸")
        sub.setObjectName("subtitle")
        col.addWidget(title)
        col.addWidget(sub)
        row.addLayout(col)
        row.addStretch(1)

        self.btn_theme = self._mkbtn("", "sun", slot=self.toggle_theme)
        self.btn_theme.setFixedWidth(42)
        self.btn_theme.setToolTip("切换到浅色主题")
        row.addWidget(self.btn_theme)
        return box

    def _build_materials(self) -> QWidget:
        frame, outer = make_card("材料")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        for text, ic, slot in (
            ("选择文件", "file-plus-2", self.pick_files),
            ("选择文件夹", "folder-open", self.pick_folder),
            ("清空", "trash-2", self.clear_files),
            ("移除选中", "minus", self.remove_selected),
        ):
            bar.addWidget(self._mkbtn(text, ic, slot=slot))
        bar.addStretch(1)
        outer.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in self.COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(190)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        for i, (_t, w) in enumerate(self.COLS):
            self.table.setColumnWidth(i, w)
        header.setStretchLastSection(True)
        outer.addWidget(self.table)

        move = QHBoxLayout()
        move.setSpacing(8)
        move.addWidget(field_label("调整顺序"))
        for text, ic, delta in (("上移", "arrow-up", -1), ("下移", "arrow-down", 1)):
            move.addWidget(self._mkbtn(
                text, ic, slot=lambda _=False, d=delta: self.move_one(d)))
        for text, ic, direction in (("移到最前", "chevrons-up", -1),
                                    ("移到末尾", "chevrons-down", 1)):
            move.addWidget(self._mkbtn(
                text, ic, slot=lambda _=False, x=direction: self.move_selected(x)))
        move.addStretch(1)
        self.count_label = QLabel("尚未添加文件")
        self.count_label.setObjectName("hint")
        move.addWidget(self.count_label)
        outer.addLayout(move)
        return frame

    def _build_options(self) -> QWidget:
        frame, outer = make_card("选项")

        # 输出尺寸：两个选项排一行；左侧标签独立成列，避免被网格压扁
        self.size_buttons = {}
        size_line = QHBoxLayout()
        size_line.setSpacing(20)
        for mode in core.SIZE_ORDER:
            rb = QRadioButton(core.SIZE_LABELS[mode])
            self.size_buttons[mode] = rb
            size_line.addWidget(rb)
        size_line.addStretch(1)
        # 默认：统一 A4 纵向（横向页旋转填满）
        self.size_buttons[core.SIZE_A4_ROTATE].setChecked(True)

        size_box = QHBoxLayout()
        size_box.setSpacing(16)
        size_box.addWidget(field_label("输出尺寸"), 0, Qt.AlignVCenter)
        size_box.addLayout(size_line, 1)
        outer.addLayout(size_box)

        # 处理开关：7 个开关分两行
        self.chk_organize = QCheckBox("整理报销顺序")
        self.chk_band = QCheckBox("小票拼版（酒店 / 机票 / 其他）")
        self.chk_autorot = QCheckBox("自动扶正横躺内容")
        self.chk_fixclip = QCheckBox("恢复被裁切内容")
        self.chk_report = QCheckBox("生成报告")
        self.chk_recursive = QCheckBox("包含子文件夹")
        self.chk_scan = QCheckBox("输出为扫描件（300dpi）")
        switches = (self.chk_organize, self.chk_band, self.chk_autorot,
                    self.chk_fixclip, self.chk_report, self.chk_recursive,
                    self.chk_scan)
        for chk in switches:
            chk.setChecked(chk is not self.chk_recursive)

        sw_rows = QVBoxLayout()
        sw_rows.setSpacing(6)
        for group in (switches[:3], switches[3:]):
            line = QHBoxLayout()
            line.setSpacing(20)
            for chk in group:
                line.addWidget(chk)
            line.addStretch(1)
            sw_rows.addLayout(line)

        sw_box = QHBoxLayout()
        sw_box.setSpacing(16)
        sw_box.addWidget(field_label("处理开关"), 0, Qt.AlignTop)
        sw_box.addLayout(sw_rows, 1)
        outer.addLayout(sw_box)

        # 排序方式 + 输出路径
        out_row = QHBoxLayout()
        out_row.setSpacing(12)
        out_row.addWidget(field_label("排序方式"))
        self.sort_combo = QComboBox()
        self.sort_combo.setMinimumWidth(186)
        for mode in core.SORT_ORDER:
            self.sort_combo.addItem(core.SORT_LABELS[mode])
        out_row.addWidget(self.sort_combo)
        out_row.addSpacing(14)
        out_row.addWidget(field_label("输出到"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("留空则输出到第一个文件所在目录")
        out_row.addWidget(self.path_edit, 1)
        out_row.addWidget(self._mkbtn("另存为", "folder-open", slot=self.pick_output))
        outer.addLayout(out_row)

        hint = QLabel(
            "整理顺序：酒店发票 → 差旅费报销单 → 火车 / 飞机票据 → 其他材料 → 票据粘贴单封面\n"
            "拼版：酒店 / 机票两张一页，其他材料三张一转一页，带间画裁切虚线便于裁剪")
        hint.setObjectName("hint")
        outer.addWidget(hint)
        return frame

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.setContentsMargins(2, 0, 2, 0)

        self.btn_run = self._mkbtn("开始合并", "play", "primary",
                                   "on_primary", self.start_merge)
        self.btn_cancel = self._mkbtn("取消", "square", slot=self.cancel_merge)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.setMinimumWidth(240)
        self.progress.setRange(0, 1)
        row.addWidget(self.progress)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")
        self.status_label.setMinimumWidth(110)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.status_label)
        return row

    def _build_log(self) -> QWidget:
        frame, outer = make_card("日志")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(104)
        self.log.setMaximumBlockCount(600)
        outer.addWidget(self.log)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_reveal = self._mkbtn("在访达中显示", "folder-search", slot=self.reveal_output)
        self.btn_print = self._mkbtn("打印", "printer", "accent",
                                     "on_accent", self.print_output)
        row.addWidget(self.btn_reveal)
        row.addWidget(self.btn_print)
        outer.addLayout(row)
        return frame

    # ---------- 目录记忆 ----------

    def _dialog_dir(self) -> str:
        if self.last_dir and self.last_dir.is_dir():
            return str(self.last_dir)
        desktop = Path.home() / "Desktop"
        return str(desktop if desktop.is_dir() else Path.home())

    def _remember_dir(self, path) -> None:
        if not path:
            return
        p = Path(path)
        folder = p if p.is_dir() else p.parent
        if not folder.is_dir():
            return
        self.last_dir = folder
        self.settings["last_dir"] = str(folder)
        core.save_settings(self.settings)

    # ---------- 文件列表 ----------

    def _set_files(self, paths: Sequence[Path], base_dir: Optional[Path] = None) -> None:
        self.files = list(paths)
        self.infos = {}
        self.last_report = None
        self.btn_reveal.setEnabled(False)
        self.btn_print.setEnabled(False)
        self._sync_output_path(base_dir)
        self._refresh_table()
        self.start_analysis()

    def _sync_output_path(self, base_dir: Optional[Path] = None) -> None:
        """输出路径跟着当前材料走，别留在上一次那个文件夹里。

        只在"用户没动过这个字段"时刷新：内容为空、或还是上次自动填的那个值。
        手输过、另存为选过的路径保留不动。
        """
        current = self.path_edit.text().strip()
        if current and current != self._auto_output:
            return
        if not self.files:
            self._auto_output = ""
            self.path_edit.setText("")
            return
        self._auto_output = str(core.default_output_path(
            self.files, base_dir.name if base_dir else None))
        self.path_edit.setText(self._auto_output)

    def _refresh_table(self) -> None:
        opts = self._options()
        self.table.setRowCount(len(self.files))
        for r, f in enumerate(self.files):
            si = self.infos.get(f)
            if si is None:
                cells = [f.name, "…", "", "", "", "分析中…"]
            elif not si.ok:
                cells = [f.name, "-", "-", "-", "-", si.error]
            else:
                sizes = sorted({(round(p.vis_w), round(p.vis_h)) for p in si.pages})
                size_txt = ", ".join("%dx%d" % s for s in sizes[:2])
                if len(sizes) > 2:
                    size_txt += " 等"
                notes = []
                if core.band_applies(si.category, opts):
                    notes.append("拼版")
                if any(p.clipped for p in si.pages):
                    notes.append("需恢复裁切")
                if any(p.fix_rotation for p in si.pages):
                    notes.append("需扶正")
                if not notes:
                    warns = sum(len(p.warnings) for p in si.pages)
                    if warns:
                        notes.append("%d 条提示" % warns)
                cells = [f.name, si.category_label, str(si.page_count),
                         si.orientation_summary(), size_txt, " ".join(notes)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, item)
        n = len(self.files)
        self.count_label.setText("共 %d 个文件" % n if n else "尚未添加文件")

    def selected_rows(self) -> List[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要合并的 PDF 文件", self._dialog_dir(),
            "PDF 文件 (*.pdf);;所有文件 (*)")
        if not paths:
            return
        self._remember_dir(paths[0])
        self._set_files([Path(p) for p in paths])

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择包含发票 PDF 的文件夹", self._dialog_dir())
        if not folder:
            return
        fdir = Path(folder)
        self._remember_dir(fdir)
        pdfs = core.list_pdfs(fdir, self.chk_recursive.isChecked())
        if not pdfs:
            QMessageBox.warning(self, "没有找到 PDF",
                                "该文件夹内没有 PDF 文件。\n可勾选「包含子文件夹」后重试。")
            return
        self._set_files(pdfs, base_dir=fdir)

    def clear_files(self) -> None:
        self.files = []
        self.infos = {}
        self.last_report = None
        self._sync_output_path(None)
        self._refresh_table()
        self.btn_reveal.setEnabled(False)
        self.btn_print.setEnabled(False)
        self.status_label.setText("就绪")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

    def remove_selected(self) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        for i in reversed(rows):
            if 0 <= i < len(self.files):
                self.files.pop(i)
        self._refresh_table()

    def move_one(self, delta: int) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        order = rows if delta < 0 else list(reversed(rows))
        moved = []
        for i in order:
            j = i + delta
            if 0 <= j < len(self.files):
                self.files[i], self.files[j] = self.files[j], self.files[i]
                moved.append(j)
            else:
                moved.append(i)
        self._refresh_table()
        self._select_rows(moved)

    def move_selected(self, direction: int) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        picked = [self.files[i] for i in rows]
        rest = [f for i, f in enumerate(self.files) if i not in rows]
        self.files = (picked + rest) if direction < 0 else (rest + picked)
        self._refresh_table()
        target = (list(range(len(picked))) if direction < 0
                  else list(range(len(rest), len(self.files))))
        self._select_rows(target)

    def _select_rows(self, rows: Sequence[int]) -> None:
        self.table.clearSelection()
        for r in rows:
            if 0 <= r < self.table.rowCount():
                self.table.selectRow(r)

    # ---------- 分析 ----------

    def start_analysis(self) -> None:
        if not self.files:
            return
        old = self.analyzer
        if old is not None and old.isRunning():
            old.requestInterruption()
            old.wait(2000)

        self.analyzer = AnalyzeWorker(self.files, self._options(), self)
        self.analyzer.one_done.connect(self._on_analyzed)
        self.analyzer.all_done.connect(self._after_analysis)
        self.analyzer.start()

    def _on_analyzed(self, path, info) -> None:
        self.infos[path] = info
        self._refresh_table()

    def _after_analysis(self) -> None:
        """分析完成后按业务顺序整理列表，并给出待处理页数概览。"""
        if self.chk_organize.isChecked():
            infos = [self.infos.get(f) for f in self.files]
            if infos and all(si is not None for si in infos):
                ok_infos = [si for si in infos if si.ok]
                failed = [si for si in infos if not si.ok]
                self.files = ([si.path for si in core.organize_sources(ok_infos)]
                              + [si.path for si in failed])
        self._refresh_table()

        ready = [si for si in (self.infos.get(f) for f in self.files) if si and si.ok]
        if ready:
            pages = sum(si.page_count for si in ready)
            out_pages = core.estimate_output_pages(ready, self._options())
            self.status_label.setText(
                "待合并 %d 页" % out_pages if out_pages == pages
                else "拼版后 %d 页（输入 %d）" % (out_pages, pages))

    # ---------- 选项 ----------

    def _options(self) -> core.Options:
        size = core.SIZE_A4_ROTATE
        for mode, rb in self.size_buttons.items():
            if rb.isChecked():
                size = mode
                break
        sort_mode = core.SORT_ORDER[max(0, self.sort_combo.currentIndex())]
        out = self.path_edit.text().strip()
        return core.Options(
            size_mode=size,
            sort_mode=sort_mode,
            auto_rotate=self.chk_autorot.isChecked(),
            fix_clipping=self.chk_fixclip.isChecked(),
            recursive=self.chk_recursive.isChecked(),
            output=Path(out).expanduser() if out else None,
            write_report=self.chk_report.isChecked(),
            organize=self.chk_organize.isChecked(),
            band_merge=self.chk_band.isChecked(),
            scan_output=self.chk_scan.isChecked(),
        )

    # ---------- 合并 ----------

    def start_merge(self) -> None:
        if not self.files:
            QMessageBox.information(self, "还没有材料", "请先选择 PDF 文件或文件夹。")
            return
        if self.worker is not None and self.worker.isRunning():
            return

        self.log.clear()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status_label.setText("正在合并…")
        self._set_running(True)

        self.worker = MergeWorker(self.files, self._options(), self)
        self.worker.logged.connect(self.log.appendPlainText)
        self.worker.progressed.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_merged)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def cancel_merge(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.log.appendPlainText("已请求取消…")

    def _on_progress(self, done: int, total: int) -> None:
        total = max(1, total)
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status_label.setText("%d / %d" % (done, total))

    def _on_merged(self, report: core.MergeReport) -> None:
        self._set_running(False)
        self.last_report = report
        if report.ok:
            self.progress.setValue(self.progress.maximum())
            self.status_label.setText("完成 %d 页" % report.written_pages)
            self.btn_reveal.setEnabled(True)
            self.btn_print.setEnabled(True)
            self.log.appendPlainText(
                "完成：%d 页 → %s" % (report.written_pages, report.output.name))
        else:
            self.status_label.setText("失败")
            self.log.appendPlainText("失败：%s" % report.error)
            QMessageBox.critical(self, "合并失败", report.error or "未知错误")

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self.status_label.setText("出错")
        self.log.appendPlainText("异常：%s" % message)
        QMessageBox.critical(self, "出错了", message)

    def _set_running(self, running: bool) -> None:
        self.btn_run.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        if running:
            self.btn_reveal.setEnabled(False)
            self.btn_print.setEnabled(False)

    # ---------- 输出动作 ----------

    def pick_output(self) -> None:
        initial = Path(self.path_edit.text().strip() or "发票合并.pdf")
        start = str(initial.parent) if initial.parent.is_dir() else self._dialog_dir()
        path, _ = QFileDialog.getSaveFileName(
            self, "保存合并结果", str(Path(start) / initial.name), "PDF 文件 (*.pdf)")
        if path:
            self._auto_output = ""       # 手动选过，之后换材料就不再自动改
            self.path_edit.setText(path)
            self._remember_dir(path)

    def reveal_output(self) -> None:
        if self.last_report and self.last_report.output:
            subprocess.run(["open", "-R", str(self.last_report.output)], check=False)

    def print_output(self) -> None:
        if not (self.last_report and self.last_report.output):
            return
        printers = core.list_printers()
        if not printers:
            if QMessageBox.question(
                self, "没有可用的打印机",
                "系统里没有检测到打印机。\n\n可以到「系统设置 → 打印机与扫描仪」添加后再打印。\n\n"
                "要现在用预览程序打开这份 PDF 手动打印吗？"
            ) == QMessageBox.Yes:
                subprocess.run(["open", str(self.last_report.output)], check=False)
            return

        dlg = PrintDialog(self.last_report.output, printers, self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, copies = dlg.selection()
        ok, msg = core.print_pdf(self.last_report.output, name, copies)
        if ok:
            self.log.appendPlainText("已送打印 → %s（%d 份）：%s" % (name, copies, msg))
            QMessageBox.information(
                self, "已提交打印任务",
                "已送到「%s」，共 %d 份。\n\n如果长时间没出纸，请确认打印机已开机、联机、有纸。"
                % (name, copies))
        else:
            QMessageBox.critical(self, "打印失败", msg)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def build_window() -> MainWindow:
    """构建主窗口但不进入事件循环 —— 供自检使用。"""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("发票合并小助手")
    # 先按上次记住的主题上色，再搭窗口，图标颜色才对得上
    pref = str(core.load_settings().get("theme", "") or "")
    C.apply(pref if pref in THEMES else DEFAULT_THEME)
    app.setStyleSheet(build_qss())
    return MainWindow()


def run() -> int:
    win = build_window()
    win.show()
    return QApplication.instance().exec()
