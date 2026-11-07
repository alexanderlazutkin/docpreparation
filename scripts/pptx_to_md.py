#!/usr/bin/env python3
"""PPTX -> Markdown для преп-пайплайна: презентации.

Источник: файл .pptx (PowerPoint / Impress). Извлекает всё содержательное:
  - текст фигур (уровни списков -> отступы), таблицы -> pipe-MD;
  - картинки -> figures/; SVG-оригиналы конвертируются в PNG (cairosvg,
    PowerPoint кладёт векторной парой к EMF-фолбэку), BMP/TIFF -> PNG (PIL);
    чистые EMF/WMF без SVG-пары локально не рендерятся — скип с варнингом;
  - встроенные диаграммы (embedded chart) -> заголовок + таблица данных
    (категории x серии) — векторный chart не имеет растра, данные честнее;
  - SmartArt -> текст узлов из ppt/diagrams/data*.xml (списком);
  - заметки докладчика -> блокцитой сразу под слайдом (для RAG локальность
    контента важнее «чистого» вида);
  - якорь слайда «⌗s.N» перед каждым слайдом (аналог ⌗p PDF / ⌗doc CHM:
    ссылка на оригинал, грепается, переживает чанкинг).

Раскладка (с 2026-11-07, A+B):
  - порядок чтения = ГЕОМЕТРИЯ (строки по вертикальному перекрытию, внутри
    строки — слева направо), а не z-order: на слайдах с десятками фигур
    z-order перемешивает «последовательность рассуждения»;
  - логические БЛОКИ: контейнер-плашка (фигура без текста) собирает лежащие
    внутри тексты/картинки в единицу «заголовок (первый текст, жирным при
    >=2 текстов и коротком заголовке) + тело»;
  - позиционные метки `<!-- x:%,y:%,w:% -->` (HTML-комментарий: человеку
    не виден, индексируется RAG, переживает чанкинг);
  - WIREFRAME-схема каждого слайда (SVG по реальным bbox с реальным
    текстом -> PNG, cairosvg) -> figures/slide_NN_layout.png, встраывается
    сразу после загорловка слайда; alt «Раскладка слайда N» — триггер для
    caption_figures.py дать слайдовый_synopsis (раскладка + суть + Mermaid
    потока) отдельным промптом.

Дедуп картинок по sha1 (одна картинка на N слайдов -> один файл, N ссылок).

Дальше — обычный пайплайн: caption_figures.py (--structured), clean_md.py,
qc_check.py --lang ru|en. Графика слайдов мельче скринов: стоит снизить
CAPTION_MIN_BYTES (напр. 4096), иначе часть диаграмм не получит описаний.

Перенос конвертера convert_pptx.py из репо design (2026-11-07): собственный
Ollama-клиент и кеш описаний убраны — captioning делает caption_figures.py;
markitdown не используется (собственная сборка покрывает его выход и точнее).

Usage: pptx_to_md.py [--out OUTBOX] [--min-image-bytes N] [--no-layouts] <файл.pptx> [...]
       pptx_to_md.py --selftest
"""

import argparse
import hashlib
import io
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import ClassVar

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SVG_BLIP = "{http://schemas.microsoft.com/office/drawing/2012/main}svgBlip"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# растры, которые рендерятся в MD-просмотрщиках и читаются VLM
RASTER_OK = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# блоки-контейнеры: фигура без текста и не мельче этой доли площади слайда
CONTAINER_MIN_AREA = 0.03
# заголовок блока: первый текст не длиннее этого (символов)
BLOCK_HEAD_MAX = 60


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Разбор blip picture-фигуры: raster rId + svg rId (векторная пара)
# ---------------------------------------------------------------------------
def blip_rids(pic_el) -> tuple:
    """(raster_rId, svg_rId) из <p:pic>; любой может быть None."""
    svg_rid = None
    for ext in pic_el.findall(f".//{{{NS_A}}}ext"):
        el = ext.find(SVG_BLIP)
        if el is not None and el.get(f"{{{NS_R}}}embed"):
            svg_rid = el.get(f"{{{NS_R}}}embed")
            break
    raster_rid = None
    blip = pic_el.find(f".//{{{NS_A}}}blip")
    if blip is not None:
        raster_rid = blip.get(f"{{{NS_R}}}embed")
    return raster_rid, svg_rid


def svg_to_png(svg_bytes: bytes) -> bytes:
    import cairosvg

    return cairosvg.svg2png(bytestring=svg_bytes)


def to_png(blob: bytes) -> bytes:
    """BMP/TIFF -> PNG через PIL."""
    from PIL import Image

    img = Image.open(io.BytesIO(blob))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fmt_val(v) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


# ---------------------------------------------------------------------------
# Хранилище фигур: дедуп по sha1, перезапись при ре-ранах
# ---------------------------------------------------------------------------
class FigureStore:
    def __init__(self, figs_dir: pathlib.Path):
        self.dir = figs_dir
        self.by_hash: dict[str, str] = {}

    def put(self, stem: str, ext: str, data: bytes) -> str:
        h = hashlib.sha1(data).hexdigest()
        if h in self.by_hash:
            return self.by_hash[h]
        name = f"{stem}{ext}"
        # имена media-частей уникальны внутри пакета; совпадение на диске —
        # следы прошлого прогона, перезаписываем (идемпотентные ре-раны)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)
        self.by_hash[h] = name
        return name


