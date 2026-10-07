"""Mirror act.edu.in's public pages into knowledge_base/website/ as markdown.

Run by hand whenever the college site changes, then restart rag-backend (or
POST /admin/reload-knowledge) so the retriever re-embeds:

    python scrape_website.py                 # full crawl
    python scrape_website.py --cache-dir X   # keep raw HTML in X, re-use on re-runs

URL discovery is three-way, because no single source lists everything: the
Rank Math sitemap only covers pages/posts, the WordPress REST API adds the
custom post types (departments, achievements, events), and a link crawl picks
up the few pages neither lists.

Each page becomes one file: "# Title", a "Source:" line, then the page body
with the shared header/footer/menus/popups stripped. The exception is the
~800 individual event and achievement posts: those are gathered into one file
per department/cell (events/civil-engineering.md, ...), newest first, so the
assistant can answer "what has the Civil department been doing?" from one place.

PDFs linked from those pages are downloaded and their text written to
website/documents/, one file per PDF — except the ones matched by
DOC_SKIP_RULES, and scanned PDFs, which have no text layer. The scanned ones
matched by OCR_DOC_RE are read with Sarvam's Document Intelligence OCR
instead — the files are uploaded to Sarvam for that, using SARVAM_API_KEY from
rag-backend/.env.
website/_documents_report.txt lists every linked document and what happened
to it. Text that only exists inside images on web pages is not captured.

The output folder is wiped and rebuilt on every run; don't hand-edit files in
it (put curated content in the top-level knowledge_base/*.md files instead).
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

SITE = "https://act.edu.in"
HOST = "act.edu.in"
OUT_DIR = Path(__file__).parent / "knowledge_base" / "website"
REST_TYPES = ["pages", "posts", "departments", "achievements", "events"]
SECTIONS = {"departments", "achievements", "events"}
USER_AGENT = "Mozilla/5.0 (compatible; AgniAI-KnowledgeSync/1.0; +https://act.edu.in)"
WORKERS = 4
MIN_BODY_CHARS = 60
MIN_DOC_CHARS = 200  # less text than this across a whole PDF = scanned images

# Linked documents whose text is deliberately not ingested: (pattern matched
# against "<source page URL> <document URL>", reason shown in the report).
DOC_SKIP_RULES = [
    (re.compile(r"/newsletter/ "), "daily newsletter archive: 600+ photo-heavy issues from 2022-24"),
    (re.compile(r"National-Innovation-and-Startup-Policy|Tamil-Nadu-Startup|List-of-NM-CO", re.I),
     "government document, not the college's own content"),
    (re.compile(r"MSME-CERTIFICATES-PROOF", re.I), "bundle of scanned certificates"),
    (re.compile(r"/academic-calendar/ "), "academic calendars not wanted in the knowledge base (decided 2026-10-07)"),
]
# Taxonomy tags that say where a post is displayed, not what it belongs to.
GENERIC_TAGS = {"homepage", "highlights", "department", "achievements", "pride-achievements"}
COLLEGE_WIDE = "College-wide"
# Tag slugs whose title-cased form isn't the real name.
CATEGORY_NAMES = {
    "aids": "Artificial Intelligence and Data Science",
    "artificial-intelligence-data-science": "Artificial Intelligence and Data Science",
    "cse": "Computer Science and Engineering",
    "computer-science-engineering": "Computer Science and Engineering",
    "m-e-computer-science-engineering": "M.E. Computer Science and Engineering",
    "m-e-mechatronics-engineering": "M.E. Mechatronics Engineering",
    "common-events": COLLEGE_WIDE,
    "edc": "Entrepreneurship Development Cell",
    "hscg": "Higher Studies and Career Guidance Cell",
    "ipd": "Innovation and Product Development Cell",
    "nss": "NSS",
    "300-startsups": "300 Startups",
    "300-startups-gallery": "300 Startups",
    "about-crd": "Centre for Research and Development",
    "research-mou-with-industries": "Research MoUs with Industries",
    "covid19-student-innovations": "COVID-19 Student Innovations",
    "alumni-meet-event": "Alumni",
}
# More posts than this sharing one publish date = a bulk import of old content,
# so that date says nothing about when the event happened.
BULK_IMPORT_POSTS_PER_DAY = 15
# Scanned PDFs worth the OCR time: what a prospective student or parent would
# ask about. Add a filename pattern here to OCR more (billed per page by Sarvam).
OCR_DOC_RE = re.compile(
    r"Mandatory-Disclos|Code-of-Conduct|STUDENT-DISCIPLINARY|GRIEVANCE-REDRESSAL|INTERNSHIP-POLICY",
    re.I,
)
# Documents whose personal data is removed before ingestion. The Mandatory
# Disclosure lists every HOD's and committee member's personal mobile and every
# faculty member's date of birth; the assistant must not read those out to
# anonymous visitors (decided 2026-10-07).
STRIP_PERSONAL_DATA_DOC_RE = re.compile(r"Mandatory-Disclos", re.I)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"  # mobile, optionally +91 / split 5-5
    r"|(?<!\d)0?\d{2,4}\s*[-–\s]\s*\d{6,8}(?!\d)"       # landline: STD code, separator, number
    r"|(?<!\d)0\d{2,3}[\s-]\d{3,4}[\s-]\d{4}(?!\d)"            # landline written 044 4997 2901
    r"|(?<!\d)(?!(?:19|20)\d\d\s)\d{4}\s\d{4}(?!\d)"            # landline written 6740 9441 (not two years)
    r"|(?<!\d)(?!20\d\d20\d\d)[0-7]\d{7}(?!\d)"             # bare 8-digit landline (not a year range)
    r"|(?<![\d-])[6-9]\d{8}(?!\d)"                         # mobile with one digit lost by OCR
    r"|\d{10,}"                                             # OCR runs several numbers together
)
# Faculty table rows end "... | <date of joining> | Male | <date of birth>".
BIRTH_DATE_RE = re.compile(r"((?:Male|Female)\s*\|?\s*)\d{2}-\d{2}-(?:19|20)\d{2}|\|?\s*Date\s*of\s*Birth", re.I)
# The college letterhead printed at the top of every scanned page (name,
# accreditation badges, address, phone/web line). Matched against the line with
# spaces removed, because OCR drops and adds spaces unpredictably.
LETTERHEAD_RE = re.compile(
    r"^.{0,12}agnicollegeoftechnology.{0,25}$|autonomousinstitution|^(naac|nba|n3a|nbr|a\+|s?o9001|n\.?rf|s|tuv|sud)$"
    r"|approvedbyai?cte|accreditedbyn|iso9001|af+i?li?ated?toanna|^omr[,.]|oldmahabalipuramroad"
    r"|www\.ac|estd\.?(in)?2[0o]{2}1|phone:|beanactioneer|^[^a-z0-9]*$",
    re.I,
)
LETTERHEAD_MAX_LINES = 8  # only look this far down each page
SARVAM_DOC_AI = "https://api.sarvam.ai/doc-ai/v1/job"
SARVAM_OCR_MAX_PAGES = 10  # per uploaded PDF — longer documents are sent in parts
SARVAM_OCR_TIMEOUT_S = 600
# Page furniture, per Sarvam's layout analysis — repeated on every page.
OCR_DROP_LAYOUT_TAGS = {"header", "footer", "page-number", "page_number", "image", "figure"}
EOA_RE = re.compile(r"AICTE-EOA-Report-(20\d{2})", re.I)
GENERIC_LABEL_RE = re.compile(r"^(view|click|download|read|here|more|pdf|open|details?)\b", re.I)

# Transactional / listing-archive URLs with no knowledge content of their own.
SKIP_PATH_RE = re.compile(
    r"^/(wp-admin|wp-json|wp-login|wp-content|wp-includes|feed|comments|author|cart|checkout)"
    r"|/(feed|page/\d+|embed)/?$"
    r"|^/(payment-failed|payment-success)/?$"
    r"|^/academic-calendar/?$"  # not wanted in the knowledge base (decided 2026-10-07)
    r"|category/|/tag/"
)
ASSET_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|jpe?g|png|gif|webp|svg|ico|mp4|mp3|css|js|xml|json|woff2?|ttf)$",
    re.I,
)
DOC_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?)$", re.I)
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "table", "tr", "h1", "h2",
              "h3", "h4", "h5", "h6", "blockquote", "figure", "figcaption", "dl", "dt", "dd",
              "details", "summary", "header", "footer", "main", "aside", "address", "pre",
              "button"}
TITLE_SUFFIX_RE = re.compile(r"\s*[-–|]\s*Agni College of Technology.*$", re.I)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def fetch(url: str, cache_dir: Path | None) -> str | None:
    cache_file = None
    if cache_dir:
        cache_file = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=45)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if "html" not in r.headers.get("content-type", ""):
                return None
            r.encoding = r.encoding or "utf-8"
            if cache_file:
                cache_file.write_text(r.text, encoding="utf-8")
            return r.text
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  FAILED {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))


def normalize(url: str, base: str = SITE) -> str | None:
    """Canonical same-site page URL, or None if it's an asset/external/skipped."""
    url = urldefrag(urljoin(base, url.strip()))[0]
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != HOST:
        return None
    if p.query or ASSET_RE.search(p.path) or SKIP_PATH_RE.search(p.path):
        return None
    path = p.path if p.path.endswith("/") else p.path + "/"
    return f"{SITE}{path}"


