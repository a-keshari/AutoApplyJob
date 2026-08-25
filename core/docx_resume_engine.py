"""Master-DOCX resume editing with LLM-authored text and deterministic validation.

The LLM decides what editable text should say. This module applies those edits
only to approved paragraphs in a copy of the user's master DOCX, converts the
copy to PDF, and validates locked content, layout, and keyword coverage.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from lxml import etree

try:
    from PyPDF2 import PdfReader
except ImportError:  # pragma: no cover - local developer fallback
    from pypdf import PdfReader  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}
W = f"{{{W_NS}}}"

SECTION_NAMES = (
    "SUMMARY",
    "SKILLS",
    "PROFESSIONAL EXPERIENCE",
    "CERTIFICATIONS",
    "EDUCATION",
)


class ResumeGenerationError(RuntimeError):
    """Raised when a generated resume cannot pass deterministic validation."""


def _paragraph_text(paragraph: Any) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag == W + "br":
            parts.append("\n")
    return "".join(parts)


def _is_numbered(paragraph: Any) -> bool:
    return bool(paragraph.xpath("./w:pPr/w:numPr/w:numId", namespaces=NS))


def _find_sections(paragraphs: list[Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, paragraph in enumerate(paragraphs):
        text = _paragraph_text(paragraph).strip()
        if text in SECTION_NAMES and text not in found:
            found[text] = index

    missing = [name for name in SECTION_NAMES if name not in found]
    if missing:
        raise ResumeGenerationError(
            f"Master DOCX is missing required resume sections: {', '.join(missing)}"
        )

    ordered = [found[name] for name in SECTION_NAMES]
    if ordered != sorted(ordered):
        raise ResumeGenerationError("Master DOCX resume sections are not in expected order")
    return found


def _experience_context(paragraphs: list[Any], index: int, experience_start: int) -> list[str]:
    """Return nearby locked role/company/client lines for an experience bullet."""
    context: list[str] = []
    cursor = index - 1
    while cursor > experience_start and len(context) < 3:
        paragraph = paragraphs[cursor]
        if _is_numbered(paragraph):
            break
        text = _paragraph_text(paragraph).strip()
        if text:
            context.append(text)
        cursor -= 1
    return list(reversed(context))


def _build_editable_context(paragraphs: list[Any], sections: dict[str, int]) -> list[dict]:
    editable: list[dict] = []

    def add_text_range(start_name: str, end_name: str, kind: str) -> None:
        for index in range(sections[start_name] + 1, sections[end_name]):
            text = _paragraph_text(paragraphs[index])
            if text.strip():
                editable.append(
                    {
                        "id": f"p{index}",
                        "index": index,
                        "kind": kind,
                        "original": text,
                    }
                )

    add_text_range("SUMMARY", "SKILLS", "summary")
    add_text_range("SKILLS", "PROFESSIONAL EXPERIENCE", "skill")

    experience_start = sections["PROFESSIONAL EXPERIENCE"]
    for index in range(experience_start + 1, sections["CERTIFICATIONS"]):
        paragraph = paragraphs[index]
        if not _is_numbered(paragraph):
            continue
        editable.append(
            {
                "id": f"p{index}",
                "index": index,
                "kind": "experience_bullet",
                "context": _experience_context(paragraphs, index, experience_start),
                "original": _paragraph_text(paragraph),
            }
        )

    add_text_range("CERTIFICATIONS", "EDUCATION", "certifications")
    return editable


def _read_document_xml(docx_path: Path) -> bytes:
    try:
        with zipfile.ZipFile(docx_path, "r") as archive:
            return archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ResumeGenerationError(f"Unable to read master DOCX: {exc}") from exc


def _parse_document(docx_path: Path) -> tuple[Any, list[Any], dict[str, int], list[dict]]:
    xml = _read_document_xml(docx_path)
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(xml, parser)
    paragraphs = list(root.xpath("//w:body/w:p", namespaces=NS))
    sections = _find_sections(paragraphs)
    editable = _build_editable_context(paragraphs, sections)
    if not editable:
        raise ResumeGenerationError("No editable resume paragraphs were found in the master DOCX")
    return root, paragraphs, sections, editable


def extract_docx_text(docx_path: Path) -> str:
    """Extract visible paragraph text from the master/generated DOCX."""
    _, paragraphs, _, _ = _parse_document(docx_path)
    return "\n".join(_paragraph_text(paragraph) for paragraph in paragraphs)


def _replace_paragraph_text(paragraph: Any, text: str) -> None:
    """Replace only visible paragraph text while preserving paragraph/run formatting."""
    runs = list(paragraph.xpath("./w:r", namespaces=NS))
    run_properties = None
    if runs:
        properties = runs[0].find(W + "rPr")
        if properties is not None:
            run_properties = deepcopy(properties)

    for child in list(paragraph):
        if child.tag != W + "pPr":
            paragraph.remove(child)

    run = etree.Element(W + "r")
    if run_properties is not None:
        run.append(run_properties)

    segments = text.split("\n")
    for index, segment in enumerate(segments):
        if index:
            run.append(etree.Element(W + "br"))
        text_node = etree.Element(W + "t")
        if segment.startswith(" ") or segment.endswith(" "):
            text_node.set(f"{{{XML_NS}}}space", "preserve")
        text_node.text = segment
        run.append(text_node)
    paragraph.append(run)


def _paragraph_property_xml(paragraph: Any) -> bytes:
    properties = paragraph.find(W + "pPr")
    return etree.tostring(properties) if properties is not None else b""


def _write_patched_docx(
    master_docx_path: Path,
    output_docx_path: Path,
    edit_map: dict[str, str],
) -> None:
    root, paragraphs, _, editable = _parse_document(master_docx_path)
    editable_by_id = {entry["id"]: entry for entry in editable}
    expected_ids = set(editable_by_id)
    actual_ids = set(edit_map)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ResumeGenerationError(
            f"LLM edits do not match editable master paragraphs; missing={missing}, extra={extra}"
        )

    editable_indexes = {entry["index"] for entry in editable}
    locked_snapshot = {
        index: etree.tostring(paragraph)
        for index, paragraph in enumerate(paragraphs)
        if index not in editable_indexes
    }
    editable_format_snapshot = {
        index: _paragraph_property_xml(paragraphs[index]) for index in editable_indexes
    }

    for entry in editable:
        _replace_paragraph_text(paragraphs[entry["index"]], edit_map[entry["id"]])

    updated_xml = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    output_docx_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(master_docx_path, "r") as source, zipfile.ZipFile(
            output_docx_path, "w"
        ) as target:
            for info in source.infolist():
                data = (
                    updated_xml
                    if info.filename == "word/document.xml"
                    else source.read(info.filename)
                )
                target.writestr(info, data)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ResumeGenerationError(f"Unable to write generated DOCX: {exc}") from exc

    _, generated_paragraphs, _, generated_editable = _parse_document(output_docx_path)
    generated_editable_indexes = {entry["index"] for entry in generated_editable}
    generated_locked_snapshot = {
        index: etree.tostring(paragraph)
        for index, paragraph in enumerate(generated_paragraphs)
        if index not in generated_editable_indexes
    }
    if locked_snapshot != generated_locked_snapshot:
        changed = [
            index
            for index, xml in locked_snapshot.items()
            if generated_locked_snapshot.get(index) != xml
        ]
        raise ResumeGenerationError(
            f"Locked resume content or formatting changed at paragraphs: {changed[:10]}"
        )

    for index, original_properties in editable_format_snapshot.items():
        if index >= len(generated_paragraphs):
            raise ResumeGenerationError("Generated DOCX paragraph structure changed unexpectedly")
        if _paragraph_property_xml(generated_paragraphs[index]) != original_properties:
            raise ResumeGenerationError(
                f"Editable paragraph formatting changed unexpectedly at paragraph {index}"
            )


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ResumeGenerationError("LLM did not return a JSON object")
    return cleaned[start : end + 1]


def _parse_llm_response(raw_response: str, editable: list[dict]) -> dict:
    try:
        payload = json.loads(_strip_json_fence(raw_response))
    except json.JSONDecodeError as exc:
        raise ResumeGenerationError(f"LLM returned invalid JSON: {exc}") from exc

    edits = payload.get("edits")
    coverage_terms = payload.get("coverage_terms")
    if not isinstance(edits, list) or not isinstance(coverage_terms, list):
        raise ResumeGenerationError("LLM JSON must contain edits and coverage_terms arrays")

    expected_ids = {entry["id"] for entry in editable}
    edit_map: dict[str, str] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise ResumeGenerationError("Each LLM edit must be an object")
        edit_id = edit.get("id")
        text = edit.get("text")
        if not isinstance(edit_id, str) or not isinstance(text, str) or not text.strip():
            raise ResumeGenerationError("Each LLM edit must contain a non-empty id and text")
        if edit_id in edit_map:
            raise ResumeGenerationError(f"LLM returned duplicate edit id: {edit_id}")
        edit_map[edit_id] = text

    if set(edit_map) != expected_ids:
        missing = sorted(expected_ids - set(edit_map))
        extra = sorted(set(edit_map) - expected_ids)
        raise ResumeGenerationError(
            f"LLM response does not cover every editable paragraph; missing={missing}, extra={extra}"
        )

    editable_by_id = {entry["id"]: entry for entry in editable}
    for edit_id, text in list(edit_map.items()):
        entry = editable_by_id[edit_id]
        if entry["kind"] != "certifications":
            continue
        original_items = Counter(_certification_items(entry["original"]))
        generated_items = Counter(_certification_items(text))
        if generated_items != original_items:
            edit_map[edit_id] = entry["original"]

    normalized_terms: list[dict] = []
    for item in coverage_terms:
        if not isinstance(item, dict):
            raise ResumeGenerationError("Each coverage term must be an object")
        group = item.get("group")
        term = item.get("term")
        jd_count = item.get("jd_count")
        target_count = item.get("target_count")
        if group not in {"a", "b", "c", "d", "e"} or not isinstance(term, str) or not term:
            raise ResumeGenerationError("Coverage terms must use group a-e and a non-empty term")
        if not isinstance(jd_count, int) or jd_count < 0:
            raise ResumeGenerationError(f"Invalid jd_count for coverage term: {term}")
        if not isinstance(target_count, int) or target_count < 1:
            raise ResumeGenerationError(f"Invalid target_count for coverage term: {term}")
        normalized_terms.append(
            {
                "group": group,
                "term": term,
                "jd_count": jd_count,
                "target_count": target_count,
            }
        )

    if not normalized_terms:
        raise ResumeGenerationError("LLM returned an empty coverage term table")

    return {
        "edit_map": edit_map,
        "coverage_terms": normalized_terms,
        "notes": payload.get("notes", ""),
    }


def _certification_items(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"\s*\|\s*|\n+", text) if item.strip()]


def _validate_certifications(
    master_docx_path: Path,
    generated_docx_path: Path,
) -> list[str]:
    _, master_paragraphs, master_sections, _ = _parse_document(master_docx_path)
    _, generated_paragraphs, generated_sections, _ = _parse_document(generated_docx_path)

    def section_items(paragraphs: list[Any], sections: dict[str, int]) -> list[str]:
        text = "\n".join(
            _paragraph_text(paragraph)
            for paragraph in paragraphs[
                sections["CERTIFICATIONS"] + 1 : sections["EDUCATION"]
            ]
            if _paragraph_text(paragraph).strip()
        )
        return _certification_items(text)

    original = Counter(section_items(master_paragraphs, master_sections))
    generated = Counter(section_items(generated_paragraphs, generated_sections))
    if original != generated:
        return ["Certification wording changed; certifications may only be reordered"]
    return []


def _validate_term_table(job_title: str, jd_text: str, coverage_terms: list[dict]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in coverage_terms:
        group = item["group"]
        term = item["term"]
        key = (group, term)
        if key in seen:
            errors.append(f"Duplicate coverage term: {term}")
            continue
        seen.add(key)

        expected_count = job_title.count(term) if group == "a" else jd_text.count(term)
        if expected_count != item["jd_count"]:
            errors.append(
                f"Coverage table count mismatch for '{term}': expected {expected_count}, "
                f"LLM reported {item['jd_count']}"
            )
    if not any(item["group"] == "a" for item in coverage_terms):
        errors.append("Coverage table is missing the exact job title (group a)")
    return errors


def _set_deterministic_term_counts(
    job_title: str, jd_text: str, coverage_terms: list[dict]
) -> None:
    """Replace model-reported occurrence counts with literal machine counts."""
    for item in coverage_terms:
        group = item["group"]
        term = item["term"]
        item["jd_count"] = job_title.count(term) if group == "a" else jd_text.count(term)


def _validate_coverage(
    resume_text: str, coverage_terms: list[dict]
) -> tuple[list[str], list[dict]]:
    errors: list[str] = []
    report: list[dict] = []
    for item in coverage_terms:
        term = item["term"]
        actual = resume_text.count(term)
        target = item["target_count"]
        report.append({**item, "resume_count": actual})
        if item["group"] != "e" and actual < target:
            errors.append(f"'{term}' appears {actual} time(s); target is {target}")
    return errors, report


def _validate_edit_lengths(editable: list[dict], edit_map: dict[str, str]) -> list[str]:
    """Reject expansion that is likely to move the resume's fixed page break."""
    errors: list[str] = []
    for entry in editable:
        original_length = len(entry["original"])
        generated_length = len(edit_map[entry["id"]])
        proportional_limit = (original_length * 105 + 99) // 100
        maximum_length = max(original_length + 5, proportional_limit)
        if generated_length > maximum_length:
            errors.append(
                f"Paragraph {entry['id']} is {generated_length} characters; "
                f"maximum is {maximum_length}. Shorten it by replacing wording"
            )
    return errors