# ---------------------------------------------------------------------------
# Таблицы / диаграммы / SmartArt
# ---------------------------------------------------------------------------
def table_md(table) -> str:
    rows = []
    for row in table.rows:
        rows.append(
            [
                c.text.replace("\n", "<br>").replace("|", "\\|").strip()
                for c in row.cells
            ]
        )
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (n - len(r))
    out = "| " + " | ".join(rows[0]) + " |\n"
    out += "|" + "|".join(["---"] * n) + "|\n"  # без пробелов: конвенция marker/QC
    for r in rows[1:]:
        out += "| " + " | ".join(r) + " |\n"
    return out


def chart_md(chart) -> str:
    """Диаграмма -> заголовок + таблица данных (категории x серии)."""
    title = ""
    try:
        if chart.has_title:
            title = re.sub(r"\s+", " ", chart.chart_title.text_frame.text).strip()
    except Exception:  # noqa: BLE001, S110 — экзотические типы chart кидают что угодно
        pass
    try:
        plot = chart.plots[0]
        cats = [str(c) for c in plot.categories]
        sers = list(plot.series)
        head = ["Категория"]
        vals = []
        for i, s in enumerate(sers):
            try:
                name = s.name or f"Серия {i + 1}"
            except Exception:  # noqa: BLE001 — битая серия -> заглушка
                name = f"Серия {i + 1}"
            head.append(re.sub(r"\n", " ", str(name)))
            try:
                vals.append([_fmt_val(v) for v in (s.values or [])])
            except Exception:  # noqa: BLE001 — битая серия -> пустые значения
                vals.append([])
        lines = [
            "| " + " | ".join(head) + " |",
            "|" + "|".join(["---"] * len(head)) + "|",
        ]
        for j, c in enumerate(cats):
            lines.append(
                "| "
                + " | ".join(
                    [c]
                    + [
                        (vals[i][j] if j < len(vals[i]) else "")
                        for i in range(len(sers))
                    ]
                )
                + " |"
            )
        body = "\n".join(lines)
    except Exception:  # noqa: BLE001 — нестандартный plot: честная заглушка
        body = "_(данные диаграммы не извлечены)_"
    t = f": {title}" if title else ""
    return f"**Диаграмма{t}**\n\n{body}\n"


def smartart_bullets(slide) -> list[str]:
    """Тексты узлов SmartArt слайда (diagramData-части его rels)."""
    texts: list[str] = []
    for rel in slide.part.rels.values():
        if not rel.reltype.endswith("/diagramData"):
            continue
        try:
            root = ET.fromstring(rel.target_part.blob)
            texts += [
                re.sub(r"\s+", " ", t.text).strip()
                for t in root.iter(f"{{{NS_A}}}t")
                if t.text and t.text.strip()
            ]
        except Exception:  # noqa: BLE001, S112 — битая diagramData-часть: скип
            continue
    seen, uniq = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return [f"- {t}" for t in uniq] if uniq else []


# ---------------------------------------------------------------------------
# Геометрия: единицы контента, контейнеры-блоки, порядок чтения
# ---------------------------------------------------------------------------
@dataclass
class Stats:
    figures: int = 0
    skipped_emf: int = 0
    skipped_small: int = 0
    charts: int = 0
    smartart: int = 0
    notes: int = 0
    skipped_svg_conv: int = 0
    layouts: int = 0


def _part(slide_part, rid):
    try:
        return slide_part.rels[rid].target_part
    except Exception:  # noqa: BLE001 — нет rel/битая часть -> нет картинки
        return None


def picture_lines(
    shape, slide_part, figs: FigureStore, min_bytes: int, st: Stats
) -> list:
    r_rid, s_rid = blip_rids(shape._element)
    data = ext = stem = None

    if s_rid:  # векторная пара (svg + emf-фолбэк) — берём SVG
        part = _part(slide_part, s_rid)
        if part is not None:
            try:
                data, ext, stem = (
                    svg_to_png(part.blob),
                    ".png",
                    pathlib.Path(str(part.partname)).stem,
                )
            except Exception:  # noqa: BLE001 — кривой SVG -> фолбэк на растр
                st.skipped_svg_conv += 1

    if data is None and r_rid:
        part = _part(slide_part, r_rid)
        if part is not None:
            blob = part.blob
            ext = "." + str(part.partname).rsplit(".", 1)[-1].lower()
            stem = pathlib.Path(str(part.partname)).stem
            if ext in RASTER_OK:
                if len(blob) >= min_bytes:
                    data = blob
                else:
                    st.skipped_small += 1
            elif ext in (".bmp", ".tiff", ".tif"):
                try:
                    data, ext = to_png(blob), ".png"
                except Exception:  # noqa: BLE001 — битый bmp/tiff -> скип
                    data = None
                if data is not None and len(blob) < min_bytes:
                    st.skipped_small += 1
                    data = None
            elif ext in (".emf", ".wmf"):
                st.skipped_emf += 1
                return []
            else:
                log(f"  [!] скип {stem}{ext}: неизвестный формат")
                return []

    if data is None:
        return []
    name = figs.put(stem or "image", ext, data)
    st.figures += 1
    return [f"![{shape.name}](figures/{name})"]


