"""LLM-backed answer generation for LinkedIn Easy Apply screening questions."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _bundled_instruction_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "templates" / "linkedin_screening_instructions.md"
    return Path(__file__).resolve().parents[1] / "templates" / "linkedin_screening_instructions.md"


def load_recruiter_instructions(profile_dir: Path | None = None) -> str:
    """Load private recruiter instructions first, then the bundled safe fallback."""
    profile_dir = profile_dir or Path.home() / ".autoapply" / "profile"
    candidates = (
        profile_dir / "MASTER RECRUITER RESPONSE WORKFLOW.md",
        profile_dir / "recruiter_response_workflow.md",
        profile_dir / "recruiter_instructions.md",
        _bundled_instruction_path(),
    )
    for path in candidates:
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError as exc:
            logger.debug("Unable to read recruiter instructions %s: %s", path, exc)
    return "Answer job-application questions truthfully from the supplied applicant context."


def _resume_context(profile) -> str:
    configured = getattr(profile, "fallback_resume_path", None)
    if not configured:
        return ""
    path = Path(configured)
    if not path.exists():
        return ""
    try:
        from core.document_parser import extract_text

        return extract_text(path)[:30000]
    except Exception as exc:
        logger.debug("Resume context unavailable for screening answers: %s", exc)
        return ""


def _profile_context(profile) -> dict[str, Any]:
    """Return only application-relevant profile data, never API/auth configuration."""
    return {
        "full_name": getattr(profile, "full_name", ""),
        "first_name": getattr(profile, "first_name", ""),
        "last_name": getattr(profile, "last_name", ""),
        "email": getattr(profile, "email", ""),
        "phone": getattr(profile, "phone_full", ""),
        "location": getattr(profile, "location", ""),
        "city": getattr(profile, "city", ""),
        "state": getattr(profile, "state", ""),
        "zip_code": getattr(profile, "zip_code", ""),
        "country": getattr(profile, "country", ""),
        "linkedin_url": getattr(profile, "linkedin_url", "") or "",
        "portfolio_url": getattr(profile, "portfolio_url", "") or "",
        "bio": getattr(profile, "bio", ""),
        "saved_screening_answers": getattr(profile, "screening_answers", {}) or {},
    }


def build_screening_prompt(job, profile, questions: list[dict], instructions: str) -> str:
    """Build one batched prompt for every unanswered control on the current form step."""
    profile_json = json.dumps(_profile_context(profile), ensure_ascii=False, indent=2, default=str)
    questions_json = json.dumps(questions, ensure_ascii=False, indent=2, default=str)
    resume_text = _resume_context(profile)
    return f"""You are answering LinkedIn Easy Apply screening questions for this applicant.

Use the RECRUITER RESPONSE INSTRUCTIONS as authoritative, then the applicant profile,
resume, and job description. Answer every question you can determine truthfully.
Do not invent employers, degrees, certifications, dates, work authorization, compensation,
or years of experience. Never report more than 9 years for a numeric experience answer.
For sensitive identifiers (SSN, passport, driver's-license number, date of birth, immigration
receipt/document numbers), use a value ONLY when it is explicitly present in
saved_screening_answers or the private recruiter instructions. Otherwise use an empty answer.

CONTROL RULES:
- select/radio/combobox: answer with exactly one provided option label/value.
- checkbox: answer with an array containing zero or more exact option labels/values.
- number: return only a numeric value, without prose or units.
- date: use the format requested by the question/control when it is known.
- text/textarea/email/tel/url: concise text only.
- If a question is already answered in saved_screening_answers, prefer that explicit answer.
- Preserve truthfulness even when an answer may reduce the chance of selection.

Return JSON only in this exact shape:
{{"answers":[{{"id":"q1","answer":"value"}},{{"id":"q2","answer":["A","B"]}}]}}
Return one entry for every supplied question id. Use an empty string/list only when the
answer cannot be truthfully determined from the supplied context.

RECRUITER RESPONSE INSTRUCTIONS:
{instructions}

APPLICANT PROFILE AND SAVED ANSWERS:
{profile_json}

RESUME CONTEXT:
{resume_text}

JOB TITLE:
{getattr(job.raw, 'title', '')}

COMPANY:
{getattr(job.raw, 'company', '')}