def _first_nonempty_line(page_text: str) -> str:
    for line in page_text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            return cleaned
    return ""


def _preserve_page_two_opening_paragraph(
    baseline_pdf: Path, editable: list[dict], edit_map: dict[str, str]
) -> None:
    """Keep the editable paragraph that begins page two byte-for-byte unchanged."""
    try:
        reader = PdfReader(str(baseline_pdf))
    except Exception as exc:
        logger.warning("Could not inspect the baseline PDF page break: %s", exc)
        return
    if len(reader.pages) < 2:
        return
    expected = _first_nonempty_line(reader.pages[1].extract_text() or "")
    if not expected:
        return
    for entry in editable:
        original = re.sub(r"\s+", " ", entry["original"]).strip()
        if original.startswith(expected) or expected.startswith(original):
            edit_map[entry["id"]] = entry["original"]
            return


def _validate_pdf_layout(master_pdf_path: Path, generated_pdf_path: Path) -> list[str]:
    master_reader = PdfReader(str(master_pdf_path))
    generated_reader = PdfReader(str(generated_pdf_path))
    errors: list[str] = []

    if len(master_reader.pages) != len(generated_reader.pages):
        errors.append(
            f"PDF page count changed from {len(master_reader.pages)} to "
            f"{len(generated_reader.pages)}"
        )
        return errors

    if len(master_reader.pages) >= 2:
        expected = _first_nonempty_line(master_reader.pages[1].extract_text() or "")
        actual = _first_nonempty_line(generated_reader.pages[1].extract_text() or "")
        if expected and actual != expected:
            errors.append(f"Page 2 must start with '{expected}', but starts with '{actual}'")
    return errors


