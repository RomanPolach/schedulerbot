from __future__ import annotations

import csv
import os
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List

from langchain.tools import tool

from tools.shared_shell import strip_wrapping_quotes

OPEN_FILE_TYPE_LABELS: Dict[str, str] = {
    # Word (OOXML)
    ".docx": "Word document",
    ".docm": "Word document",
    # Excel (OOXML / CSV)
    ".xlsx": "Excel workbook",
    ".xlsm": "Excel workbook",
    ".csv": "CSV spreadsheet",
    # Text
    ".txt": "Text file",
    # OpenDocument
    ".odt": "OpenDocument text",
    ".ods": "OpenDocument spreadsheet",
    ".odp": "OpenDocument presentation",
}
OPEN_FILE_MAX_CHARS = max(1_000, min(int(os.getenv("OPEN_FILE_MAX_CHARS", "50000")), 300_000))
OPEN_FILE_MAX_ROWS = max(5, min(int(os.getenv("OPEN_FILE_MAX_ROWS", "80")), 1_000))
OPEN_FILE_MAX_COLS = max(3, min(int(os.getenv("OPEN_FILE_MAX_COLS", "24")), 200))
OPEN_FILE_MAX_SHEETS = max(1, min(int(os.getenv("OPEN_FILE_MAX_SHEETS", "6")), 50))


def _truncate_text(value: str, max_chars: int = OPEN_FILE_MAX_CHARS) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _markdown_escape_cell(value: str) -> str:
    v = str(value or "")
    v = v.replace("|", r"\|").replace("\n", "<br>").strip()
    return v


def _to_markdown_table(rows: List[List[str]]) -> str:
    if not rows:
        return "(empty)"
    width = max(len(r) for r in rows)
    normalized = [r + [""] * (width - len(r)) for r in rows]
    header = normalized[0]
    body = normalized[1:] if len(normalized) > 1 else []
    lines = [
        "| " + " | ".join(_markdown_escape_cell(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(_markdown_escape_cell(c) for c in row) + " |")
    return "\n".join(lines)


def _load_text_with_fallback(file_path: str) -> str:
    last_exc: Exception | None = None
    for encoding in ["utf-8-sig", "utf-16", "cp1250", "latin-1"]:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"Could not decode text file: {last_exc}")


def _format_markdown_file_output(file_path: str, file_type: str, body_md: str, truncated: bool = False) -> str:
    lines = [
        "# File Content",
        f"- Path: `{file_path}`",
        f"- Type: {file_type}",
        "",
        "## Content",
        body_md.strip() or "(empty)",
    ]
    if truncated:
        lines.extend(
            [
                "",
                f"> Note: Output truncated to {OPEN_FILE_MAX_CHARS} characters.",
            ]
        )
    return "\n".join(lines).strip()


def _read_txt_markdown(file_path: str) -> tuple[str, bool]:
    text = _load_text_with_fallback(file_path)
    text, truncated = _truncate_text(text)
    return f"```text\n{text}\n```", truncated


def _read_csv_markdown(file_path: str) -> tuple[str, bool]:
    raw = _load_text_with_fallback(file_path)
    sample = raw[:8192]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample).delimiter
    except Exception:
        pass
    reader = csv.reader(raw.splitlines(), delimiter=delimiter)
    rows: List[List[str]] = []
    for idx, row in enumerate(reader):
        if idx >= OPEN_FILE_MAX_ROWS:
            break
        clipped = [str(c)[:500] for c in row[:OPEN_FILE_MAX_COLS]]
        rows.append(clipped)
    truncated = len(raw) > OPEN_FILE_MAX_CHARS
    return _to_markdown_table(rows), truncated


def _docx_paragraphs(file_path: str) -> str:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(file_path, "r") as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    paragraphs: List[str] = []
    for paragraph in root.findall(".//w:p", ns):
        texts = [t.text or "" for t in paragraph.findall(".//w:t", ns)]
        merged = "".join(texts).strip()
        if merged:
            paragraphs.append(merged)
    return "\n\n".join(paragraphs)


def _read_docx_markdown(file_path: str) -> tuple[str, bool]:
    text = _docx_paragraphs(file_path)
    text, truncated = _truncate_text(text)
    return text or "(empty)", truncated


def _excel_col_to_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in str(cell_ref or "") if ch.isalpha()).upper()
    if not letters:
        return 0
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return max(0, idx - 1)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values: List[str] = []
    for si in root.findall(".//m:si", ns):
        chunks = [t.text or "" for t in si.findall(".//m:t", ns)]
        values.append("".join(chunks))
    return values


def _xlsx_sheet_targets(zf: zipfile.ZipFile) -> List[tuple[str, str]]:
    ns_main = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    ns_rel = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map: Dict[str, str] = {}
    for rel in rels.findall(".//rel:Relationship", ns_rel):
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rid and target:
            rel_map[rid] = target if target.startswith("xl/") else f"xl/{target}"
    result: List[tuple[str, str]] = []
    for sheet in wb.findall(".//m:sheets/m:sheet", ns_main):
        name = sheet.attrib.get("name", "Sheet")
        rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_map.get(rid)
        if target:
            result.append((name, target))
    return result


