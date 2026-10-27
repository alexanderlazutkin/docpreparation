#!/usr/bin/env python3

Purpose: offline reproduction of the raw-HTML tail bug (marker#2.0.0 +
markdownify 1.2.3 + bs4 4.15.0 + Ubuntu-patched py3.12.3 html.parser).
Run with services up (modelset 1): SURYA_INFERENCE_URL is forced to the proxy.

Usage:
  .venv/bin/python /tmp/opencode/rawhtml_ab/dump_driver.py <pdf> <out_dir> [page_range_json]
  page_range_json: e.g. '[0, 233]' (list of ints, inclusive) — omit for all pages.
"""

import json
import os
import re
import sys

OUT_DEFAULT = "/tmp/opencode/rawhtml_ab"

os.environ["SURYA_INFERENCE_URL"] = "http://127.0.0.1:8511/v1"
os.environ["IN_STREAMLIT"] = "true"
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

from marker.renderers.markdown import MarkdownRenderer, MarkdownOutput, cleanup_text
from marker.config.parser import ConfigParser
from marker.models import create_model_dict

PDF = sys.argv[1]
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else OUT_DEFAULT
PAGE_RANGE = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
os.makedirs(OUT_DIR, exist_ok=True)

STATS = {}


def patched_call(self, document):
    document_output = document.render(self.block_config)
    full_html, images = self.extract_html(document, document_output)

    html_path = os.path.join(OUT_DIR, "full_html.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    STATS["full_html_len"] = len(full_html)
    print(f"[dump] full_html: {len(full_html)} bytes -> {html_path}", flush=True)

    markdown = self.md_cls.convert(full_html)
    markdown = cleanup_text(markdown)
    if self.paginate_output:
        if not markdown.startswith("\n\n"):
            markdown = "\n\n" + markdown
        if markdown.endswith(self.page_separator):
            markdown += "\n\n"

    md_path = os.path.join(OUT_DIR, "converted.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    m = re.search(r"<div class='page'", markdown)
    STATS["leak_at_md_byte"] = m.start() if m else -1
    STATS["md_len"] = len(markdown)
    STATS["leak_page_id"] = (
        re.search(r"data-page-id='(\d+)'", markdown[m.start() :]).group(1)
        if m
        else None
    )
    print(
        f"[dump] md: {len(markdown)} bytes, leak at {STATS['leak_at_md_byte']} "
        f"(page id {STATS['leak_page_id']}) -> {md_path}",
        flush=True,
    )

    return MarkdownOutput(
        markdown=markdown,
        images=images,
        metadata=self.generate_document_metadata(document, document_output),
    )


MarkdownRenderer.__call__ = patched_call


def main():
    cli_options = {
        "output_dir": OUT_DIR,
        "paginate_output": True,
        "mode": "balanced",
        "output_format": "markdown",
    }
    if PAGE_RANGE is not None:
        # marker expects a list of page indices (0-based)
        cli_options["page_range"] = [i for i in range(PAGE_RANGE[0], PAGE_RANGE[1] + 1)]

    config_parser = ConfigParser(cli_options)
    config_dict = config_parser.generate_config_dict()
    config_dict["disable_tqdm"] = False

    models = create_model_dict()
    converter_cls = config_parser.get_converter_cls()
    converter = converter_cls(
        config=config_dict,
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(PDF)

    with open(os.path.join(OUT_DIR, "stats.json"), "w") as f:
        json.dump(STATS, f, indent=2)
    print("[dump] DONE", json.dumps(STATS), flush=True)


if __name__ == "__main__":
    main()
