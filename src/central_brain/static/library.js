(() => {
  const app = document.getElementById('library-app');
  if (!app) return;
  const dialog = document.getElementById('workspace-dialog');
  const status = document.getElementById('workspace-status');
  let selected = app.dataset.folder || '';
  let revision = app.dataset.revision;
  let busy = false;
  let folderRequest = 0;
  let dialogRequest = 0;
  let dirty = false;
  const parser = new DOMParser();
  history.replaceState(null, '', '/library');

  const folderURL = () => '/library' + (selected ? '?folder=' + encodeURIComponent(selected) : '');
  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle('error', error);
    if (dialog.open) document.getElementById('dialog-status').textContent = text;
  }
  async function html(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
    const body = await response.text();
    if (new URL(response.url).pathname.startsWith('/login') || new URL(response.url).pathname.startsWith('/auth')) {
      throw new Error('Your session has ended. Save your work, then reload to sign in.');
    }
    const doc = parser.parseFromString(body, 'text/html');
    if (!response.ok) {
      let detail = doc.querySelector('main')?.textContent?.trim();
      if (!detail) { try { detail = JSON.parse(body).detail; } catch (_) {} }
      throw new Error(typeof detail === 'string' ? detail.slice(0, 600) : 'The request failed. Please try again.');
    }
    return {doc, url: response.url};
  }
  function selectedTree() {
    document.querySelectorAll('#folder-tree [data-folder]').forEach(item => {
      item.classList.toggle('selected-folder', item.dataset.folder === selected);
      if (item.dataset.folder === selected) {
        let parent = item.parentElement;
        while (parent && parent.id !== 'folder-tree') {
          if (parent.tagName === 'DETAILS') parent.open = true;
          parent = parent.parentElement;
        }
      }
    });
  }
  function replaceRegion(id, doc) {
    const next = doc.getElementById(id);
    if (next) document.getElementById(id).replaceChildren(...next.childNodes);
  }
  async function refresh(includeFolder = true) {
    const requestedFolder = selected;
    const open = new Set([...document.querySelectorAll('#folder-tree details[open]')].map(d => d.dataset.treeId));
    const {doc} = await html(folderURL());
    if (selected !== requestedFolder) return;
    for (const id of ['approvals', 'context-strip', 'folder-tree']) replaceRegion(id, doc);
    document.querySelectorAll('#folder-tree details').forEach(d => { d.open = open.has(d.dataset.treeId); });
    if (includeFolder && !dirty) replaceRegion('folder-content', doc);
    revision = doc.getElementById('library-app').dataset.revision;
    selectedTree();
  }
  async function openFolder(id, url) {
    if (busy) return;
    const request = ++folderRequest;
    const {doc} = await html(url || ('/library' + (id ? '?folder=' + encodeURIComponent(id) : '')));
    if (request !== folderRequest) return;
    selected = id;
    dirty = false;
    replaceRegion('folder-content', doc);
    selectedTree();
    if (dialog.open) dialog.close();
    message('');
  }
  function showDetails(doc, url) {
    const main = doc.querySelector('main');
    if (!main) throw new Error('Details could not be loaded.');
    main.querySelectorAll('script').forEach(script => script.remove());
    main.querySelectorAll('form').forEach(form => {
      form.setAttribute('action', new URL(form.getAttribute('action') || url, url).href);
    });
    document.getElementById('dialog-content').replaceChildren(...main.childNodes);
    document.getElementById('dialog-status').textContent = '';
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  async function openDetails(url) {
    const request = ++dialogRequest;
    const result = await html(url);
    if (request === dialogRequest) showDetails(result.doc, result.url);
  }
  document.getElementById('close-dialog').addEventListener('click', () => dialog.close());
  document.addEventListener('input', event => {
    if (event.target.closest('#folder-content form')) dirty = true;
  });
  document.addEventListener('change', event => {
    if (event.target.closest('#folder-content form')) dirty = true;
  });
  document.addEventListener('click', event => {
    const folder = event.target.closest('summary[data-folder],a[data-folder],button[data-folder]');
    if (folder && !event.ctrlKey && !event.metaKey) {
      if (folder.tagName !== 'SUMMARY') event.preventDefault();
      // Native summary controls still expand and collapse the tree.
      const wasOpen = folder.tagName === 'SUMMARY' && folder.parentElement.open;
      if (!wasOpen || selected !== folder.dataset.folder) {
        openFolder(folder.dataset.folder).catch(error => message(error.message, true));
      }
      return;
    }
    const link = event.target.closest('a[href]');
    if (!link || event.ctrlKey || event.metaKey || link.hasAttribute('download')) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || url.hash || /\/(download|export)$/.test(url.pathname)) return;
    if (url.pathname === '/library' || (url.pathname === '/' && !url.search)) {
      event.preventDefault();
      openFolder(url.searchParams.get('folder') || '', url.href).catch(error => message(error.message, true));
    } else if (/^\/(library\/file\/|review\/|new$|getting-started$|search$)/.test(url.pathname)
               || (url.pathname === '/' && url.searchParams.has('view'))) {
      event.preventDefault();
      openDetails(url.href).catch(error => message(error.message, true));
    }
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    // Named controls such as name="action" can shadow native form properties.
    const formURL = new URL(form.getAttribute('action') || location.href, location.href);
    if (formURL.pathname === '/logout') return;
    event.preventDefault();
    if (busy) return;
    const submitter = event.submitter;
    const action = submitter?.getAttribute('formaction') || formURL.href;
    const data = new FormData(form);
    if (submitter?.name) data.append(submitter.name, submitter.value);
    if ((form.getAttribute('method') || 'get').toLowerCase() === 'get') {
      const url = new URL(action);
      url.search = new URLSearchParams(data).toString();
      try {
        if (url.pathname === '/library') await openFolder(data.get('folder') || '', url.href);
        else await openDetails(url.href);
      } catch (error) { message(error.message, true); }
      return;
    }
    const file = data.get('file');
    if (file instanceof File && file.size > 50 * 1024 * 1024) {
      message('The file exceeds the 50 MB limit.', true);
      return;
    }
    busy = true;
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach(button => { button.disabled = true; });
    message('Saving your changes.');
    try {
      const result = await html(action, {method: 'POST', body: data});
      dirty = false;
      await refresh(true);
      if (result.doc.getElementById('library-app')) {
        if (dialog.open) dialog.close();
      } else {
        showDetails(result.doc, result.url);
      }
      message('Saved. The workspace context has been updated.');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
    }
  });
  // Other assistants can update this workspace while the page stays open.
  setInterval(async () => {
    if (busy || document.hidden) return;
    let ownsBusy = false;
    try {
      const response = await fetch('/library/context', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const context = await response.json();
      if (busy) return;
      if (context.revision !== revision) {
        busy = true;
        ownsBusy = true;
        await refresh(!dirty);
        message(dirty ? 'Context updated. Finish your form to refresh the file list.' : 'Context updated with the latest changes.');
      }
    } catch (error) {
      message('Live updates are temporarily unavailable. Your current work is still open.');
    } finally {
      if (ownsBusy) busy = false;
    }
  }, 20000);
  selectedTree();
})();