def convert_docx_to_pdf(docx_path: Path, pdf_path: Path) -> None:
    """Convert DOCX to PDF with LibreOffice, or Microsoft Word on Windows."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    converter = (
        os.environ.get("AUTOAPPLY_OFFICE_CONVERTER")
        or shutil.which("soffice")
        or shutil.which("libreoffice")
    )

    if not converter and os.name == "nt":
        for candidate in (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "LibreOffice"
            / "program"
            / "soffice.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "LibreOffice"
            / "program"
            / "soffice.exe",
        ):
            if candidate.is_file():
                converter = str(candidate)
                break

    if converter:
        with tempfile.TemporaryDirectory(prefix="autoapply-pdf-") as temp_dir:
            result = subprocess.run(
                [
                    converter,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    temp_dir,
                    str(docx_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            generated = Path(temp_dir) / f"{docx_path.stem}.pdf"
            if result.returncode == 0 and generated.exists():
                shutil.copy2(generated, pdf_path)
                return
            message = result.stderr.strip() or result.stdout.strip() or "unknown converter error"
            raise ResumeGenerationError(f"DOCX to PDF conversion failed: {message}")

    if os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            script = (
                "$ErrorActionPreference='Stop';"
                "$word=New-Object -ComObject Word.Application;"
                "$word.Visible=$false;"
                "try {$doc=$word.Documents.Open($args[0]);"
                "$doc.ExportAsFixedFormat($args[1],17);$doc.Close($false)}"
                "finally {$word.Quit()}"
            )
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                    str(docx_path.resolve()),
                    str(pdf_path.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode == 0 and pdf_path.exists():
                return
            logger.debug("Microsoft Word PDF conversion failed: %s", result.stderr.strip())

    raise ResumeGenerationError(
        "No DOCX-to-PDF converter is available. Install LibreOffice, Microsoft Word "
        "on Windows, or set AUTOAPPLY_OFFICE_CONVERTER."
    )


def _safe_filename_part(text: str) -> str:
    parts = re.findall(r"[A-Za-z0-9+#]+", text)
    return "-".join(parts) or "Resume"


def _output_base_name(job_title: str, applicant_name: str) -> str:
    title_words = re.findall(r"[A-Za-z0-9+#]+", job_title)[:3]
    title_part = "-".join(title_words) or "Job"
    name_part = _safe_filename_part(applicant_name)
    return f"{title_part}-Detailed-Resume-{name_part}"


def _build_initial_prompt(
    job_title: str,
    jd_text: str,
    instructions_text: str,
    master_text: str,
    editable: list[dict],
) -> str:
    editable_json = json.dumps(editable, ensure_ascii=False, indent=2)
    return f"""You are editing an existing master resume for a specific job.

