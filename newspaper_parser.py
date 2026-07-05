"""
newspaper_parser.py

Upload and parse newspaper files in common formats:
  - .epub  — structured PressReader/Kobo exports (best quality)
  - .pdf   — text extraction per page; Gemini fallback for scanned PDFs
  - images — Gemini reads the clipping/page photo
  - .txt   — plain text split into story blocks

After upload, click "Process with Gemini" to classify sectors and summarize.
"""

import json
import os
import re

from epub_parser import Article, guess_source_and_date, parse_epub

SUPPORTED_EXTENSIONS = {
    ".epub",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".txt",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

GEMINI_EXTRACT_PROMPT = """You are parsing an uploaded Indian newspaper page or clipping.

Extract every separate news story you can see. Skip ads, mastheads, and page furniture.

Return ONLY a JSON array. Each item must have:
  {"title": "headline", "subtitle": "", "byline": "", "body": "story text"}

Use exact headline wording when possible. Combine continuation columns into one story."""


def file_extension(path):
    return os.path.splitext(path)[1].lower()


def is_supported_upload(filename):
    return file_extension(filename) in SUPPORTED_EXTENSIONS


def _parse_pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support requires pypdf. Run: pip install pypdf") from exc

    source, pub_date = guess_source_and_date(path)
    reader = PdfReader(path)
    articles = []

    for index, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if len(text) < 40:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0][:220] if lines else f"{source} — Page {index}"
        body = "\n".join(lines[1:]) if len(lines) > 1 else text
        articles.append(
            Article(
                source=source,
                pub_date=pub_date,
                page=f"page-{index:03d}",
                article_id=str(index),
                title=title,
                subtitle="",
                byline="",
                body=body,
                origin="newspaper",
            )
        )
    return articles


def _gemini_extract_articles(path, mime_type, source, pub_date):
    from google.genai import types

    from classify_and_summarize import _get_client, _model_fallback_chain

    with open(path, "rb") as handle:
        data = handle.read()

    client = _get_client()
    last_error = None
    for model in _model_fallback_chain():
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=data, mime_type=mime_type),
                    GEMINI_EXTRACT_PROMPT,
                ],
                config={"response_mime_type": "application/json"},
            )
            text = (response.text or "").strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            items = json.loads(text)
            break
        except Exception as exc:
            last_error = exc
            items = None
    else:
        raise RuntimeError(f"Could not read document with Gemini: {last_error}")

    if not isinstance(items, list):
        raise RuntimeError("Gemini returned an unexpected format for newspaper extraction.")

    articles = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        articles.append(
            Article(
                source=source,
                pub_date=pub_date,
                page=f"doc-{index:03d}",
                article_id=str(index),
                title=title,
                subtitle=(item.get("subtitle") or "").strip(),
                byline=(item.get("byline") or "").strip(),
                body=(item.get("body") or "").strip(),
                origin="newspaper",
            )
        )
    return articles


def parse_pdf(path):
    articles = _parse_pdf_text(path)
    total_chars = sum(len(a.body or "") for a in articles)
    if len(articles) >= 1 and total_chars >= 200:
        return articles, "pdf_text"

    source, pub_date = guess_source_and_date(path)
    if not (os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")):
        if articles:
            return articles, "pdf_text_partial"
        raise RuntimeError(
            "This PDF looks scanned or image-based. Add GEMINIAPIKEY to .env so Gemini can read it."
        )

    gemini_articles = _gemini_extract_articles(path, "application/pdf", source, pub_date)
    if gemini_articles:
        return gemini_articles, "pdf_gemini"
    if articles:
        return articles, "pdf_text_partial"
    raise RuntimeError("No readable text found in this PDF.")


def parse_image(path):
    ext = file_extension(path)
    mime_type = IMAGE_MIME.get(ext)
    if not mime_type:
        raise RuntimeError(f"Unsupported image type: {ext}")

    if not (os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")):
        raise RuntimeError(
            "Image uploads need GEMINIAPIKEY in .env so Gemini can read the newspaper photo."
        )

    source, pub_date = guess_source_and_date(path)
    articles = _gemini_extract_articles(path, mime_type, source, pub_date)
    if not articles:
        raise RuntimeError("Gemini could not find any news stories in this image.")
    return articles, "image_gemini"


def parse_txt(path):
    source, pub_date = guess_source_and_date(path)
    with open(path, encoding="utf-8", errors="ignore") as handle:
        text = handle.read().strip()
    if not text:
        raise RuntimeError("The text file is empty.")

    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    articles = []
    for index, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0][:220]
        body = "\n".join(lines[1:]) if len(lines) > 1 else block
        articles.append(
            Article(
                source=source,
                pub_date=pub_date,
                page=f"block-{index:03d}",
                article_id=str(index),
                title=title,
                subtitle="",
                byline="",
                body=body,
                origin="newspaper",
            )
        )

    if not articles:
        articles = [
            Article(
                source=source,
                pub_date=pub_date,
                page="block-001",
                article_id="1",
                title=f"{source} upload",
                subtitle="",
                byline="",
                body=text,
                origin="newspaper",
            )
        ]
    return articles, "txt"


def parse_newspaper(path):
    """Parse any supported newspaper upload. Returns (articles, format, method)."""
    ext = file_extension(path)
    if ext == ".epub":
        return parse_epub(path), "epub", "epub_structure"
    if ext == ".pdf":
        articles, method = parse_pdf(path)
        return articles, "pdf", method
    if ext in IMAGE_EXTENSIONS:
        articles, method = parse_image(path)
        return articles, "image", method
    if ext == ".txt":
        articles, method = parse_txt(path)
        return articles, "txt", method
    allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    raise RuntimeError(f"Unsupported file type '{ext}'. Allowed: {allowed}")
