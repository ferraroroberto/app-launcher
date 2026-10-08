/* The `.tr-ask-opt` option button every answer surface builds (#1422): the
   transcript's question card and plan picker (transcript-decisions.js), the
   Board answer sheet (board-answers.js) and the resume list (chat-resume.js)
   share the `.tr-ask-*` styles, so they share this one builder too. */

import { icon } from './_vendored/icons/icons.js';

function span(cls, text) {
  const node = document.createElement('span');
  node.className = cls;
  node.textContent = text;
  return node;
}

// A button of number (when `n` is given), label, optional description and,
// with `mark`, the picked check. `extraClass` rides after `tr-ask-opt`. The
// caller owns `dataset`, `disabled` and any state class on the returned node.
export function askOption({ n, label, description, mark, extraClass, onTap }) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = extraClass ? 'tr-ask-opt ' + extraClass : 'tr-ask-opt';
  const body = document.createElement('span');
  body.className = 'tr-ask-opt-body';
  body.appendChild(span('tr-ask-label', label));
  if (description) body.appendChild(span('tr-ask-desc', description));
  if (n != null) b.appendChild(span('tr-ask-num', String(n)));
  b.appendChild(body);
  if (mark) {
    const check = document.createElement('span');
    check.className = 'tr-ask-mark';
    check.setAttribute('aria-hidden', 'true');
    check.innerHTML = icon('circle-check');
    b.appendChild(check);
  }
  if (onTap) b.addEventListener('click', onTap);
  return b;
}
