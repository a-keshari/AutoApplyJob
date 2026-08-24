"""LinkedIn Easy Apply automation.

Implements: FR-047 (LinkedIn Easy Apply).
"""

from __future__ import annotations

import logging

from bot.apply.base import ApplyResult, BaseApplier

logger = logging.getLogger(__name__)


_COLLECT_QUESTIONS_JS = r"""
() => {
  const root = document.querySelector('.jobs-easy-apply-modal, .artdeco-modal[role="dialog"]') || document;
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const clean = (text) => (text || '').replace(/\s+/g, ' ').replace(/\*+\s*$/, '').trim();
  const associatedLabel = (el) => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (clean(text)) return clean(text);
    }
    if (el.id) {
      const label = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && clean(label.innerText)) return clean(label.innerText);
    }
    const wrapping = el.closest('label');
    if (wrapping && clean(wrapping.innerText)) return clean(wrapping.innerText);
    return '';
  };
  const containerFor = (el) => el.closest(
    'fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element], .artdeco-form-item'
  );
  const questionText = (el, container = null) => {
    const box = container || containerFor(el);
    if (box) {
      const candidate = box.querySelector(
        'legend, .fb-dash-form-element__label, .artdeco-text-input--label, label:not([for]), .jobs-easy-apply-form-section__grouping > label'
      );
      const text = clean(candidate?.innerText);
      if (text) return text;
    }
    return associatedLabel(el) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('name')) || 'Application question';
  };
  let counter = root.querySelectorAll('[data-autoapply-qid]').length;
  const nextId = () => `aaq${++counter}`;
  const questions = [];
  const mark = (elements, qid, type) => elements.forEach(el => {
    el.dataset.autoapplyQid = qid;
    el.dataset.autoapplyQtype = type;
  });

  const radios = Array.from(root.querySelectorAll('input[type="radio"]')).filter(visible);
  const radioGroups = new Map();
  radios.forEach(el => {
    if (el.dataset.autoapplyQid) return;
    const box = containerFor(el);
    const key = el.name || box || el;
    if (!radioGroups.has(key)) radioGroups.set(key, []);
    radioGroups.get(key).push(el);
  });
  radioGroups.forEach(group => {
    if (group.some(el => el.checked)) return;
    const qid = nextId();
    const box = containerFor(group[0]);
    const options = group.map(el => ({
      label: associatedLabel(el) || clean(el.value),
      value: clean(el.value),
    })).filter(opt => opt.label || opt.value);
    mark(group, qid, 'radio');
    questions.push({id: qid, type: 'radio', question: questionText(group[0], box), options,
                    required: group.some(el => el.required)});
  });

  const checks = Array.from(root.querySelectorAll('input[type="checkbox"]')).filter(visible);
  const checkGroups = new Map();
  checks.forEach(el => {
    if (el.dataset.autoapplyQid) return;
    const box = containerFor(el);
    const key = el.name || box || el;
    if (!checkGroups.has(key)) checkGroups.set(key, []);
    checkGroups.get(key).push(el);
  });
  checkGroups.forEach(group => {
    if (group.some(el => el.checked)) return;
    const qid = nextId();
    const box = containerFor(group[0]);
    const options = group.map(el => ({
      label: associatedLabel(el) || clean(el.value) || 'Yes',
      value: clean(el.value),
    }));
    mark(group, qid, 'checkbox');
    questions.push({id: qid, type: 'checkbox', question: questionText(group[0], box), options,
                    required: group.some(el => el.required)});
  });

  Array.from(root.querySelectorAll('select')).filter(visible).forEach(el => {
    if (el.dataset.autoapplyQid || (el.value && el.selectedIndex > 0)) return;
    const qid = nextId();
    const options = Array.from(el.options)
      .filter(opt => !opt.disabled && clean(opt.textContent) && (opt.value || opt.index > 0))
      .map(opt => ({label: clean(opt.textContent), value: clean(opt.value)}));
    mark([el], qid, 'select');
    questions.push({id: qid, type: 'select', question: questionText(el), options, required: el.required});
  });

  const inputTypes = new Set(['text', 'number', 'date', 'email', 'tel', 'url', 'search']);
  Array.from(root.querySelectorAll('input, textarea')).filter(visible).forEach(el => {
    if (el.dataset.autoapplyQid) return;
    const tag = el.tagName.toLowerCase();
    const type = tag === 'textarea' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (tag !== 'textarea' && !inputTypes.has(type)) return;
    if ((el.value || '').trim()) return;
    const question = questionText(el);
    if (/cover\s*letter|resume|curriculum\s+vitae/i.test(question)) return;
    const qid = nextId();
    mark([el], qid, type);
    questions.push({id: qid, type, question, options: [], required: !!el.required,
                    min: el.min || null, max: el.max || null, step: el.step || null});
  });

  Array.from(root.querySelectorAll('[contenteditable="true"]')).filter(visible).forEach(el => {
    if (el.dataset.autoapplyQid || clean(el.innerText)) return;
    const qid = nextId();
    mark([el], qid, 'textarea');
    questions.push({id: qid, type: 'textarea', question: questionText(el), options: [],
                    required: el.getAttribute('aria-required') === 'true'});
  });

  Array.from(root.querySelectorAll('[role="combobox"]')).filter(visible).forEach(el => {
    if (el.dataset.autoapplyQid) return;
    const current = clean(el.value || el.textContent);
    if (current && !/select|choose/i.test(current)) return;
    const qid = nextId();
    mark([el], qid, 'combobox');
    questions.push({id: qid, type: 'combobox', question: questionText(el), options: [],
                    required: el.getAttribute('aria-required') === 'true'});
  });

  return questions.filter(q => q.question && !/^application question$/i.test(q.question));
}
"""