def shape_lines(shape, slide_part, figs, min_bytes, st) -> list:
    """Контент одной фигуры (без геометрии): картинка/таблица/диаграмма/текст."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    out: list = []
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            out += shape_lines(child, slide_part, figs, min_bytes, st)
        return out
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return picture_lines(shape, slide_part, figs, min_bytes, st)
    if getattr(shape, "has_table", False):
        # ведущая пустая строка: GFM-таблица не может прерывать абзац
        out.append("")
        out.append(table_md(shape.table))
        return out
    if getattr(shape, "has_chart", False):
        st.charts += 1
        out.append("")
        out.append(chart_md(shape.chart))
        return out
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        for p in shape.text_frame.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            lvl = min(p.level or 0, 3)
            out.append(("", "  - ", "    - ", "      - ")[lvl] + t)
    return out


@dataclass
class Node:
    """Единица дерева слайда: контент-фигура, группа или контейнер-блок."""

    shape: object
    l: float
    t: float
    w: float
    h: float  # нормированные доли слайда (0..1)
    children: list = None
    is_container: bool = False

    def __post_init__(self):
        if self.children is None:
            self.children = []


def _kind(shape):
    """('picture'|'table'|'chart'|'text'|'group'|'blank', контент_есть)."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return "picture", True
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return "group", True
    if getattr(shape, "has_table", False):
        return "table", True
    if getattr(shape, "has_chart", False):
        return "chart", True
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return "text", True
    return "blank", False


def slide_nodes(slide, title_el, W, H) -> list:
    """Лес фигур слайда: контейнеры-блоки собирают фигуры, чей центр внутри."""
    raw = []
    for sh in slide.shapes:
        if sh._element is title_el:
            continue
        kind, has = _kind(sh)
        if kind == "blank":
            continue  # линии/декор без текста — только в wireframe
        l, t = max(0.0, sh.left / W), max(0.0, sh.top / H)
        w, h = min(1.0, sh.width / W), min(1.0, sh.height / H)
        if kind == "blank" or not has:
            continue
        node = Node(sh, l, t, w, h, is_container=False)
        node.kind = kind
        raw.append(node)

    # контейнеры: blank-фигуры достаточной площади (плашки/подложки блоков)
    for sh in slide.shapes:
        if sh._element is title_el:
            continue
        kind, has = _kind(sh)
        if kind != "blank" or has:
            continue
        w, h = sh.width / W, sh.height / H
        if w * h < CONTAINER_MIN_AREA:
            continue  # мелкий декор — не блок
        raw.append(
            Node(
                sh,
                max(0.0, sh.left / W),
                max(0.0, sh.top / H),
                min(1.0, w),
                min(1.0, h),
                is_container=True,
            )
        )

    # разделяй: контейнеры и контент; контент прикрепляем к САМОМУ МАЛЕНЬКОМУ
    # контейнеру, содержащему его центр (полосы-подложки уступают колонкам)
    containers = sorted(
        (n for n in raw if n.is_container), key=lambda n: n.w * n.h, reverse=True
    )
    units = [n for n in raw if not n.is_container]
    top: list = []
    for c in containers:
        placed = False
        for bigger in top:
            if bigger.is_container and _contains(bigger, c):
                bigger.children.append(c)
                placed = True
                break
        if not placed:
            top.append(c)
    for u in units:
        best = None
        for c in containers:
            if _contains(c, u) and (best is None or c.w * c.h < best.w * best.h):
                best = c
        (best.children if best else top).append(u)
    return top


def _contains(box, node) -> bool:
    cx, cy = node.l + node.w / 2, node.t + node.h / 2
    return box.l <= cx <= box.l + box.w and box.t <= cy <= box.t + box.h


def reading_order(nodes: list) -> list:
    """Строки по вертикальному перекрытию (>=40% меньшей высоты), в строке —
    слева направо. Приближение человеческого порядка чтения."""
    ordered = sorted(nodes, key=lambda n: (n.t, n.l))
    rows, cur = [], []
    for n in ordered:
        if not cur or _yoverlap(cur, n) >= 0.4:
            cur.append(n)
        else:
            rows.append(cur)
            cur = [n]
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        out += sorted(row, key=lambda n: n.l)
    return out


def _yoverlap(row: list, n: Node) -> float:
    """Перекрытие n с диапазоном строки / минимум высот."""
    t0 = min(r.t for r in row)
    b0 = max(r.t + r.h for r in row)
    lo, hi = max(t0, n.t), min(b0, n.t + n.h)
    minh = min(b0 - t0, n.h)
    return max(0.0, hi - lo) / max(1e-6, minh)


def pos_comment(n: Node) -> str:
    return f"<!-- x:{n.l:.0%},y:{n.t:.0%},w:{n.w:.0%} -->"