def category_name(slug: str) -> str:
    return CATEGORY_NAMES.get(slug) or slug.replace("-", " ").title().replace(" And ", " and ")


def seed_urls() -> tuple[set[str], dict[str, list[str]], dict[str, str]]:
    """Returns (urls, {url: [category names]}, {url: publish date}) — categories
    come from the REST API's taxonomy classes, e.g. which department an event
    belongs to."""
    urls = {SITE + "/"}
    categories: dict[str, list[str]] = {}
    dates: dict[str, str] = {}
    index = session.get(f"{SITE}/sitemap_index.xml", timeout=45).text
    for sm in re.findall(r"<loc>([^<]+\.xml)</loc>", index):
        for loc in re.findall(r"<loc>([^<]+)</loc>", session.get(sm, timeout=45).text):
            if u := normalize(loc):
                urls.add(u)
    for rest_type in REST_TYPES:
        page = 1
        while True:
            r = session.get(
                f"{SITE}/wp-json/wp/v2/{rest_type}",
                params={"per_page": 100, "page": page, "_fields": "link,date,class_list"},
                timeout=60,
            )
            if r.status_code != 200:
                break
            for item in r.json():
                if u := normalize(item["link"]):
                    urls.add(u)
                    cats = [
                        category_name(m.group(1))
                        for c in item.get("class_list") or []
                        if (m := re.fullmatch(r"\w+_category-(.+)", c))
                        and m.group(1) not in GENERIC_TAGS
                    ]
                    categories[u] = list(dict.fromkeys(cats))
                    dates[u] = (item.get("date") or "")[:10]
            if page >= int(r.headers.get("X-WP-TotalPages", 1)):
                break
            page += 1
    return urls, categories, dates


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _alnum(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _table_to_lines(table: Tag) -> list[str]:
    lines = []
    for tr in table.find_all("tr"):
        cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render(node, out: list[str], buf: list[str]):
    """Depth-first walk emitting markdown lines into `out`; `buf` holds the
    inline text of the block currently being assembled."""

    def flush(prefix: str = ""):
        text = _clean("".join(buf))
        buf.clear()
        if text:
            out.append(prefix + text)

    for child in node.children:
        if isinstance(child, NavigableString):
            if not isinstance(child, Comment):  # also skips CDATA/doctype/PIs
                buf.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name == "br":
            flush()
        elif name == "img":
            alt = _clean(child.get("alt") or "")
            # Logo walls (recruiters, partners) carry their only text in alt.
            if len(alt) > 2 and not re.search(r"\.(jpe?g|png|webp)$|^(image|img|logo|icon)[\s\d_-]*$", alt, re.I):
                flush()
                out.append(f"[Image: {alt}]")
        elif name == "table":
            flush()
            out.extend(_table_to_lines(child))
            out.append("")
        elif re.fullmatch(r"h[1-6]", name):
            flush()
            text = _clean(child.get_text(" "))
            if text:
                out.extend(["", "#" * max(2, int(name[1])) + " " + text, ""])
        elif name == "li":
            flush()
            _render(child, out, buf)
            flush("- ")
        elif name in BLOCK_TAGS:
            flush()
            _render(child, out, buf)
            flush()
            if name in ("p", "ul", "ol", "blockquote"):
                out.append("")
        else:
            _render(child, out, buf)
    if node.name not in BLOCK_TAGS and node.name not in ("body", "[document]"):
        return
    flush()


def extract(page_html: str, url: str) -> tuple[str, str, list[tuple[str, str]], set[str]]:
    """Returns (title, markdown body, [(label, document url)], internal page links)."""
    soup = BeautifulSoup(page_html, "lxml")

    links = set()
    for a in soup.find_all("a", href=True):
        if u := normalize(a["href"], url):
            links.add(u)

    title_tag = soup.find("title")
    title = TITLE_SUFFIX_RE.sub("", _clean(title_tag.get_text())) if title_tag else ""

    body = soup.body or soup
    # Site chrome shared by every page: Elementor header/footer/popup templates,
    # menus, forms, and the floating enquiry buttons.
    for el in body.select(
        '[data-elementor-type="header"], [data-elementor-type="footer"], '
        '[data-elementor-type="popup"], [data-elementor-type="floating-buttons"], '
        "nav, script, style, noscript, form, svg, select, "
        ".elementor-screen-only, .elementor-nav-menu, .e-floating-buttons, "
        ".elementor-pagination, .e-load-more-anchor, .skip-link"
    ):
        el.decompose()

    # Single event/achievement pages end with a "Recent ..." card grid of
    # OTHER items — identical on every page, and it would make every event
    # look relevant to a query about the three newest ones.
    if urlparse(url).path.split("/")[1] in ("events", "achievements") and urlparse(url).path.count("/") > 2:
        for el in body.select(".elementor-widget-posts, .elementor-widget-loop-grid"):
            el.decompose()
        for el in body.select(".elementor-widget-heading"):
            if _clean(el.get_text()).lower().startswith(("recent", "related", "event", "achievement")) and len(_clean(el.get_text())) < 25:
                el.decompose()

    # Animated counters render as "0" until JS runs; the real figure is in an attribute.
    for el in body.select(".elementor-counter-number[data-to-value]"):
        el.string = el["data-to-value"]

    docs: list[tuple[str, str]] = []
    seen_docs = set()

    def add_doc(href: str, label: str):
        href = urldefrag(urljoin(url, href))[0]
        # The site has upload links written without the leading slash, which
        # resolve to /<page>/wp-content/... or http://wp-content/... and 404.
        href = re.sub(r"^https?://(act\.edu\.in/[^?]*?/)?wp-content/", f"{SITE}/wp-content/", href)
        if DOC_RE.search(urlparse(href).path) and href not in seen_docs:
            seen_docs.add(href)
            docs.append((label or Path(urlparse(href).path).stem.replace("-", " "), href))

    for a in body.find_all("a", href=True):
        add_doc(a["href"], _clean(a.get_text(" ")))
    # Some pages are nothing but a PDF shown in an inline viewer.
    for frame in body.find_all("iframe"):
        add_doc(frame.get("src") or frame.get("data-src") or "", "")
        frame.decompose()

    out: list[str] = []
    _render(body, out, [])

    # Collapse blank runs and the duplicate lines Elementor emits for
    # desktop+mobile copies of the same tab/accordion title.
    lines: list[str] = []
    for line in out:
        if line == "" and (not lines or lines[-1] == ""):
            continue
        if line.startswith("Read More"):  # card link text on listing grids
            continue
        if line and lines and line.lstrip("#- ") == lines[-1].lstrip("#- "):
            continue
        if line and len(lines) >= 2 and lines[-1] == "" and line.lstrip("#- ") == lines[-2].lstrip("#- "):
            continue
        lines.append(line)
    text = html.unescape("\n".join(lines)).strip()

    if not title:
        h = re.search(r"^#+ (.+)$", text, re.M)
        title = h.group(1) if h else url
    return title, text, docs, links


def out_path(url: str) -> Path:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return OUT_DIR / "pages" / "home.md"
    if parts[0] in SECTIONS and len(parts) > 1:
        return OUT_DIR / parts[0] / ("-".join(parts[1:]) + ".md")
    return OUT_DIR / "pages" / ("-".join(parts) + ".md")


def doc_skip_reason(href: str, src: str, latest_eoa: str) -> str | None:
    for pattern, reason in DOC_SKIP_RULES:
        if pattern.search(f"{src} {href}"):
            return reason
    # Each year's AICTE approval report restates intake per course; old years
    # would be retrieved alongside the current one and contradict it.
    if (m := EOA_RE.search(href)) and m.group(1) != latest_eoa:
        return "superseded by a newer year's report"
    return None


def read_document(href: str, cache_dir: Path) -> tuple[str, list[str]]:
    """Download one PDF and extract its text. Returns (status, [page texts]).
    Runs in a worker process — pdfplumber is CPU-bound."""
    import logging

    logging.disable(logging.CRITICAL)  # pdfminer warns per malformed object
    if not href.lower().endswith(".pdf"):
        return "not a PDF", []
    pdf_file = cache_dir / (hashlib.sha1(href.encode()).hexdigest() + ".pdf")
    text_file = pdf_file.with_suffix(".pages.json")  # extraction is the slow part
    if text_file.exists():
        cached = tuple(json.loads(text_file.read_text(encoding="utf-8")))
        # A cached "scanned" verdict may predate this file being added to OCR_DOC_RE.
        # ...or come from a different OCR engine than the current one.
        if not (OCR_DOC_RE.search(href) and "Sarvam OCR" not in cached[0]):
            return cached
    result = _read_document(href, pdf_file)
    if not result[0].startswith("download failed"):
        text_file.write_text(json.dumps(result), encoding="utf-8")
    return result


def _read_document(href: str, pdf_file: Path) -> tuple[str, list[str]]:
    import pdfplumber

    try:
        if not pdf_file.exists():
            r = requests.get(href, timeout=180, headers={"User-Agent": USER_AGENT})
            if r.status_code != 200:
                return f"download failed (HTTP {r.status_code})", []
            pdf_file.write_bytes(r.content)
        with pdfplumber.open(pdf_file) as pdf:
            pages = [(page.extract_text() or "").strip() for page in pdf.pages]
    except Exception as e:
        return f"unreadable ({type(e).__name__})", []
    if sum(len(t) for t in pages) < MIN_DOC_CHARS:
        if not OCR_DOC_RE.search(href):
            return "scanned, no text layer (needs OCR)", []
        try:
            return "ingested via Sarvam OCR", ocr_pdf(pdf_file)
        except Exception as e:
            return f"scanned, OCR failed ({type(e).__name__}: {e})", []
    return "ingested", pages


def strip_letterhead(page_text: str) -> str:
    lines = [line for line in page_text.split("\n") if line.strip()]
    head = [
        line for line in lines[:LETTERHEAD_MAX_LINES]
        if not LETTERHEAD_RE.search(line.replace(" ", "").replace("|", "").lstrip("#"))
    ]
    return "\n".join(head + lines[LETTERHEAD_MAX_LINES:]).strip()


def html_tables_to_rows(text: str) -> str:
    """Sarvam returns tables as inline HTML; turn each into "| a | b |" rows,
    the same shape tables from web pages get (and a third of the tokens)."""
    def convert(match: re.Match) -> str:
        table = BeautifulSoup(match.group(0), "lxml")
        return "\n" + "\n".join(_table_to_lines(table)) + "\n"

    return re.sub(r"<table\b.*?</table>", convert, text, flags=re.S | re.I)


def _sarvam(method: str, url: str, **kwargs) -> requests.Response:
    """One Sarvam call, waiting out the 10-requests/minute limit if hit."""
    headers = {"api-subscription-key": os.environ["SARVAM_API_KEY"]}
    for _ in range(8):
        r = requests.request(method, url, headers=headers, timeout=180, **kwargs)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        time.sleep(20)
    r.raise_for_status()
    return r


def ocr_pdf(pdf_file: Path) -> list[str]:
    """Text of each page of a scanned PDF, via Sarvam Document Intelligence
    (https://docs.sarvam.ai/api-reference-docs/document-intelligence/overview)."""
    import io
    import zipfile

    import pypdfium2
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
    source = pypdfium2.PdfDocument(pdf_file)
    pages: list[str] = []
    for first in range(0, len(source), SARVAM_OCR_MAX_PAGES):
        part = pypdfium2.PdfDocument.new()
        part.import_pages(source, list(range(first, min(first + SARVAM_OCR_MAX_PAGES, len(source)))))
        buf = io.BytesIO()
        part.save(buf)
        job_id = _sarvam(
            "POST", f"{SARVAM_DOC_AI}/digitise",
            files={"file": ("doc.pdf", buf.getvalue(), "application/pdf")},
            data={"language": "en-IN", "output_format": "md"},
        ).json()["job_id"]
        deadline = time.time() + SARVAM_OCR_TIMEOUT_S
        while True:
            time.sleep(8)
            state = _sarvam("GET", f"{SARVAM_DOC_AI}/{job_id}/status").json()["status"].lower()
            if state in ("completed", "partially_completed"):
                break
            if state in ("failed", "rejected") or time.time() > deadline:
                raise RuntimeError(f"Sarvam OCR job {job_id} ended as {state}")
        archive = zipfile.ZipFile(io.BytesIO(
            requests.get(_sarvam("GET", f"{SARVAM_DOC_AI}/{job_id}/download-url").json()["url"], timeout=180).content
        ))
        # metadata/page_NNN.json holds each page's blocks with a layout tag —
        # used instead of the combined .md, which has no page boundaries.
        by_page = {}
        for name in archive.namelist():
            if re.search(r"metadata/page_\d+\.json$", name):
                page = json.loads(archive.read(name).decode("utf-8"))
                by_page[page["page_num"]] = page
        for n in range(1, len(part) + 1):
            blocks = sorted(by_page.get(n, {}).get("blocks", []), key=lambda b: b.get("reading_order", 0))
            lines = []
            for b in blocks:
                text = (b.get("text") or "").strip()
                tag = (b.get("layout_tag") or "").lower()
                if not text or tag in OCR_DROP_LAYOUT_TAGS:
                    continue
                lines.append(f"### {text}" if tag in ("section-title", "title") else text)
            pages.append("\n\n".join(lines))
    source.close()
    return pages


def write_documents(all_docs: dict, titles: dict[str, str], cache_dir: Path) -> int:
    """all_docs: {doc url: (link label, source page url)}. Returns files written."""
    eoa_years = [m.group(1) for href in all_docs if (m := EOA_RE.search(href))]
    latest_eoa = max(eoa_years, default="")
    report, todo = {}, []
    for href, (_, src) in all_docs.items():
        if reason := doc_skip_reason(href, src, latest_eoa):
            report[href] = f"skipped: {reason}"
        else:
            todo.append(href)

    print(f"Reading {len(todo)} linked documents...")
    written, used_names = 0, set()
    with ProcessPoolExecutor(WORKERS) as pool:
        results = pool.map(read_document, todo, [cache_dir] * len(todo))
        for href, (status, pages) in zip(todo, results):
            report[href] = status
            if not pages:
                continue
            label, src = all_docs[href]
            if "OCR" in status:
                pages = [strip_letterhead(html_tables_to_rows(text)) for text in pages]
            if STRIP_PERSONAL_DATA_DOC_RE.search(href):
                pages = [BIRTH_DATE_RE.sub(r"\1", PHONE_RE.sub("", text)) for text in pages]
                status += ", phone numbers and birth dates removed"
            stem = re.sub(r"[-_]+", " ", Path(urlparse(href).path).stem).strip()
            name = stem if GENERIC_LABEL_RE.match(label) or len(label) < 6 else f"{label} ({stem})"
            parts = [
                f"# {name} — document from the {titles.get(src, 'website')} page",
                f"Source: {href}",
                f"Linked from: {src}",
            ]
            for n, text in enumerate(pages, 1):
                if text:
                    parts += ["", f"## Page {n}", "", text]
            slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:80] or "document"
            while slug in used_names:
                slug += "-2"
            used_names.add(slug)
            path = OUT_DIR / "documents" / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(parts) + "\n", encoding="utf-8")
            written += 1
            report[href] = f"{status} ({sum(1 for t in pages if t)} pages) -> documents/{slug}.md"

    # Underscore prefix + .txt: a human-readable report, not ingested by the retriever.
    (OUT_DIR / "_documents_report.txt").write_text(
        "\n".join(
            f"{report[href]}\t{href}\t(linked from {all_docs[href][1]})"
            for href in sorted(all_docs, key=lambda h: (report[h].split(" ")[0], h))
        ) + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for status in report.values():
        key = re.sub(r" \(\d+ pages\).*", "", status)
        counts[key] = counts.get(key, 0) + 1
    for key, n in sorted(counts.items()):
        print(f"  {n:4}  {key}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", type=Path, help="keep/re-use raw HTML and PDFs here")
    args = ap.parse_args()
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    print("Discovering URLs (sitemap + REST API)...")
    queue, categories, dates = seed_urls()
    print(f"  {len(queue)} seed URLs")

    done: dict[str, tuple[str, str, list]] = {}
    seen = set(queue)
    with ThreadPoolExecutor(WORKERS) as pool:
        while queue:
            batch = sorted(queue)
            queue = set()
            for url, page_html in zip(batch, pool.map(lambda u: fetch(u, args.cache_dir), batch)):
                if page_html is None:
                    continue
                title, text, docs, links = extract(page_html, url)
                if cats := categories.get(url):
                    text = f"Category: {', '.join(cats)}\n\n{text}"
                    # Every department's sub-pages share the same titles
                    # ("Faculty", "Events", "Infrastructure") — say whose.
                    if urlparse(url).path.startswith("/departments/"):
                        title = f"{title} — {cats[0]} department"
                done[url] = (title, text, docs)
                for link in links - seen:
                    seen.add(link)
                    queue.add(link)
            print(f"  fetched {len(done)} pages, {len(queue)} newly discovered")

    if not done:
        sys.exit("Nothing fetched — leaving the existing knowledge_base/website/ untouched.")

    # Empty the folder rather than removing it: on Windows the folder itself is
    # often held open (an editor, a shell sitting in it) and can't be deleted.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for child in OUT_DIR.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    written, skipped, all_docs = 0, [], {}
    # {section: {category: [(publish date, title, body, url)]}}
    grouped: dict[str, dict[str, list[tuple[str, str, str, str]]]] = {}
    for url in sorted(done):
        title, text, docs = done[url]
        for label, href in docs:
            all_docs.setdefault(href, (label, url))
        section = urlparse(url).path.split("/")[1]
        if section == "newsletter":
            # The page is only a calendar of 600+ dated PDF links (see DOC_SKIP_RULES).
            text = ("Agni College of Technology publishes a daily newsletter. The archive of "
                    f"past issues (2022 to 2024) can be browsed by date at {url}")
            docs = []
        if section in ("events", "achievements") and url.rstrip("/") != f"{SITE}/{section}":
            # One post -> an entry in its department's file (see module docstring).
            body = "\n".join(
                line for line in text.splitlines()
                # Compare letters/digits only: the page heading uses curly
                # quotes where the <title> has straight ones.
                if not line.startswith("Category:") and _alnum(line) != _alnum(title)
            ).strip()
            body = re.sub(r"\n{3,}", "\n\n", body)
            if docs:
                body += "\n" + "\n".join(f"Document: {label}: {href}" for label, href in docs)
            for cat in categories.get(url) or [COLLEGE_WIDE]:
                grouped.setdefault(section, {}).setdefault(cat, []).append(
                    (dates.get(url, ""), title, body.strip(), url)
                )
            continue
        # What the page says beyond repeating its own title / category line.
        substance = re.sub(r"^Category:.*$", "", text, flags=re.M).replace(title, "").strip("# \n")
        if len(substance) < MIN_BODY_CHARS and not docs:
            skipped.append(url)
            continue
        parts = [f"# {title}", f"Source: {url}", "", text]
        if docs:
            parts += ["", "## Documents linked on this page"]
            parts += [f"- {label}: {href}" for label, href in docs]
        path = out_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")
        written += 1

    for section, by_cat in grouped.items():
        per_day: dict[str, int] = {}
        for date in {(d, u) for posts in by_cat.values() for d, _, _, u in posts}:
            per_day[date[0]] = per_day.get(date[0], 0) + 1
        for cat, posts in sorted(by_cat.items()):
            is_dept = bool(re.search(r"engineering|technology|science|humanities|aerospace", cat, re.I))
            heading = f"{section.title()} — {cat}{' department' if is_dept else ''}"
            parts = [
                f"# {heading}",
                f"Source: {SITE}/{section}/",
                "",
                f"Every one of the {len(posts)} {section} that the Agni College of Technology "
                f"website lists under {cat}, newest first. Entries with only a title have no "
                "description on the website (the details are in a poster or photo).",
            ]
            for date, title, body, _ in sorted(posts, key=lambda p: p[0], reverse=True):
                parts += ["", f"## {title}"]
                if date and per_day[date] <= BULK_IMPORT_POSTS_PER_DAY:
                    parts.append(f"Posted on the website: {time.strftime('%d %B %Y', time.strptime(date, '%Y-%m-%d'))}")
                if body:
                    parts += ["", body]
            slug = re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-")
            path = OUT_DIR / section / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(parts) + "\n", encoding="utf-8")
            written += 1
        print(f"  {section}: {sum(len(v) for v in by_cat.values())} entries in {len(by_cat)} files")

    print(f"\nWrote {written} pages to {OUT_DIR}")
    print(f"Skipped {len(skipped)} near-empty pages (image/video/form only): {skipped}")

    titles = {url: title for url, (title, _, _) in done.items()}
    with tempfile.TemporaryDirectory() as tmp:
        n_docs = write_documents(all_docs, titles, args.cache_dir or Path(tmp))
    print(f"Wrote {n_docs} documents; full list in {OUT_DIR / '_documents_report.txt'}")


if __name__ == "__main__":
    main()
