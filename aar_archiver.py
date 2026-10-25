#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aar_archiver.py — делает локальную офлайн-копию треда XenForo (форум Paradox Plaza
и любой другой на движке XenForo 2.x): AAR, летсплеи, дневники разработки.

Только стандартная библиотека Python 3.8+, никаких pip install.

Что получается на выходе:

    out/
      raw/page-001.html ...   исходный HTML каждой страницы (как скачался)
      images/                 все картинки треда, скачанные локально
      posts.json              структурированные данные (автор, дата, id, HTML, текст)
      <Slug>.html             один файл-«читалка»: оглавление + все посты
      markdown/all.md         весь тред одним Markdown-файлом
      markdown/page-001.md    по странице за раз
      <Slug>.epub             (с флагом --epub) книжка для ридера

Примеры:

    python3 aar_archiver.py https://forum.paradoxplaza.com/forum/threads/siberian-prosperity-an-economic-ck2-aar.1567294/
    python3 aar_archiver.py <url> --pages 1-5 --op-only --epub
    python3 aar_archiver.py <url> --source wayback          # если форум недоступен/закрыт
    python3 aar_archiver.py <url> --inline-images           # HTML-копия одним файлом

Назначение — личная резервная копия публично доступного треда для чтения офлайн.
Авторские права на текст и скриншоты принадлежат их владельцам; не публикуйте
и не распространяйте полученную копию без разрешения автора.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html as html_mod
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

WAYBACK_HOST = "https://web.archive.org"
CDX_URL = WAYBACK_HOST + "/cdx/search/cdx"

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Какие ОТКРЫТЫЕ теги implicitly закрываются, когда встречается новый тег.
# Ключ — открываемый тег, значение — что нужно снять со стека перед ним.
_BLOCKERS = {
    "div", "ul", "ol", "li", "blockquote", "table", "pre", "p", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "header",
    "footer", "form", "fieldset", "figure", "aside", "nav", "dl",
}

IMPLICIT_CLOSE: Dict[str, set] = {
    "p": {"p"},
    "li": {"li", "p"},
    "dt": {"dt", "dd", "p"},
    "dd": {"dt", "dd", "p"},
    "option": {"option"},
    "optgroup": {"option", "optgroup"},
    "thead": {"tr", "td", "th", "p"},
    "tbody": {"tr", "td", "th", "p", "thead", "tbody"},
    "tfoot": {"tr", "td", "th", "p", "thead", "tbody"},
    "tr": {"tr", "td", "th", "p"},
    "td": {"td", "th", "p"},
    "th": {"td", "th", "p"},
    "blockquote": {"p"},
}
for _t in _BLOCKERS:
    IMPLICIT_CLOSE.setdefault(_t, set()).add("p")
del _t

SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template"}

