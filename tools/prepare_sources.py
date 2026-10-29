#!/usr/bin/env python3
"""隔离抓取 Happy 官方手册，提取完整正文及翻译任务清单。"""
import argparse
import concurrent.futures
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

BASE = "https://happy.engineering/"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def classes(self):
        return self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return html.unescape("".join(c.text() if isinstance(c, Node) else c for c in self.children)).strip()

    def render(self, base=None):
        if self.tag in {"script", "style"}:
            return ""
        if not self.tag:
            return "".join(c.render(base) if isinstance(c, Node) else c for c in self.children)
        attrs = []
        for key, value in self.attrs.items():
            if key.startswith("on"):
                continue
            if value is not None and base and key in {"href", "src", "poster"} and not value.startswith("#"):
                value = urllib.parse.urljoin(base, value)
            if value is not None and value.strip().lower().startswith("javascript:"):
                continue
            attrs.append(" " + key if value is None else f' {key}="{html.escape(value, quote=True)}"')
        start = "<" + self.tag + "".join(attrs) + ">"
        if self.tag in VOID:
            return start
        return start + "".join(c.render(base) if isinstance(c, Node) else c for c in self.children) + "</" + self.tag + ">"


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.root = Node()
        self.current = self.root
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)

    def handle_entityref(self, name):
        self.current.children.append("&" + name + ";")

    def handle_charref(self, name):
        self.current.children.append("&#" + name + ";")


def canonical(url):
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, url))
    if parts.netloc != "happy.engineering":
        return None
    path = parts.path
    if not path or path == "/":
        return None
    if "." in path.rsplit("/", 1)[-1]:
        return None
    return BASE.rstrip("/") + path.rstrip("/") + "/"


def content_node(doc):
    for node in doc.root.walk():
        if "document-content" in node.classes():
            return node
    return None


def extract_navigation(doc):
    seen = set()
    result = []
    for nav in doc.root.walk():
        if "docs-navigation" not in nav.classes():
            continue
        for section in nav.children:
            if not isinstance(section, Node) or section.tag != "section":
                continue
            group = next((n.text() for n in section.walk() if n.tag in {"h2", "summary"}), "其他")
            for anchor in section.walk():
                if anchor.tag != "a":
                    continue
                href = anchor.attrs.get("href", "")
                url = BASE + "welcome/" if href == "#welcome" else canonical(href)
                if url and url not in seen:
                    result.append({"url": url, "group": group, "title_zh": anchor.text()})
                    seen.add(url)
    return result


def fetch(url, download_root):
    # 每个 URL 独占新建空目录；只把字节写入隔离目录，不加载或运行网页脚本。
    directory = Path(tempfile.mkdtemp(prefix="page-", dir=download_root))
    request = urllib.request.Request(url, headers={"User-Agent": "HappyDocsChineseTranslation/1.0"})
    with urllib.request.urlopen(request, timeout=75) as response:
        data = response.read()
        final_url = response.url
    (directory / "response.html").write_bytes(data)
    (directory / "url.txt").write_text(url + "\n" + final_url + "\n", encoding="utf-8")
    return data.decode("utf-8"), str(directory), final_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    download_root = tempfile.mkdtemp(prefix="happy-docs-manual-20261029-", dir="/private/tmp")
    local_doc = Document((workspace / "index.html").read_text(encoding="utf-8"))
    pages = extract_navigation(local_doc)
    navigation_count = len(pages)
    known = {p["url"] for p in pages}
    for node in local_doc.root.walk():
        if node.tag == "a" and "footer-links" in (node.parent.classes() if node.parent else []):
            url = canonical(node.attrs.get("href", ""))
            if url and url not in known and "/docs/" not in url:
                pages.append({"url": url, "group": "页脚", "title_zh": node.text()})
                known.add(url)
    legacy = []
    failures = []
    fetched = {}
    pending = list(pages)
    while pending:
        next_round = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fetch, page["url"], download_root): page for page in pending}
            for future in concurrent.futures.as_completed(futures):
                page = futures[future]
                try:
                    text, directory, final_url = future.result()
                    doc = Document(text)
                    body = content_node(doc)
                    if body is None:
                        raise ValueError("页面缺少 document-content 容器，不用摘要替代正文")
                    slug = urllib.parse.urlsplit(page["url"]).path.strip("/")
                    title = next((n.text() for n in body.walk() if n.tag == "h1"), slug)
                    article = body.render(page["url"])
                    source_dir = workspace / "source" / slug
                    source_dir.mkdir(parents=True, exist_ok=True)
                    (source_dir / "article.html").write_text(article + "\n", encoding="utf-8")
                    (source_dir / "translate.html").write_text(article + "\n", encoding="utf-8")
                    links = []
                    for node in body.walk():
                        if node.tag != "a":
                            continue
                        href = node.attrs.get("href", "")
                        url = canonical(urllib.parse.urljoin(page["url"], href))
                        if url and url not in links:
                            links.append(url)
                        if not url or url in known:
                            continue
                        if urllib.parse.urlsplit(url).path.startswith("/docs/"):
                            if url not in legacy:
                                legacy.append(url)
                            continue
                        entry = {"url": url, "group": "补充文档", "title_zh": ""}
                        known.add(url)
                        pages.append(entry)
                        next_round.append(entry)
                    page.update(slug=slug, title=title, output="index.html" if slug == "welcome" else slug + "/index.html", source="source/" + slug + "/article.html", translation_source="source/" + slug + "/translate.html", status="ok", bytes=len(article.encode("utf-8")), text_chars=len(body.text()), links=links, download_directory=directory, final_url=final_url, sha256=hashlib.sha256(article.encode("utf-8")).hexdigest())
                    fetched[page["url"]] = page
                    print(f"已提取 {slug}: {page['bytes']} 字节", flush=True)
                except Exception as error:
                    page.update(status="failed", error=str(error))
                    failures.append({"url": page["url"], "error": str(error)})
                    print(f"抓取失败 {page['url']}: {error}", flush=True)
        pending = next_round
    welcome = workspace / "source" / "welcome"
    welcome.mkdir(parents=True, exist_ok=True)
    translated = content_node(local_doc)
    if translated:
        (welcome / "existing-zh.html").write_text(translated.render() + "\n", encoding="utf-8")
    # 旧版 CLI 手册仅识别其独立站点结构，保持原链接，不将整套旧文档混入新版导航。
    legacy_url = BASE + "docs/"
    try:
        text, directory, final_url = fetch(legacy_url, download_root)
        legacy_doc = Document(text)
        legacy_info = {"url": legacy_url, "final_url": final_url, "download_directory": directory, "title": next((n.text() for n in legacy_doc.root.walk() if n.tag == "title"), ""), "has_current_document_content": content_node(legacy_doc) is not None, "policy": "独立旧版 Happy CLI 手册，保留官方链接，不纳入新版手册翻译范围", "linked_pages": legacy}
    except Exception as error:
        legacy_info = {"url": legacy_url, "error": str(error), "policy": "旧手册识别失败，暂保持官方链接", "linked_pages": legacy}
    manifest = {"base_url": BASE, "published_base_url": "https://yisen321.github.io/happy-docs-zh/", "navigation_count": navigation_count, "download_root": download_root, "pages": pages, "legacy_links": legacy_info, "failures": failures}
    (workspace / "content").mkdir(exist_ok=True)
    (workspace / "content" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pages": len(pages), "ok": len(fetched), "failures": failures, "navigation_count": navigation_count, "download_root": download_root}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
