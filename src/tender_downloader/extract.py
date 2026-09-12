from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .htmlparse import parse_html_document


MAX_TEXT = 300_000
MAX_ARCHIVE_ENTRIES = 200
MAX_UNCOMPRESSED = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    needs_review: bool = False
    reason: str = ""


def _xml_text(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    values = [node.text for node in root.iter() if node.text and node.text.strip()]
    return " ".join(values)


def _office_zip_text(path: Path) -> ExtractedText:
    values: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return ExtractedText("", True, "压缩包条目过多")
            for info in infos:
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    return ExtractedText("\n".join(values)[:MAX_TEXT], True, "解压后体积超过限制")
                name = info.filename.lower()
                if name.endswith(".xml") and (
                    name.startswith("word/")
                    or name.startswith("xl/sharedstrings")
                    or name.startswith("xl/worksheets")
                ):
                    values.append(_xml_text(archive.read(info)))
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return ExtractedText("", True, f"Office/ZIP 解析失败: {exc}")
    text = re.sub(r"\s+", " ", " ".join(values)).strip()[:MAX_TEXT]
    return ExtractedText(text, not bool(text), "未提取到文本" if not text else "")


def _generic_zip_text(path: Path) -> ExtractedText:
    values: list[str] = []
    total = 0
    unparsed_entries = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return ExtractedText("", True, "压缩包条目过多")
            for info in infos:
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    return ExtractedText("\n".join(values)[:MAX_TEXT], True, "解压后体积超过限制")
                suffix = Path(info.filename).suffix.lower()
                if suffix in (".txt", ".html", ".htm", ".xml", ".json"):
                    raw = archive.read(info)
                    if suffix in (".html", ".htm"):
                        values.append(parse_html_document(raw, "https://local.invalid/").text)
                    else:
                        values.append(raw.decode("utf-8", errors="replace"))
                else:
                    unparsed_entries += 1
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return ExtractedText("", True, f"ZIP 解析失败: {exc}")
    text = "\n".join(values)[:MAX_TEXT]
    needs_review = not bool(text) or unparsed_entries > 0
    if unparsed_entries:
        reason = f"ZIP 内有 {unparsed_entries} 个条目未解析"
    elif not text:
        reason = "ZIP 内无可直接读取文本"
    else:
        reason = ""
    return ExtractedText(text, needs_review, reason)


def _pdf_text(path: Path) -> ExtractedText:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return ExtractedText("", True, "未安装可选依赖 pypdf")
    try:
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_TEXT]
    except Exception as exc:  # pypdf 对损坏/加密文件抛出多种异常
        return ExtractedText("", True, f"PDF 解析失败: {exc}")
    if not text.strip():
        return ExtractedText("", True, "扫描 PDF 或无文本层，需要 OCR")
    return ExtractedText(text)


def extract_text(path: Path) -> ExtractedText:
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        return ExtractedText(parse_html_document(path.read_bytes(), "https://local.invalid/").text[:MAX_TEXT])
    if suffix in (".txt", ".json", ".xml", ".csv"):
        return ExtractedText(path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT])
    if suffix in (".docx", ".xlsx"):
        return _office_zip_text(path)
    if suffix == ".zip":
        return _generic_zip_text(path)
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix in (".doc", ".xls", ".rar", ".7z", ".ofd"):
        return ExtractedText("", True, f"两天版暂不解析 {suffix}，原件已保留")
    return ExtractedText("", True, f"未知文件格式: {suffix or '无扩展名'}")