def emit_node(n: Node, slide_part, figs, min_bytes, st) -> list:
    """Node -> строки MD: блок (первый текст — заголовок) либо контент."""
    if n.is_container:
        kids = reading_order(n.children)
        lines = []
        texts = [k for k in kids if getattr(k, "kind", "text") == "text"]
        head_done = False
        for k in kids:
            piece = emit_node(k, slide_part, figs, min_bytes, st)
            if not piece:
                continue
            if (
                not head_done
                and getattr(k, "kind", "text") == "text"
                and len(texts) >= 2
                and len(k.shape.text_frame.text) <= BLOCK_HEAD_MAX
            ):
                # юнит-коммент снимаем: блок подписан своим контейнером
                head = re.sub(r"\s*<!--[^>]*-->\s*$", "", piece[0]).strip()
                lines.append(f"**{head}** {pos_comment(n)}")
                lines += piece[1:]
                head_done = True
            else:
                lines += piece
        return lines
    lines = shape_lines(n.shape, slide_part, figs, min_bytes, st)
    if not lines:
        return []
    if lines[0] == "":
        # таблица/диаграмма: коммент отдельной строкой, вокруг — пустые
        # (GFM: pipe-таблица не может прерывать абзац/HTML-блок)
        return ["", pos_comment(n), "", *lines[1:]]
    lines[0] = f"{lines[0]} {pos_comment(n)}"
    return lines


# ---------------------------------------------------------------------------
# Цвета темы/фигур (для цветного wireframe)
# ---------------------------------------------------------------------------
# MSO_THEME_COLOR -> ключ <a:clrScheme> темы (sysClr/srgbClr -> #RRGGBB)
THEME_KEYS = {
    "DARK_1": "dk1",
    "LIGHT_1": "lt1",
    "DARK_2": "dk2",
    "LIGHT_2": "lt2",
    "ACCENT_1": "accent1",
    "ACCENT_2": "accent2",
    "ACCENT_3": "accent3",
    "ACCENT_4": "accent4",
    "ACCENT_5": "accent5",
    "ACCENT_6": "accent6",
    "TEXT_1": "dk1",
    "BACKGROUND_1": "lt1",
    "TEXT_2": "dk2",
    "BACKGROUND_2": "lt2",
    "HYPERLINK": "hlink",
    "FOLLOWED_HYPERLINK": "folHlink",
}