PLACEHOLDER_HINTS = ("loading", "spacer", "blank", "1x1", "pixel")


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("  ! " + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #

def parse_cookie_header(spec: Optional[str], cookie_file: Optional[str]) -> str:
    """Собирает строку Cookie из --cookies 'a=b; c=d' и/или файла Netscape."""
    pairs: List[str] = []
    if spec:
        pairs.extend(p.strip() for p in spec.split(";") if p.strip())
    if cookie_file:
        try:
            with open(cookie_file, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        pairs.append("%s=%s" % (parts[5], parts[6]))
        except OSError as exc:
            warn("не прочитал файл куки %s: %s" % (cookie_file, exc))
    return "; ".join(pairs)


class HttpClient:
    def __init__(self, ua: str = DEFAULT_UA, delay: float = 3.0, retries: int = 4,
                 cookie: str = "", timeout: int = 60, source: str = "forum",
                 wayback_ts: str = "2", quiet: bool = False) -> None:
        self.ua = ua
        self.delay = delay
        self.retries = retries
        self.cookie = cookie
        self.timeout = timeout
        self.source = source
        self.wayback_ts = wayback_ts
        self.quiet = quiet
        self._last_request = 0.0
        self._cdx_cache: Dict[str, Optional[str]] = {}
        self.stats = {"requests": 0, "bytes": 0, "failures": 0}

    # -- внутренние ------------------------------------------------------- #

    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _raw_get(self, url: str, referer: Optional[str] = None) -> bytes:
        """Один запрос с ретраями. Возвращает тело или бросает FetchError."""
        headers = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if referer:
            headers["Referer"] = referer

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            self._last_request = time.time()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    if enc == "gzip":
                        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                    elif enc == "deflate":
                        try:
                            body = zlib_decompress(body)
                        except Exception:
                            pass
                    self.stats["requests"] += 1
                    self.stats["bytes"] += len(body)
                    return body
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (403, 404, 410):
                    # 429/5xx стоит повторить, 403/404 — обычно нет
                    if exc.code != 429:
                        break
                self._backoff(attempt, "%s -> HTTP %s" % (url, exc.code))
            except Exception as exc:  # URLError, таймауты, SSL и т.п.
                last_err = exc
                self._backoff(attempt, "%s -> %s" % (url, exc))
        self.stats["failures"] += 1
        raise FetchError(url, last_err)

    def _backoff(self, attempt: int, note: str) -> None:
        if attempt >= self.retries:
            return
        pause = min(30.0, 2.0 ** attempt)
        if not self.quiet:
            warn("повтор через %.0fс (%s)" % (pause, note))
        time.sleep(pause)

    # -- wayback ---------------------------------------------------------- #

    def _wayback_timestamp(self, original_url: str) -> Optional[str]:
        """Самый свежий успешный снимок URL в web.archive.org (через CDX)."""
        if original_url in self._cdx_cache:
            return self._cdx_cache[original_url]
        key = original_url.split("://", 1)[-1].rstrip("/")
        params = urllib.parse.urlencode({
            "url": key,
            "output": "json",
            "fl": "timestamp,statuscode",
            "filter": "statuscode:200",
            "limit": "-3",
        })
        ts: Optional[str] = None
        try:
            body = self._raw_get(CDX_URL + "?" + params)
            rows = json.loads(body.decode("utf-8", "replace"))
            for row in rows[1:]:
                if len(row) >= 2 and row[1] == "200":
                    ts = row[0]
        except Exception as exc:
            warn("CDX-запрос не удался для %s: %s" % (key, exc))
        if ts is None:
            ts = self.wayback_ts
        self._cdx_cache[original_url] = ts
        return ts

    def wayback_url(self, original_url: str) -> str:
        ts = self._wayback_timestamp(original_url)
        return "%s/web/%sid_/%s" % (WAYBACK_HOST, ts, original_url)

    # -- публичный -------------------------------------------------------- #

    def get_page(self, url: str, referer: Optional[str] = None) -> Tuple[str, bytes]:
        """Возвращает (url-который-парсить, тело). Учитывает --source."""
        if self.source == "wayback":
            target = self.wayback_url(url)
            if not self.quiet:
                log("  wayback: %s" % target)
            return url, self._raw_get(target, referer=None)
        return url, self._raw_get(url, referer=referer)

    def get_binary(self, url: str, referer: Optional[str] = None) -> bytes:
        return self._raw_get(url, referer=referer)


def zlib_decompress(body: bytes) -> bytes:
    import zlib
    try:
        return zlib.decompress(body)
    except zlib.error:
        return zlib.decompress(body, -zlib.MAX_WBITS)


class FetchError(Exception):
    def __init__(self, url: str, cause: Optional[Exception]) -> None:
        super().__init__("не удалось скачать %s (%s)" % (url, cause))
        self.url = url
        self.cause = cause


# --------------------------------------------------------------------------- #
#  XenForo: структура треда
# --------------------------------------------------------------------------- #

@dataclass
class Thread:
    url: str
    thread_id: str = ""
    slug: str = ""
    title: str = ""
    last_page: int = 1
    starter: str = ""

    @property
    def out_name(self) -> str:
        base = self.slug or self.thread_id or "thread"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "thread"


def split_thread_url(url: str) -> Thread:
    url = url.split("#", 1)[0].rstrip("/") + "/"
    m = re.search(r"/([^/?#]+)\.(\d+)/?$", url)
    if m:
        slug, tid = m.group(1), m.group(2)
    else:
        slug, tid = "", ""
    return Thread(url=url, thread_id=tid, slug=slug)


def page_url(thread_url: str, page: int) -> str:
    base = thread_url.rstrip("/") + "/"
    if page <= 1:
        return base
    return base + "page-%d" % page


TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", re.S)


def find_blocks(html_text: str, tag: str,
                predicate=None) -> List[Tuple[int, int, str]]:
    """Все блоки <tag ...>...</tag> верхнего уровня: (start, end, attrs)."""
    out: List[Tuple[int, int, str]] = []
    depth = 0
    start = 0
    attrs = ""
    tag_l = tag.lower()
    for m in TAG_RE.finditer(html_text):
        closing, name, attr_txt = m.group(1), m.group(2).lower(), m.group(3)
        if name != tag_l:
            continue
        if not closing:
            if depth == 0:
                start, attrs = m.start(), attr_txt
            depth += 1
        else:
            depth -= 1
            if depth <= 0:
                depth = 0
                if predicate is None or predicate(attrs):
                    out.append((start, m.end(), attrs))
    return out


def _match_end(html_text: str, tag: str, open_end: int) -> int:
    """Конец блока, открытого в open_end (учитывает вложенность одноимённых тегов)."""
    depth = 1
    tag_l = tag.lower()
    pat = re.compile(r"<(/?)%s\b" % re.escape(tag_l), re.I)
    for m in pat.finditer(html_text, open_end):
        if m.group(1):
            depth -= 1
            if depth == 0:
                close = html_text.find(">", m.start())
                return close + 1 if close != -1 else len(html_text)
        else:
            depth += 1
    return len(html_text)


def find_all_blocks(html_text: str, tag: str,
                    predicate=None) -> List[Tuple[int, int, str]]:
    """Все блоки <tag>, подходящие под predicate, на ЛЮБОЙ глубине, по порядку."""
    out: List[Tuple[int, int, str]] = []
    pat = re.compile(r"<%s\b([^>]*)>" % re.escape(tag.lower()), re.I | re.S)
    for m in pat.finditer(html_text):
        attrs = m.group(1)
        if attrs.rstrip().endswith("/"):
            continue
        if predicate is not None and not predicate(attrs):
            continue
        out.append((m.start(), _match_end(html_text, tag, m.end()), attrs))
    return out


def block_inner(html_text: str, block: Tuple[int, int, str]) -> str:
    """Содержимое блока без его собственного открывающего/закрывающего тега."""
    s_, e_, _ = block
    frag = html_text[s_:e_]
    m = re.match(r"<[^>]*>", frag)
    start = m.end() if m else 0
    end = frag.rfind("</")
    return frag[start:end] if end != -1 else frag[start:]


def attr_value(attrs: str, name: str) -> str:
    m = re.search(r'\b%s\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))' % re.escape(name),
                  attrs, re.I)
    if not m:
        return ""
    val = m.group(2) or m.group(3) or m.group(4) or ""
    return html_mod.unescape(val)


def has_class(attrs: str, cls: str) -> bool:
    return cls in attr_value(attrs, "class").split()


def detect_last_page(html_text: str, thread_hint: str = "") -> int:
    """Максимальный номер страницы из пагинации XenForo.

    Смотрит только в блок навигации, чтобы не подхватить page-N из сайдбара
    («последние сообщения» других тредов).
    """
    best = 1
    key = ""
    if thread_hint:
        m = re.search(r"/([^/?#]+\.(\d+))/?$", thread_hint.rstrip("/"))
        if m:
            key = m.group(1)

    scopes: List[str] = []
    for m in re.finditer(r'<nav[^>]*pageNav[^>]*>(.*?)</nav>', html_text, re.S):
        scopes.append(m.group(1))
    for m in re.finditer(r'<ul[^>]*pageNav-main[^>]*>(.*?)</ul>', html_text, re.S):
        scopes.append(m.group(1))
    if not scopes:
        scopes = [html_text]

    for scope in scopes:
        for m in re.finditer(r'href="([^"]*page-(\d+)[^"]*)"', scope):
            href, num = m.group(1), int(m.group(2))
            if key and key not in href and not href.startswith("?"):
                continue
            best = max(best, num)
        for m in re.finditer(r'class="[^"]*pageNav-page[^"]*"[^>]*>\s*(\d+)\s*<', scope):
            best = max(best, int(m.group(1)))
    return best


def detect_title(html_text: str) -> str:
    m = re.search(r'<h1[^>]*class="[^"]*p-title-value[^"]*"[^>]*>(.*?)</h1>', html_text, re.S)
    if not m:
        m = re.search(r"<title>(.*?)</title>", html_text, re.S)
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", "", m.group(1))
    txt = html_mod.unescape(txt).strip()
    return re.sub(r"\s*\|\s*Paradox Interactive Forums\s*$", "", txt).strip()


def detect_starter(html_text: str) -> str:
    m = re.search(r'data-author="([^"]+)"', html_text)
    if m:
        return html_mod.unescape(m.group(1))
    m = re.search(r'class="username"[^>]*>([^<]+)<', html_text)
    return html_mod.unescape(m.group(1)).strip() if m else ""


@dataclass
class Post:
    post_id: str = ""
    author: str = ""
    date: str = ""
    page: int = 0
    index: int = 0
    body_html: str = ""
    images: List[str] = field(default_factory=list)
    is_op: bool = False


def cut_before(chunk: str, markers: Sequence[str]) -> str:
    pos = len(chunk)
    for mk in markers:
        i = chunk.find(mk)
        if i != -1:
            pos = min(pos, i)
    return chunk[:pos]


def extract_body_html(article: str) -> str:
    """HTML тела поста без подписи и футлера."""
    body = cut_before(article, ('class="message-signature', 'class="message-footer',
                                "<footer", 'class="message-lastEdit'))
    scope = body
    for tag, cls in (("article", "message-body"), ("div", "js-messageBody"),
                     ("div", "message-userContent")):
        blocks = find_all_blocks(body, tag, lambda a, c=cls: has_class(a, c))
        if blocks:
            scope = body[blocks[0][0]:blocks[0][1]]
            break
    wrappers = find_all_blocks(scope, "div", lambda a: has_class(a, "bbWrapper"))
    if not wrappers:
        return block_inner(scope, find_all_blocks(scope, "div")[0]) \
            if find_all_blocks(scope, "div") else scope
    # первая (самая внешняя) bbWrapper — тело поста; вложенные относятся к цитатам
    return block_inner(scope, wrappers[0])


def extract_date(article: str) -> str:
    footer_i = article.find('class="message-footer')
    parts = [article[footer_i:]] if footer_i != -1 else []
    parts.append(article)
    for part in parts:
        m = re.search(r'<time[^>]*datetime="([^"]+)"', part)
        if m:
            return m.group(1)
    m = re.search(r'data-date="([^"]+)"', article)
    return m.group(1) if m else ""


def parse_posts(html_text: str, page: int) -> List[Post]:
    posts: List[Post] = []
    for s, e, attrs in find_blocks(html_text, "article",
                                   lambda a: has_class(a, "message--post")):
        chunk = html_text[s:e]
        pid = attr_value(attrs, "id") or attr_value(attrs, "data-content")
        pid = re.sub(r"^post-", "", pid)
        author = attr_value(attrs, "data-author")
        if not author:
            m = re.search(r'class="username"[^>]*>([^<]+)<', chunk)
            author = html_mod.unescape(m.group(1)).strip() if m else ""
        posts.append(Post(post_id=pid, author=author, date=extract_date(chunk),
                          page=page, body_html=extract_body_html(chunk)))
    return posts


# --------------------------------------------------------------------------- #
#  HTML -> дерево
# --------------------------------------------------------------------------- #

@dataclass
class El:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List[object] = field(default_factory=list)  # El | str

    def walk(self) -> Iterator["El"]:
        yield self
        for ch in self.children:
            if isinstance(ch, El):
                yield from ch.walk()

    def classes(self) -> List[str]:
        return self.attrs.get("class", "").split()


class TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = El("#root")
        self.stack: List[El] = [self.root]
        self._skip_depth = 0

    # -- helpers ---------------------------------------------------------- #
    def _append(self, node: object) -> None:
        self.stack[-1].children.append(node)

    def _close_to(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    # -- HTMLParser ------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        parent = self.stack[-1]
        # неявное закрытие соседей: <p> без </p>, <li> без </li> и т.п.
        closable = IMPLICIT_CLOSE.get(tag)
        if closable:
            while len(self.stack) > 1 and self.stack[-1].tag in closable:
                self.stack.pop()
            parent = self.stack[-1]
        el = El(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        parent.children.append(el)
        if tag not in VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip_depth or tag in SKIP_CONTENT_TAGS:
            return
        self._append(El(tag, {k.lower(): (v if v is not None else "") for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in VOID_TAGS:
            return
        self._close_to(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        self._append(data)


def build_tree(fragment: str) -> El:
    tb = TreeBuilder()
    tb.feed(fragment or "")
    tb.close()
    return tb.root


def text_of(node: object) -> str:
    if isinstance(node, str):
        return node
    return "".join(text_of(c) for c in node.children)


# --------------------------------------------------------------------------- #
#  Картинки
# --------------------------------------------------------------------------- #

def img_candidates(el: El) -> List[str]:
    """XenForo грузит картинки лениво: настоящая ссылка в data-src/data-url."""
    out = []
    for key in ("data-src", "data-url", "data-lazy-src", "src", "href"):
        val = el.attrs.get(key, "").strip()
        if not val or val.startswith("data:"):
            continue
        low = val.lower()
        if key == "src" and any(h in low for h in PLACEHOLDER_HINTS) \
                and low.endswith((".gif", ".png")):
            continue
        if val not in out:
            out.append(val)
    return out


def ext_from(url: str, content_type: str = "") -> str:
    path = urllib.parse.urlparse(url).path
    base = os.path.basename(path)
    _, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"):
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
        "image/avif": ".avif",
    }.get(ct, ".jpg")


def sanitize_name(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    base = os.path.basename(path) or os.path.basename(os.path.dirname(path))
    base = os.path.splitext(base)[0]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:60].strip("._")
    return base or "image"


class ImageStore:
    def __init__(self, client: HttpClient, out_dir: str, download: bool = True,
                 inline: bool = False, quiet: bool = False) -> None:
        self.client = client
        self.dir = out_dir
        self.download = download
        self.inline = inline
        self.quiet = quiet
        self.map: Dict[str, str] = {}       # исходный URL -> локальное имя файла
        self.data: Dict[str, str] = {}      # исходный URL -> data: URI
        self.failed: List[str] = []
        os.makedirs(out_dir, exist_ok=True)

    def localize(self, src: str, page_url_: str) -> Optional[str]:
        if src.startswith("data:"):
            return src
        absolute = urllib.parse.urljoin(page_url_, src)
        if absolute in self.map:
            return self.data[absolute] if self.inline else self.map[absolute]
        if not self.download:
            return absolute
        try:
            blob = self.client.get_binary(absolute, referer=page_url_)
        except FetchError as exc:
            self.failed.append(absolute)
            if not self.quiet:
                warn("картинка не скачалась: %s" % exc)
            return absolute
        digest = hashlib.sha1(blob).hexdigest()[:12]
        name = "%s-%s%s" % (digest, sanitize_name(absolute),
                            ext_from(absolute, self._guess_ct(blob)))
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(blob)
        self.map[absolute] = "images/" + name
        if self.inline:
            mime = self._guess_ct(blob) or "image/jpeg"
            self.data[absolute] = "data:%s;base64,%s" % (
                mime, base64.b64encode(blob).decode("ascii"))
            return self.data[absolute]
        return self.map[absolute]

    @staticmethod
    def _guess_ct(blob: bytes) -> str:
        if blob[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if blob[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            return "image/webp"
        if blob[:2] == b"BM":
            return "image/bmp"
        head = blob[:200].lstrip().lower()
        if head.startswith(b"<svg") or b"<svg" in head:
            return "image/svg+xml"
        return ""


def localize_tree(root: El, store: ImageStore, page_url_: str) -> List[str]:
    found: List[str] = []
    for el in root.walk():
        if el.tag == "img":
            for cand in img_candidates(el):
                local = store.localize(cand, page_url_)
                if local:
                    el.attrs["src"] = local
                    break
            for junk in ("data-src", "data-url", "data-lazy-src", "srcset",
                         "data-srcset", "loading"):
                el.attrs.pop(junk, None)
            el.attrs.pop("class", None)
            el.attrs.setdefault("loading", "lazy")
            found.append(el.attrs.get("src", ""))
        elif el.tag == "a":
            href = el.attrs.get("href", "")
            if href:
                el.attrs["href"] = urllib.parse.urljoin(page_url_, href)
                el.attrs["rel"] = "noopener"
                # ссылка-лайтбокс на уже скачанную картинку -> локальный файл
                if href in store.map:
                    el.attrs["href"] = store.map[href]
    return [f for f in found if f]


# --------------------------------------------------------------------------- #
#  Рендеры
# --------------------------------------------------------------------------- #

def to_xhtml(node: object) -> str:
    if isinstance(node, str):
        return html_mod.escape(node, quote=False)
    if node.tag == "#root":
        return "".join(to_xhtml(c) for c in node.children)
    attrs = "".join(
        ' %s="%s"' % (k, html_mod.escape(v, quote=True))
        for k, v in node.attrs.items() if v is not None
    )
    if node.tag in VOID_TAGS:
        return "<%s%s />" % (node.tag, attrs)
    inner = "".join(to_xhtml(c) for c in node.children)
    return "<%s%s>%s</%s>" % (node.tag, attrs, inner, node.tag)


BLOCK_TAGS = {"p", "div", "blockquote", "ul", "ol", "li", "pre", "table",
              "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr"}


def _heading_from_style(el: El) -> Optional[int]:
    style = el.attrs.get("style", "")
    m = re.search(r"font-size\s*:\s*(\d+)", style)
    if not m:
        return None
    size = int(m.group(1))
    if size >= 26:
        return 2
    if size >= 22:
        return 3
    if size >= 18:
        return 4
    return None


def _plain_heading(text: str) -> str:
    """Заголовок без markdown-разметки: XenForo делает их как <span font-size><b>…"""
    return re.sub(r"\*{1,2}|_{2}|~~", "", text).strip()


class MarkdownRenderer:
    def __init__(self) -> None:
        self.list_stack: List[str] = []
        self.ol_counters: List[int] = []

    def render(self, root: El) -> str:
        out = self._nodes(root.children, block=True)
        out = re.sub(r"(?:^>\s*\n){2,}", "> \n", out, flags=re.M)  # пустые строки в цитате
        out = re.sub(r"(?<!  )[ \t]+\n", "\n", out)                  # хвостовые пробелы
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip() + "\n"

    # -- узлы ------------------------------------------------------------- #
    def _nodes(self, children: Sequence[object], block: bool = False) -> str:
        parts: List[str] = []
        for c in children:
            piece = self._node(c, block)
            # после переноса строки ведущие пробелы исходной разметки не нужны
            if parts and parts[-1].endswith("\n"):
                piece = piece.lstrip(" \t")
            parts.append(piece)
        if block and parts:
            # отступы исходной разметки не должны попадать в начало/конец блока:
            # в Markdown строка из 4+ пробелов превращается в блок кода
            parts[0] = parts[0].lstrip()
            parts[-1] = parts[-1].rstrip()
        return "".join(parts)

    def _node(self, node: object, block: bool) -> str:
        if isinstance(node, str):
            # пробелы/переносы исходной разметки не значимы: в Markdown отступ
            # в 4 пробела превратил бы обычный текст в блок кода
            return re.sub(r"\s+", " ", node)
        el: El = node  # type: ignore[assignment]
        tag = el.tag

        if tag in ("b", "strong"):
            inner = self._inline(el)
            return "**%s**" % inner.strip() if inner.strip() else ""
        if tag in ("i", "em"):
            inner = self._inline(el)
            return "*%s*" % inner.strip() if inner.strip() else ""
        if tag in ("s", "strike", "del"):
            inner = self._inline(el)
            return "~~%s~~" % inner.strip() if inner.strip() else ""
        if tag == "u":
            return "<u>%s</u>" % self._inline(el).strip()
        if tag == "a":
            inner = self._inline(el).strip() or el.attrs.get("href", "")
            href = el.attrs.get("href", "")
            return "[%s](%s)" % (inner, href) if href else inner
        if tag == "img":
            src = el.attrs.get("src", "")
            alt = el.attrs.get("alt", "") or ""
            return "\n\n![%s](%s)\n\n" % (alt, src)
        if tag == "br":
            return "  \n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "span":
            level = _heading_from_style(el)
            inner = self._nodes(el.children).strip()
            if level and inner:
                return "\n\n%s %s\n\n" % ("#" * level, _plain_heading(inner))
            return inner
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = min(6, int(tag[1]) + 1)
            inner = self._nodes(el.children).strip()
            return "\n\n%s %s\n\n" % ("#" * level, _plain_heading(inner))
        if tag in ("p", "div"):
            cls = el.classes()
            if "bbCodeSpoiler-content" in cls:
                inner = self._nodes(el.children).strip()
                return "\n\n" + self._prefix_block("> ", inner) + "\n\n" if inner else ""
            if any(c.startswith("bbCodeSpoiler") for c in cls):
                inner = self._nodes(el.children).strip()
                return ("\n\n> **Spoiler**\n" + self._prefix_block("> ", inner) + "\n\n") \
                    if inner else ""
            if any(c.startswith("bbCodeBlock--quote") for c in cls) or "bbQuote" in cls:
                inner = self._nodes(el.children).strip()
                return "\n\n" + self._prefix_block("> ", inner) + "\n\n" if inner else ""
            if "bbCodeUnfurl" in cls:
                return "\n\n> %s\n\n" % text_of(el).strip()[:300]
            if any(c.startswith("bbCodeBlock--code") for c in cls) or "bbCodeCode" in cls:
                return self._code_block(el)
            inner = self._nodes(el.children, block=True)
            return "\n\n%s\n\n" % inner.strip() if inner.strip() else ""
        if tag == "blockquote":
            return "\n\n" + self._prefix_block("> ", self._nodes(el.children)) + "\n\n"
        if tag in ("ul", "ol"):
            return "\n" + self._list(el, tag) + "\n"
        if tag == "li":
            return self._nodes(el.children).strip()
        if tag in ("pre", "code"):
            code = self._raw_text(el)
            return "\n\n```\n%s\n```\n\n" % code.strip("\n") if "\n" in code \
                else "`%s`" % code.strip()
        if tag == "table":
            return self._table(el)
        if tag in ("tr", "td", "th", "tbody", "thead"):
            return self._nodes(el.children)
        if tag in BLOCK_TAGS:
            return "\n\n%s\n\n" % self._nodes(el.children).strip()
        return self._nodes(el.children)

    def _inline(self, el: El) -> str:
        return self._nodes(el.children)

    def _prefix_block(self, prefix: str, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        lines = []
        for ln in text.split("\n"):
            # XenForo вкладывает цитату в цитату (bbCodeBlock--quote > blockquote
            # > bbQuote). Снимаем чужие уровни и ставим ровно один свой.
            bare = re.sub(r"^(?:>\s?)+", "", ln)
            lines.append(prefix + bare if bare.strip() else prefix.rstrip())
        return "\n".join(lines) + "\n\n"

    def _code_block(self, el: El) -> str:
        code = self._raw_text(el)
        return "\n\n```\n%s\n```\n\n" % code.strip("\n")

    def _raw_text(self, node: object) -> str:
        """Текст с сохранением переносов: <br> и блочные теги становятся \n."""
        if isinstance(node, str):
            return node
        if node.tag == "br":
            return "\n"
        inner = "".join(self._raw_text(c) for c in node.children)
        if node.tag in ("p", "div", "li", "tr", "blockquote"):
            return inner + "\n"
        return inner

    def _list(self, el: El, tag: str) -> str:
        ordered = tag == "ol"
        self.list_stack.append(tag)
        self.ol_counters.append(0)
        depth = len(self.list_stack) - 1
        lines: List[str] = []
        n = 0
        for child in el.children:
            if not isinstance(child, El) or child.tag != "li":
                continue
            n += 1
            marker = ("%d. " % n) if ordered else "- "
            body = self._nodes(child.children).strip().replace("\n", "\n" + "  " * (depth + 1))
            lines.append("  " * depth + marker + body)
        self.list_stack.pop()
        self.ol_counters.pop()
        return "\n".join(lines) + "\n"

    def _table(self, el: El) -> str:
        rows: List[List[str]] = []
        for tr in [c for c in el.walk() if isinstance(c, El) and c.tag == "tr"]:
            cells = [self._nodes(c.children).strip().replace("\n", " ")
                     for c in tr.children
                     if isinstance(c, El) and c.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |",
               "| " + " | ".join(["---"] * width) + " |"]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n\n" + "\n".join(out) + "\n\n"


def to_markdown(root: El) -> str:
    return MarkdownRenderer().render(root)


DROP_TAGS = {"button", "script", "style", "iframe", "input", "form", "select"}
DROP_CLASSES = {
    "message-signature", "sharePage", "message-attribution-control",
    "js-notices", "reactionsBar", "bbCodeSpoiler-title", "bbCodeSpoiler-button",
    "message-lastEdit", "bbCodeBlock-title",
}


def strip_noise(root: El) -> None:
    """Убирает мусор форума: кнопки, счётчики, подписи, lazy-обвязку."""
    for parent in list(root.walk()):
        if not isinstance(parent, El):
            continue
        parent.children = [
            c for c in parent.children
            if not (isinstance(c, El)
                    and (c.tag in DROP_TAGS or (set(c.classes()) & DROP_CLASSES)))
        ]


# --------------------------------------------------------------------------- #
#  Вывод
# --------------------------------------------------------------------------- #

CSS = """
:root { color-scheme: light dark; }
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.6;
       max-width: 60rem; margin: 0 auto; padding: 2rem 1.2rem 6rem; color: #1c1c1c; }
@media (prefers-color-scheme: dark) { body { background:#14161a; color:#dfe3e8; }
  a { color:#7fb3ff; } .post { border-color:#2a2f38; } .meta { color:#8b95a3; } }
h1 { font-size: 1.9rem; line-height: 1.25; }
a { color: #14539a; text-decoration: none; } a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; border-radius: 4px; }
.meta { font-size: .85rem; opacity: .75; margin-bottom: 2rem; }
nav#toc { border: 1px solid #ddd; border-radius: 8px; padding: .8rem 1.1rem; margin: 1.5rem 0 3rem; }
nav#toc ol { margin: .4rem 0 0; padding-left: 1.3rem; columns: 2; column-gap: 2rem; }
nav#toc li { font-size: .9rem; margin: .15rem 0; break-inside: avoid; }
.post { border-top: 1px solid #ddd; padding: 1.6rem 0; }
.post-head { display:flex; flex-wrap:wrap; gap:.5rem; align-items:baseline; margin-bottom:.7rem; }
.post-head .num { font-variant-numeric: tabular-nums; opacity:.5; font-size:.85rem; }
.author { font-weight: 700; }
.date { font-size: .85rem; opacity: .7; margin-left: auto; }
.op { background: rgba(20,83,154,.05); border-radius: 8px; padding: 0 1rem; }
blockquote { border-left: 3px solid #ccc; margin: .8rem 0; padding: .2rem 0 .2rem 1rem; opacity: .9; }
pre, code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9em; }
pre { background: rgba(127,127,127,.1); padding: .7rem; overflow-x: auto; border-radius: 6px; }
table { border-collapse: collapse; } td, th { border: 1px solid #bbb; padding: .2rem .5rem; }
hr { border: 0; border-top: 1px solid #ddd; margin: 2rem 0; }
.footer { margin-top: 4rem; font-size: .8rem; opacity: .6; }
"""


def esc(s: str) -> str:
    return html_mod.escape(s or "", quote=True)


def render_reader_html(thread: Thread, posts: List[Post], out_dir: str,
                       images_rel: str = "images/", inline: bool = False,
                       notes: str = "") -> str:
    title = thread.title or thread.out_name
    parts: List[str] = []
    parts.append("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">")
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>%s</title>" % esc(title))
    parts.append("<style>%s</style>\n</head>\n<body>" % CSS)
    parts.append("<h1>%s</h1>" % esc(title))
    parts.append('<p class="meta">Локальная копия треда · %d постов · '
                 'источник: <a href="%s">%s</a>%s</p>'
                 % (len(posts), esc(thread.url), esc(thread.url),
                    ("<br>" + esc(notes)) if notes else ""))

    parts.append('<nav id="toc"><strong>Содержание</strong><ol>')
    for p in posts:
        label = text_of(build_tree(p.body_html))[:70].strip().replace("\n", " ")
        parts.append('<li><a href="#post-%s">%s</a> <span class="date">%s</span></li>'
                     % (esc(p.post_id), esc(label or ("пост " + p.post_id)),
                        esc(p.date[:10])))
    parts.append("</ol></nav>")

    last_page = 0
    for p in posts:
        if p.page != last_page:
            last_page = p.page
            parts.append('<hr><p class="meta">— страница %d —</p>' % p.page)
        src = "images_rel_placeholder"
        body = p.body_html
        if not inline:
            body = re.sub(r'src="images/', 'src="%s' % images_rel, body)
        parts.append('<article class="post%s" id="post-%s">'
                     % (" op" if p.is_op else "", esc(p.post_id)))
        parts.append('<div class="post-head"><span class="num">#%d</span>'
                     '<span class="author">%s</span>'
                     '<span class="date">%s</span></div>'
                     % (p.index, esc(p.author), esc(p.date[:19].replace("T", " "))))
        parts.append('<div class="body">%s</div></article>' % body)

    parts.append('<p class="footer">Скопировано %s · aar_archiver %s · '
                 'только для личного чтения</p>'
                 % (time.strftime("%Y-%m-%d %H:%M"), VERSION))
    parts.append("</body>\n</html>")
    del src
    return "\n".join(parts)


EPUB_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def build_epub(path: str, thread: Thread, posts: List[Post], images: Dict[str, str],
               image_files: Dict[str, bytes]) -> None:
    """Минимальный валидный EPUB 3 из уже собранных данных."""
    title = thread.title or thread.out_name
    author = thread.starter or "Unknown"
    uid = "urn:aar:%s:%s" % (thread.thread_id or "thread",
                             hashlib.sha1(thread.url.encode()).hexdigest()[:8])
    n_sections = max(1, (len(posts) + 24) // 25)
    sections: List[List[Post]] = [[] for _ in range(n_sections)]
    for i, p in enumerate(posts):
        sections[min(n_sections - 1, i * n_sections // max(1, len(posts)))].append(p)

    manifest: List[str] = []
    spine: List[str] = []
    nav_items: List[str] = []
    used_images: Dict[str, str] = {}

    for si, sec in enumerate(sections, start=1):
        sid = "sec%03d" % si
        first = sec[0]
        label = text_of(build_tree(first.body_html))[:60].strip().replace("\n", " ") \
            or ("Часть %d" % si)
        nav_items.append('<li><a href="%s.xhtml">%s</a></li>' % (sid, esc(label)))
        manifest.append('<item id="%s" href="%s.xhtml" media-type="application/xhtml+xml"/>'
                        % (sid, sid))
        spine.append('<itemref idref="%s"/>' % sid)

    for url, local in images.items():
        name = os.path.basename(local)
        if name in used_images:
            continue
        blob = image_files.get(name)
        if blob is None:
            continue
        ext = os.path.splitext(name)[1].lower()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                ".bmp": "image/bmp"}.get(ext, "image/jpeg")
        used_images[name] = mime
        manifest.append('<item id="img-%s" href="images/%s" media-type="%s"/>'
                        % (os.path.splitext(name)[0], esc(name), mime))

    manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                    'properties="nav"/>')
    manifest.append('<item id="css" href="style.css" media-type="text/css"/>')

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">%s</dc:identifier>
    <dc:title>%s</dc:title>
    <dc:creator>%s</dc:creator>
    <dc:language>en</dc:language>
    <dc:source>%s</dc:source>
    <meta property="dcterms:modified">%s</meta>
  </metadata>
  <manifest>%s</manifest>
  <spine>%s</spine>
</package>
""" % (esc(uid), esc(title), esc(author), esc(thread.url),
       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "\n    ".join(manifest), "\n    ".join(spine))

    nav = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Содержание</title><meta charset="utf-8"/></head>
<body><nav epub:type="toc" id="toc"><h1>Содержание</h1><ol>
%s
</ol></nav></body></html>
""" % "\n".join(nav_items)

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", EPUB_CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/style.css", CSS.replace("@media (prefers-color-scheme: dark)",
                                                   "@media screen and (min-width: 0px)"))
        for si, sec in enumerate(sections, start=1):
            sid = "sec%03d" % si
            body_parts = []
            for p in sec:
                xhtml = to_xhtml(build_tree(p.body_html))
                xhtml = re.sub(r'src="images/', 'src="images/', xhtml)
                body_parts.append(
                    '<section id="post-%s"><h2>%s · %s</h2>%s</section>'
                    % (esc(p.post_id), esc(p.author), esc(p.date[:10]), xhtml))
            doc = ('<?xml version="1.0" encoding="utf-8"?>\n'
                   '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                   '<title>%s — часть %d</title>'
                   '<link rel="stylesheet" type="text/css" href="style.css"/>'
                   '</head><body>%s</body></html>'
                   % (esc(title), si, "\n".join(body_parts)))
            zf.writestr("OEBPS/%s.xhtml" % sid, doc)
        for name, blob in image_files.items():
            if name in used_images:
                zf.writestr("OEBPS/images/" + name, blob)


# --------------------------------------------------------------------------- #
#  Основной поток
# --------------------------------------------------------------------------- #

def parse_pages_spec(spec: str, last_page: int) -> List[int]:
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else last_page
            pages.extend(range(min(a, b), max(a, b) + 1))
        else:
            pages.append(int(part))
    return sorted(set(p for p in pages if 1 <= p <= max(last_page, 1)))


def archive(args: argparse.Namespace) -> int:
    thread = split_thread_url(args.thread)
    out_dir = os.path.abspath(args.out)
    raw_dir = os.path.join(out_dir, "raw")
    img_dir = os.path.join(out_dir, "images")
    md_dir = os.path.join(out_dir, "markdown")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    client = HttpClient(ua=args.ua, delay=args.delay, retries=args.retries,
                        cookie=parse_cookie_header(args.cookies, args.cookie_file),
                        timeout=args.timeout, source=args.source,
                        wayback_ts=args.wayback_ts, quiet=args.quiet)
    store = ImageStore(client, img_dir, download=not args.no_images,
                       inline=args.inline_images, quiet=args.quiet)

    # 1. первая страница: заголовок, автор, число страниц
    log("Страница 1/%s …" % "?")
    try:
        first_url, first_html = client.get_page(page_url(thread.url, 1))
    except FetchError as exc:
        warn(str(exc))
        if args.source == "forum":
            log("Пробую через web.archive.org …")
            client.source = "wayback"
            first_url, first_html = client.get_page(page_url(thread.url, 1))
        else:
            return 2
    first_text = first_html.decode("utf-8", "replace")
    with open(os.path.join(raw_dir, "page-001.html"), "w", encoding="utf-8") as fh:
        fh.write(first_text)

    thread.title = detect_title(first_text)
    thread.starter = detect_starter(first_text)
    thread.last_page = detect_last_page(first_text, thread.url)
    if args.max_pages:
        thread.last_page = min(thread.last_page, args.max_pages)

    log("Тред: %s" % (thread.title or "(без названия)"))
    log("Автор: %s · страниц: %d · id %s"
        % (thread.starter or "?", thread.last_page, thread.thread_id or "?"))

    wanted = parse_pages_spec(args.pages, thread.last_page) if args.pages \
        else list(range(1, thread.last_page + 1))
    log("Качаю страницы: %d-%d (всего %d)" % (wanted[0], wanted[-1], len(wanted)))

    all_posts: List[Post] = []
    pages_html: Dict[int, str] = {1: first_text}
    for page in wanted:
        if page != 1:
            if args.resume:
                cached = os.path.join(raw_dir, "page-%03d.html" % page)
                if os.path.exists(cached):
                    with open(cached, "r", encoding="utf-8") as fh:
                        pages_html[page] = fh.read()
                    log("Страница %d/%d … (из кэша)" % (page, thread.last_page))
                    continue
            log("Страница %d/%d …" % (page, thread.last_page))
            try:
                _, body = client.get_page(page_url(thread.url, page),
                                          referer=thread.url)
                text = body.decode("utf-8", "replace")
                pages_html[page] = text
                with open(os.path.join(raw_dir, "page-%03d.html" % page), "w",
                          encoding="utf-8") as fh:
                    fh.write(text)
            except FetchError as exc:
                warn(str(exc))
                if args.source == "forum":
                    warn("пробую wayback для страницы %d" % page)
                    try:
                        client.source = "wayback"
                        _, body = client.get_page(page_url(thread.url, page))
                        text = body.decode("utf-8", "replace")
                        pages_html[page] = text
                        with open(os.path.join(raw_dir, "page-%03d.html" % page), "w",
                                  encoding="utf-8") as fh:
                            fh.write(text)
                    except FetchError as exc2:
                        warn(str(exc2))
                    finally:
                        client.source = "forum"
                else:
                    continue

    # 2. разбор постов
    seen: set = set()
    for page in wanted:
        text = pages_html.get(page)
        if not text:
            continue
        for post in parse_posts(text, page):
            key = post.post_id or (post.author, post.date, hash(post.body_html))
            if key in seen:
                continue
            seen.add(key)
            all_posts.append(post)

    if args.op_only or args.author:
        who = (args.author or thread.starter).strip().lower()
        all_posts = [p for p in all_posts if p.author.strip().lower() == who]
        log("Фильтр по автору «%s»: осталось %d постов" % (who, len(all_posts)))

    all_posts.sort(key=lambda p: (p.page, int(p.post_id) if p.post_id.isdigit() else 0))
    for i, p in enumerate(all_posts, start=1):
        p.index = i
        p.is_op = bool(thread.starter) and p.author == thread.starter

    if not all_posts:
        warn("не нашёл ни одного поста — возможно, разметка форума изменилась "
             "или страница требует вход. Загляните в raw/page-001.html")
        return 3

    # 3. картинки
    log("Обрабатываю %d постов, скачиваю картинки …" % len(all_posts))
    image_files: Dict[str, bytes] = {}
    for p in all_posts:
        root = build_tree(p.body_html)
        strip_noise(root)
        purl = page_url(thread.url, p.page)
        p.images = localize_tree(root, store, purl)
        p.body_html = to_xhtml(root)

    for name in sorted(set(os.path.basename(v) for v in store.map.values())):
        path = os.path.join(img_dir, name)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                image_files[name] = fh.read()

    # 4. форматы вывода
    fmts = {f.strip().lower() for f in args.formats.split(",") if f.strip()}

    if "json" in fmts:
        payload = {
            "thread": {"url": thread.url, "title": thread.title,
                       "thread_id": thread.thread_id, "starter": thread.starter,
                       "last_page": thread.last_page, "archived_at": time.time(),
                       "archiver_version": VERSION},
            "posts": [{"index": p.index, "post_id": p.post_id, "author": p.author,
                       "date": p.date, "page": p.page, "is_op": p.is_op,
                       "images": p.images, "html": p.body_html,
                       "markdown": to_markdown(build_tree(p.body_html))}
                      for p in all_posts],
            "images": store.map,
            "failed_images": store.failed,
        }
        with open(os.path.join(out_dir, "posts.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        log("  posts.json")

    if "html" in fmts:
        notes = ""
        if store.failed:
            notes = "%d картинок не скачались" % len(store.failed)
        doc = render_reader_html(thread, all_posts, out_dir,
                                 inline=args.inline_images, notes=notes)
        html_path = os.path.join(out_dir, thread.out_name + ".html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        log("  %s" % os.path.basename(html_path))

    if "md" in fmts:
        os.makedirs(md_dir, exist_ok=True)
        md = MarkdownRenderer()
        chunks: List[str] = ["# %s\n" % (thread.title or thread.out_name),
                             "> Локальная копия: %s\n" % thread.url,
                             "> Постов: %d · автор треда: %s\n"
                             % (len(all_posts), thread.starter or "?")]
        by_page: Dict[int, List[Post]] = {}
        for p in all_posts:
            by_page.setdefault(p.page, []).append(p)
        for page in sorted(by_page):
            page_md: List[str] = ["# Страница %d\n" % page]
            for p in by_page[page]:
                head = "## #%d · %s · %s\n" % (p.index, p.author, p.date[:10])
                body = md.render(build_tree(p.body_html))
                page_md.append(head + "\n" + body)
                chunks.append(head + "\n" + body)
            name = os.path.join(md_dir, "page-%03d.md" % page)
            with open(name, "w", encoding="utf-8") as fh:
                fh.write("\n".join(page_md))
        with open(os.path.join(md_dir, "all.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(chunks))
        log("  markdown/all.md (+ %d файлов по страницам)" % len(by_page))

    if "epub" in fmts or args.epub:
        epub_path = os.path.join(out_dir, thread.out_name + ".epub")
        try:
            build_epub(epub_path, thread, all_posts, store.map, image_files)
            log("  %s" % os.path.basename(epub_path))
        except Exception as exc:
            warn("EPUB не собрался: %s" % exc)

    # 5. отчёт
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(out_dir) for f in fs)
    log("\nГотово: %d постов, %d картинок, %.1f МБ" %
        (len(all_posts), len(store.map), size / 1048576.0))
    log("Папка: %s" % out_dir)
    if store.failed:
        warn("%d картинок не скачались (список в posts.json -> failed_images)"
             % len(store.failed))
    return 0


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="aar_archiver.py",
        description="Локальная офлайн-копия треда XenForo (AAR с форума Paradox).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Примеры:")[-1].split("Назначение")[0])
    ap.add_argument("thread", help="URL треда")
    ap.add_argument("-o", "--out", default="out", help="папка для копии (default: out)")
    ap.add_argument("--pages", default="", help="например 1-5 или 1,3,7-9")
    ap.add_argument("--max-pages", type=int, default=0, help="не идти дальше этой страницы")
    ap.add_argument("--source", choices=("forum", "wayback"), default="forum",
                    help="forum (default) или wayback — брать снимки web.archive.org")
    ap.add_argument("--wayback-ts", default="2",
                    help="timestamp для wayback, если CDX не ответил (default: 2 = ближайший)")
    ap.add_argument("--formats", default="html,md,json",
                    help="список форматов через запятую: html,md,json (default: все)")
    ap.add_argument("--epub", action="store_true", help="собрать ещё и EPUB")
    ap.add_argument("--op-only", action="store_true",
                    help="только посты автора треда — чистый текст AAR без ответов")
    ap.add_argument("--author", default="", help="фильтр по нику (вместо --op-only)")
    ap.add_argument("--inline-images", action="store_true",
                    help="вшить картинки в HTML как data: URI (один файл)")
    ap.add_argument("--no-images", action="store_true", help="не качать картинки")
    ap.add_argument("--resume", action="store_true",
                    help="не перекачивать страницы, которые уже лежат в raw/")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="пауза между запросами в секундах (default: 3)")
    ap.add_argument("--retries", type=int, default=4, help="сколько раз повторять запрос")
    ap.add_argument("--timeout", type=int, default=60, help="таймаут запроса, сек")
    ap.add_argument("--ua", default=DEFAULT_UA, help="User-Agent")
    ap.add_argument("--cookies", default="", help="строка куки: 'xf_user=...; other=...'")
    ap.add_argument("--cookie-file", default="", help="файл куки в формате Netscape")
    ap.add_argument("--quiet", action="store_true", help="меньше сообщений")
    ap.add_argument("--version", action="version", version="aar_archiver " + VERSION)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        return archive(args)
    except KeyboardInterrupt:
        log("\nПрервано пользователем")
        return 130


if __name__ == "__main__":
    sys.exit(main())
