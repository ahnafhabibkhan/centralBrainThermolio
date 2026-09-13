document.addEventListener('submit', async event => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-archive-batch')) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (form.dataset.uploading) return;
  form.dataset.uploading = 'true';
  const progress = form.querySelector('[data-upload-progress]');
  const button = form.querySelector('button');
  button.disabled = true;
  const inventory = JSON.parse(form.dataset.inventory);
  let completed = 0;
  try {
    for (const file of form.elements.files.files) {
      if (file.size > 50 * 1024 * 1024) throw new Error(`${file.name} exceeds 50 MB.`);
      progress.textContent = `Checking ${file.name}.`;
      const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
      const sha = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
      const matches = inventory.map((item, index) => ({item, index})).filter(({item}) =>
        !item.received && (!item.state || item.state === 'pending') && item.size === file.size && item.sha256 === sha);
      if (!matches.length) throw new Error(`${file.name} does not match a missing original. Completed uploads are preserved.`);
      for (const {item, index} of matches) {
        progress.textContent = `Uploading ${item.name}. ${completed} files completed.`;
        const data = new FormData();
        data.set('file', file);
        data.set('index', index);
        data.set('csrf_token', form.elements.csrf_token.value);
        const response = await fetch(form.getAttribute('action'), {method: 'POST', body: data,
          credentials: 'same-origin', redirect: 'manual', headers: {Accept: 'application/json'}});
        if (!response.ok) throw new Error('Upload stopped. Refresh to check completed files, then retry the missing originals.');
        await response.json();
        item.received = true;
        form.dataset.inventory = JSON.stringify(inventory);
        completed++;
      }
    }
    progress.textContent = `${completed} originals uploaded. Refreshing the approval status.`;
    location.reload();
  } catch (error) {
    progress.textContent = error.message;
  } finally {
    delete form.dataset.uploading;
    button.disabled = false;
  }
}, true);