def _read_xlsx_markdown(file_path: str) -> tuple[str, bool]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sections: List[str] = []
    truncated = False
    with zipfile.ZipFile(file_path, "r") as zf:
        shared = _xlsx_shared_strings(zf)
        for idx, (sheet_name, target) in enumerate(_xlsx_sheet_targets(zf)):
            if idx >= OPEN_FILE_MAX_SHEETS:
                truncated = True
                break
            if target not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(target))
            rows: List[List[str]] = []
            for r_idx, row in enumerate(root.findall(".//m:sheetData/m:row", ns)):
                if r_idx >= OPEN_FILE_MAX_ROWS:
                    truncated = True
                    break
                values_by_col: Dict[int, str] = {}
                for cell in row.findall("m:c", ns):
                    col = _excel_col_to_index(cell.attrib.get("r", ""))
                    if col >= OPEN_FILE_MAX_COLS:
                        truncated = True
                        continue
                    cell_type = cell.attrib.get("t", "")
                    value = ""
                    v_node = cell.find("m:v", ns)
                    if cell_type == "s" and v_node is not None and v_node.text:
                        try:
                            s_idx = int(v_node.text)
                            value = shared[s_idx] if 0 <= s_idx < len(shared) else v_node.text
                        except Exception:
                            value = v_node.text
                    elif cell_type == "inlineStr":
                        t_nodes = cell.findall(".//m:t", ns)
                        value = "".join((n.text or "") for n in t_nodes)
                    elif v_node is not None and v_node.text is not None:
                        value = v_node.text
                    values_by_col[col] = value
                if values_by_col:
                    width = min(max(values_by_col.keys()) + 1, OPEN_FILE_MAX_COLS)
                    rows.append([values_by_col.get(c, "") for c in range(width)])
                elif rows:
                    rows.append([])
            sections.append(f"### Sheet: {sheet_name}\n{_to_markdown_table(rows)}")
    return "\n\n".join(sections) or "(empty)", truncated


def _odf_text_content(file_path: str) -> str:
    ns_text = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    with zipfile.ZipFile(file_path, "r") as zf:
        root = ET.fromstring(zf.read("content.xml"))
    lines: List[str] = []
    for elem in root.iter():
        if elem.tag in {f"{ns_text}p", f"{ns_text}h"}:
            text = "".join(elem.itertext()).strip()
            if text:
                lines.append(text)
    return "\n\n".join(lines)


def _read_odt_odp_markdown(file_path: str) -> tuple[str, bool]:
    text = _odf_text_content(file_path)
    text, truncated = _truncate_text(text)
    return text or "(empty)", truncated


def _read_ods_markdown(file_path: str) -> tuple[str, bool]:
    ns_table = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    ns_text = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    sections: List[str] = []
    truncated = False
    with zipfile.ZipFile(file_path, "r") as zf:
        root = ET.fromstring(zf.read("content.xml"))

    tables = [elem for elem in root.iter() if elem.tag == f"{ns_table}table"]
    for t_idx, table in enumerate(tables):
        if t_idx >= OPEN_FILE_MAX_SHEETS:
            truncated = True
            break
        table_name = table.attrib.get(f"{ns_table}name", f"Table{t_idx + 1}")
        rows: List[List[str]] = []
        for r_idx, row in enumerate([e for e in table if e.tag == f"{ns_table}table-row"]):
            if r_idx >= OPEN_FILE_MAX_ROWS:
                truncated = True
                break
            values: List[str] = []
            for cell in [e for e in row if e.tag in {f"{ns_table}table-cell", f"{ns_table}covered-table-cell"}]:
                text_parts = []
                for p in cell.iter():
                    if p.tag in {f"{ns_text}p", f"{ns_text}h"}:
                        text_parts.append("".join(p.itertext()).strip())
                cell_text = " ".join(part for part in text_parts if part).strip()
                values.append(cell_text)
                if len(values) >= OPEN_FILE_MAX_COLS:
                    truncated = True
                    break
            rows.append(values)
        sections.append(f"### Sheet: {table_name}\n{_to_markdown_table(rows)}")
    return "\n\n".join(sections) or "(empty)", truncated


def create_open_file_tool() -> Any:
    @tool
    def open_file(file_path: str) -> str:
        """Read a supported local file and return markdown content.

        Required args:
        - file_path: absolute or relative path to a local file.

        Supported file types:
        - .txt, .csv, .docx, .docm, .xlsx, .xlsm, .odt, .ods, .odp

        Notes:
        - output may be truncated by configured size/row/column limits.

        Examples:
        - open_file(file_path="data/report.txt")
        - open_file(file_path="C:\\\\docs\\\\notes.docx")
        """
        raw = strip_wrapping_quotes((file_path or "").strip())
        if not raw:
            return "No file path provided."

        expanded = os.path.expandvars(os.path.expanduser(raw))
        resolved = expanded if os.path.isabs(expanded) else os.path.abspath(expanded)
        if not os.path.exists(resolved):
            return f"File not found: {resolved}"
        if not os.path.isfile(resolved):
            return f"Path is not a file: {resolved}"

        ext = os.path.splitext(resolved)[1].lower()
        if ext not in OPEN_FILE_TYPE_LABELS:
            supported = ", ".join(sorted(OPEN_FILE_TYPE_LABELS.keys()))
            ext_label = ext or "(no extension)"
            return f"Unsupported file type '{ext_label}'. Supported types: {supported}"

        readers = {
            ".txt": _read_txt_markdown,
            ".csv": _read_csv_markdown,
            ".docx": _read_docx_markdown,
            ".docm": _read_docx_markdown,
            ".xlsx": _read_xlsx_markdown,
            ".xlsm": _read_xlsx_markdown,
            ".odt": _read_odt_odp_markdown,
            ".odp": _read_odt_odp_markdown,
            ".ods": _read_ods_markdown,
        }
        reader = readers.get(ext)
        if reader is None:
            supported = ", ".join(sorted(readers.keys()))
            return f"Unsupported file type '{ext}'. Supported types: {supported}"

        try:
            body_md, truncated = reader(resolved)
        except Exception as exc:
            return f"Failed to parse file: {type(exc).__name__}: {exc}"

        file_type = OPEN_FILE_TYPE_LABELS.get(ext, "File")
        return _format_markdown_file_output(resolved, file_type, body_md, truncated=truncated)

    return open_file
