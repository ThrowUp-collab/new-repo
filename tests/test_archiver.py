#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты aar_archiver без сети: HTTP подменяется локальными фикстурами XenForo.

    python3 tests/test_archiver.py            # прогнать тесты
    python3 tests/test_archiver.py --demo     # собрать демо-копию в demo_out/
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import sys
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import aar_archiver as A  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
THREAD_URL = "https://forum.example.test/forum/threads/sample-realm-diary.900101/"

# минимальный валидный PNG 1x1 (красный пиксель)
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/"
    "AJ8j2T9AAAAAElFTkSuQmCC")


def fake_png(w=4, h=4):
    """Небольшой настоящий PNG, чтобы проверка сигнатур была честной."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib_crc(tag + data)))
    import zlib

    def zlib_crc(b):
        return zlib.crc32(b) & 0xFFFFFFFF

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xc0\x39\x39" * w for _ in range(h))
    idat = zlib.compress(raw)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class FakeNetwork:
    """Подмена HttpClient._raw_get: страницы из фикстур, картинки из памяти."""

    def __init__(self):
        self.requested = []
        self.pages = {}
        for name in os.listdir(FIXTURES):
            m = name.startswith("page-") and name.endswith(".html")
            if m:
                n = int(name[5:-5])
                with open(os.path.join(FIXTURES, name), "rb") as fh:
                    self.pages[n] = fh.read()
        self.images = {
            "https://i.example.org/shots/county_view.png": fake_png(8, 8),
            "https://forum.example.test/forum/attachments/shot2.12345/": PNG_1PX,
        }
        self.missing = {"https://i.example.org/gone.jpg"}

    def page_bytes(self, url):
        if url.rstrip("/").endswith("900101"):
            return self.pages[1]
        m = __import__("re").search(r"page-(\d+)", url)
        if m and int(m.group(1)) in self.pages:
            return self.pages[int(m.group(1))]
        raise A.FetchError(url, RuntimeError("404 in fixture"))

    def get(self, url, referer=None):
        self.requested.append(url)
        if "/cdx/search" in url:
            return b'[["timestamp","statuscode"],["20240501000000","200"]]'
        if url in self.images:
            return self.images[url]
        if url in self.missing:
            raise A.FetchError(url, RuntimeError("404"))
        return self.page_bytes(url)

    def patch(self, client):
        client._raw_get = lambda url, referer=None: self.get(url, referer)
        return client

    def reset(self):
        self.requested.clear()


NET = FakeNetwork()


def setUpModule():
    """Глобально отключаем реальную сеть: ни один тест не должен ходить в DNS."""
    A.HttpClient._raw_get = lambda self, url, referer=None: NET.get(url, referer)


def tearDownModule():
    import unittest as _u
    del _u


def make_client(source="forum", **kw):
    c = A.HttpClient(delay=0, retries=1, quiet=True, source=source, **kw)
    return c, NET


class TestXenForoParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIXTURES, "page-1.html"), encoding="utf-8") as fh:
            cls.html = fh.read()

    def test_last_page_ignores_sidebar(self):
        self.assertEqual(A.detect_last_page(self.html, THREAD_URL), 3)

    def test_last_page_without_hint(self):
        # без подсказки сайдбар (page-87) тоже не должен попасть: есть nav-блок
        self.assertEqual(A.detect_last_page(self.html), 3)

    def test_title_and_starter(self):
        self.assertEqual(A.detect_title(self.html),
                         "Sample Realm Diary - an Economic Test AAR")
        self.assertEqual(A.detect_starter(self.html), "SampleAuthor")

    def test_posts_found(self):
        posts = A.parse_posts(self.html, 1)
        self.assertEqual(len(posts), 2)
        self.assertEqual([p.post_id for p in posts], ["900101", "900102"])
        self.assertEqual(posts[0].author, "SampleAuthor")
        self.assertEqual(posts[0].date, "2024-05-01T10:00:00+0000")

    def test_signature_excluded(self):
        body = A.parse_posts(self.html, 1)[0].body_html
        self.assertNotIn("Signature text", body)
        self.assertNotIn("message-signature", body)
        self.assertIn("synthetic", body)

    def test_lazy_image_and_lists(self):
        body = A.parse_posts(self.html, 1)[0].body_html
        self.assertIn("data-src", body)          # ещё не локализовано
        self.assertIn("bbCodeList", body)
        self.assertIn("Spoiler", body)


class TestTree(unittest.TestCase):
    def test_unclosed_p_and_li(self):
        root = A.build_tree("<div><p>one<p>two<ul><li>a<li>b</ul></div>")
        self.assertEqual(A.to_xhtml(root).count("<p>"), 2)
        self.assertEqual(A.to_xhtml(root).count("<li>"), 2)
        self.assertTrue(A.to_xhtml(root).startswith("<div>"))
        self.assertTrue(A.to_xhtml(root).endswith("</div>"))

    def test_void_tags_selfclose(self):
        out = A.to_xhtml(A.build_tree('a<br>b<img src="x.png">c<hr>'))
        self.assertIn("<br />", out)
        self.assertIn('<img src="x.png" />', out)
        self.assertIn("<hr />", out)

    def test_script_content_skipped(self):
        out = A.to_xhtml(A.build_tree("<div>ok<script>var a='<b>';</script></div>"))
        self.assertEqual(out, "<div>ok</div>")

    def test_escaping(self):
        out = A.to_xhtml(A.build_tree('<div>5 < 6 & 7 > 3</div>'))
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)


class TestMarkdown(unittest.TestCase):
    def test_bold_italic_link(self):
        md = A.to_markdown(A.build_tree('<div><b>B</b> <i>i</i> '
                                        '<a href="http://x">link</a></div>'))
        self.assertIn("**B**", md)
        self.assertIn("*i*", md)
        self.assertIn("[link](http://x)", md)

    def test_fontsize_heading(self):
        md = A.to_markdown(A.build_tree('<div><span style="font-size: 22px">'
                                        '<b>Year 769</b></span></div>'))
        self.assertTrue(any(l.startswith("### Year 769") for l in md.splitlines()))

    def test_lists(self):
        md = A.to_markdown(A.build_tree("<ul><li>alpha</li><li>beta</li></ul>"))
        self.assertIn("- alpha", md)
        self.assertIn("- beta", md)
        md2 = A.to_markdown(A.build_tree("<ol><li>one</li><li>two</li></ol>"))
        self.assertIn("1. one", md2)
        self.assertIn("2. two", md2)

    def test_table(self):
        md = A.to_markdown(A.build_tree(
            "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"))
        self.assertIn("| a | b |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| 1 | 2 |", md)

    def test_image(self):
        md = A.to_markdown(A.build_tree('<img src="images/x.png" alt="x">'))
        self.assertIn("![x](images/x.png)", md)


class TestImageStore(unittest.TestCase):
    def test_localize_prefers_data_src(self):
        client, net = make_client()
        net.reset()
        out = os.path.join(ROOT, "tests", "_tmp_img")
        store = A.ImageStore(client, out, quiet=True)
        root = A.build_tree('<img src="/forum/styles/paradox/xenforo/loading-32x32.gif" '
                            'data-src="https://i.example.org/shots/county_view.png" '
                            'class="bbImage lazyload">')
        found = A.localize_tree(root, store, THREAD_URL)
        self.assertEqual(len(found), 1)
        local = found[0]
        self.assertTrue(local.startswith("images/"), local)
        self.assertTrue(os.path.exists(os.path.join(out, os.path.basename(local))))
        self.assertEqual(A.to_xhtml(root).count("loading-32x32"), 0)
        with open(os.path.join(out, os.path.basename(local)), "rb") as fh:
            self.assertEqual(fh.read()[:8], b"\x89PNG\r\n\x1a\n")
        import shutil
        shutil.rmtree(out, ignore_errors=True)

    def test_failed_image_keeps_absolute(self):
        client, net = make_client()
        out = os.path.join(ROOT, "tests", "_tmp_img2")
        store = A.ImageStore(client, out, quiet=True)
        root = A.build_tree('<img src="https://i.example.org/gone.jpg">')
        A.localize_tree(root, store, THREAD_URL)
        self.assertEqual(store.failed, ["https://i.example.org/gone.jpg"])
        import shutil
        shutil.rmtree(out, ignore_errors=True)


class TestEndToEnd(unittest.TestCase):
    out = os.path.join(ROOT, "tests", "_tmp_out")

    def run_tool(self, extra=(), clean=True):
        import shutil
        if clean:
            shutil.rmtree(self.out, ignore_errors=True)
        argv = [THREAD_URL, "--out", self.out, "--delay", "0", "--quiet"] + list(extra)
        rc = A.main(argv)
        self.assertEqual(rc, 0, "архиватор вернул %s" % rc)

    def test_full_run(self):
        self.run_tool(["--epub"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "raw", "page-001.html")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "raw", "page-003.html")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "markdown", "all.md")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "posts.json")))
        html_path = os.path.join(self.out, "sample-realm-diary.html")
        self.assertTrue(os.path.exists(html_path))

        with open(os.path.join(self.out, "posts.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["thread"]["title"],
                         "Sample Realm Diary - an Economic Test AAR")
        self.assertEqual(data["thread"]["starter"], "SampleAuthor")
        self.assertEqual(len(data["posts"]), 5)      # 2 + 2 + 1
        self.assertEqual([p["page"] for p in data["posts"]], [1, 1, 2, 2, 3])
        self.assertFalse(any("Signature text" in p["html"] or "SIG-" in p["html"]
                             for p in data["posts"]))
        self.assertTrue(any(p["images"] for p in data["posts"]))

        with open(html_path, encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("<!DOCTYPE html>", doc)
        self.assertIn('id="post-900101"', doc)
        self.assertIn("images/", doc)
        self.assertNotIn("Signature text", doc)

        # картинки реально лежат на диске и это PNG
        img_dir = os.path.join(self.out, "images")
        files = os.listdir(img_dir)
        self.assertGreaterEqual(len(files), 2)
        for f in files:
            with open(os.path.join(img_dir, f), "rb") as fh:
                self.assertEqual(fh.read(4), b"\x89PNG", f)

        # EPUB: структура и mimetype без сжатия
        epub = os.path.join(self.out, "sample-realm-diary.epub")
        self.assertTrue(os.path.exists(epub))
        with zipfile.ZipFile(epub) as zf:
            names = zf.namelist()
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(zf.read("mimetype"), b"application/epub+zip")
            info = zf.getinfo("mimetype")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertIn("META-INF/container.xml", names)
            self.assertIn("OEBPS/content.opf", names)
            self.assertIn("OEBPS/nav.xhtml", names)
            opf = zf.read("OEBPS/content.opf").decode()
            self.assertIn('version="3.0"', opf)
            for item in zf.namelist():
                if item.endswith(".xhtml") and item.startswith("OEBPS/sec"):
                    body = zf.read(item).decode()
                    self.assertIn("<html", body)
                    self.assertNotIn("SIG-", body)
            imgs = [n for n in names if n.startswith("OEBPS/images/")]
            self.assertGreaterEqual(len(imgs), 1)
            for n in imgs:
                self.assertIn(os.path.basename(n), opf)

    def test_op_only_filter(self):
        self.run_tool(["--op-only"])
        with open(os.path.join(self.out, "posts.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual([p["author"] for p in data["posts"]], ["SampleAuthor"] * 3)
        self.assertEqual([p["page"] for p in data["posts"]], [1, 2, 3])

    def test_pages_spec(self):
        self.run_tool(["--pages", "1-2"])
        with open(os.path.join(self.out, "posts.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(sorted({p["page"] for p in data["posts"]}), [1, 2])
        self.assertFalse(os.path.exists(os.path.join(self.out, "raw", "page-003.html")))

    def test_formats_subset_and_no_images(self):
        self.run_tool(["--formats", "md", "--no-images"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "posts.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "markdown", "all.md")))
        self.assertEqual(os.listdir(os.path.join(self.out, "images")), [])

    def test_inline_images(self):
        self.run_tool(["--inline-images", "--formats", "html"])
        with open(os.path.join(self.out,
                  "sample-realm-diary.html"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("data:image/png;base64,", doc)

    def test_resume(self):
        self.run_tool(["--pages", "1-2"])
        with open(os.path.join(self.out, "raw", "page-002.html"), encoding="utf-8") as fh:
            before = fh.read()
        n_before = len(NET.requested)
        self.run_tool(["--pages", "1-3", "--resume"], clean=False)
        with open(os.path.join(self.out, "raw", "page-002.html"), encoding="utf-8") as fh:
            after = fh.read()
        self.assertEqual(before, after)
        self.assertTrue(os.path.exists(os.path.join(self.out, "raw", "page-003.html")))
        # страница 2 не перекачивалась
        self.assertFalse(any(u.endswith("page-2") for u in NET.requested[n_before:]))


class TestCliRobustness(unittest.TestCase):
    """Окно консоли не должно закрываться молча и без внятного сообщения."""

    out = os.path.join(ROOT, "tests", "_tmp_cli")

    def test_no_args_prints_help_not_argparse_error(self):
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = A.main([])
        text = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("usage:", text)
        self.assertIn("--op-only", text)
        self.assertNotIn("the following arguments are required", text)

    def test_missing_url_in_argv_still_reports_cleanly(self):
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = A.main(["--epub"])
        self.assertEqual(rc, 1)
        self.assertIn("usage:", buf.getvalue())

    def test_interactive_args_reads_url_and_flags(self):
        import builtins, io as _io, contextlib
        answers = iter([THREAD_URL, "--op-only --epub"])
        orig = builtins.input
        builtins.input = lambda prompt="": next(answers)
        try:
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                argv = A.interactive_args()
        finally:
            builtins.input = orig
        self.assertEqual(argv[0], THREAD_URL)
        self.assertIn("--op-only", argv)
        self.assertIn("--epub", argv)
        self.assertIn("aar_archiver", buf.getvalue())

    def test_interactive_args_empty_input(self):
        import builtins, io as _io, contextlib
        orig = builtins.input
        builtins.input = lambda prompt="": "   "
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                self.assertEqual(A.interactive_args(), [])
        finally:
            builtins.input = orig

    def test_log_file_written(self):
        import shutil, io as _io, contextlib
        shutil.rmtree(self.out, ignore_errors=True)
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            rc = A.main([THREAD_URL, "--out", self.out, "--delay", "0",
                         "--quiet", "--formats", "json"])
        self.assertEqual(rc, 0)
        path = os.path.join(self.out, "archiver.log")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("=== запуск", body)
        self.assertIn("Страница", body)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_no_log_flag(self):
        import shutil, io as _io, contextlib
        shutil.rmtree(self.out, ignore_errors=True)
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            A.main([THREAD_URL, "--out", self.out, "--delay", "0",
                    "--quiet", "--no-log", "--formats", "json"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "archiver.log")))
        shutil.rmtree(self.out, ignore_errors=True)

    def test_pause_skipped_when_not_tty(self):
        self.assertEqual(A.pause_on_exit(0), 0)
        self.assertEqual(A.pause_on_exit(3), 3)


class TestBlockedPageDetection(unittest.TestCase):
    def test_cloudflare_stub_detected(self):
        stub = ("<html><head><title>Attention Required! | Cloudflare</title></head>"
                "<body>Enable JavaScript and cookies to continue</body></html>")
        self.assertTrue(A.looks_blocked(stub))

    def test_real_thread_not_flagged(self):
        with open(os.path.join(FIXTURES, "page-1.html"), encoding="utf-8") as fh:
            self.assertFalse(A.looks_blocked(fh.read()))


class TestWayback(unittest.TestCase):
    def test_wayback_url_uses_cdx(self):
        client, net = make_client(source="wayback")
        url = client.wayback_url(THREAD_URL + "page-5")
        self.assertTrue(url.startswith("https://web.archive.org/web/20240501000000id_/"), url)
        self.assertTrue(url.endswith(THREAD_URL + "page-5"))

    def test_wayback_fetch_parses(self):
        client, net = make_client(source="wayback")
        url, body = client.get_page(A.page_url(THREAD_URL, 1))
        self.assertEqual(url, A.page_url(THREAD_URL, 1))   # original url для резолва
        self.assertEqual(A.detect_title(body.decode()),
                         "Sample Realm Diary - an Economic Test AAR")


def demo(out_dir=None):
    """Собирает демо-копию из фикстур, чтобы посмотреть на результат."""
    out_dir = out_dir or os.path.join(ROOT, "demo_out")
    setUpModule()
    argv = [THREAD_URL, "--out", out_dir, "--delay", "0", "--epub"]
    rc = A.main(argv)
    # index.html — чтобы папку можно было просто открыть/показать как сайт
    reader = os.path.basename(out_dir) and [f for f in os.listdir(out_dir)
                                            if f.endswith(".html")]
    if reader:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write('<!DOCTYPE html><meta charset="utf-8">'
                     '<meta http-equiv="refresh" content="0; url=%s">'
                     '<a href="%s">открыть копию</a>' % (reader[0], reader[0]))
    print("demo rc=%s -> %s" % (rc, out_dir))
    return rc


if __name__ == "__main__":
    if "--demo" in sys.argv:
        sys.exit(demo())
    unittest.main(verbosity=2)
