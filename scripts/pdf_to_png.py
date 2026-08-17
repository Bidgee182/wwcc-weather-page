#!/usr/bin/env python3
"""Render PDF pages to PNG images so they can be viewed/inspected.

A proper PDF renderer (PyMuPDF / MuPDF) instead of scraping raw PDF bytes.
Self-contained: no poppler / system tools needed.

Usage:
    python scripts/pdf_to_png.py <file.pdf> [pages] [--dpi N] [--out DIR]

  pages: "1" | "1-5" | "1,3,7" | "all"   (default: all, capped at 40 pages)
  --dpi: render resolution (default 170; 200+ for dense schematics)
  --out: output directory (default: .pdf-view/ in the repo, which is gitignored)

Requires pymupdf (see scripts/requirements.txt).
"""
import argparse
import pathlib

import pymupdf


def parse_pages(spec: str, n: int) -> list[int]:
    if not spec or spec.lower() == "all":
        return list(range(n))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n]


def main() -> None:
    ap = argparse.ArgumentParser(description="Render PDF pages to PNG.")
    ap.add_argument("pdf")
    ap.add_argument("pages", nargs="?", default="all")
    ap.add_argument("--dpi", type=int, default=170)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    doc = pymupdf.open(a.pdf)
    stem = pathlib.Path(a.pdf).stem
    outdir = pathlib.Path(a.out) if a.out else pathlib.Path(".pdf-view")
    outdir.mkdir(parents=True, exist_ok=True)

    pages = parse_pages(a.pages, len(doc))[:40]
    for p in pages:
        pix = doc[p].get_pixmap(dpi=a.dpi)
        f = outdir / f"{stem}_p{p + 1}.png"
        pix.save(str(f))
        print(f"{f}\t{pix.width}x{pix.height}")


if __name__ == "__main__":
    main()
