#!/usr/bin/env python3
"""Extrae texto local con localizadores; no modifica originales ni usa APIs de IA."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
ODF = {"office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0", "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0", "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0"}
T, O, TB = ("{" + ODF[k] + "}" for k in ["text", "office", "table"])
ST = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
MAX_XML = 32 * 1024 * 1024


class ExtractionFailure(Exception):
    pass


def tool(args):
    try:
        return subprocess.run(args, capture_output=True, timeout=120, check=True, env=None).stdout
    except FileNotFoundError:
        raise ExtractionFailure("missing_tool:" + args[0]) from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ExtractionFailure("tool_failed:" + args[0]) from None


def xml_member(archive, name):
    if archive.getinfo(name).file_size > MAX_XML:
        raise ExtractionFailure("xml_size_limit")
    data = archive.read(name)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ExtractionFailure("xml_entities_not_allowed")
    return ET.fromstring(data)


def selected_children(node):
    """OOXML guarda a veces un dibujo dos veces: versión moderna y fallback."""
    if node.tag == MC + "AlternateContent":
        supported = {"w", "wps", "wpg", "v"}
        choice = next((c for c in node.findall(MC + "Choice") if set(c.get("Requires", "").split()) <= supported), None)
        if choice is None:
            choice = node.find(MC + "Fallback")
        return list(choice) if choice is not None else []
    return list(node)


def selected_walk(node):
    yield node
    for child in selected_children(node):
        yield from selected_walk(child)


def docx_inline(node):
    if node.tag in {W + "del", W + "instrText", W + "txbxContent"}:
        return ""
    if node.tag == W + "t":
        return node.text or ""
    if node.tag in {W + "br", W + "cr"}:
        return "\n"
    if node.tag == W + "tab":
        return "\t"
    if node.tag == W + "noBreakHyphen":
        return "\u2011"
    if node.tag == W + "softHyphen":
        return "\u00ad"
    return "".join(docx_inline(child) for child in selected_children(node))


def odt_inline(node):
    if node.tag == T + "s":
        return " " * min(int(node.get(T + "c", "1")), 10000)
    if node.tag == T + "tab":
        return "\t"
    if node.tag == T + "line-break":
        return "\n"
    if node.tag in {O + "annotation", O + "annotation-end"}:
        return ""
    return (node.text or "") + "".join(odt_inline(c) + (c.tail or "") for c in node)


def number_label(value, style):
    if style in {"decimal", "1"}:
        return str(value)
    if style in {"lowerLetter", "upperLetter", "a", "A"}:
        result = ""
        while value > 0:
            value, remainder = divmod(value - 1, 26)
            result = chr(97 + remainder) + result
        return result.upper() if style in {"upperLetter", "A"} else result
    if style in {"lowerRoman", "upperRoman", "i", "I"} and 0 < value < 4000:
        result = ""
        for n, symbol in [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]:
            while value >= n:
                result += symbol
                value -= n
        return result.lower() if style in {"lowerRoman", "i"} else result
    return None


class DocxNumbering:
    def __init__(self, root):
        self.levels, self.counters = {}, {}
        if root is None:
            return
        abstract = {n.get(W + "abstractNumId"): n for n in root.findall(W + "abstractNum")}
        for num in root.findall(W + "num"):
            aid = num.find(W + "abstractNumId")
            definition = abstract.get(aid.get(W + "val")) if aid is not None else None
            if definition is None:
                continue
            levels = {int(n.get(W + "ilvl")): n for n in definition.findall(W + "lvl")}
            for override in num.findall(W + "lvlOverride"):
                level = int(override.get(W + "ilvl"))
                explicit = override.find(W + "lvl")
                if explicit is not None:
                    levels[level] = explicit
                start = override.find(W + "startOverride")
                if start is not None and level in levels:
                    node = ET.fromstring(ET.tostring(levels[level]))
                    old = node.find(W + "start")
                    if old is not None:
                        node.remove(old)
                    ET.SubElement(node, W + "start", {W + "val": start.get(W + "val")})
                    levels[level] = node
            self.levels[num.get(W + "numId")] = levels

    def next(self, metadata):
        num_id, level = metadata.get("numId"), int(metadata.get("ilvl", "0"))
        levels = self.levels.get(num_id, {})
        definition = levels.get(level)
        if definition is None:
            return None
        start = definition.find(W + "start")
        counts = self.counters.setdefault(num_id, {})
        counts[level] = counts.get(level, int(start.get(W + "val", "1")) - 1 if start is not None else 0) + 1
        for deeper in list(counts):
            if deeper > level:
                restart_node = levels[deeper].find(W + "lvlRestart")
                restart = int(restart_node.get(W + "val")) if restart_node is not None else deeper
                if restart > deeper:
                    restart = deeper
                if restart and level < restart:
                    del counts[deeper]
        template = definition.find(W + "lvlText")
        if template is None:
            return None
        label = template.get(W + "val", "")
        for index in range(9):
            placeholder = "%" + str(index + 1)
            if placeholder not in label:
                continue
            previous = levels.get(index)
            fmt = previous.find(W + "numFmt") if previous is not None else None
            if fmt is None:
                return None
            rendered = number_label(counts.get(index, 1), fmt.get(W + "val"))
            if rendered is None:
                return None
            label = label.replace(placeholder, rendered)
        return label


def pdf_extract(path):
    help_text = subprocess.run(["pdftotext", "-h"], capture_output=True, check=False).stderr
    options = ["-layout", "-enc", "UTF-8", "-eol", "unix"]
    warnings = []
    if b"-remove-hyphens" in help_text:
        options += ["-remove-hyphens", "none"]
    else:
        warnings.append("dehyphenation_control_unavailable")
    text = tool(["pdftotext", *options, str(path), "-"]).decode("utf-8")
    info = tool(["pdfinfo", str(path)]).decode("utf-8", errors="replace")
    pages_match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if pages_match is None:
        raise ExtractionFailure("pdf_page_count_unknown")
    count = int(pages_match.group(1))
    pages = text.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    if len(pages) != count:
        raise ExtractionFailure("pdf_page_count_mismatch")
    images = Counter()
    for line in tool(["pdfimages", "-list", str(path)]).decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit() and fields[2] == "image":
            images[int(fields[0])] += 1
    units = []
    for page, content in enumerate(pages, 1):
        flags = []
        if images[page]:
            flags.append("images_not_transcribed")
        if len(content.strip()) < 30:
            flags.append("low_text_page")
        if not content.strip() and images[page]:
            flags.append("needs_ocr")
        units.append({"kind": "page", "locator": {"page": page}, "text": content.rstrip("\n"), "image_count": images[page], "warnings": flags})
    return units, warnings, {"page_count": count, "image_count": sum(images.values()), "layout": True}


def docx_extract(path):
    warnings = ["office_layout_not_preserved"]
    units = []
    with zipfile.ZipFile(path) as archive:
        root = xml_member(archive, "word/document.xml")
        body = root.find(W + "body")
        if body is None:
            raise ExtractionFailure("docx_body_missing")
        members = archive.namelist()
        image_count = sum(name.startswith("word/media/") and not name.endswith("/") for name in members)
        if image_count:
            warnings.append("images_not_transcribed")
        if any(n.tag in {W + "ins", W + "del"} for n in root.iter()):
            warnings.append("tracked_changes_present")
        if any(n.tag == MC + "AlternateContent" and not selected_children(n) for n in root.iter()):
            warnings.append("alternate_content_unresolved")
        if any(n.tag == W + "instrText" for n in root.iter()):
            warnings.append("fields_use_cached_text")
        if any(re.fullmatch(r"word/(header\d+|footer\d+|footnotes|endnotes)\.xml", name) for name in members):
            warnings.append("headers_footers_notes_not_extracted")
        numbering = DocxNumbering(xml_member(archive, "word/numbering.xml") if "word/numbering.xml" in members else None)

        def visit(container):
            for node in selected_children(container):
                if node.tag == W + "del":
                    continue
                if node.tag == W + "p":
                    text = docx_inline(node)
                    style = node.find(W + "pPr/" + W + "pStyle")
                    number = node.find(W + "pPr/" + W + "numPr")
                    meta = {}
                    if style is not None:
                        meta["style"] = style.get(W + "val")
                    if number is not None:
                        meta["numbering"] = {n.tag.split("}")[-1]: n.get(W + "val") for n in number}
                        label = numbering.next(meta["numbering"]) if meta["numbering"].get("numId") != "0" else ""
                        if label is None:
                            warnings.append("list_numbering_unresolved")
                        elif label:
                            meta["list_label"] = label
                    if text.strip():
                        units.append({"kind": "paragraph", "text": text, **meta})
                    for textbox in selected_walk(node):
                        if textbox.tag == W + "txbxContent":
                            warnings.append("textboxes_read_in_xml_order")
                            visit(textbox)
                elif node.tag == W + "tbl":
                    rows = []
                    for row in node.findall(W + "tr"):
                        cells = []
                        for cell in row.findall(W + "tc"):
                            paragraphs = [docx_inline(p) for p in selected_walk(cell) if p.tag == W + "p"]
                            cells.append("\n".join(paragraphs))
                        rows.append(cells)
                    units.append({"kind": "table", "rows": rows, "text": "\n".join("\t".join(c.replace("\n", " | ") for c in row) for row in rows)})
                elif node.tag != W + "sectPr":
                    visit(node)
        visit(body)
    for i, unit in enumerate(units, 1):
        unit["locator"] = {"block": i}
        unit["warnings"] = []
    return units, sorted(set(warnings)), {"image_count": image_count}


def odt_extract(path):
    warnings = ["office_layout_not_preserved"]
    units = []
    with zipfile.ZipFile(path) as archive:
        root = xml_member(archive, "content.xml")
        body = root.find(O + "body/" + O + "text")
        if body is None:
            raise ExtractionFailure("odt_body_missing")
        image_count = sum(name.startswith("Pictures/") and not name.endswith("/") for name in archive.namelist())
        if image_count:
            warnings.append("images_not_transcribed")
        style_roots = [root]
        if "styles.xml" in archive.namelist():
            style_roots.append(xml_member(archive, "styles.xml"))
        list_styles = {n.get(ST + "name"): n for r in style_roots for n in r.iter(T + "list-style")}
        previous_lists = {}
        if any(n.tag == T + "tracked-changes" for n in body.iter()):
            warnings.append("tracked_changes_present")

        def visit(container, level=0, list_item=None, list_style=None, label=None):
            for node in container:
                if node.tag in {O + "annotation", T + "tracked-changes"}:
                    continue
                if node.tag in {T + "p", T + "h"}:
                    text = odt_inline(node)
                    if text.strip():
                        unit = {"kind": "heading" if node.tag == T + "h" else "paragraph", "text": text}
                        if list_item is not None:
                            unit["list_context"] = {"level": level, "item_index": list_item}
                            if label is not None:
                                unit["list_label"] = label
                                label = None
                        units.append(unit)
                    for image in node.iter(DRAW + "image"):
                        member = image.get(XLINK + "href", "").removeprefix("./")
                        if member.startswith("Pictures/") and ".." not in Path(member).parts and member in archive.namelist():
                            units.append({"kind": "image", "text": "", "image_member": member, "warnings": ["images_not_transcribed"]})
                elif node.tag == T + "list":
                    name = node.get(T + "style-name", list_style)
                    style = list_styles.get(name)
                    definition = next((n for n in style if n.get(T + "level") == str(level + 1)), None) if style is not None else None
                    start = int(definition.get(T + "start-value", "1")) if definition is not None else 1
                    continuation = node.get(T + "continue-list")
                    if continuation in previous_lists:
                        start = previous_lists[continuation] + 1
                    elif node.get(T + "continue-numbering") == "true":
                        start = previous_lists.get((name, level), start - 1) + 1
                    last = start - 1
                    for item in node.findall(T + "list-item"):
                        last = int(item.get(T + "start-value", str(last + 1)))
                        item_label = None
                        if definition is not None:
                            if definition.tag == T + "list-level-style-bullet":
                                item_label = definition.get(T + "bullet-char", "•")
                            else:
                                rendered = number_label(last, definition.get(ST + "num-format", "1"))
                                if rendered is not None:
                                    item_label = definition.get(ST + "num-prefix", "") + rendered + definition.get(ST + "num-suffix", "")
                        if item_label is None:
                            warnings.append("list_numbering_unresolved")
                        visit(item, level + 1, last, name, item_label)
                    previous_lists[(name, level)] = last
                    identity = node.get("{http://www.w3.org/XML/1998/namespace}id")
                    if identity:
                        previous_lists[identity] = last
                elif node.tag == TB + "table":
                    rows = []
                    for row in node.iter(TB + "table-row"):
                        cells = []
                        for cell in row:
                            if cell.tag not in {TB + "table-cell", TB + "covered-table-cell"}:
                                continue
                            if int(cell.get(TB + "number-columns-repeated", "1")) > 1000:
                                raise ExtractionFailure("odt_table_repeat_limit")
                            text = "\n".join(odt_inline(p) for p in cell.iter() if p.tag in {T + "p", T + "h"})
                            cells.extend([text] * int(cell.get(TB + "number-columns-repeated", "1")))
                        repeat = int(row.get(TB + "number-rows-repeated", "1"))
                        if repeat > 1000:
                            raise ExtractionFailure("odt_table_repeat_limit")
                        rows.extend([cells] * repeat)
                    units.append({"kind": "table", "rows": rows, "text": "\n".join("\t".join(c.replace("\n", " | ") for c in row) for row in rows)})
                else:
                    visit(node, level, list_item, list_style, label)
        visit(body)
    for i, unit in enumerate(units, 1):
        unit["locator"] = {"block": i}
        unit.setdefault("warnings", [])
    return units, sorted(set(warnings)), {"image_count": image_count}


def plain_extract(path, mime):
    raw = path.read_bytes()
    warnings = []
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1252")
                warnings.append("encoding_cp1252_assumed_requires_review")
        if "\x00" in text:
            raise ExtractionFailure("text_encoding_invalid_or_binary")
    except UnicodeError:
        raise ExtractionFailure("text_encoding_invalid_or_binary") from None
    if mime == "text/csv":
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
            warnings.append("csv_dialect_fallback")
        rows = list(csv.reader(io.StringIO(text, newline=""), dialect))
        units = [{"kind": "row", "locator": {"row": i}, "cells": row, "text": "\t".join(row), "warnings": []} for i, row in enumerate(rows, 1)]
        return units, warnings, {"delimiter": dialect.delimiter}
    return [{"kind": "text", "locator": {"block": 1}, "text": text, "warnings": []}], warnings, {}


class ManualHTMLText(HTMLParser):
    """Extrae la zona principal de un manual HTML sin ejecutar su contenido."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BREAKS = {"p", "div", "pre", "li", "h1", "h2", "h3", "h4", "tr", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.active = False
        self.found = False
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.active:
            if attrs.get("id") == "layout-content":
                self.active = self.found = True
                self.depth = 1
            return
        if tag not in self.VOID:
            self.depth += 1
        if self.suppressed:
            if tag not in self.VOID:
                self.suppressed += 1
            return
        if tag in {"script", "style", "svg", "nav"}:
            self.suppressed = 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if not self.active or tag in self.VOID:
            return
        if self.suppressed:
            self.suppressed -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")
        self.depth -= 1
        if self.depth == 0:
            self.active = False

    def handle_data(self, data):
        if self.active and not self.suppressed:
            self.parts.append(data)