Follow the RESUME INSTRUCTIONS below as authoritative. Do not create a new resume.
The automation will apply your text edits to the existing DOCX and will reject any
change outside the listed editable paragraphs.

Additional machine constraints:
- Return exactly one edit for every editable paragraph ID, even if unchanged.
- Do not add or remove paragraph IDs. Paragraph count and formatting are fixed.
- Keep every replacement at or below the original paragraph's character count whenever
  possible; never exceed it by more than 5%. Replace irrelevant wording to make room.
- Job/company/title/date/client/education text is locked and cannot be changed.
- Certifications may only be reordered; preserve each certification's exact wording.
- Build the term table required by the instructions and return groups (a)-(e) as
  coverage_terms. Use exact JD strings and exact case/punctuation for groups (a)-(d),
  and standard industry wording for the predicted-skill terms in group (e).
- For each coverage term, jd_count is its exact occurrence count in the job title
  for group (a), otherwise in the JD below.
- target_count is the minimum exact occurrence count the final resume must contain.
- Output JSON only. No Markdown fences or explanation.

Required JSON shape:
{{
  "edits": [{{"id": "p5", "text": "replacement text"}}],
  "coverage_terms": [
    {{"group": "a", "term": "Exact Job Title", "jd_count": 1, "target_count": 1}}
  ],
  "notes": "optional short validation note"
}}

