#!/usr/bin/env python3
"""merge_fragments.py — склейка фрагментов обработки (--fragments K>1) в один
результат без потери информации. Запускается prepare_all.sh автоматически.

Что делает (фрагменты лежат в outbox/<имя>__pS-E/, каждый: MD + фигуры +
_meta.json):
  - MD конкатенируются по порядку, разделитель — пустая строка;
  - медиа копируются в общий каталог (имена _page_N_* уникальны по страницам;
    при коллизии с иным содержимым файл переименовывается с префиксом фрагмента
    и ссылки переписываются);
  - _meta.json: table_of_contents / page_stats объединяются по порядку;
  - проверки: каждая часть входит в результат посимвольно, все ссылки на
    картинки резолвятся, мягкий QC-отчёт (гейты уже пройдены фрагментами);
  - успех: <out>/<имя>.md + .qc_pass (resume).

Usage: merge_fragments.py --out DIR [--force] FRAG_DIR [FRAG_DIR ...]
       merge_fragments.py --selftest
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys

REF = re.compile(r"!\[[^]]*\]\(([^)]+)\)")
FRAG_RE = re.compile(r"^(.*)__p\d+-\d+$")


def frag_parts(frag_dir: pathlib.Path) -> tuple[str, pathlib.Path, pathlib.Path]:
    """(md-текст, md-файл, meta-файл) единственного фрагмента в каталоге."""
    mds = sorted(frag_dir.glob("*.md"))
    if len(mds) != 1:
        raise SystemExit(f"FAIL: в {frag_dir} ожидается один .md, найдено {len(mds)}")
    md = mds[0]
    meta = md.with_name(md.stem + "_meta.json")
    return md.read_text(encoding="utf-8"), md, meta


def copy_media(frag_dir: pathlib.Path, out: pathlib.Path, idx: int) -> dict[str, str]:
    """Копирует медиа фрагмента в out; возвращает карта переименований ссылок."""
    renames: dict[str, str] = {}
    for f in sorted(frag_dir.iterdir()):
        if f.name.startswith(".") or f.suffix.lower() in (".md", ".json"):
            continue
        dst = out / f.name
        if dst.exists():
            if dst.read_bytes() == f.read_bytes():
                continue  # тот же файл (повторный запуск) — оставляем
            new = f"{idx}_{f.name}"  # коллизия с иным содержимым
            shutil.copy2(f, out / new)
            renames[f.name] = new
        else:
            shutil.copy2(f, dst)
    return renames


def merge_meta(metas: list[pathlib.Path | None]) -> dict:
    """Объединяет _meta.json фрагментов: списки конкатенируются по порядку."""
    merged: dict = {}
    for p in metas:
        if p is None or not p.exists():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for k, v in m.items():
            if isinstance(v, list):
                merged.setdefault(k, []).extend(v)
            elif k not in merged:
                merged[k] = v
    return merged


def cyr_share(t: str) -> float:
    letters = re.findall(r"[А-Яа-яЁёA-Za-z]", t)
    if not letters:
        return 0.0
    return sum(1 for c in letters if re.match(r"[А-Яа-яЁё]", c)) / len(letters)


def merge(out_dir: pathlib.Path, frag_dirs: list[pathlib.Path], force: bool) -> int:
    if out_dir.exists() and any(out_dir.iterdir()):
        if (out_dir / ".qc_pass").exists() and not force:
            print(f"[--] {out_dir.name}: склеенный результат уже готов (.qc_pass)")
            return 0
        if force:
            shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = out_dir / (out_dir.name + ".md")

    parts, renames_all = [], {}
    for i, fd in enumerate(frag_dirs):
        if not fd.is_dir():
            print(f"FAIL: фрагмент не найден: {fd}")
            return 1
        text, _, _ = frag_parts(fd)
        ren = copy_media(fd, out_dir, i + 1)
        for old, new in ren.items():
            text = text.replace(f"]({old})", f"]({new})")
        renames_all.update(ren)
        parts.append(text.rstrip("\n"))

    out_md.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    merged = out_md.read_text(encoding="utf-8")

    # --- проверки -----------------------------------------------------------
    problems = []
    for i, (fd, text) in enumerate(zip(frag_dirs, parts), 1):
        if text not in merged:
            problems.append(f"фрагмент {i} ({fd.name}) входит не полностью")
    missing = sorted(
        {
            ref
            for ref in REF.findall(merged)
            if not ref.startswith(("http://", "https://"))
            and not (out_dir / ref).exists()
        }
    )
    if missing:
        problems.append(f"битые ссылки на медиа: {missing[:5]}")

    metas = []
    for fd in frag_dirs:
        _, md, meta = frag_parts(fd)
        metas.append(meta)
    meta_merged = merge_meta(metas)
    (out_dir / (out_dir.name + "_meta.json")).write_text(
        json.dumps(meta_merged, ensure_ascii=False), encoding="utf-8"
    )

    n_imgs = len(
        [
            f
            for f in out_dir.iterdir()
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif")
        ]
    )
    n_media_refs = len({r for r in REF.findall(merged)})
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1

    (out_dir / ".qc_pass").touch()
    print(
        f"[PASS] {out_dir.name}: {len(parts)} фрагм., {len(merged)} симв., "
        f"кириллица {cyr_share(merged) * 100:.1f}%, картинок {n_imgs} "
        f"(ссылок {n_media_refs}), переименований при коллизии: {len(renames_all)}"
    )
    return 0


# ----------------------------------------------------------- selftest ---
def selftest() -> int:
    import tempfile

    failures = []

    def check(name, cond):
        print(f"  {'ok' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="merge_selftest_"))
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000"
        "001f15c4890000000d49444154789c626001000000ffff0300000600"
        "0557bfabd40000000049454e44ae426082"
    )

    def mkfrag(name: str, img: str, title: str) -> pathlib.Path:
        d = tmp / name
        d.mkdir()
        (d / f"{name}.md").write_text(
            f"# {title}\n\n![]({img})\n\nтекст {title}\n", encoding="utf-8"
        )
        (d / img).write_bytes(png)
        (d / f"{name}_meta.json").write_text(
            json.dumps(
                {
                    "table_of_contents": [title],
                    "page_stats": [{"page": name}],
                    "debug_data_path": "x",
                }
            ),
            encoding="utf-8",
        )
        return d

    # 1. базовая склейка + проверки
    f1 = mkfrag("doc__p1-2", "_page_1_I.jpeg", "Часть1")
    f2 = mkfrag("doc__p3-4", "_page_3_I.jpeg", "Часть2")
    out = tmp / "doc"
    rc = merge(out, [f1, f2], force=False)
    md = (out / "doc.md").read_text(encoding="utf-8")
    check("успешный выход", rc == 0)
    check("обе части в результате", "текст Часть1" in md and "текст Часть2" in md)
    check("порядок частей", md.index("Часть1") < md.index("Часть2"))
    check(
        "картинки скопированы",
        (out / "_page_1_I.jpeg").exists() and (out / "_page_3_I.jpeg").exists(),
    )
    check(".qc_pass создан", (out / ".qc_pass").exists())
    meta = json.loads((out / "doc_meta.json").read_text(encoding="utf-8"))
    check("meta: toc из обоих", len(meta["table_of_contents"]) == 2)
    check("meta: page_stats из обоих", len(meta["page_stats"]) == 2)

    # 2. resume: повтор без --force — no-op
    rc = merge(out, [f1, f2], force=False)
    check("resume: повтор — пропуск (rc=0)", rc == 0)

    # 3. коллизия имён с иным содержимым → переименование + переписывание ссылки
    f3 = mkfrag("doc__p5-6", "_page_1_I.jpeg", "Часть3")
    (f3 / "_page_1_I.jpeg").write_bytes(png + b"x")  # иное содержимое
    rc = merge(out, [f1, f2, f3], force=True)
    md = (out / "doc.md").read_text(encoding="utf-8")
    check("коллизия: успешная склейка", rc == 0)
    check("коллизия: файл переименован 3_", (out / "3__page_1_I.jpeg").exists())
    check(
        "коллизия: ссылка переписана",
        "]((3__page_1_I.jpeg)" in md.replace("[", "(").replace("((", "((")
        or "]((3__page_1_I.jpeg))" in md
        or "](3__page_1_I.jpeg)" in md,
    )

    # 4. отсутствующий фрагмент → FAIL
    rc = merge(tmp / "bad", [f1, tmp / "нет_такого"], force=False)
    check("нет фрагмента -> rc=1", rc == 1)

    shutil.rmtree(tmp)
    print(f"\n{'FAIL: ' + str(failures) if failures else 'Все проверки пройдены'}")
    return 1 if failures else 0


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    out, force, frags = None, False, []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out = args[i + 1]
            i += 2
        elif args[i] == "--force":
            force = True
            i += 1
        else:
            frags.append(args[i])
            i += 1
    if not out or len(frags) < 2:
        sys.exit(__doc__)
    sys.exit(merge(pathlib.Path(out), [pathlib.Path(f) for f in frags], force))


if __name__ == "__main__":
    main()