def manual_html_extract(path):
    parser = ManualHTMLText()
    parser.feed(path.read_text(encoding="utf-8-sig"))
    if not parser.found:
        raise ExtractionFailure("manual_html_layout_missing")
    text = "\n".join(line.rstrip() for line in "".join(parser.parts).splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return [{"kind": "text", "locator": {"block": 1}, "text": text, "warnings": []}], ["html_layout_not_preserved"], {"html_scope": "layout-content"}


def extract(source, source_root, render_html=False):
    path = (source_root / source["path"]).resolve()
    if not path.is_relative_to(source_root.resolve()):
        raise ExtractionFailure("source_path_outside_root")
    if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
        raise ExtractionFailure("source_hash_mismatch")
    mime = source["mime"]
    if mime == "application/pdf":
        units, warnings, details = pdf_extract(path)
        engine = "poppler-layout"
    elif mime == "application/vnd.oasis.opendocument.text":
        units, warnings, details = odt_extract(path)
        engine = "odf-xml"
    elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        units, warnings, details = docx_extract(path)
        engine = "ooxml-body"
    elif mime == "text/html" and render_html:
        units, warnings, details = manual_html_extract(path)
        engine = "html-main-text"
    elif mime in {"text/plain", "text/csv", "text/html", "text/css", "application/x-sh"}:
        units, warnings, details = plain_extract(path, mime)
        engine = "text-csv"
    elif mime in {"image/png", "image/jpeg", "image/tiff", "image/gif"}:
        units = [{"kind": "page", "locator": {"page": 1}, "text": "", "image_count": 1, "warnings": ["needs_ocr"]}]
        warnings, details, engine = [], {"page_count": 1, "image_count": 1}, "image"
    else:
        raise ExtractionFailure("unsupported_mime")
    if not any(u["text"].strip() for u in units):
        warnings.append("no_text_extracted")
    if any("\ufffd" in u["text"] for u in units):
        warnings.append("unicode_replacement_character")
    for unit in units:
        unit["id"] = source["sha256"][:16] + ":" + ":".join(f"{k}-{v}" for k, v in unit["locator"].items())
    return {"schema_version": 1, "source": {k: source[k] for k in ["sha256", "path", "url", "title", "section", "mime", "filename"]}, "extractor": {"name": engine, "version": VERSION, **details}, "units": units, "warnings": sorted(set(warnings)), "characters": sum(len(u["text"]) for u in units)}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run(manifest_path, output, sample=False, allow_unsupported=False, render_html=False):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("complete") or manifest.get("issues"):
        raise ExtractionFailure("import_manifest_not_complete")
    sources = {}
    for item in manifest["items"]:
        if item.get("sha256"):
            sources.setdefault(item["sha256"], []).append(item)
    selected = [aliases[0] for aliases in sources.values()]
    if sample:
        by_mime = {}
        for item in selected:
            if item["mime"] not in by_mime or item["filename"] == "UD1_Administracion_de_procesos.pdf":
                by_mime[item["mime"]] = item
        selected = list(by_mime.values())
    output.mkdir(parents=True, exist_ok=True)
    poppler = subprocess.run(["pdftotext", "-v"], capture_output=True, check=False).stderr.decode("utf-8", errors="replace").splitlines()[0]
    report = {"schema_version": 1, "extractor_version": VERSION, "runtime": {"python": sys.version.split()[0], "poppler": poppler}, "started_at": datetime.now(timezone.utc).isoformat(), "mode": "sample" if sample else "all", "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "documents": [], "errors": [], "skipped": []}
    for item in selected:
        try:
            document = extract(item, manifest_path.parent, render_html)
            document["source_aliases"] = [{k: alias[k] for k in ["sha256", "path", "url", "title", "section", "mime", "filename"]} for alias in sources[item["sha256"]][1:]]
            relative = Path("documents") / (item["sha256"] + ".json")
            save_json(output / relative, document)
            readable = "\n\n".join("[" + ", ".join(f"{k} {v}" for k, v in u["locator"].items()) + "]\n" + (u["list_label"] + " " if u.get("list_label") else "") + u["text"] for u in document["units"])
            (output / relative.with_suffix(".txt")).write_text(readable + "\n", encoding="utf-8")
            flags = sorted(set(document["warnings"] + [f for u in document["units"] for f in u["warnings"]]))
            report["documents"].append({"source_sha256": item["sha256"], "title": item["title"], "mime": item["mime"], "path": relative.as_posix(), "document_sha256": hashlib.sha256((output / relative).read_bytes()).hexdigest(), "characters": document["characters"], "units": len(document["units"]), "warnings": flags, "review_required": bool(flags)})
        except (ExtractionFailure, OSError, ValueError, TypeError, ET.ParseError, zipfile.BadZipFile, KeyError, csv.Error) as error:
            if allow_unsupported and isinstance(error, ExtractionFailure) and str(error) == "unsupported_mime":
                report["skipped"].append({"source_sha256": item["sha256"], "mime": item["mime"], "filename": item["filename"], "reason": "unsupported_mime"})
                continue
            report["errors"].append({"source_sha256": item["sha256"], "error": str(error) if isinstance(error, ExtractionFailure) else type(error).__name__})
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["complete"] = not report["errors"] and len(report["documents"]) + len(report["skipped"]) == len(selected)
    report["summary"] = {"documents": len(report["documents"]), "characters": sum(d["characters"] for d in report["documents"]), "units": sum(d["units"] for d in report["documents"]), "review_required": sum(d["review_required"] for d in report["documents"]), "warnings": dict(Counter(w for d in report["documents"] for w in d["warnings"]))}
    save_json(output / "report.json", report)
    print(json.dumps({"complete": report["complete"], **report["summary"], "errors": report["errors"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0 if report["complete"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["sample", "all"])
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/aso/moodle/manifest.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-unsupported", action="store_true", help="Registra formatos no compatibles como omitidos, sin ocultar errores de extracción.")
    parser.add_argument("--render-html", action="store_true", help="Extrae texto del layout-content del manual HTML.")
    args = parser.parse_args()
    output = args.output or ROOT / "data/aso/extracted" / args.mode
    try:
        if any(shutil.which(cmd) is None for cmd in ["pdftotext", "pdfinfo", "pdfimages"]):
            raise ExtractionFailure("missing_poppler_tools")
        return run(args.manifest, output, args.mode == "sample", args.allow_unsupported, args.render_html)
    except (ExtractionFailure, OSError, ValueError) as error:
        print(str(error) if isinstance(error, ExtractionFailure) else "No se han podido leer los archivos locales.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
