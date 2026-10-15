from __future__ import annotations

import hashlib
import html
import io
import os
import shutil
import re
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .config import settings


@dataclass
class ExtractedBlock:
    content: str
    content_type: str = "text"
    page_number: int | None = None
    section: str = ""
    table_id: str | None = None
    extraction_method: str = "html_text"


class FilingHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[ExtractedBlock] = []
        self.current_heading = ""
        self.heading_depth = 0
        self.heading_text: list[str] = []
        self.table_depth = 0
        self.table_rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []
        self.cell_depth = 0
        self.paragraph: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "ix:header", "ix:hidden"}:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_depth += 1
            self.heading_text = []
        elif tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.table_rows = []
        elif self.table_depth and tag == "tr":
            self.row = []
        elif self.table_depth and tag in {"td", "th"}:
            self.cell_depth += 1
            self.cell = []
        elif not self.table_depth and tag in {"p", "div", "li"}:
            if self.paragraph:
                self._flush_paragraph()

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "ix:header", "ix:hidden"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.heading_depth:
            text = _clean(" ".join(self.heading_text))
            if text:
                self.current_heading = text[:180]
            self.heading_depth -= 1
        elif self.table_depth and tag in {"td", "th"} and self.cell_depth:
            self.row.append(_clean(" ".join(self.cell)))
            self.cell_depth -= 1
        elif self.table_depth and tag == "tr" and self.row:
            self.table_rows.append(self.row)
            self.row = []
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1
            if self.table_depth == 0:
                table = _format_table(self.table_rows)
                if table:
                    self.blocks.append(ExtractedBlock(table, "table", None,
                        self.current_heading, f"table-{len([b for b in self.blocks if b.table_id]) + 1}",
                        "html_table"))
        elif not self.table_depth and tag in {"p", "div", "li", "br"}:
            self._flush_paragraph()

    def handle_data(self, data: str):
        if self.skip_depth:
            return
        if self.heading_depth:
            self.heading_text.append(data)
        elif self.table_depth:
            if self.cell_depth:
                self.cell.append(data)
        else:
            self.paragraph.append(data)

    def _flush_paragraph(self):
        text = _clean(" ".join(self.paragraph))
        self.paragraph.clear()
        if not text or len(text) <= 25:
            return
        if not re.search(r"[a-zA-Z0-9]{3,}", text):
            return
        if len(text) > 300 and " " not in text[:200]:
            return
        self.blocks.append(ExtractedBlock(text, "text", None, self.current_heading,
                                           None, "html_text"))


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _format_table(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    return "\n".join(" | ".join(cell for cell in row) for row in rows)


def _render_pdf_page(pdf_path, page_number: int) -> bytes:
    executable = shutil.which(settings.pdftoppm_cmd) or (settings.pdftoppm_cmd if settings.pdftoppm_cmd and os.path.isfile(settings.pdftoppm_cmd) else None)
    if not executable:
        raise RuntimeError("Scanned PDF page needs OCR or vision, but Poppler pdftoppm was not found. Install Poppler or set PDFTOPPM_CMD.")
    with tempfile.TemporaryDirectory(prefix="meridian-page-") as temp:
        prefix = str(Path(temp) / "page")
        subprocess.run([executable, "-f", str(page_number), "-l", str(page_number), "-singlefile", "-png", "-r", "220", str(pdf_path), prefix],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90)
        return Path(prefix + ".png").read_bytes()


def _transcribe_page(image: bytes) -> tuple[str, str]:
    tesseract = shutil.which(settings.tesseract_cmd) or (settings.tesseract_cmd if settings.tesseract_cmd and os.path.isfile(settings.tesseract_cmd) else None)
    if tesseract:
        try:
            result = subprocess.run([tesseract, "stdin", "stdout", "--psm", "6"], input=image,
                capture_output=True, check=True, timeout=90)
            text = result.stdout.decode("utf-8", errors="replace").strip()
            if len(text) >= 20:
                return text, "tesseract_ocr"
        except (OSError, subprocess.SubprocessError) as exc:
            if not settings.vision_enabled:
                raise RuntimeError(f"Tesseract OCR failed: {exc}") from exc
    if settings.vision_enabled:
        from .llm import LLMClient
        return LLMClient().describe_document_page(image), "vision_llm"
    raise RuntimeError("Scanned PDF page needs OCR; install Tesseract and set TESSERACT_CMD, or enable LLM_VISION_ENABLED with a vision-capable model.")


def extract_document(filename: str, payload: bytes, content_type: str = "") -> list[ExtractedBlock]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "pdf" or content_type == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF extraction requires pypdf. Install project requirements with pip install -r requirements.txt") from exc
        reader = PdfReader(io.BytesIO(payload), strict=False)
        blocks = []
        scanned_pages = []
        with tempfile.TemporaryDirectory(prefix="meridian-pdf-") as temp:
            pdf_path = Path(temp) / "upload.pdf"
            pdf_path.write_bytes(payload)
            for number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                method = "pypdf"
                if not text.strip():
                    image = _render_pdf_page(pdf_path, number)
                    text, method = _transcribe_page(image)
                    if len(text.strip()) < 20:
                        scanned_pages.append(number)
                        continue
                for part, section in _split_text(text):
                    blocks.append(ExtractedBlock(part, "text", number, section, None, method))
        if scanned_pages:
            raise ValueError("Text extraction and OCR/vision could not read PDF page(s): " + ", ".join(map(str, scanned_pages)))
        if not blocks:
            raise ValueError("No extractable PDF text found after trying configured text and OCR/vision extraction.")
        return blocks
    if suffix in {"html", "htm", "xhtml"} or "html" in content_type:
        parser = FilingHTMLParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser._flush_paragraph()
        if parser.blocks:
            if len(parser.blocks) > 400:
                tables = [b for b in parser.blocks if b.content_type == "table"]
                text_blocks = [b for b in parser.blocks if b.content_type != "table"]
                def _priority(b):
                    sec = (b.section or "").lower()
                    high = any(k in sec for k in ("item 1", "item 7", "risk", "strategy", "business", "discussion", "analysis"))
                    return (1 if high else 0, len(b.content))
                budget = max(250, 400 - len(tables))
                indexed_text = sorted(range(len(text_blocks)), key=lambda i: _priority(text_blocks[i]), reverse=True)[:budget]
                keep_indices = set(indexed_text)
                combined = []
                tx_idx = 0
                for b in parser.blocks:
                    if b.content_type == "table":
                        combined.append(b)
                    elif tx_idx < len(text_blocks) and b is text_blocks[tx_idx]:
                        if tx_idx in keep_indices:
                            combined.append(b)
                        tx_idx += 1
                return combined
            return parser.blocks
    text = payload.decode("utf-8", errors="replace")
    blocks = [ExtractedBlock(part, "text", None, section, None, "plain_text")
              for part, section in _split_text(text)]
    if not blocks:
        raise ValueError("Document contains no extractable text")
    return blocks


def _split_text(text: str, max_chars: int = 1800, overlap: int = 220) -> list[tuple[str, str]]:
    text = text.replace("\x00", " ").replace("\r", "")
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.split("\n")]
    out: list[tuple[str, str]] = []
    section = ""
    para: list[str] = []
    for line in lines:
        if not line:
            if para:
                out.extend(_split_paragraph(" ".join(para), section, max_chars, overlap))
                para = []
            continue
        if len(line) < 130 and (line.upper() == line and any(ch.isalpha() for ch in line)
                                or re.match(r"^(item\s+\d+[a-z]?\.?|part\s+[ivx]+)", line, re.I)):
            if para:
                out.extend(_split_paragraph(" ".join(para), section, max_chars, overlap))
                para = []
            section = line[:180]
        else:
            para.append(line)
    if para:
        out.extend(_split_paragraph(" ".join(para), section, max_chars, overlap))
    return out


def _split_paragraph(text: str, section: str, limit: int, overlap: int) -> list[tuple[str, str]]:
    text = _clean(text)
    if len(text) <= limit:
        return [(text, section)] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = text.rfind(". ", start + int(limit * .6), end)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, section))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

