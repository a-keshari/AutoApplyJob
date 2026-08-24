"""Tests for LLM-backed LinkedIn Easy Apply screening questions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.apply.linkedin import LinkedInApplier
from core.linkedin_screening import (
    build_screening_prompt,
    parse_screening_answers,
    saved_answer_fallback,
)


def _profile(**overrides):
    profile = MagicMock()
    profile.full_name = "Jane Doe"
    profile.first_name = "Jane"
    profile.last_name = "Doe"
    profile.email = "jane@example.com"
    profile.phone_full = "+15550100"
    profile.location = "Austin, TX"
    profile.city = "Austin"
    profile.state = "TX"
    profile.zip_code = "78701"
    profile.country = "United States"
    profile.linkedin_url = "https://linkedin.com/in/jane"
    profile.portfolio_url = ""
    profile.bio = "Engineer"
    profile.fallback_resume_path = None
    profile.screening_answers = overrides.get(
        "screening_answers",
        {"work authorization": "Yes", "years of python": "9"},
    )
    return profile


def _job():
    job = MagicMock()
    job.raw.title = "Senior Engineer"
    job.raw.company = "Acme"
    job.raw.description = "Python and Azure are required."
    return job


def test_parse_screening_answers_handles_text_and_checkbox():
    questions = [
        {"id": "q1", "type": "radio", "question": "Authorized?", "options": []},
        {"id": "q2", "type": "checkbox", "question": "Skills", "options": []},
    ]
    raw = '{"answers":[{"id":"q1","answer":"Yes"},{"id":"q2","answer":["Python","Azure"]}]}'
    parsed = parse_screening_answers(raw, questions)
    by_id = {item["id"]: item["answer"] for item in parsed}
    assert by_id["q1"] == "Yes"
    assert by_id["q2"] == ["Python", "Azure"]


def test_parse_screening_answers_ignores_unknown_ids():
    questions = [{"id": "q1", "type": "text", "question": "Why?", "options": []}]
    parsed = parse_screening_answers(
        '{"answers":[{"id":"bogus","answer":"x"},{"id":"q1","answer":"Because"}]}',
        questions,
    )
    assert parsed == [{"id": "q1", "answer": "Because"}]


def test_numeric_experience_is_capped_at_nine():
    questions = [{
        "id": "q1", "type": "number",
        "question": "How many years of Python experience do you have?", "options": [],
    }]
    parsed = parse_screening_answers(
        '{"answers":[{"id":"q1","answer":"12"}]}', questions
    )
    assert parsed == [{"id": "q1", "answer": "9"}]


def test_blank_multiple_choice_uses_neutral_option_when_available():
    questions = [{
        "id": "q1", "type": "radio", "question": "Voluntary disclosure",
        "options": [
            {"label": "Yes", "value": "yes"},
            {"label": "Prefer not to answer", "value": "decline"},
        ],
    }]
    parsed = parse_screening_answers('{"answers":[{"id":"q1","answer":""}]}', questions)
    assert parsed == [{"id": "q1", "answer": "Prefer not to answer"}]


def test_saved_answer_fallback_matches_question_text():
    questions = [
        {"id": "q1", "question": "Are you legally authorized for work authorization?"},
        {"id": "q2", "question": "How many years of Python experience do you have?"},
        {"id": "q3", "question": "Email address"},
    ]
    answers = saved_answer_fallback(_profile(), questions)
    by_id = {item["id"]: item["answer"] for item in answers}
    assert by_id["q1"] == "Yes"
    assert by_id["q2"] == "9"
    assert by_id["q3"] == "jane@example.com"


def test_prompt_contains_all_questions_context_and_rules():
    questions = [{
        "id": "q1", "type": "select", "question": "Need sponsorship?",
        "options": [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}],
    }]
    prompt = build_screening_prompt(_job(), _profile(), questions, "PRIVATE RULES")
    assert "PRIVATE RULES" in prompt
    assert "Need sponsorship?" in prompt
    assert "Python and Azure are required." in prompt
    assert "more than 9 years" in prompt.lower()
    assert "saved_screening_answers" in prompt


def test_applier_collects_questions_with_dom_script():
    page = MagicMock()
    expected = [{"id": "q1", "type": "textarea", "question": "Explain", "options": []}]
    page.evaluate.return_value = expected
    applier = LinkedInApplier(page)
    assert applier._collect_visible_questions() == expected


@patch("bot.apply.base.time.sleep")
@patch("core.linkedin_screening.answer_screening_questions")
def test_applier_batches_questions_through_llm(mock_answer, _sleep):
    page = MagicMock()
    questions = [
        {"id": "q1", "type": "radio", "question": "Authorized?", "options": []},
        {"id": "q2", "type": "number", "question": "Years?", "options": []},
    ]
    page.evaluate.side_effect = [questions, [{"id": "q1", "filled": True}], []]
    mock_answer.return_value = [
        {"id": "q1", "answer": "Yes"},
        {"id": "q2", "answer": "9"},
    ]

    applier = LinkedInApplier(page)
    applier._llm_config = MagicMock(api_key="test-key")
    applier._llm_config_loaded = True
    applier._answer_visible_questions(_job(), _profile())

    mock_answer.assert_called_once()
    assert mock_answer.call_args.args[2] == questions
    assert page.evaluate.call_count == 3


@patch("bot.apply.base.time.sleep")
@patch("core.linkedin_screening.saved_answer_fallback")
@patch("core.linkedin_screening.answer_screening_questions")
def test_applier_falls_back_when_llm_fails(mock_answer, mock_fallback, _sleep):
    page = MagicMock()
    questions = [{"id": "q1", "type": "select", "question": "Authorized?", "options": []}]
    page.evaluate.side_effect = [questions, [], []]
    mock_answer.side_effect = RuntimeError("API down")
    mock_fallback.return_value = [{"id": "q1", "answer": "Yes"}]

    applier = LinkedInApplier(page)
    applier._llm_config = MagicMock(api_key="test-key")
    applier._llm_config_loaded = True
    profile = _profile()
    applier._answer_visible_questions(_job(), profile)

    mock_fallback.assert_called_once_with(profile, questions)