def theme_colors(pptx_path: pathlib.Path) -> dict:
    """<a:clrScheme> первой темы пакета -> {'dk1': '#RRGGBB', ...}."""
    out = {}
    try:
        with zipfile.ZipFile(pptx_path) as z:
            name = next(
                (n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml", n)),
                None,
            )
            if not name:
                return out
            scheme = ET.fromstring(z.read(name)).find(f".//{{{NS_A}}}clrScheme")
            if scheme is None:
                return out
            for child in scheme:
                tag = child.tag.split("}")[1]
                srgb = child.find(f"{{{NS_A}}}srgbClr")
                sysc = child.find(f"{{{NS_A}}}sysClr")
                if srgb is not None and srgb.get("val"):
                    out[tag] = f"#{srgb.get('val')}"
                elif sysc is not None and sysc.get("lastClr"):
                    out[tag] = f"#{sysc.get('lastClr')}"
    except Exception:  # noqa: BLE001, S110 — без темы рисуем нейтрально
        pass
    return out


def _fill_hex(shape, theme: dict):
    """Заливка фигуры -> '#RRGGBB' | None (нет/фоновая/неразборчивая)."""
    from pptx.enum.dml import MSO_COLOR_TYPE, MSO_FILL_TYPE

    try:
        f = shape.fill
        if f.type != MSO_FILL_TYPE.SOLID:
            return None
        c = f.fore_color
        if c.type == MSO_COLOR_TYPE.RGB:
            return f"#{c.rgb}"
        if c.type == MSO_COLOR_TYPE.SCHEME:
            key = THEME_KEYS.get(str(c.theme_color).split(" ")[0].upper())
            return theme.get(key)
    except Exception:  # noqa: BLE001, S110 — цвет опционален, дефолт нейтральный
        pass
    return None


def _text_color(text_frame, theme: dict):
    """Явный цвет шрифта первого резолвящегося run -> '#RRGGBB' | None."""
    from pptx.enum.dml import MSO_COLOR_TYPE

    try:
        for p in text_frame.paragraphs:
            for r in p.runs:
                c = r.font.color
                if c and c.type == MSO_COLOR_TYPE.RGB:
                    return f"#{c.rgb}"
                if c and c.type == MSO_COLOR_TYPE.SCHEME:
                    key = THEME_KEYS.get(str(c.theme_color).split(" ")[0].upper())
                    if theme.get(key):
                        return theme[key]
    except Exception:  # noqa: BLE001, S110 — цвет опционален
        pass
    return None


def _lum(hexcolor: str) -> float:
    """Относительная светимость 0..1 (для выбора контрастного текста)."""
    try:
        r, g, b = (int(hexcolor[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return 1.0
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _contrast_text(bg: str) -> str:
    return "#111111" if _lum(bg) > 0.5 else "#ffffff"


# ---------------------------------------------------------------------------
# Цветной wireframe слайда: bbox реальные, заливки/цвета — из pptx
# ---------------------------------------------------------------------------
WF_W = 1280  # ширина PNG
WF_FONT = 15


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap(text: str, width_px: int, font: int = WF_FONT) -> list:
    """Жадная разбивка по ширине блока (0.6*font на символ — sans-serif)."""
    maxch = max(6, int(width_px / (0.6 * font)))
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) + 1 > maxch:
            if cur:
                lines.append(cur)
            cur = w_
        else:
            cur = f"{cur} {w_}".strip()
    if cur:
        lines.append(cur)
    return lines[:4] + (["…"] if len(lines) > 4 else [])


def table_grid_svg(table, frame, theme) -> list:
    """Таблица в wireframe: сетка по реальным ширинам колонок pptx + текст
    ячеек (wrap по ширине ячейки, шапка жирным). -> список SVG-элементов."""
    x0, y0, fw, fh = frame
    try:
        cols = [c.width for c in table.columns]
        rows_n, cols_n = len(table.rows), len(table.columns)
    except Exception:  # noqa: BLE001 — битая геометрия: плоская сетка
        cols, rows_n, cols_n = [], len(table.rows), len(table.columns)
    total = sum(cols) or 1
    if not cols or cols_n == 0:
        xs = [x0 + int(fw * i / cols_n) for i in range(cols_n + 1)]
    else:
        acc, xs = 0, [x0]
        for cw in cols[:-1]:
            acc += cw
            xs.append(x0 + int(fw * acc / total))
        xs.append(x0 + fw)
    rh = max(WF_FONT + 4, fh // max(1, rows_n))
    out = [
        (
            f'<rect x="{x0}" y="{y0}" width="{fw}" height="{fh}" fill="#ffffff" '
            f'stroke="#555"/>'
        )
    ]
    f = WF_FONT - 2
    for r in range(rows_n):
        y = y0 + r * rh
        if r:
            out.append(
                f'<line x1="{x0}" y1="{y}" x2="{x0 + fw}" y2="{y}" stroke="#999"/>'
            )
        for c in range(cols_n):
            try:
                txt = " ".join(table.cell(r, c).text.split())
            except Exception:  # noqa: BLE001 — битая ячейка: пусто
                txt = ""
            if not txt:
                continue
            cw = xs[c + 1] - xs[c]
            ls = _wrap(txt, cw - 6, f)[: (2 if r else 4)]
            ty = y + f
            bold = ' font-weight="bold"' if r == 0 else ""
            for ln in ls:
                out.append(
                    f'<text x="{xs[c] + 3}" y="{ty}" font-family="sans-serif" '
                    f'font-size="{f}" fill="#111"{bold}>{_esc(ln)}</text>'
                )
                ty += f + 1
    return out


def wireframe_svg(slide, n_slide: int, title: str, W, H, theme=None) -> str:
    """Цветная SVG-реконструкция слайда: bbox реальные, заливки — реальные
    цвета pptx (solid-заливки фигур + тема), текст — контрастный к подложке,
    картинки/таблицы/диаграммы — плейсхолдеры. Два прохода: подложки/фигуры
    в z-order, текст поверх."""
    theme = theme or {}
    k = WF_W / W

    def px(v):
        return int(v * k)

    def geom(sh):
        return (
            px(sh.left),
            px(sh.top),
            max(1, px(sh.width)),
            max(1, px(sh.height)),
        )

    bg_slide = None
    try:
        bg_slide = _fill_hex(slide.background.fill, theme)
    except Exception:  # noqa: BLE001, S110 — фон опционален
        pass

    el = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WF_W}" '
            f'height="{int(H * k)}" viewBox="0 0 {WF_W} {int(H * k)}">'
        ),
        f'<rect width="100%" height="100%" fill="{bg_slide or "#ffffff"}"/>',
        (
            f'<text x="16" y="28" font-family="sans-serif" font-size="20" '
            f'font-weight="bold" fill="#333">{_esc(title)} — слайд {n_slide}</text>'
        ),
    ]

    # подложки для контраста текста: solid-заливки blank-фигур (плашки/ленты)
    plates = []
    texts = []
    for sh in slide.shapes:
        kind, has = _kind(sh)
        if kind == "blank" and not has:
            fill = _fill_hex(sh, theme)
            if fill:
                plates.append((geom(sh), fill))
    for sh in slide.shapes:
        kind, has = _kind(sh)
        if kind != "text" or not has:
            continue
        x, y, w, h = geom(sh)
        # подложка: самая маленькая solid-плашка, содержащая центр текстбокса
        cx, cy = x + w / 2, y + h / 2
        under = None
        for (gx, gy, gw, gh), fill in plates:
            if (
                gx <= cx <= gx + gw
                and gy <= cy <= gy + gh
                and (under is None or gw * gh < under[1])
            ):
                under = ((gx, gy, gw, gh), gw * gh, fill)
        bg_local = under[2] if under else (bg_slide or "#ffffff")
        explicit = _text_color(sh.text_frame, theme)
        if explicit:
            color = explicit
            chip = _lum(color) > 0.85 and _lum(bg_local) > 0.6
        else:
            color = _contrast_text(bg_local)
            chip = False
        texts.append((x, y, w, h, sh.text_frame, color, chip, bg_local))

    for (gx, gy, gw, gh), fill in plates:
        el.append(
            f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" '
            f'fill="{fill}" stroke="none"/>'
        )
    for sh in slide.shapes:
        kind, has = _kind(sh)
        if kind == "table" and has:
            el += table_grid_svg(sh.table, geom(sh), theme)
    for sh in slide.shapes:
        kind, has = _kind(sh)
        x, y, w, h = geom(sh)
        if kind in ("text", "blank", "table"):
            continue  # текст — финальным проходом; blank-декор — не рисуем
        fill = _fill_hex(sh, theme)
        label = {
            "picture": "[img]",
            "chart": "[диаграмма]",
            "group": "[группа]",
        }.get(kind, "?")
        el.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="{fill or "#e8e8f8"}" stroke="#88a"/>'
        )
        el.append(
            f'<text x="{x + 4}" y="{y + WF_FONT}" font-family="sans-serif" '
            f'font-size="{WF_FONT}" fill="{_contrast_text(fill or "#e8e8f8")}">'
            f"{_esc(label)}</text>"
        )
    for x, y, w, h, tf, color, chip, bg_local in texts:
        txt = " ".join(tf.text.split())
        ls = _wrap(txt, w) or [" "]
        el.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" '
            f'stroke="{bg_local}" stroke-opacity="0.25" stroke-dasharray="3 3"/>'
        )
        ty = y + WF_FONT
        for ln in ls:
            if chip:  # светлый текст на светлом фоне — тёмная плашка-бейдж
                el.append(
                    f'<rect x="{x}" y="{ty - WF_FONT + 2}" width="{w}" '
                    f'height="{WF_FONT + 4}" rx="4" fill="#2b2b2b"/>'
                )
            el.append(
                f'<text x="{x + 4}" y="{ty}" font-family="sans-serif" '
                f'font-size="{WF_FONT}" fill="{color}">{_esc(ln)}</text>'
            )
            ty += WF_FONT + 2
    el.append("</svg>")
    return "".join(el)


