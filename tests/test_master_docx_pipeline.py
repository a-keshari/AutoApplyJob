"""Focused tests for the master-DOCX bot integration."""

from pathlib import Path
from unittest.mock import MagicMock, patch


def _config(master_path: Path):
    config = MagicMock()
    config.profile.fallback_resume_path = str(master_path)
    config.bot.cover_letter_enabled = True
    config.bot.cover_letter_template = "Fallback cover letter"
    return config


def _scored():
    scored = MagicMock()
    scored.raw.title = "Power Platform Architect"
    scored.raw.company = "Example Corp"
    scored.raw.description = "Power Platform architecture"
    scored.id = "job-1"
    return scored


def test_generate_docs_prefers_master_docx(tmp_path):
    from bot.bot import _generate_docs

    master = tmp_path / "master.docx"
    master.write_bytes(b"docx")
    config = _config(master)
    expected = (tmp_path / "resume.pdf", None, "cover", {"reuse_source": "master_docx"})

    with (
        patch("bot.master_docx.generate_master_documents", return_value=expected) as generate_master,
        patch("bot.bot._try_kb_assembly") as kb_assembly,
        patch("core.ai_engine.generate_documents") as standard_generate,
    ):
        result = _generate_docs(_scored(), config, tmp_path)

    assert result == expected
    generate_master.assert_called_once()
    kb_assembly.assert_not_called()
    standard_generate.assert_not_called()


def test_generate_docs_uses_master_resume_when_ai_generation_fails(tmp_path):
    from bot.bot import _generate_docs

    master = tmp_path / "master.docx"
    master.write_bytes(b"docx")
    config = _config(master)

    with (
        patch("bot.master_docx.generate_master_documents", side_effect=RuntimeError("API down")),
        patch("bot.master_docx.master_resume_fallback", return_value=master),
        patch("bot.bot._try_kb_assembly") as kb_assembly,
    ):
        resume, cl_path, cover, meta = _generate_docs(_scored(), config, tmp_path)

    assert resume == master
    assert cl_path is None
    assert cover == "Fallback cover letter"
    assert meta["reuse_source"] == "master_docx_fallback"
    kb_assembly.assert_not_called()


def test_master_resume_fallback_returns_docx_if_pdf_conversion_fails(tmp_path):
    from bot.master_docx import master_resume_fallback

    master = tmp_path / "master.docx"
    master.write_bytes(b"docx")
    config = _config(master)

    with patch(
        "core.docx_resume_engine.convert_docx_to_pdf",
        side_effect=RuntimeError("converter unavailable"),
    ):
        result = master_resume_fallback(config, tmp_path)

    assert result == master


def test_cover_letter_failure_does_not_block_application(tmp_path):
    from bot.master_docx import _cover_letter_with_fallback

    master = tmp_path / "master.docx"
    master.write_bytes(b"docx")
    config = _config(master)

    with patch("bot.master_docx._generate_cover_letter", side_effect=RuntimeError("API down")):
        cl_path, text = _cover_letter_with_fallback(_scored(), config, tmp_path)

    assert cl_path is None
    assert text == "Fallback cover letter"


def test_resume_instructions_fall_back_to_bundled_template(tmp_path):
    from bot.master_docx import _load_resume_instructions

    bundled = tmp_path / "master_resume_instructions.md"
    bundled.write_text("Use my master resume instructions", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    with patch("bot.master_docx._bundled_instruction_path", return_value=bundled):
        assert _load_resume_instructions(profile_dir) == "Use my master resume instructions"