_FILL_ANSWERS_JS = r"""
(answers) => {
  const normalize = (value) => String(value ?? '').trim().toLowerCase();
  const fire = (el) => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const setNativeValue = (el, value) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, String(value)); else el.value = String(value);
    fire(el);
  };
  const labelFor = (el) => {
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return normalize(label.innerText);
    }
    return normalize(el.closest('label')?.innerText || el.getAttribute('aria-label') || el.value);
  };
  const results = [];
  for (const item of answers || []) {
    const qid = String(item.id || '');
    const controls = Array.from(document.querySelectorAll(`[data-autoapply-qid="${CSS.escape(qid)}"]`));
    if (!controls.length) { results.push({id: qid, filled: false}); continue; }
    const type = controls[0].dataset.autoapplyQtype || controls[0].type || 'text';
    const answer = item.answer;
    let filled = false;
    if (['text','textarea','number','date','email','tel','url','search'].includes(type)) {
      const value = Array.isArray(answer) ? answer.join(', ') : answer;
      if (String(value ?? '').trim()) {
        if (controls[0].isContentEditable) {
          controls[0].innerText = String(value); fire(controls[0]);
        } else setNativeValue(controls[0], value);
        filled = true;
      }
    } else if (type === 'select') {
      const target = normalize(Array.isArray(answer) ? answer[0] : answer);
      const select = controls[0];
      const option = Array.from(select.options).find(opt =>
        normalize(opt.textContent) === target || normalize(opt.value) === target ||
        normalize(opt.textContent).includes(target) || target.includes(normalize(opt.textContent))
      );
      if (option && target) { select.value = option.value; fire(select); filled = true; }
    } else if (type === 'radio') {
      const target = normalize(Array.isArray(answer) ? answer[0] : answer);
      const choice = controls.find(el => labelFor(el) === target || normalize(el.value) === target ||
        labelFor(el).includes(target) || target.includes(labelFor(el)));
      if (choice && target) { choice.click(); filled = true; }
    } else if (type === 'checkbox') {
      const targets = (Array.isArray(answer) ? answer : [answer]).map(normalize).filter(Boolean);
      controls.forEach(el => {
        const label = labelFor(el); const value = normalize(el.value);
        if (targets.some(t => t === label || t === value || label.includes(t) || t.includes(label))) {
          if (!el.checked) el.click(); filled = true;
        }
      });
    } else if (type === 'combobox') {
      const value = Array.isArray(answer) ? answer[0] : answer;
      if (String(value ?? '').trim()) {
        const el = controls[0];
        if ('value' in el) setNativeValue(el, value);
        else { el.focus(); el.click(); }
        filled = true;
      }
    }
    results.push({id: qid, filled});
  }
  return results;
}
"""


