#!/usr/bin/env python3
"""只读取官方静态脚本中的法律 Markdown 字面量，不执行任何外部脚本。"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import runpy
import tempfile
import urllib.parse


def inline(text):
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: '<a href="' + html.escape(urllib.parse.urljoin("https://happy.engineering/", html.unescape(m[2])), quote=True) + '">' + m[1] + '</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def render(markdown):
    output, paragraph, listed = ['<div class="document-content">'], [], False
    def flush():
        if paragraph:
            output.append("<p>" + inline("\n".join(paragraph)) + "</p>")
            paragraph.clear()
    for line in markdown.splitlines():
        if not line.strip():
            flush()
            if listed:
                output.append("</ul>")
                listed = False
            continue
        heading = re.match(r"^(#{1,6}) (.+)$", line)
        if heading:
            flush()
            level = len(heading[1])
            output.append(f"<h{level}>" + inline(heading[2]) + f"</h{level}>")
        elif line == "---":
            flush()
            output.append("<hr>")
        elif line.startswith("- "):
            flush()
            if not listed:
                output.append("<ul>")
                listed = True
            output.append("<li>" + inline(line[2:]) + "</li>")
        else:
            paragraph.append(line)
    flush()
    if listed:
        output.append("</ul>")
    output.append("</div>")
    return "\n".join(output) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    tools = runpy.run_path(str(workspace / "tools" / "prepare_sources.py"))
    manifest_path = workspace / "content" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    root = tempfile.mkdtemp(prefix="happy-legal-static-", dir="/private/tmp")
    # 从页面自身提取当前资源 URL，不固定外部构建产物哈希。
    page, _, _ = tools["fetch"]("https://happy.engineering/privacy/", root)
    doc = tools["Document"](page)
    script_url = next(urllib.parse.urljoin("https://happy.engineering/", n.attrs["src"]) for n in doc.root.walk() if n.tag == "script" and n.attrs.get("src"))
    script, directory, final_url = tools["fetch"](script_url, root)
    for slug, title, title_zh in [("privacy", "Privacy Policy", "隐私政策"), ("terms", "Terms of Use", "使用条款")]:
        # 仅允许无插值的模板字符串；不以 eval、JS 引擎或动态导入解释下载内容。
        pattern = r"[A-Za-z_$][\w$]*=`(# " + re.escape(title) + r"\n(?:[^`\\]|\\.)*)`"
        found = re.search(pattern, script)
        if not found:
            raise ValueError(f"未找到 {slug} 的完整静态正文")
        markdown = found[1]
        if "${" in markdown:
            raise ValueError("正文存在模板插值，拒绝推测或执行")
        markdown = re.sub(r"\\([`\\$])", r"\1", markdown)
        article = render(markdown)
        source_dir = workspace / "source" / slug
        source_dir.mkdir(exist_ok=True)
        (source_dir / "original.md").write_text(markdown, encoding="utf-8")
        for name in ["article.html", "translate.html"]:
            (source_dir / name).write_text(article, encoding="utf-8")
        url = "https://happy.engineering/" + slug + "/"
        record = next(p for p in manifest["pages"] if p["url"] == url)
        body = tools["content_node"](tools["Document"](article))
        links = sorted({tools["canonical"](n.attrs.get("href", "")) for n in body.walk() if n.tag == "a"} - {None})
        record.pop("error", None)
        record.update(slug=slug, title=title, title_zh=title_zh, output=slug + "/index.html", source="source/" + slug + "/article.html", translation_source="source/" + slug + "/translate.html", status="ok", bytes=len(article.encode()), text_chars=len(body.text()), links=links, download_directory=directory, final_url=url, source_method="官方页面 JS 资源中的完整静态 Markdown 字面量（只读提取，未执行脚本）", source_asset_url=final_url, sha256=hashlib.sha256(article.encode()).hexdigest())
        manifest["failures"] = [p for p in manifest["failures"] if p["url"] != url]
        print(f"已恢复 {slug}: {record['bytes']} 字节；Markdown {len(markdown)} 字符")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