RESUME INSTRUCTIONS:
{instructions_text}

EXACT JOB TITLE:
{job_title}

JOB DESCRIPTION:
{jd_text}

MASTER RESUME TEXT (locked except editable paragraphs):
{master_text}

EDITABLE PARAGRAPHS:
{editable_json}
"""


def _build_correction_prompt(
    initial_prompt: str,
    previous_response: str,
    validation_errors: list[str],
) -> str:
    error_text = "\n".join(f"- {error}" for error in validation_errors)
    return f"""{initial_prompt}

The previous JSON failed deterministic validation. Correct it once, keeping the same
JSON schema and exactly the same editable paragraph IDs.

VALIDATION FAILURES:
{error_text}

PREVIOUS JSON:
{previous_response}

Return corrected JSON only.
"""


def generate_docx_resume(
    master_docx_path: Path,
    job_title: str,
    jd_text: str,
    instructions_text: str,
    output_dir: Path,
    applicant_name: str,
    llm_config: Any,
    max_corrections: int = 3,
) -> tuple[Path, Path, dict]:
    """Generate a job-specific DOCX/PDF by editing only approved master paragraphs."""
    if master_docx_path.suffix.lower() != ".docx":
        raise ResumeGenerationError("Master resume must be a DOCX for deterministic editing")
    if not master_docx_path.exists():
        raise ResumeGenerationError(f"Master resume does not exist: {master_docx_path}")
    if not job_title.strip() or not jd_text.strip():
        raise ResumeGenerationError("Job title and job description are required")
    if not instructions_text.strip():
        raise ResumeGenerationError("Resume instruction file is empty")

    from core.ai_engine import invoke_llm

    _, _, _, editable = _parse_document(master_docx_path)
    master_text = extract_docx_text(master_docx_path)
    prompt = _build_initial_prompt(job_title, jd_text, instructions_text, master_text, editable)

    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _output_base_name(job_title, applicant_name)
    output_docx_path = output_dir / f"{base_name}.docx"
    output_pdf_path = output_dir / f"{base_name}.pdf"

    with tempfile.TemporaryDirectory(prefix="autoapply-master-resume-") as temp_dir:
        baseline_pdf = Path(temp_dir) / "master.pdf"
        convert_docx_to_pdf(master_docx_path, baseline_pdf)

        previous_response = ""
        last_errors: list[str] = []
        coverage_report: list[dict] = []

        for attempt in range(max_corrections + 1):
            current_prompt = prompt
            if attempt and previous_response:
                current_prompt = _build_correction_prompt(prompt, previous_response, last_errors)

            previous_response = invoke_llm(current_prompt, llm_config)
            try:
                parsed = _parse_llm_response(previous_response, editable)
            except ResumeGenerationError as exc:
                last_errors = [str(exc)]
                if attempt < max_corrections:
                    continue
                break

            _set_deterministic_term_counts(
                job_title, jd_text, parsed["coverage_terms"]
            )
            term_table_errors = _validate_term_table(
                job_title, jd_text, parsed["coverage_terms"]
            )
            length_errors = _validate_edit_lengths(editable, parsed["edit_map"])
            if length_errors:
                last_errors = term_table_errors + length_errors
                if attempt < max_corrections:
                    continue
                break
            _preserve_page_two_opening_paragraph(
                baseline_pdf, editable, parsed["edit_map"]
            )
            _write_patched_docx(master_docx_path, output_docx_path, parsed["edit_map"])
            convert_docx_to_pdf(output_docx_path, output_pdf_path)

            resume_text = extract_docx_text(output_docx_path)
            coverage_errors, coverage_report = _validate_coverage(
                resume_text, parsed["coverage_terms"]
            )
            last_errors = (
                term_table_errors
                + _validate_certifications(master_docx_path, output_docx_path)
                + coverage_errors
                + _validate_pdf_layout(baseline_pdf, output_pdf_path)
            )
            if not last_errors:
                provider = getattr(llm_config, "provider", None)
                model = getattr(llm_config, "model", None)
                return output_docx_path, output_pdf_path, {
                    "resume_docx_path": str(output_docx_path),
                    "resume_pdf_path": str(output_pdf_path),
                    "llm_provider": provider,
                    "llm_model": model,
                    "coverage_terms": coverage_report,
                    "notes": parsed.get("notes", ""),
                    "correction_attempts": attempt,
                }

    output_docx_path.unlink(missing_ok=True)
    output_pdf_path.unlink(missing_ok=True)
    raise ResumeGenerationError(
        "Generated resume failed validation after correction pass: " + "; ".join(last_errors)
    )
