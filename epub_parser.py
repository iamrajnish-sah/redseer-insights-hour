"""
epub_parser.py

Parses NewspaperDirect / PressReader-style .epub newspaper exports
(the kind Kobo / Mint / similar apps generate) into a clean list of
article dicts.

These epubs are just zip files. Each page lives at OEBPS/page-XXX/page-XXX.xhtml
and contains one or more <div class="art-cnt" id="art-N"> blocks, one per article.
"""

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

    def full_text(self):
        return f"{self.title}\n{self.subtitle}\n{self.body}".strip()


def guess_source_and_date(file_path: str):
    """Try to pull a human-readable source name + date from the filename,
    e.g. 'mint-delhi_01-07-2026__Kobo_.epub' -> ('Mint Delhi', '2026-07-01')"""
    fname = file_path.replace("\\", "/").split("/")[-1]
    stem = fname.rsplit(".", 1)[0]
    name_part = stem.split("_")[0]
    source = name_part.replace("-", " ").title() or "Newspaper Upload"

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


def _guess_source_and_date(epub_path: str):
    return guess_source_and_date(epub_path)


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
