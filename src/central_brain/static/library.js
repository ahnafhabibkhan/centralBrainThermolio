const form = document.getElementById('upload-form');
if (form) {
  const input = form.querySelector('input[type=file]');
  const status = document.getElementById('upload-status');
  form.addEventListener('dragover', event => { event.preventDefault(); form.classList.add('drop-active'); });
  form.addEventListener('dragleave', () => form.classList.remove('drop-active'));
  form.addEventListener('drop', event => {
    event.preventDefault(); form.classList.remove('drop-active');
    if (event.dataTransfer.files.length !== 1) { status.textContent = 'Please upload one file at a time.'; return; }
    input.files = event.dataTransfer.files;
    status.textContent = 'The file is selected. Click Upload file to continue.';
  });
  form.addEventListener('submit', event => {
    if (input.files[0]?.size > 50 * 1024 * 1024) { event.preventDefault(); status.textContent = 'The file exceeds the 50 MB limit.'; return; }
    status.textContent = 'Uploading your original. Keep this page open.';
    form.querySelector('button').disabled = true;
  });
}
