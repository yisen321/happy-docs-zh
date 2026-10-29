#!/usr/bin/env python3
"""构建完整中文手册；只改 HTML 属性，不改正文、命令或代码字面量。"""

import argparse
from collections import Counter
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
OFFICIAL = "https://happy.engineering/"
ATTR = re.compile(r'''(?P<name>[^\s=<>/]+)(?P<space>\s*=\s*)(?P<value>"[^"]*"|'[^']*'|[^\s>]+)''')


def inside(root, name):
    """输入、输出均不得越过仓库边界。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"路径越界：{name}")
    return path


def normalized_path(url):
    return unquote(urlsplit(url).path).rstrip("/") or "/"


def relative_url(current, target, query="", fragment=""):
    path = os.path.relpath(target, Path(current).parent).replace(os.sep, "/")
    if current == target and fragment and not query:
        path = ""
    return urlunsplit(("", "", path, query, fragment))


class Document(HTMLParser):
    """只读 HTML 索引，同时记录精确的起始标签位置供属性替换。"""

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0]
        for match in re.finditer("\n", text):
            self.lines.append(match.end())
        self.tags = []
        self.ids = set()
        self.codes = []
        self.pre_codes = []
        self.inline_codes = []
        self.code_depth = 0
        self.code_start = None
        self.code_tag = None
        self.plain = []
        self.feed(text)
        self.close()

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self.position()
        raw = self.get_starttag_text()
        values = dict(attrs)
        self.tags.append((tag, values, start, raw, self.code_depth > 0))
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("name"):
            self.ids.add(values["name"])
        if tag in {"pre", "code"}:
            if self.code_depth == 0:
                self.code_start = start
                self.code_tag = tag
            self.code_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in {"pre", "code"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"pre", "code"} and self.code_depth:
            self.code_depth -= 1
            if not self.code_depth:
                end = self.text.find(">", self.position()) + 1
                code = self.text[self.code_start:end]
                self.codes.append(code)
                if self.code_tag == "pre":
                    self.pre_codes.append(code)
                else:
                    self.inline_codes.append(code)

    def handle_data(self, data):
        if not self.code_depth:
            self.plain.append(data)


def rewrite(text, page, mapping):
    """保留原始标签及文本，仅替换链接和资源的属性值。"""
    replacements = []
    for tag, attrs, start, raw, in_code in Document(text).tags:
        if in_code or tag in {"code", "pre"}:
            continue
        for match in ATTR.finditer(raw):
            name = match.group("name").lower()
            if name not in {"href", "src", "poster", "srcset"}:
                continue
            value = html.unescape(match.group("value").strip("\"'"))
            if tag == "a" and attrs.get("data-original") == "true":
                continue

            def convert(url):
                if not url or url.startswith("#"):
                    return url
                resolved = urlsplit(urljoin(page["url"], url))
                if resolved.scheme not in {"https", "http"}:
                    return url
                target = mapping.get(normalized_path(resolved.geturl()))
                if tag == "a" and name == "href" and resolved.hostname == "happy.engineering" and target:
                    return relative_url(page["output"], target["output"], resolved.query, resolved.fragment)
                return resolved.geturl()

            if name == "srcset":
                # data URL 内含逗号，直接保留，避免破坏内联资源。
                if "data:" in value:
                    continue
                candidates = []
                for candidate in value.split(","):
                    parts = candidate.strip().split(maxsplit=1)
                    if parts:
                        candidates.append(convert(parts[0]) + (" " + parts[1] if len(parts) > 1 else ""))
                new_value = ", ".join(candidates)
            else:
                new_value = convert(value)
            if value != new_value:
                begin, end = match.span("value")
                replacements.append((start + begin, start + end, '"' + html.escape(new_value, quote=True) + '"'))
    for begin, end, value in reversed(replacements):
        text = text[:begin] + value + text[end:]
    return text


def load_inputs(root):
    manifest = json.loads((root / "content/manifest.json").read_text(encoding="utf-8"))
    if manifest.get("failures"):
        raise ValueError("原文采集尚有失败项，禁止生成不完整手册")
    pages = manifest.get("pages", [])
    groups = json.loads((root / "templates/navigation.json").read_text(encoding="utf-8"))
    expected = [slug for group in groups for slug, _ in group["items"]]
    if len(groups) != 7 or len(expected) != 45:
        raise ValueError("导航必须保持 7 组、45 项")
    slugs = [page["slug"] for page in pages]
    if len(slugs) != 51 or not (set(expected) | {"privacy", "terms"}).issubset(slugs) or len(slugs) != len(set(slugs)):
        raise ValueError("清单必须完整覆盖 45 个导航页面、4 篇更新详情及 2 篇法律页面，共 51 页，且不可重复")
    for page in pages:
        page["title"] = page.get("title_zh") or page["title"]
    outputs = set()
    translations = {}
    for page in pages:
        slug = page["slug"]
        output = inside(root, page["output"])
        if output.suffix != ".html" or page["output"].startswith("/") or output in outputs:
            raise ValueError(f"输出必须是唯一的站内 HTML 文件：{slug}")
        outputs.add(output)
        if slug == "welcome" and page["output"] != "index.html":
            raise ValueError("欢迎页必须映射到根 index.html")
        if urlsplit(page["url"]).hostname != "happy.engineering":
            raise ValueError(f"原文不是官方站点：{slug}")
        source = inside(root, f"content/zh/{slug}.html")
        if slug == "welcome" and not source.is_file():
            source = root / "templates/welcome.zh.html"
        if not source.is_file():
            raise ValueError(f"缺少完整中文译文：{source}")
        text = source.read_text(encoding="utf-8")
        document = Document(text)
        plain = " ".join(document.plain)
        if not re.search(r"[㐀-鿿]", plain) or not any(tag == "h1" for tag, *_ in document.tags):
            raise ValueError(f"译文缺少中文正文或一级标题：{source}")
        if any(tag in {"script", "base", "link"} for tag, *_ in document.tags):
            raise ValueError(f"译文不得引入脚本、基础路径或外部样式：{source}")
        original = inside(root, page["source"])
        if not original.is_file():
            raise ValueError(f"缺少原文快照：{original}")
        source_document = Document(original.read_text(encoding="utf-8"))
        # 块级命令必须原序逐字保真；行内标识符允许随中文语序重排，但字面量及重复次数不能改变。
        if source_document.pre_codes != document.pre_codes:
            raise ValueError(f"中文译文修改、遗漏或重排了原文 pre 代码块：{source}")
        if Counter(source_document.inline_codes) != Counter(document.inline_codes):
            raise ValueError(f"中文译文修改或遗漏了原文行内代码，或改变了重复次数：{source}")
        if not source_document.ids.issubset(document.ids):
            raise ValueError(f"中文译文遗漏了原文标题或锚点：{source}")
        structure_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "blockquote", "pre", "img", "video", "iframe"}
        source_structure = [tag for tag, *_ in source_document.tags if tag in structure_tags]
        translated_structure = [tag for tag, *_ in document.tags if tag in structure_tags]
        if source_structure != translated_structure:
            raise ValueError(f"中文译文的段落、列表、标题或媒体结构与原文不一致：{source}")
        translations[slug] = text
    indexed = {page["slug"]: page for page in pages}
    # 上下页顺序严格遵循原有导航，而不是翻译任务完成顺序。
    pages = [indexed[slug] for slug in expected] + [page for page in pages if page["slug"] not in expected]
    mapping = {normalized_path(page["url"]): page for page in pages}
    mapping["/welcome"] = indexed["welcome"]
    return pages, groups, translations, mapping


def navigation(page, groups, by_slug):
    sections = []
    for group in groups:
        links = []
        active = False
        for slug, title in group["items"]:
            current = slug == page["slug"]
            parent = page["slug"].startswith(slug + "/") and page["slug"] not in {item for section in groups for item, _ in section["items"]}
            active = active or current or parent
            href = relative_url(page["output"], by_slug[slug]["output"])
            status = ' aria-current="page"' if current else (' aria-current="location"' if parent else '')
            links.append(f'<li><a href="{html.escape(href, quote=True)}"{status}>{html.escape(title)}</a></li>')
        heading = html.escape(group["title"])
        body = "<ul>" + "".join(links) + "</ul>"
        if group.get("fold"):
            body = '<details class="docs-nav-fold"' + (' open' if active else '') + f'><summary>{heading}</summary>{body}</details>'
        else:
            body = f"<h2>{heading}</h2>" + body
        sections.append('<section class="docs-nav-group">' + body + "</section>")
    return "\n".join(sections)


def render(root, pages, groups, translations, mapping):
    template = (root / "templates/page.html").read_text(encoding="utf-8")
    styles = (root / "templates/styles.css").read_text(encoding="utf-8")
    by_slug = {page["slug"]: page for page in pages}
    rendered = {}
    for index, page in enumerate(pages):
        content = rewrite(translations[page["slug"]], page, mapping)
        if Document(content).codes != Document(translations[page["slug"]]).codes:
            raise ValueError(f"属性改写改变了代码块：{page['slug']}")
        pagination = []
        for offset, label, css in [(-1, "上一页", "document-pagination-prev"), (1, "下一页", "document-pagination-next")]:
            adjacent = index + offset
            if adjacent < 0 or adjacent >= len(pages):
                pagination.append("<span></span>")
                continue
            target = pages[adjacent]
            href = relative_url(page["output"], target["output"])
            pagination.append(f'<a class="{css}" href="{html.escape(href, quote=True)}"><span>{label}</span>{html.escape(target["title"])}</a>')
        group = next((group["title"] for group in groups if any(slug == page["slug"] or page["slug"].startswith(slug + "/") for slug, _ in group["items"])), page.get("group", "文档"))
        plain = html.unescape(" ".join(" ".join(Document(content).plain).split()))
        description = page.get("description") or f"Happy 非官方中文手册：{page['title']}。{plain[:140]}"
        values = {
            "TITLE": html.escape(page["title"]), "DESCRIPTION": html.escape(description, quote=True),
            "STYLES": styles, "GROUP": html.escape(group), "SLUG": html.escape(page["slug"], quote=True),
            "HOME": html.escape(relative_url(page["output"], by_slug["welcome"]["output"]), quote=True),
            "NAVIGATION": navigation(page, groups, by_slug), "CONTENT": content,
            "PAGINATION": "".join(pagination), "ORIGINAL": html.escape(page["url"], quote=True),
            "DESKTOP": html.escape(relative_url(page["output"], by_slug["desktop-app"]["output"]), quote=True),
            "MOBILE": html.escape(relative_url(page["output"], by_slug["mobile-app"]["output"]), quote=True),
            "PRIVACY": html.escape(relative_url(page["output"], by_slug["privacy"]["output"]), quote=True),
            "TERMS": html.escape(relative_url(page["output"], by_slug["terms"]["output"]), quote=True),
            "PRIVACYSTATE": ' aria-current="page"' if page["slug"] == "privacy" else "",
            "TERMSSTATE": ' aria-current="page"' if page["slug"] == "terms" else "",
        }
        # 单次替换：正文中的模板形状文本不能被二次解释。
        output = re.sub(r"\{\{([A-Z]+)\}\}", lambda match: values[match[1]], template)
        rendered[page["output"]] = output
    return rendered


def check_site(root, pages, translations, mapping, rendered=None):
    documents = {}
    texts = {}
    errors = []
    for page in pages:
        name = page["output"]
        if rendered is not None:
            text = rendered[name]
        else:
            path = inside(root, name)
            if not path.is_file():
                errors.append(f"缺少生成页面：{path}")
                continue
            text = path.read_text(encoding="utf-8")
        texts[name] = text
        documents[name] = Document(text)
        if documents[name].codes != Document(translations[page["slug"]]).codes:
            errors.append(f"生成页的代码块与译文不一致：{name}")
    for name, document in documents.items():
        for tag, attrs, _, _, in_code in document.tags:
            if in_code:
                continue
            for attribute in (["href"] if tag == "a" else ["src", "poster"]):
                value = attrs.get(attribute)
                if not value:
                    continue
                url = urlsplit(value)
                if url.scheme or url.netloc:
                    if tag == "a" and url.hostname == "happy.engineering" and normalized_path(value) in mapping and attrs.get("data-original") != "true":
                        errors.append(f"已翻译文档仍指向官网：{name} → {value}")
                    continue
                if url.path.startswith("/"):
                    errors.append(f"站内链接不能依赖域名根路径：{name} → {value}")
                    continue
                target = (Path(name).parent / unquote(url.path)).as_posix() if url.path else name
                target = os.path.normpath(target).replace(os.sep, "/")
                if target.endswith("/") or not Path(target).suffix:
                    target = target.rstrip("/") + "/index.html"
                try:
                    target_path = inside(root, target)
                except ValueError:
                    errors.append(f"站内链接越界：{name} → {value}")
                    continue
                if target not in documents and not target_path.is_file():
                    errors.append(f"站内文件不存在：{name} → {value}")
                elif url.fragment and target in documents and unquote(url.fragment) not in documents[target].ids:
                    errors.append(f"站内锚点不存在：{name} → {value}")
        current = [attrs for tag, attrs, *_ in document.tags if tag == "a" and attrs.get("aria-current") in {"page", "location"}]
        expected_current = 2 if any(page["output"] == name and page["slug"] in {"privacy", "terms"} for page in pages) else 3
        if len(current) != expected_current:
            errors.append(f"当前导航数量不正确：{name}，期望 {expected_current} 项")
    if errors:
        raise ValueError("静态验收失败：\n" + "\n".join(errors))
    return len(documents)


def main():
    parser = argparse.ArgumentParser(description="构建 Happy 完整中文静态手册，缺失译文立即停止")
    parser.add_argument("--root", type=Path, default=ROOT, help="手册仓库绝对路径")
    parser.add_argument("--check", action="store_true", help="只验收已有生成文件，不写入")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        pages, groups, translations, mapping = load_inputs(root)
        if args.check:
            count = check_site(root, pages, translations, mapping)
            print(f"静态验收通过：{count} 页；译文、代码块、站内文件、锚点与当前导航均有效。")
            return 0
        rendered = render(root, pages, groups, translations, mapping)
        # 先完成全部渲染与链接验收，再写任何页面，避免缺译时污染已有站点。
        check_site(root, pages, translations, mapping, rendered)
        for name, text in rendered.items():
            path = inside(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        print(f"构建完成：{len(rendered)} 页。")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"构建停止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