class LinkedInApplier(BaseApplier):
    """Automate LinkedIn Easy Apply submissions."""

    def __init__(self, page) -> None:
        super().__init__(page)
        self._llm_config = None
        self._llm_config_loaded = False

    def _do_apply(
        self, job, resume_pdf_path, cover_letter_text, profile
    ) -> ApplyResult:
        logger.info("LinkedIn: applying to %s at %s", job.raw.title, job.raw.company)
        self._safe_goto(job.raw.apply_url)
        self._random_pause(1, 3)

        if self._detect_captcha():
            return ApplyResult(
                success=False, captcha_detected=True,
                error_message="CAPTCHA detected",
            )

        easy_apply_btn = self._wait_and_query(
            "button.jobs-apply-button, "
            "button[aria-label*='Easy Apply'], "
            ".jobs-apply-button--top-card",
            timeout=8000,
        )

        if not easy_apply_btn:
            return ApplyResult(
                success=False, manual_required=True,
                error_message="Easy Apply button not found — external application required",
            )

        easy_apply_btn.click()
        self._random_pause(1, 2)

        max_steps = 10
        for step in range(max_steps):
            if self._detect_captcha():
                return ApplyResult(
                    success=False, captcha_detected=True,
                    error_message="CAPTCHA detected in application form",
                )

            self._fill_form_fields(profile)

            if resume_pdf_path:
                self._safe_upload(resume_pdf_path, [
                    "input[type='file'][name*='resume']",
                    "input[type='file']",
                ])

            self._fill_cover_letter(cover_letter_text)
            self._answer_visible_questions(job, profile)

            if self._safe_click(
                "button[aria-label*='Submit application'], "
                "button[aria-label*='Submit']",
                timeout=2000,
            ):
                self._random_pause(2, 4)
                self._safe_click(
                    "button[aria-label*='Dismiss'], "
                    "[data-test-modal-close-btn]",
                    timeout=3000,
                )
                return ApplyResult(success=True)

            if self._safe_click(
                "button[aria-label*='Continue'], "
                "button[aria-label*='Next'], "
                "button[aria-label*='Review']",
                timeout=2000,
            ):
                self._random_pause(1, 2)
            else:
                break

        return ApplyResult(
            success=False,
            error_message="Could not complete Easy Apply — ran out of steps",
        )

    def _resolve_llm_config(self):
        if self._llm_config_loaded:
            return self._llm_config
        self._llm_config_loaded = True
        try:
            from config.settings import load_config

            config = load_config()
            self._llm_config = config.llm if config else None
        except Exception as exc:
            logger.warning("LinkedIn: could not load AI configuration: %s", exc)
        return self._llm_config

    def _collect_visible_questions(self) -> list[dict]:
        try:
            questions = self.page.evaluate(_COLLECT_QUESTIONS_JS)
            return questions if isinstance(questions, list) else []
        except Exception as exc:
            logger.debug("LinkedIn: question collection failed: %s", exc)
            return []

    def _fill_screening_answers(self, answers: list[dict]) -> None:
        if not answers:
            return
        try:
            self.page.evaluate(_FILL_ANSWERS_JS, answers)
            self._random_pause(0.3, 0.7)
        except Exception as exc:
            logger.warning("LinkedIn: failed to fill generated screening answers: %s", exc)

    def _answer_visible_questions(self, job, profile) -> None:
        """Batch visible unanswered controls through the LLM, with a local fallback."""
        from core.linkedin_screening import answer_screening_questions, saved_answer_fallback

        for _ in range(2):
            questions = self._collect_visible_questions()
            if not questions:
                return

            answers = []
            llm_config = self._resolve_llm_config()
            if llm_config and getattr(llm_config, "api_key", ""):
                try:
                    answers = answer_screening_questions(job, profile, questions, llm_config)
                except Exception as exc:
                    logger.warning(
                        "LinkedIn: LLM screening answers failed; using saved-answer fallback: %s",
                        exc,
                    )

            if not answers:
                answers = saved_answer_fallback(profile, questions)

            self._fill_screening_answers(answers)

    def _fill_form_fields(self, profile) -> None:
        """Fill common form fields if they are empty."""
        self._safe_fill(
            "input[name*='phone'], input[id*='phone']",
            profile.phone_full,
        )

    def _fill_cover_letter(self, text: str) -> None:
        """Fill cover letter textarea if visible."""
        if not text:
            return
        textarea = self.page.query_selector(
            "textarea[name*='cover'], "
            "textarea[id*='cover'], "
            "textarea[aria-label*='cover']"
        )
        if textarea and textarea.is_visible() and not textarea.input_value():
            textarea.fill(text)
            self._random_pause(0.5, 1)
