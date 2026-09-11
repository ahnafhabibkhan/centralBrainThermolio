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
  let detailResource = null;
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
      const error = new Error(typeof detail === 'string' ? detail.slice(0, 600) : 'The request failed. Please try again.');
      error.status = response.status;
      throw error;
    }
    return {doc, url: response.url};
  }
  function expandBranch(branch, expanded) {
    branch.classList.toggle('expanded', expanded);
    const toggle = branch.querySelector(':scope > .tree-row > .tree-toggle');
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${toggle.dataset.folderName}`);
    branch.querySelector(':scope > .tree-children').inert = !expanded;
  }
  function selectedTree(reveal = false) {
    document.querySelectorAll('#folder-tree [data-folder]').forEach(item => {
      item.classList.toggle('selected-folder', item.dataset.folder === selected);
      if (item.dataset.folder === selected) item.setAttribute('aria-current', 'location');
      else item.removeAttribute('aria-current');
      if (reveal && item.dataset.folder === selected) {
        let parent = item.parentElement;
        while (parent && parent.id !== 'folder-tree') {
          if (parent.matches('.tree-branch')) expandBranch(parent, true);
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
    let result;
    let missingFolder = false;
    try { result = await html(folderURL()); }
    catch (error) {
      if (error.status !== 404 || !requestedFolder) throw error;
      result = await html('/library');
      missingFolder = true;
    }
    if (selected !== requestedFolder) return;
    const {doc} = result;
    if (missingFolder) { selected = ''; dirty = false; includeFolder = true; }
    // Capture the latest disclosure state, including clicks while the fetch was pending.
    const open = new Set([...document.querySelectorAll('#folder-tree .tree-branch.expanded')].map(d => d.dataset.treeId));
    const focusedToggle = document.activeElement?.closest('.tree-toggle')?.closest('.tree-branch')?.dataset.treeId;
    for (const id of ['approvals', 'context-strip', 'folder-tree']) replaceRegion(id, doc);
    document.querySelectorAll('#folder-tree .tree-branch').forEach(d => expandBranch(d, open.has(d.dataset.treeId)));
    if (focusedToggle) document.querySelector(`#folder-tree [data-tree-id="${CSS.escape(focusedToggle)}"] .tree-toggle`)?.focus({preventScroll: true});
    if (includeFolder && !dirty) replaceRegion('folder-content', doc);
    revision = doc.getElementById('library-app').dataset.revision;
    selectedTree();
    if (dialog.open && detailResource && !document.querySelector(
      `#folder-tree a[href="/library/file/${detailResource}"],#folder-tree [data-folder="${detailResource}"]`)) {
      dialog.close();
      ++dialogRequest;
    }
    return missingFolder;
  }
  async function openFolder(id, url) {
    if (busy) return;
    const request = ++folderRequest;
    const {doc} = await html(url || ('/library' + (id ? '?folder=' + encodeURIComponent(id) : '')));
    if (request !== folderRequest) return;
    selected = id;
    dirty = false;
    replaceRegion('folder-content', doc);
    selectedTree(true);
    if (dialog.open) dialog.close();
    if (matchMedia('(max-width: 800px)').matches) {
      document.getElementById('folder-content').scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
        block: 'start'
      });
    }
    message('');
  }
  function showDetails(doc, url) {
    detailResource = new URL(url).pathname.match(/^\/(?:library\/file|review)\/([0-9a-f-]{36})(?:\/|$)/)?.[1] || null;
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
    const toggle = event.target.closest('.tree-toggle');
    if (toggle) {
      const branch = toggle.closest('.tree-branch');
      expandBranch(branch, toggle.getAttribute('aria-expanded') !== 'true');
      return;
    }
    const folder = event.target.closest('a[data-folder],button[data-folder]');
    if (folder && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      openFolder(folder.dataset.folder).catch(error => message(error.message, true));
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
      const deleting = /^\/library\/file\/[0-9a-f-]+\/delete$/.test(new URL(action).pathname);
      if (deleting && result.doc.getElementById('library-app')) {
        selected = result.doc.getElementById('library-app').dataset.folder || '';
        ++folderRequest;
      }
      dirty = false;
      await refresh(true);
      if (result.doc.getElementById('library-app')) {
        if (dialog.open) dialog.close();
      } else {
        showDetails(result.doc, result.url);
      }
      message(deleting ? 'Deleted. Workspace context is updated, and original files are queued for permanent removal.'
                       : 'Saved. The workspace context has been updated.');
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
        const missingFolder = await refresh(!dirty);
        message(missingFolder ? 'That folder is no longer available. Showing Workspace.'
          : dirty ? 'Context updated. Finish your form to refresh the file list.' : 'Context updated with the latest changes.');
      }
    } catch (error) {
      message('Live updates are temporarily unavailable. Your current work is still open.');
    } finally {
      if (ownsBusy) busy = false;
    }
  }, 20000);
  selectedTree(true);
})();