JOB DESCRIPTION:
{getattr(job.raw, 'description', '')}

VISIBLE LINKEDIN QUESTIONS:
{questions_json}
"""


def _strip_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM did not return a JSON object")
    return cleaned[start : end + 1]


def _normalize(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _neutral_option(question: dict) -> str:
    preferences = (
        "prefer not to answer",
        "prefer not to say",
        "decline to answer",
        "decline to self identify",
        "do not wish to answer",
        "not applicable",
        "n a",
        "other",
    )
    options = question.get("options") or []
    for preference in preferences:
        for option in options:
            label = str(option.get("label", ""))
            value = str(option.get("value", ""))
            if preference in _normalize(label) or preference in _normalize(value):
                return label or value
    return ""


def _normalize_answer(question: dict, answer: Any) -> Any:
    """Apply deterministic guardrails after the LLM response."""
    qtype = str(question.get("type", ""))
    qtext = _normalize(question.get("question", ""))

    if qtype == "number" and isinstance(answer, (str, int, float)):
        text = str(answer).strip()
        match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
        if match and "year" in qtext and "experience" in qtext:
            value = float(text)
            if value > 9:
                return "9"

    if answer in (None, "", []):
        if qtype in {"select", "radio", "combobox"}:
            return _neutral_option(question)
        return [] if qtype == "checkbox" else ""

    if qtype in {"select", "radio", "combobox"}:
        target = _normalize(answer[0] if isinstance(answer, list) and answer else answer)
        for option in question.get("options") or []:
            label = str(option.get("label", ""))
            value = str(option.get("value", ""))
            if target in {_normalize(label), _normalize(value)}:
                return label or value

    return answer


def parse_screening_answers(raw: str, questions: list[dict]) -> list[dict]:
    """Validate LLM JSON and normalize it to the supplied question ids."""
    payload = json.loads(_strip_json(raw))
    raw_answers = payload.get("answers", [])
    if not isinstance(raw_answers, list):
        raise ValueError("LLM response must contain an answers array")

    allowed = {str(question.get("id")) for question in questions}
    by_id: dict[str, Any] = {}
    for item in raw_answers:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id", ""))
        if qid in allowed and qid not in by_id:
            answer = item.get("answer", "")
            if isinstance(answer, (str, int, float, bool, list)):
                by_id[qid] = answer

    return [
        {
            "id": str(question.get("id", "")),
            "answer": _normalize_answer(question, by_id.get(str(question.get("id", "")), "")),
        }
        for question in questions
    ]


def saved_answer_fallback(profile, questions: list[dict]) -> list[dict]:
    """Best-effort local fallback when the LLM is unavailable."""
    saved = getattr(profile, "screening_answers", {}) or {}
    candidates: dict[str, Any] = {
        **saved,
        "full name": getattr(profile, "full_name", ""),
        "first name": getattr(profile, "first_name", ""),
        "last name": getattr(profile, "last_name", ""),
        "email": getattr(profile, "email", ""),
        "phone": getattr(profile, "phone_full", ""),
        "location": getattr(profile, "location", ""),
        "city": getattr(profile, "city", ""),
        "state": getattr(profile, "state", ""),
        "zip code": getattr(profile, "zip_code", ""),
        "country": getattr(profile, "country", ""),
        "linkedin": getattr(profile, "linkedin_url", "") or "",
        "portfolio": getattr(profile, "portfolio_url", "") or "",
    }
    normalized = [
        (_normalize(key), value) for key, value in candidates.items() if value not in (None, "")
    ]

    answers: list[dict] = []
    for question in questions:
        qid = str(question.get("id", ""))
        qtext = _normalize(question.get("question", ""))
        answer: Any = ""
        for key, value in normalized:
            if key and (key in qtext or qtext in key):
                answer = value
                break
        answers.append({"id": qid, "answer": _normalize_answer(question, answer)})
    return answers


def answer_screening_questions(job, profile, questions: list[dict], llm_config) -> list[dict]:
    """Ask the configured LLM for one batched set of answers."""
    if not questions:
        return []
    from core.ai_engine import invoke_llm

    instructions = load_recruiter_instructions()
    prompt = build_screening_prompt(job, profile, questions, instructions)
    raw = invoke_llm(prompt, llm_config)
    return parse_screening_answers(raw, questions)
