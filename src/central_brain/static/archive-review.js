document.addEventListener('submit', async event => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-archive-review')) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const card = form.closest('.archive-review');
  if (card.dataset.saving) return;
  const status = card.querySelector('[data-review-status]');
  const signin = card.querySelector('[data-review-signin]');
  const buttons = [...card.querySelectorAll('button')];
  const disabled = buttons.map(button => button.disabled);
  const data = new FormData(form);
  const subject = form.getAttribute('action').includes('/files/') ? 'File' : 'Archive';
  card.dataset.saving = 'true';
  buttons.forEach(button => { button.disabled = true; });
  signin.hidden = true;
  status.textContent = (data.get('action') === 'reject' ? 'Rejecting this ' : 'Approving this ') + subject.toLowerCase() + '.';
  try {
    const response = await fetch(form.getAttribute('action'), {
      method: 'POST', body: data, credentials: 'same-origin', redirect: 'manual',
      headers: {Accept: 'application/json'}
    });
    if (response.type === 'opaqueredirect' || response.status === 401) {
      signin.hidden = false;
      throw new Error('Your session has ended. Sign in, reopen this archive, and try again.');
    }
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 403) throw new Error('This page can no longer submit changes. Reload it, sign in if prompted, and try again.');
      throw new Error(typeof result.detail === 'string' ? result.detail : 'The archive could not be reviewed. Please retry.');
    }
    status.textContent = subject + (result.status === 'rejected' ? ' rejected. Returning to your library.' : ' approved. Returning to your library.');
    location.assign('/library');
  } catch (error) {
    status.textContent = error.message || 'The request could not reach Central Brain. Please retry.';
  } finally {
    delete card.dataset.saving;
    buttons.forEach((button, index) => { button.disabled = disabled[index]; });
  }
}, true);
