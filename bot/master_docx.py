"""Master DOCX document pipeline used by the bot when a DOCX resume is configured."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def get_master_docx_path(config) -> Path | None:
    configured = getattr(config.profile, "fallback_resume_path", None)
    if not configured:
        return None
    path = Path(configured)
    if path.suffix.lower() != ".docx" or not path.exists():
        return None
    return path


def _load_resume_instructions(profile_dir: Path) -> str:
    candidates = (
        profile_dir / "resume_instructions.md",
        profile_dir / "MASTER RESUME EDIT PROMPT.md",
    )
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    raise RuntimeError(
        "Master DOCX mode requires ~/.autoapply/profile/resume_instructions.md "
        "containing the resume-edit instructions."
    )


def _safe_file_part(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return value or "job"


def _generate_cover_letter(scored, config, profile_dir: Path) -> tuple[Path | None, str]:
    if not config.bot.cover_letter_enabled:
        return None, ""

    from core.ai_engine import COVER_LETTER_PROMPT, invoke_llm, read_all_experience_files

    experience_content = read_all_experience_files(profile_dir / "experiences")
    text = invoke_llm(
        COVER_LETTER_PROMPT.format(
            experience_files_content=experience_content,
            job_description=scored.raw.description,
            full_name=config.profile.full_name,
            bio=config.profile.bio,
        ),
        config.llm,
    )
    output_dir = profile_dir / "cover_letters"
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = output_dir / (
        f"{scored.id}_{_safe_file_part(scored.raw.company)}_{date_str}.txt"
    )
    path.write_text(text, encoding="utf-8")
    return path, text


def generate_master_documents(scored, config, profile_dir: Path):
    """Generate the validated DOCX/PDF resume and optional cover letter."""
    from core.docx_resume_engine import generate_docx_resume

    master_path = get_master_docx_path(config)
    if master_path is None:
        raise RuntimeError("Configured master resume is not an available DOCX file")

    instructions = _load_resume_instructions(profile_dir)
    docx_path, pdf_path, engine_meta = generate_docx_resume(
        master_docx_path=master_path,
        job_title=scored.raw.title,
        jd_text=scored.raw.description,
        instructions_text=instructions,
        output_dir=profile_dir / "resumes",
        applicant_name=config.profile.full_name,
        llm_config=config.llm,
    )
    cl_path, cover_letter_text = _generate_cover_letter(scored, config, profile_dir)

    version_meta = {
        "resume_md_path": "",
        "resume_docx_path": str(docx_path),
        "resume_pdf_path": str(pdf_path),
        "llm_provider": engine_meta.get("llm_provider"),
        "llm_model": engine_meta.get("llm_model"),
        "reuse_source": "master_docx",
        "source_entry_ids": [],
        "coverage_terms": engine_meta.get("coverage_terms", []),
        "correction_attempts": engine_meta.get("correction_attempts", 0),
    }
    return pdf_path, cl_path, cover_letter_text, version_meta