# ---------------------------------------------------------------------------
# Конвертация одного файла
# ---------------------------------------------------------------------------
def convert_pptx(
    pptx_path: pathlib.Path, out_root: str, title, min_bytes: int, layouts=True
) -> tuple:
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    title = title or pptx_path.stem
    out_dir = pathlib.Path(out_root) / re.sub(r"[^\w\-. ]", "_", title)[:80]
    figs = FigureStore(out_dir / "figures")
    st = Stats()
    W, H = prs.slide_width, prs.slide_height
    theme = theme_colors(pptx_path)
    parts = [f"# {title}\n"]

    for n, slide in enumerate(prs.slides, 1):
        head = f"## Слайд {n}"
        tshape = slide.shapes.title
        title_el = tshape._element if tshape is not None else None
        stitle = re.sub(r"\s+", " ", tshape.text).strip() if tshape is not None else ""
        if stitle:
            head += ". " + stitle
        parts.append(f"\n\n{head}\n\n⌗s.{n}\n\n")

        if layouts:
            try:
                png = svg_to_png(
                    wireframe_svg(slide, n, stitle or title, W, H, theme).encode()
                )
                name = figs.put(f"slide_{n:02d}_layout", ".png", png)
                st.layouts += 1
                parts.append(f"![Раскладка слайда {n}](figures/{name})\n\n")
            except Exception:  # noqa: BLE001, S110 — wireframe не критичен
                pass

        for node in reading_order(slide_nodes(slide, title_el, W, H)):
            block = "\n".join(emit_node(node, slide.part, figs, min_bytes, st))
            if block.strip():
                parts.append(block + "\n")

        sa = smartart_bullets(slide)
        if sa:
            st.smartart += 1
            parts.append("**SmartArt:**\n" + "\n".join(sa) + "\n")

        try:
            if slide.has_notes_slide:
                nt = slide.notes_slide.notes_text_frame.text.strip()
                if nt:
                    st.notes += 1
                    parts.append("\n> **Заметки:** " + nt.replace("\n", "\n> ") + "\n")
        except Exception:  # noqa: BLE001, S110 — заметки опциональны, слайд важнее
            pass

    md = re.sub(r"\n{4,}", "\n\n\n", "".join(parts)) + "\n"
    md_file = out_dir / f"{title}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_file.write_text(md, encoding="utf-8")
    meta = {
        "source": str(pptx_path),
        "title": title,
        "slides": len(prs.slides),
        "figures": st.figures,
        "layouts": st.layouts,
        "charts": st.charts,
        "smartart_sections": st.smartart,
        "notes": st.notes,
        "skipped": {
            "emf_wmf_no_svg": st.skipped_emf,
            "small_raster": st.skipped_small,
            "svg_convert_fail": st.skipped_svg_conv,
        },
    }
    (out_dir / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(
        f"pptx_to_md: {title}: слайдов {len(prs.slides)}, фигур {st.figures}, "
        f"wireframe {st.layouts}, диаграмм {st.charts}, SmartArt {st.smartart}, "
        f"заметок {st.notes}, скипнуто (emf/мелкие/svg-fail) "
        f"{st.skipped_emf}/{st.skipped_small}/{st.skipped_svg_conv} -> {md_file}"
    )
    return 0, meta


# ---------------------------------------------------------------------------
# Selftest: синтетический pptx (python-pptx + PIL), без сети и сервисов
# ---------------------------------------------------------------------------
def selftest() -> int:
    import tempfile

    from PIL import Image
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # --- чистые функции ---------------------------------------------------
    png = svg_to_png(
        b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>"
        b"<rect width='10' height='10' fill='red'/></svg>"
    )
    check("svg->png конвертация", png[:8] == PNG_MAGIC)

    xml = (
        f'<p:pic xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        f' xmlns:a="{NS_A}" xmlns:r="{NS_R}"><p:blipFill><a:blip r:embed="rId4">'
        f'<a:extLst><a:ext uri="{{962A43B1-9C74-41CD-89C9-C4FAC5FC1F49}}">'
        f'<svg:svgBlip xmlns:svg="http://schemas.microsoft.com/office/drawing/2012/main"'
        f' r:embed="rId9"/></a:ext></a:extLst></a:blip></p:blipFill></p:pic>'
    )
    check("blip_rids: raster+svg", blip_rids(ET.fromstring(xml)) == ("rId4", "rId9"))
    check(
        "blip_rids: без svg",
        blip_rids(ET.fromstring(xml.replace(' r:embed="rId9"', ""))) == ("rId4", None),
    )
    check("wrap: обрезка по ширине", len(_wrap("слово " * 30, 100)) <= 5)
    check(
        "wireframe_svg: валидный XML с текстом",
        "слайд 1" in wireframe_svg(_fake_slide(), 1, "Т", 12192000, 6858000),
    )

    # --- синтетический pptx ------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        img_f = td / "pic.png"
        Image.new("RGB", (60, 40), "#336699").save(img_f)

        prs = Presentation()
        s1 = prs.slides.add_slide(prs.slide_layouts[0])
        s1.shapes.title.text = "Титульный слайд"
        s1.placeholders[1].text = "Подзаголовок"

        s2 = prs.slides.add_slide(prs.slide_layouts[5])
        # z-order намеренно ПРОТИВ визуального порядка: низ добавлен раньше верха
        s2.shapes.add_textbox(
            Inches(1), Inches(4), Inches(4), Inches(1)
        ).text_frame.text = "Нижний текст"
        s2.shapes.add_textbox(
            Inches(1), Inches(1), Inches(4), Inches(1)
        ).text_frame.text = "Верхний текст"
        tb = s2.shapes.add_textbox(Inches(6), Inches(1), Inches(4), Inches(1))
        tb.text_frame.text = "Первый пункт"
        p1 = tb.text_frame.add_paragraph()
        p1.text = "Уровень 1"
        p1.level = 1
        s2.shapes.add_picture(str(img_f), 0, 0)
        tbl = s2.shapes.add_table(2, 2, 0, Inches(6), Inches(3), Inches(2)).table
        tbl.cell(0, 0).text = "Ключ"
        tbl.cell(0, 1).text = "Зна|чение"
        tbl.cell(1, 0).text = "a"
        tbl.cell(1, 1).text = "b"
        g = s2.shapes.add_group_shape()
        gtx = g.shapes.add_textbox(0, 0, Inches(2), Inches(1))
        gtx.text_frame.text = "текст в группе"
        s2.notes_slide.notes_text_frame.text = "Заметка спикера"

        s3 = prs.slides.add_slide(prs.slide_layouts[5])
        s3.shapes.add_picture(str(img_f), 0, 0)  # та же картинка -> дедуп
        cd = CategoryChartData()
        cd.categories = ["Q1", "Q2"]
        cd.add_series("Продажи", (10, 20))
        s3.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, 0, Inches(3), Inches(4), Inches(3), cd
        )
        # блок-контейнер: плашка (blank, без текста) + 2 текста внутри
        from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

        plate = s3.shapes.add_shape(
            MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE,
            Inches(6),
            Inches(1),
            Inches(4),
            Inches(3),
        )
        plate.fill.solid()
        plate.fill.fore_color.rgb = __import__(
            "pptx.dml.color", fromlist=["RGBColor"]
        ).RGBColor(0x33, 0x66, 0x99)
        plate.line.color.rgb = __import__(
            "pptx.dml.color", fromlist=["RGBColor"]
        ).RGBColor(0x99, 0x99, 0x99)
        s3.shapes.add_textbox(
            Inches(6.2), Inches(1.2), Inches(3), Inches(0.5)
        ).text_frame.text = "Заголовок блока"
        s3.shapes.add_textbox(
            Inches(6.2), Inches(2.0), Inches(3), Inches(1)
        ).text_frame.text = "Тело блока"

        f = td / "test.pptx"
        prs.save(f)

        out = td / "outbox"
        rc, meta = convert_pptx(f, out, None, min_bytes=64)
        md = (out / "test" / "test.md").read_text(encoding="utf-8")

        check("rc=0", rc == 0)
        check("H1 один, в начале", md.count("# test") == 1 and md.startswith("# test"))
        check("якорь ⌗s.1", "⌗s.1" in md and "⌗s.3" in md)
        check("заголовок слайда из title", "## Слайд 1. Титульный слайд" in md)
        check("титул не задвоен", md.count("Титульный слайд") == 1)
        check(
            "гео-порядок: верх раньше низа",
            md.find("Верхний текст") < md.find("Нижний текст"),
        )
        check("текст уровня 1 отступом", "\n  - Уровень 1" in md)
        check("группа рекурсией", "текст в группе" in md)
        check(
            "фигуры разделены переводом строки",
            "Первый пункт <!--" in md and "\nтекст в группе <!--" in md,
        )
        check(
            "таблица pipe + эскейп |",
            "| Ключ | Зна\\|чение |" in md and "| a | b |" in md,
        )
        check(
            "пустая строка перед таблицей (GFM)",
            "\n\n| Ключ |" in md and " \n| Ключ |" not in md,
        )
        svg2 = wireframe_svg(
            prs.slides[1], 2, "T", prs.slide_width, prs.slide_height, theme_colors(f)
        )
        check(
            "wireframe: сетка таблицы с текстом ячеек",
            "Ключ" in svg2 and "<line" in svg2,
        )
        check(
            "картинка: файл + ссылка",
            (out / "test" / "figures" / "image1.png").is_file()
            and "](figures/image1.png)" in md,
        )
        check(
            "дедуп: 2 вставки, 1 файл",
            len([p for p in (out / "test" / "figures").iterdir() if "image" in p.name])
            == 1
            and md.count("](figures/image1.png)") == 2,
        )
        check(
            "диаграмма: данные таблицей",
            "**Диаграмма" in md and "| Q1 | 10 |" in md and "| Q2 | 20 |" in md,
        )
        check("заметки инлайн под слайдом", "> **Заметки:** Заметка спикера" in md)
        check(
            "блок-контейнер: заголовок жирным + тело",
            "**Заголовок блока** <!--" in md and "Тело блока" in md,
        )
        check("позиционные метки <!-- x:", "<!-- x:" in md and ",w:" in md)
        th = theme_colors(f)
        check(
            "theme_colors: dk1 + accent1 из theme1.xml",
            th.get("dk1", "").lower() in ("#000000", "#0")
            and th.get("accent1", "").startswith("#"),
        )
        check(
            "контраст: тёмный фон -> белый текст",
            _contrast_text("#336699") == "#ffffff"
            and _contrast_text("#ffffff") == "#111111",
        )
        svg3 = wireframe_svg(
            prs.slides[2], 3, "T", prs.slide_width, prs.slide_height, th
        )
        check("цветной wireframe: заливка плашки в SVG", "#336699" in svg3)
        check(
            "wireframe: PNG + ссылка с alt",
            (out / "test" / "figures" / "slide_01_layout.png").is_file()
            and "![Раскладка слайда 1](figures/slide_01_layout.png)" in md
            and (out / "test" / "figures" / "slide_01_layout.png").read_bytes()[:8]
            == PNG_MAGIC,
        )
        check(
            "meta.json",
            (out / "test" / "_meta.json").is_file() and meta["figures"] == 2,
        )
        check(
            "meta: charts/notes/layouts",
            meta["charts"] == 1 and meta["notes"] == 1 and meta["layouts"] == 3,
        )

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'}  {n}")
    print(f"selftest pptx_to_md: {len(checks) - len(bad)}/{len(checks)}")
    return 1 if bad else 0


