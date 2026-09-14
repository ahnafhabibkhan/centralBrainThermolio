(() => {
  const key = 'central-brain-theme';
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = null;
  try { preference = localStorage.getItem(key); } catch (_) { /* Storage may be disabled. */ }
  if (!['light', 'dark'].includes(preference)) preference = null;
  function apply() {
    const dark = (preference || (system.matches ? 'dark' : 'light')) === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const button = document.querySelector('.theme-toggle');
    if (button) {
      button.hidden = false;
      button.setAttribute('aria-pressed', String(dark));
      button.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    }
  }
  apply();
  system.addEventListener('change', () => { if (!preference) apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = ['dark', 'light'].includes(event.newValue) ? event.newValue : null;
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelector('.theme-toggle')?.addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, preference); } catch (_) { /* Keep the choice for this page. */ }
      apply();
    });
  });
})();
