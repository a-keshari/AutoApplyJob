"""DOM helpers for broad LinkedIn Easy Apply control support."""

from __future__ import annotations

import logging
from typing import Any, Callable

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
    return clean(el.innerText || el.textContent || '');
  };
  const containerFor = (el) => el.closest(
    'fieldset, [role="radiogroup"], [role="group"], .jobs-easy-apply-form-section__grouping, '
    + '.fb-dash-form-element, [data-test-form-element], .artdeco-form-item'
  );
  const questionText = (el, container = null) => {
    const box = container || containerFor(el);
    if (box) {
      const candidate = box.querySelector(
        'legend, .fb-dash-form-element__label, .artdeco-text-input--label, '
        + 'label:not([for]), .jobs-easy-apply-form-section__grouping > label, [role="heading"]'
      );
      const text = clean(candidate?.innerText);
      if (text) return text;
    }
    return associatedLabel(el) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('name')) || 'Application question';
  };
  let counter = root.querySelectorAll('[data-autoapply-qid]').length;
  const nextId = () => `aaq${++counter}`;
  const qidFor = (elements) => elements.find(el => el.dataset.autoapplyQid)?.dataset.autoapplyQid || nextId();
  const mark = (elements, qid, type) => elements.forEach(el => {
    el.dataset.autoapplyQid = qid;
    el.dataset.autoapplyQtype = type;
  });
  const questions = [];

  const collectChoiceGroups = (selector, type, checkedFn) => {
    const elements = Array.from(root.querySelectorAll(selector)).filter(visible);
    const groups = new Map();
    elements.forEach(el => {
      const box = containerFor(el);
      const key = el.name || box || el;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
    });
    groups.forEach(group => {
      if (group.some(checkedFn)) return;
      const qid = qidFor(group);
      const box = containerFor(group[0]);
      const options = group.map(el => ({
        label: associatedLabel(el) || clean(el.value) || 'Yes',
        value: clean(el.value || el.getAttribute('data-value') || associatedLabel(el)),
      })).filter(opt => opt.label || opt.value);
      mark(group, qid, type);
      questions.push({
        id: qid, type, question: questionText(group[0], box), options,
        required: group.some(el => el.required || el.getAttribute('aria-required') === 'true')
      });
    });
  };

  collectChoiceGroups('input[type="radio"]', 'radio', el => !!el.checked);
  collectChoiceGroups('[role="radio"]', 'radio', el => el.getAttribute('aria-checked') === 'true');
  collectChoiceGroups('input[type="checkbox"]', 'checkbox', el => !!el.checked);
  collectChoiceGroups('[role="checkbox"], [role="switch"]', 'checkbox',
                      el => el.getAttribute('aria-checked') === 'true');

  Array.from(root.querySelectorAll('select')).filter(visible).forEach(el => {
    if (el.value && el.selectedIndex > 0) return;
    const qid = qidFor([el]);
    const options = Array.from(el.options)
      .filter(opt => !opt.disabled && clean(opt.textContent) && (opt.value || opt.index > 0))
      .map(opt => ({label: clean(opt.textContent), value: clean(opt.value)}));
    mark([el], qid, 'select');
    questions.push({id: qid, type: 'select', question: questionText(el), options, required: el.required});
  });

  const inputTypes = new Set(['text', 'number', 'date', 'email', 'tel', 'url', 'search', 'month']);
  Array.from(root.querySelectorAll('input, textarea')).filter(visible).forEach(el => {
    if (el.matches('input[type="radio"], input[type="checkbox"], input[type="file"]')) return;
    const tag = el.tagName.toLowerCase();
    const type = tag === 'textarea' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (tag !== 'textarea' && !inputTypes.has(type)) return;
    if ((el.value || '').trim()) return;
    const question = questionText(el);
    if (/cover\s*letter|resume|curriculum\s+vitae/i.test(question)) return;
    const qid = qidFor([el]);
    mark([el], qid, type);
    questions.push({
      id: qid, type, question, options: [], required: !!el.required,
      min: el.min || null, max: el.max || null, step: el.step || null
    });
  });

  Array.from(root.querySelectorAll('[contenteditable="true"]')).filter(visible).forEach(el => {
    if (clean(el.innerText)) return;
    const qid = qidFor([el]);
    mark([el], qid, 'textarea');
    questions.push({
      id: qid, type: 'textarea', question: questionText(el), options: [],
      required: el.getAttribute('aria-required') === 'true'
    });
  });

  Array.from(root.querySelectorAll('[role="combobox"]')).filter(visible).forEach(el => {
    if (el.tagName === 'SELECT') return;
    const current = clean(el.value || el.getAttribute('value') || el.textContent);
    if (current && !/select|choose|please select/i.test(current)) return;
    const qid = qidFor([el]);
    mark([el], qid, 'combobox');
    const listboxId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
    const options = listboxId
      ? Array.from(document.querySelectorAll(`#${CSS.escape(listboxId)} [role="option"]`))
          .map(opt => ({label: clean(opt.innerText), value: clean(opt.getAttribute('data-value') || opt.innerText)}))
      : [];
    questions.push({
      id: qid, type: 'combobox', question: questionText(el), options,
      required: el.getAttribute('aria-required') === 'true'
    });
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
    return normalize(
      el.closest('label')?.innerText || el.getAttribute('aria-label') || el.innerText || el.textContent || el.value
    );
  };
  const results = [];
  for (const item of answers || []) {
    const qid = String(item.id || '');
    const controls = Array.from(document.querySelectorAll(`[data-autoapply-qid="${CSS.escape(qid)}"]`));
    if (!controls.length) { results.push({id: qid, filled: false, type: ''}); continue; }
    const type = controls[0].dataset.autoapplyQtype || controls[0].type || 'text';
    const answer = item.answer;
    let filled = false;
    if (['text','textarea','number','date','email','tel','url','search','month'].includes(type)) {
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
        (target && normalize(opt.textContent).includes(target)) ||
        (normalize(opt.textContent) && target.includes(normalize(opt.textContent)))
      );
      if (option && target) { select.value = option.value; fire(select); filled = true; }
    } else if (type === 'radio') {
      const target = normalize(Array.isArray(answer) ? answer[0] : answer);
      const choice = controls.find(el => labelFor(el) === target || normalize(el.value) === target ||
        (target && labelFor(el).includes(target)) || (labelFor(el) && target.includes(labelFor(el))));
      if (choice && target) { choice.click(); filled = true; }
    } else if (type === 'checkbox') {
      const targets = (Array.isArray(answer) ? answer : [answer]).map(normalize).filter(Boolean);
      controls.forEach(el => {
        const label = labelFor(el); const value = normalize(el.value || el.getAttribute('data-value'));
        if (targets.some(t => t === label || t === value || label.includes(t) || (label && t.includes(label)))) {
          const checked = el.checked === true || el.getAttribute('aria-checked') === 'true';
          if (!checked) el.click();
          filled = true;
        }
      });
    } else if (type === 'combobox') {
      const value = Array.isArray(answer) ? answer[0] : answer;
      if (String(value ?? '').trim() && controls[0].tagName === 'INPUT') {
        setNativeValue(controls[0], value);
        filled = true;
      }
    }
    results.push({id: qid, filled, type});
  }
  return results;
}
"""


def collect_visible_questions(page) -> list[dict]:
    """Return visible unanswered Easy Apply controls with labels and options."""
    try:
        questions = page.evaluate(_COLLECT_QUESTIONS_JS)
        return questions if isinstance(questions, list) else []
    except Exception as exc:
        logger.debug("LinkedIn: question collection failed: %s", exc)
        return []


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _fill_custom_comboboxes(page, answers: list[dict], results: list[dict], pause: Callable) -> None:
    """Select options for non-input ARIA comboboxes that require opening a listbox."""
    filled_ids = {str(item.get("id")) for item in results if item.get("filled")}
    for item in answers:
        qid = str(item.get("id", ""))
        if not qid or qid in filled_ids:
            continue
        answer = item.get("answer", "")
        value = answer[0] if isinstance(answer, list) and answer else answer
        target = _normalized(value)
        if not target:
            continue
        try:
            combo = page.query_selector(
                f'[data-autoapply-qid="{qid}"][data-autoapply-qtype="combobox"]'
            )
            if not combo or not combo.is_visible():
                continue
            combo.click()
            pause(0.2, 0.5)
            options = page.query_selector_all('[role="option"]')
            visible_options = [option for option in options if option.is_visible()]
            choice = None
            for option in visible_options:
                text = _normalized(option.inner_text())
                value_attr = _normalized(option.get_attribute("data-value"))
                if target in {text, value_attr}:
                    choice = option
                    break
            if choice is None:
                for option in visible_options:
                    text = _normalized(option.inner_text())
                    if text and (target in text or text in target):
                        choice = option
                        break
            if choice:
                choice.click()
                pause(0.2, 0.5)
        except Exception as exc:
            logger.debug("LinkedIn: custom combobox fill failed for %s: %s", qid, exc)


def fill_screening_answers(page, answers: list[dict], pause: Callable) -> None:
    """Fill supported native/ARIA controls and custom comboboxes."""
    if not answers:
        return
    try:
        results = page.evaluate(_FILL_ANSWERS_JS, answers)
        if not isinstance(results, list):
            results = []
        _fill_custom_comboboxes(page, answers, results, pause)
        pause(0.3, 0.7)
    except Exception as exc:
        logger.warning("LinkedIn: failed to fill generated screening answers: %s", exc)
