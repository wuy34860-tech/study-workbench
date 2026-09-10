"""从 PDF / Word / EPUB / 纯文本文件里提取纯文本。"""

import html as _html
import re
import zipfile
from pathlib import Path


def extract_text(path: Path) -> str:
    """根据扩展名分派到对应的解析器，返回纯文本。解析失败返回空字符串。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _from_pdf(path)
    if ext == ".docx":
        return _from_docx(path)
    if ext == ".epub":
        return _from_epub(path)
    # .txt / .md 等纯文本
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _from_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(parts)


def _from_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return ""
    try:
        d = docx.Document(str(path))
    except Exception:
        return ""
    return "\n".join(p.text for p in d.paragraphs)


def _from_epub(path: Path) -> str:
    # EPUB 本质是一个 zip，里面是多个 xhtml 章节
    parts = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".xhtml", ".html", ".htm")):
                    raw = z.read(name).decode("utf-8", errors="ignore")
                    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
                    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = _html.unescape(text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        parts.append(text)
    except Exception:
        return ""
    return "\n\n".join(parts)