def _fake_slide():
    """Утка-слайд для wireframe_svg (без python-pptx в тесте геометрии)."""

    class Shp:
        def __init__(self, kind, l, t, w, h, text=""):
            self.shape_type = kind
            self.left, self.top, self.width, self.height = l, t, w, h
            self.has_text_frame = kind == "text"
            if kind == "text":
                self.text_frame = type("TF", (), {"text": text})()

    class Slide:
        slide_width, slide_height = 12192000, 6858000
        shapes: ClassVar = [
            Shp("text", 0, 0, 9144000, 914400, "Заголовок слайда"),
            Shp("blank", 0, 1828800, 9144000, 4572000),
            Shp("picture", 457200, 2743200, 914400, 914400),
        ]

    return Slide()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "files", nargs="*", type=pathlib.Path, help=".pptx (один или несколько)"
    )
    ap.add_argument("--out", default="outbox", help="выходной корень (деф. outbox)")
    ap.add_argument(
        "--min-image-bytes",
        type=int,
        default=4096,
        help="мин. размер растровой картинки (деф. 4096 — логотипы/декор в отброс; SVG не фильтруется)",
    )
    ap.add_argument(
        "--no-layouts",
        action="store_true",
        help="не генерировать wireframe-схемы слайдов",
    )
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    files = [
        f
        for f in a.files
        if f.suffix.lower() == ".pptx" and not f.name.startswith("~$")
    ]
    if not files:
        sys.exit("usage: pptx_to_md.py [--out OUTBOX] <файл.pptx> [...] | --selftest")
    rc = 0
    for f in files:
        rc |= convert_pptx(f, a.out, None, a.min_image_bytes, layouts=not a.no_layouts)[
            0
        ]
    sys.exit(rc)


if __name__ == "__main__":
    main()
