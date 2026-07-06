"""
epub_parser.py

Parses NewspaperDirect / PressReader-style .epub newspaper exports
(the kind Kobo / Mint / similar apps generate) into a clean list of
article dicts.

These epubs are just zip files. Each page lives at OEBPS/page-XXX/page-XXX.xhtml
and contains one or more <div class="art-cnt" id="art-N"> blocks, one per article.
"""

import base64
import os
import zipfile
import re
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Article:
    source: str
    pub_date: str
    page: str
    article_id: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = "epub"
    url: str = None
    image_url: str = None

    def full_text(self):
        return f"{self.title}\n{self.subtitle}\n{self.body}".strip()


def guess_source_and_date(file_path: str):
    """Try to pull a human-readable source name + date from the filename,
    e.g. 'mint-delhi_01-07-2026__Kobo_.epub' -> ('Mint Newspaper', '2026-07-01')"""
    fname = file_path.replace("\\", "/").split("/")[-1]
    stem = fname.rsplit(".", 1)[0]
    name_part = stem.split("_")[0]
    source = display_source_name(fname)

    date_match = re.search(r"(\d{2})-(\d{2})-(\d{4})", fname)
    if date_match:
        d, m, y = date_match.groups()
        pub_date = f"{y}-{m}-{d}"
    else:
        date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", fname)
        if date_match:
            pub_date = date_match.group(0)
        else:
            pub_date = str(date.today())

    return source, pub_date


def display_source_name(filename: str) -> str:
    """Friendly label shown on news cards for uploaded newspapers."""
    fname = (filename or "").replace("\\", "/").split("/")[-1]
    stem = fname.rsplit(".", 1)[0].lower()
    name_part = stem.split("_")[0]

    if "mint" in stem or "mint" in name_part:
        return "Mint Newspaper"
    if "economic" in stem and "times" in stem:
        return "Economic Times Newspaper"
    if "business" in stem and "standard" in stem:
        return "Business Standard Newspaper"
    if "hindu" in stem:
        return "The Hindu Newspaper"
    if "livemint" in stem or "live-mint" in stem:
        return "LiveMint Newspaper"

    label = name_part.replace("-", " ").strip().title() or "Newspaper Upload"
    if "newspaper" not in label.lower():
        label = f"{label} Newspaper"
    return label


def _guess_source_and_date(epub_path: str):
    return guess_source_and_date(epub_path)


_MAX_EPUB_IMAGE_BYTES = int(os.environ.get("EPUB_MAX_IMAGE_BYTES", "180000"))

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def _guess_image_mime(path: str, data: bytes) -> str:
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _resolve_epub_asset_path(page_file: str, src: str) -> str | None:
    src = (src or "").strip().split("#")[0].split("?")[0]
    if not src or src.startswith("data:"):
        return None
    if src.startswith(("http://", "https://")):
        return None
    page_dir = "/".join(page_file.split("/")[:-1])
    if src.startswith("/"):
        return src.lstrip("/")
    return f"{page_dir}/{src}".replace("//", "/")


def _read_epub_image(z: zipfile.ZipFile, page_file: str, src: str) -> bytes | None:
    zip_path = _resolve_epub_asset_path(page_file, src)
    if not zip_path:
        return None
    try:
        return z.read(zip_path)
    except KeyError:
        page_dir = "/".join(page_file.split("/")[:-1])
        basename = src.split("/")[-1]
        try:
            return z.read(f"{page_dir}/{basename}")
        except KeyError:
            return None


def _art_image_data_url(art_div, page_file: str, z: zipfile.ZipFile) -> str | None:
    """Embed the first article photo from the EPUB as a data URL (no API calls)."""
    search_roots = [art_div]
    parent = art_div.parent
    if parent and parent.name in ("div", "article", "section"):
        search_roots.append(parent)

    seen_src = set()
    for root in search_roots:
        for img in root.select("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src or src in seen_src:
                continue
            seen_src.add(src)
            data = _read_epub_image(z, page_file, src)
            if not data or len(data) < 100 or len(data) > _MAX_EPUB_IMAGE_BYTES:
                continue
            mime = _guess_image_mime(src or "", data)
            if not mime.startswith("image/"):
                continue
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    return None


def parse_epub(epub_path: str) -> list[Article]:
    """Extract all articles from a newspaper epub file."""
    source, pub_date = _guess_source_and_date(epub_path)
    articles = []

    with zipfile.ZipFile(epub_path, "r") as z:
        page_files = sorted(
            [n for n in z.namelist() if re.match(r"OEBPS/page-\d+/page-\d+\.xhtml", n)]
        )

        for page_file in page_files:
            page_id = page_file.split("/")[1]  # "page-007"
            with z.open(page_file) as f:
                soup = BeautifulSoup(f.read(), "lxml")

            for art_div in soup.select("div.art-cnt"):
                art_id = art_div.get("id", "")

                title_el = art_div.select_one("div.title")
                subtitle_el = art_div.select_one("div.subtitle")
                byline_el = art_div.select_one("span.byline")

                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue  # skip anything without a headline

                subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""
                byline = byline_el.get_text(" ", strip=True) if byline_el else ""

                paragraphs = art_div.select("p")
                body = "\n".join(
                    p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
                )

                articles.append(
                    Article(
                        source=source,
                        pub_date=pub_date,
                        page=page_id,
                        article_id=art_id,
                        title=title,
                        subtitle=subtitle,
                        byline=byline,
                        body=body,
                    )
                )

    return articles


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "test.epub"
    arts = parse_epub(path)
    print(f"Parsed {len(arts)} articles from {path}\n")
    for a in arts[:5]:
        print(f"[{a.page} / {a.article_id}] {a.title}")
        print(f"   byline: {a.byline}")
        print(f"   body: {a.body[:120]}...")
        print()
