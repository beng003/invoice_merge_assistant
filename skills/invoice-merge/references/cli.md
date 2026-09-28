# 命令行读法与校验

参数清单以 `python run.py --help` 为准，不要凭记忆猜。这里只讲 help 里看不出来的东西。

## 包装脚本

`scripts/run.py` 是透传包装（Python 写成，macOS / Linux / Windows 通用），**必须显式
带上 Python 路径运行**：`<python路径> <skill目录>/scripts/run.py <参数>`。用哪个解释器
由调用方（agent）指定，包装不做任何探测、也不换解释器 —— 拉起它的那个 Python 就是
干活的 Python。它只负责定位同目录的 `invoice_merge.py` 并把参数原样传过去，
从任意目录调用都可以。解释器没装 PyMuPDF 时退出码 2，stderr 提示装依赖。

工具本体 `invoice_merge.py` 就打包在同目录下，所以整个 skill 目录可以原样拷到别的机器。

覆盖手段：`INVOICE_MERGE_PY`（改用别的工具副本）。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 合并失败（`失败：…` 在 stderr），或部分文件被跳过时仍可能返回 0 —— 务必看 `跳过 N 项` |
| 2 | 用法错误（没给 `--files` 也没给 `--folder`，或文件夹不存在；不带参数跑包装脚本也是 2）；解释器没装 PyMuPDF 时同样是 2（stderr 为「缺少依赖 PyMuPDF…」） |
| 3 | 包装脚本找不到 `invoice_merge.py`（skill 目录被拆散过） |
| 4 | 包装脚本无法确定当前解释器（sys.executable 为空，罕见） |
| 130 | 用户中断 |

## 成功时的 stdout

```
拼版（带间已画裁切虚线）：酒店发票 1 页、火车 / 飞机票据 3 页、其他材料 3 页，共 7 页合排为 4 张纸
正在转为扫描件（300 dpi，不留文字层）…
正在写入 /path/合并.pdf
完成：8 页 -> 合并.pdf（3.4 MB，耗时 2.2 秒）

输出：/path/合并.pdf
页数：8    大小：3.4 MB
报告：/path/合并_报告.txt
```

取输出路径就抓 `^输出：` 这一行，报告路径是同名加 `_报告.txt`。

## `--dry-run` 的读法

每个文件一段，`命中「…」` 是分类依据，后面每页给方向与尺寸：

```
  [其他材料] 个人登机凭证 (1).pdf  共 1 页  纵向  命中「登机凭证」
      第 1页 纵向 595x842pt
输入 11 页（拼版省 3 页） → 预计输出 8 页
```

末行的"预计输出"就是最终页数（不含扫描件带来的变化）。拿它和合并后 `页数：` 对一下，
不一致说明有文件被跳过。

## 报告 txt

`xxx_报告.txt` 依次是：尺寸策略、输出形式、排序方式、输入/输出页数、`处理明细`
（每页做了什么）、`提示与警告`（哪些内容被缩小了）、`已跳过`。用户问"为什么这张小"
时去 `提示与警告` 里找 `内容放不下 1/N 页，已缩小到约 M%`。

## 校验输出的两条命令

页数与文字层（扫描件应为 0 字、每页 1 张图）：

```bash
"$PY" -c "import pymupdf as f,sys; d=f.open(sys.argv[1]); \
p=d[0]; print('页数',d.page_count,'首页文字',len(p.get_text()),'首页图片',len(p.get_images(full=True)))" 合并.pdf
```

肉眼确认版式 —— 渲染某一页成 PNG（把 `0` 换成页号；PNG 落临时目录，Windows 用
`%TEMP%\pg.png`），再用 Read 工具看那张图：

```bash
"$PY" -c "import pymupdf as f,sys; d=f.open(sys.argv[1]); \
d[0].get_pixmap(dpi=80).save(sys.argv[2])" 合并.pdf /tmp/pg.png
```

解释器不用另要 —— 就是你用来拉起 run.py 的那个（第一步选定的 `$PY`）。

## 装到新机器后验证

```bash
"<python路径>" <skill目录>/scripts/run.py --folder <某个真实票据目录> --dry-run
```

dry-run 能列出分类与页数，环境就算可用。这里的 `<python路径>` 就是你自己选定的解释器；
报「缺少依赖 PyMuPDF」（退出码 2）就先装依赖再跑。
（Windows 上解释器一般叫 `python`，macOS / Linux 叫 `python3`。）
