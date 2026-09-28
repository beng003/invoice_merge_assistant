#!/bin/bash
# 发票合并小助手 — 双击启动图形界面

cd "$(dirname "$0")" || exit 1

SELF="$PWD/invoice_merge.py"
if [ ! -f "$SELF" ]; then
    echo "找不到 invoice_merge.py，请确认本文件与主程序在同一目录。"
    read -r -p "按回车键退出…"
    exit 1
fi

# 依次尝试：本机预置环境 -> 当前项目环境 -> 系统 python
# 注意：Python 的图形界面还需要 Tcl/Tk 资源文件，优先选已验证可用的解释器。
CANDIDATES=(
    "/Users/beng003/.workbuddy/binaries/python/envs/invoice-tool/bin/python"
    "$PWD/.venv/bin/python"
    "/Users/beng003/.local/bin/python3"
    "$(command -v python3)"
)

for PY in "${CANDIDATES[@]}"; do
    [ -n "$PY" ] || continue
    [ -x "$PY" ] || continue
    if "$PY" -c "import pymupdf, tkinter" >/dev/null 2>&1; then
        exec "$PY" "$SELF" "$@"
    fi
done

echo "没有找到同时具备 PyMuPDF 和图形界面的 Python 环境。"
echo
echo "请在终端里执行下面这些命令，然后重新双击本文件："
echo "    cd \"$PWD\""
echo "    python3 -m venv .venv"
echo "    .venv/bin/pip install pymupdf numpy"
echo
echo "（若只想在命令行里合并，不需要图形界面："
echo "     python3 -m pip install pymupdf"
echo "     python3 invoice_merge.py --folder ./发票 ）"
echo
read -r -p "按回车键退出…"
exit 1
