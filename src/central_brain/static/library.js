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
  let queueFilter = 'ready';
  const parser = new DOMParser();
  const fileSelections = new WeakMap();
  const folderSelections = new WeakMap();
  const fileLimit = 200 * 1024 * 1024;
  history.replaceState(null, '', '/library');

  const folderURL = () => '/library' + (selected ? '?folder=' + encodeURIComponent(selected) : '');
  function filterQueue() {
    const panel = document.getElementById('approvals');
    const rows = [...panel.querySelectorAll('[data-review-state]')];
    rows.forEach(row => { row.hidden = queueFilter !== 'all' && row.dataset.reviewState !== queueFilter; });
    panel.querySelectorAll('[data-queue-filter]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.queueFilter === queueFilter));
    });
    panel.querySelectorAll('[data-queue-count]').forEach(count => {
      count.textContent = rows.filter(row => row.dataset.reviewState === count.dataset.queueCount).length;
    });
    panel.querySelector('.approval-count').textContent = rows.length;
    panel.querySelector('[data-queue-empty]').hidden = rows.some(row => !row.hidden);
    panel.querySelector('.bulk-approval').hidden = queueFilter === 'upload';
    const recovery = panel.querySelector('[data-batch-recovery]');
    if (recovery) recovery.hidden = queueFilter === 'ready';
  }
  async function postJSON(url, data, options = {}) {
    for (let attempt = 0; attempt < 4; attempt++) {
      let response;
      try {
        response = await fetch(url, {method: 'POST', body: data, credentials: 'same-origin',
          redirect: 'manual', headers: {Accept: 'application/json'}});
      } catch (_) {
        throw new Error('The connection was interrupted. Your request may have completed. Refresh the queue to check before retrying.');
      }
      if (response.type === 'opaqueredirect' || response.status === 401) {
        throw new Error('Your session has ended. Sign in and reopen the workspace to continue.');
      }
      if (response.status === 429 && options.retryRateLimit && attempt < 3) {
        const retryAfter = Number(response.headers.get('Retry-After'));
        const delay = Number.isFinite(retryAfter) && retryAfter > 0 ? Math.min(retryAfter, 60) : 2;
        message(`Upload paused by traffic protection. It will continue automatically in ${delay} seconds.`);
        await new Promise(resolve => setTimeout(resolve, delay * 1000));
        continue;
      }
      if (response.status === 429) {
        const delay = Number(response.headers.get('Retry-After'));
        throw new Error(`Too many requests. ${Number.isFinite(delay) && delay > 0 ? `Wait ${delay} seconds, then` : 'Please'} refresh the queue before retrying.`);
      }
      let result;
      try { result = await response.json(); }
      catch (_) {
        throw new Error('The server did not confirm the result. Refresh the queue to check completed uploads and approvals before retrying.');
      }
      if (!response.ok) {
        const error = new Error(typeof result.detail === 'string' ? result.detail : 'The request could not be completed.');
        error.status = response.status;
        throw error;
      }
      return result;
    }
  }
  function formatBytes(value) {
    if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
    if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${value} bytes`;
  }
  function accepted(file, input) {
    const extensions = input.accept.split(',').map(value => value.trim().toLowerCase()).filter(Boolean);
    return !extensions.length || extensions.some(extension => file.name.toLowerCase().endsWith(extension));
  }
  function validSegment(value) {
    return value && value.length <= 240 && !['.', '..'].includes(value)
      && !/[\\/\u0000-\u001f]/.test(value);
  }
  function systemFile(path) {
    const name = path.split('/').at(-1).toLowerCase();
    return name === '.ds_store' || name === 'thumbs.db' || name === 'desktop.ini';
  }
  function folderSelectionFromInput(input) {
    const selected = [...input.files].map(file => ({file, path: file.webkitRelativePath || file.name}));
    const files = selected.filter(item => !systemFile(item.path));
    const directories = new Set();
    files.forEach(({path}) => {
      const parts = path.split('/').filter(Boolean);
      for (let end = 1; end < parts.length; end++) directories.add(parts.slice(0, end).join('/'));
    });
    return {files, directories, ignored: selected.length - files.length};
  }
  function showSelection(form, selection, folder = false) {
    const notice = form.querySelector('[data-drop-selection]');
    const files = folder ? selection.files.map(item => item.file) : selection;
    const total = files.reduce((sum, file) => sum + file.size, 0);
    notice.textContent = files.length
      ? `${folder ? 'Folder selected' : `${files.length} file${files.length === 1 ? '' : 's'} selected`}: ${formatBytes(total)}.`
      : folder ? 'No folder selected.' : 'No files selected.';
    if (folder && selection.ignored) notice.textContent += ` ${selection.ignored} system metadata files will be ignored.`;
  }
  function readEntryFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
  }
  function readDirectory(reader) {
    return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  }
  async function collectEntry(entry, parent, selection) {
    const path = parent ? `${parent}/${entry.name}` : entry.name;
    if (entry.isFile) {
      if (systemFile(path)) { selection.ignored++; return; }
      selection.files.push({file: await readEntryFile(entry), path});
      return;
    }
    if (!entry.isDirectory) return;
    selection.directories.add(path);
    const reader = entry.createReader();
    while (true) {
      const children = await readDirectory(reader);
      if (!children.length) break;
      for (const child of children) await collectEntry(child, path, selection);
    }
  }
  async function droppedFolder(dataTransfer) {
    const selection = {files: [], directories: new Set(), ignored: 0};
    const entries = [...dataTransfer.items]
      .map(item => item.webkitGetAsEntry?.()).filter(Boolean);
    if (entries.length) {
      for (const entry of entries) await collectEntry(entry, '', selection);
      return selection;
    }
    const inputLike = [...dataTransfer.files]
      .map(file => ({file, path: file.webkitRelativePath || file.name}));
    selection.files = inputLike.filter(item => !systemFile(item.path));
    selection.ignored = inputLike.length - selection.files.length;
    selection.files.forEach(({path}) => {
      const parts = path.split('/').filter(Boolean);
      for (let end = 1; end < parts.length; end++) selection.directories.add(parts.slice(0, end).join('/'));
    });
    return selection;
  }
  async function uploadFiles(form) {
    if (busy) return;
    const input = form.querySelector('input[type="file"]');
    const files = fileSelections.get(form) || [...input.files];
    if (!files.length) { message('Choose or drop at least one file.', true); return; }
    if (files.some(file => !accepted(file, input))) {
      message('One or more selected files use an unsupported format.', true); return;
    }
    if (files.some(file => !file.size || file.size > fileLimit)) {
      message('Every file must contain data and be no larger than 200 MB.', true); return;
    }
    busy = true;
    const controls = [...form.querySelectorAll('input, select, button')];
    controls.forEach(control => { control.disabled = true; });
    const progress = form.querySelector('progress');
    progress.max = files.length; progress.value = 0; progress.hidden = false;
    let completed = 0;
    try {
      for (const file of files) {
        message(`Uploading ${file.name}. ${completed} of ${files.length} files complete.`);
        const data = new FormData();
        data.set('csrf_token', form.elements.csrf_token.value);
        data.set('parent_id', form.elements.parent_id.value);
        data.set('visibility', form.elements.visibility.value);
        data.set('file', file, file.name);
        await postJSON('/library/upload', data, {retryRateLimit: true});
        completed++; progress.value = completed;
      }
      dirty = false;
      fileSelections.delete(form);
      await refresh(true);
      message(`${completed} files uploaded. Workspace context has been updated.`);
    } catch (error) {
      message(`${completed} uploads completed. ${error.message} Completed files are preserved.`, true);
      try { await refresh(false); } catch (_) {}
    } finally {
      busy = false;
      controls.forEach(control => { control.disabled = false; });
    }
  }
  async function uploadFolder(form) {
    if (busy) return;
    const input = form.querySelector('input[type="file"]');
    const selection = folderSelections.get(form) || folderSelectionFromInput(input);
    if (!selection.files.length) { message('Choose or drop a folder containing files.', true); return; }
    const invalidSizes = selection.files.filter(item => !item.file.size || item.file.size > fileLimit);
    if (invalidSizes.length && !form.elements.skip_invalid.checked) {
      const names = invalidSizes.slice(0, 3).map(item => item.path).join(', ');
      const remaining = invalidSizes.length > 3 ? `, plus ${invalidSizes.length - 3} more` : '';
      message(`These files are empty or exceed 200 MB: ${names}${remaining}. Select the skip option to upload the remaining files.`, true); return;
    }
    const files = invalidSizes.length
      ? selection.files.filter(item => item.file.size && item.file.size <= fileLimit)
      : selection.files;
    if (!files.length) { message('No uploadable files remain after applying the skip option.', true); return; }
    const paths = files.map(item => item.path.split('/').filter(Boolean));
    const roots = new Set(paths.map(parts => parts[0]));
    if (roots.size !== 1 || paths.some(parts => parts.length < 2 || parts.some(part => !validSegment(part)))) {
      message('Choose one folder with valid file and folder names.', true); return;
    }
    const rootName = [...roots][0];
    selection.directories.add(rootName);
    const total = files.reduce((sum, item) => sum + item.file.size, 0);
    const limit = Number(document.getElementById('library-actions').dataset.folderLimit);
    if (!total || total > limit) {
      message(`The folder must contain data and be no larger than ${formatBytes(limit)}.`, true); return;
    }
    if (files.length + selection.directories.size > 10000) {
      message('The folder exceeds the 10,000-item library limit.', true); return;
    }
    if (files.some(item => !accepted(item.file, input))) {
      message('The folder contains an unsupported file format.', true); return;
    }
    busy = true;
    const controls = [...form.querySelectorAll('input, select, button')];
    controls.forEach(control => { control.disabled = true; });
    const progress = form.querySelector('progress');
    const directories = [...selection.directories].sort((a, b) => a.split('/').length - b.split('/').length || a.localeCompare(b));
    progress.max = directories.length + files.length; progress.value = 0; progress.hidden = false;
    let completedFiles = 0;
    let completedFolders = 0;
    let rootId = '';
    try {
      const start = new FormData();
      start.set('csrf_token', form.elements.csrf_token.value);
      start.set('parent_id', form.elements.parent_id.value);
      start.set('visibility', form.elements.visibility.value);
      start.set('name', rootName);
      start.set('total_size', String(total));
      start.set('file_count', String(files.length));
      start.set('folder_count', String(directories.length));
      message(`Preparing ${rootName}.`);
      const root = await postJSON('/library/folder-upload/start', start, {retryRateLimit: true});
      rootId = root.id;
      const folderIds = new Map([[rootName, rootId]]);
      completedFolders = 1; progress.value = 1;
      for (const path of directories.slice(1)) {
        const parts = path.split('/');
        const parentPath = parts.slice(0, -1).join('/');
        const data = new FormData();
        data.set('csrf_token', form.elements.csrf_token.value);
        data.set('parent_id', folderIds.get(parentPath));
        data.set('visibility', form.elements.visibility.value);
        data.set('name', parts.at(-1));
        message(`Creating ${path}.`);
        const created = await postJSON('/library/folders', data, {retryRateLimit: true});
        folderIds.set(path, created.id);
        completedFolders++; progress.value++;
      }
      for (const item of files) {
        const parts = item.path.split('/');
        const parentPath = parts.slice(0, -1).join('/');
        const data = new FormData();
        data.set('csrf_token', form.elements.csrf_token.value);
        data.set('parent_id', folderIds.get(parentPath));
        data.set('visibility', form.elements.visibility.value);
        data.set('file', item.file, parts.at(-1));
        message(`Uploading ${item.path}. ${completedFiles} of ${files.length} files complete.`);
        await postJSON('/library/upload', data, {retryRateLimit: true});
        completedFiles++; progress.value++;
      }
      dirty = false;
      folderSelections.delete(form);
      selected = rootId;
      await refresh(true);
      const skipped = invalidSizes.length ? ` ${invalidSizes.length} empty or oversized files were skipped.` : '';
      message(`${rootName} uploaded with ${completedFiles} files and ${completedFolders} folders.${skipped}`);
    } catch (error) {
      dirty = false;
      if (rootId) selected = rootId;
      try { await refresh(true); } catch (_) {}
      message(`${completedFiles} files and ${completedFolders} folders completed. ${error.message} Completed items are preserved.`, true);
    } finally {
      busy = false;
      controls.forEach(control => { control.disabled = false; });
    }
  }
  async function approveQueue(form) {
    if (busy) return;
    busy = true;
    const panel = document.getElementById('approvals');
    const buttons = [...panel.querySelectorAll('form button')];
    buttons.forEach(button => { button.disabled = true; });
    const progress = panel.querySelector('[data-queue-progress]');
    const notice = panel.querySelector('[data-queue-status]');
    let completed = 0;
    let outcome;
    const conflicts = [];
    const approvedKeys = new Set();
    try {
      const items = JSON.parse(form.elements.items.value);
      progress.max = items.length;
      progress.value = 0;
      progress.hidden = false;
      const key = item => `${item.kind}:${item.id}:${item.index ?? ''}`;
      async function submit(batch) {
        const data = new FormData();
        data.set('csrf_token', form.elements.csrf_token.value);
        data.set('items', JSON.stringify(batch));
        const result = await postJSON('/library/approve-all', data);
        completed += result.approved;
        batch.forEach(item => approvedKeys.add(key(item)));
        panel.querySelectorAll('[data-review-key]').forEach(row => {
          if (approvedKeys.has(row.dataset.reviewKey)) row.remove();
        });
        form.elements.items.value = JSON.stringify(items.filter(item => !approvedKeys.has(key(item))));
        filterQueue();
      }
      for (let start = 0; start < items.length; start += 3) {
        notice.textContent = `Approving items ${start + 1} to ${Math.min(start + 3, items.length)} of ${items.length}. ${completed} approved.`;
        const batch = items.slice(start, start + 3);
        try { await submit(batch); }
        catch (error) {
          if (![409, 422].includes(error.status)) throw error;
          // These responses confirm that the whole group rolled back. Isolate the conflicts.
          for (const item of batch) {
            try { await submit([item]); }
            catch (itemError) {
              if (![409, 422].includes(itemError.status)) throw itemError;
              const row = [...panel.querySelectorAll('[data-review-key]')].find(row => row.dataset.reviewKey === key(item));
              const name = row?.querySelector('td')?.firstChild?.textContent?.trim() || 'A proposal';
              conflicts.push(`${name}: ${itemError.message}`);
            }
          }
        }
        progress.value = Math.min(start + 3, items.length);
      }
      outcome = `${completed} items approved. The workspace context has been updated.`;
      if (conflicts.length) outcome += ` ${conflicts.length} items still need attention. ${conflicts.slice(0, 5).join(' ')}`;
    } catch (error) {
      outcome = `${completed} approvals confirmed. ${error.message} Completed approvals are preserved. Check the refreshed queue before retrying.`;
    } finally {
      try { await refresh(true); }
      catch (_) { outcome += ' Reload the workspace to check the latest status before continuing.'; }
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
      document.querySelector('[data-queue-status]').textContent = outcome;
    }
  }
  async function uploadQueue(form) {
    const files = [...form.elements.files.files, ...form.elements.folder.files];
    const notice = document.querySelector('[data-queue-status]');
    if (!files.length) { notice.textContent = 'Select original files or a folder first.'; return; }
    const inventory = JSON.parse(form.dataset.inventory);
    const controls = [...form.querySelectorAll('input, button')];
    controls.forEach(control => { control.disabled = true; });
    busy = true;
    let completed = 0;
    const skipped = [];
    let outcome = '';
    try {
      for (const file of files) {
        if (file.size > fileLimit || !inventory.some(item => !item.ready && item.size === file.size)) {
          skipped.push(file.name); continue;
        }
        notice.textContent = `Checking ${file.name}. ${completed} uploads confirmed.`;
        const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        const hash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
        const matches = inventory.filter(item => !item.ready && item.size === file.size && item.sha256 === hash);
        if (!matches.length) { skipped.push(file.name); continue; }
        for (const item of matches) {
          notice.textContent = `Uploading ${item.name} to ${item.destination}. ${completed} uploads confirmed.`;
          const data = new FormData();
          data.set('file', file);
          data.set('index', item.index);
          data.set('csrf_token', form.elements.csrf_token.value);
          await postJSON(`/library/suggestions/${encodeURIComponent(item.archive_id)}/original`, data);
          item.ready = true;
          form.dataset.inventory = JSON.stringify(inventory);
          completed++;
        }
      }
      outcome = `${completed} originals received and ready for approval. ${inventory.filter(item => !item.ready).length} originals in this queue still need upload.`;
      if (skipped.length) outcome += ` ${skipped.length} selections had no remaining match and were skipped: ${skipped.slice(0, 5).join(', ')}.`;
    } catch (error) {
      outcome = `${completed} uploads confirmed. ${error.message} Completed uploads are preserved.`;
    } finally {
      try { await refresh(true); }
      catch (_) { outcome += ' Reload the workspace to check the latest status.'; }
      busy = false;
      controls.forEach(control => { control.disabled = false; });
      document.querySelector('[data-queue-status]').textContent = outcome;
    }
  }
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
      let detail = doc.querySelector('main p')?.textContent?.trim() || doc.querySelector('main')?.textContent?.trim();
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
    if (!dirty) replaceRegion('library-actions', doc);
    filterQueue();
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
    replaceRegion('library-actions', doc);
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
    if (result.doc.getElementById('library-app')) {
      if (request !== dialogRequest) return;
      if (dialog.open) dialog.close();
      await refresh(false);
      document.getElementById('approvals').scrollIntoView({block: 'start'});
      return;
    }
    if (request === dialogRequest) showDetails(result.doc, result.url);
  }
  document.getElementById('close-dialog').addEventListener('click', () => dialog.close());
  document.addEventListener('input', event => {
    if (event.target.closest('#folder-content form,#library-actions form')) dirty = true;
  });
  document.addEventListener('change', event => {
    const fileForm = event.target.closest('form[data-file-upload]');
    const folderForm = event.target.closest('form[data-folder-upload]');
    if (fileForm && event.target.matches('input[type="file"]')) {
      fileSelections.delete(fileForm);
      showSelection(fileForm, [...event.target.files]);
    }
    if (folderForm && event.target.matches('input[type="file"]')) {
      const selection = folderSelectionFromInput(event.target);
      folderSelections.set(folderForm, selection);
      showSelection(folderForm, selection, true);
    }
    if (event.target.closest('#folder-content form,#library-actions form')) dirty = true;
  });
  document.addEventListener('dragover', event => {
    const zone = event.target.closest('[data-drop-zone]');
    if (!zone) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    zone.classList.add('drop-active');
  });
  document.addEventListener('dragleave', event => {
    const zone = event.target.closest('[data-drop-zone]');
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove('drop-active');
  });
  document.addEventListener('drop', async event => {
    const zone = event.target.closest('[data-drop-zone]');
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove('drop-active');
    const form = zone.closest('form');
    try {
      if (zone.dataset.dropZone === 'folder') {
        const selection = await droppedFolder(event.dataTransfer);
        folderSelections.set(form, selection);
        showSelection(form, selection, true);
      } else {
        const files = [...event.dataTransfer.files];
        fileSelections.set(form, files);
        showSelection(form, files);
      }
      dirty = true;
      message('Selection ready. Review the access setting, then start the upload.');
    } catch (_) {
      message('That dropped folder could not be read. Use Choose folder instead.', true);
    }
  });
  document.addEventListener('click', event => {
    const queueToggle = event.target.closest('[data-queue-filter]');
    if (queueToggle) {
      queueFilter = queueToggle.dataset.queueFilter;
      filterQueue();
      return;
    }
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
    } else if (/^\/(library\/(?:file|suggestions)\/|review\/|new$|getting-started$|search$)/.test(url.pathname)
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
    if (form.hasAttribute('data-file-upload')) {
      await uploadFiles(form);
      return;
    }
    if (form.hasAttribute('data-folder-upload')) {
      await uploadFolder(form);
      return;
    }
    if (form.hasAttribute('data-queue-batch')) {
      await uploadQueue(form);
      return;
    }
    if (form.matches('.bulk-approval')) {
      await approveQueue(form);
      return;
    }
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
    if (file instanceof File && file.size > fileLimit) {
      message('The file exceeds the 200 MB limit.', true);
      return;
    }
    busy = true;
    const buttons = [...form.querySelectorAll('button')];
    const disabledBefore = buttons.map(button => button.disabled);
    buttons.forEach(button => { button.disabled = true; });
    message('Saving your changes.');
    try {
      const queueAction = form.hasAttribute('data-queue-upload') || /^\/library\/suggestions\/[^/]+\/files\/[^/]+\/review$/.test(new URL(action).pathname);
      if (queueAction) {
        document.querySelector('[data-queue-status]').textContent = form.hasAttribute('data-queue-upload') ? 'Uploading the original file.' : 'Saving your review.';
        await postJSON(action, data);
        if (form.hasAttribute('data-queue-upload')) queueFilter = 'ready';
        await refresh(true);
        document.querySelector('[data-queue-status]').textContent = 'Saved. Received files are ready for approval.';
        return;
      }
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
      if (form.closest('#approvals')) document.querySelector('[data-queue-status]').textContent = error.message;
    } finally {
      busy = false;
      buttons.forEach((button, index) => { button.disabled = disabledBefore[index]; });
    }
  });
  // Other assistants can update this workspace while the page stays open.
  setInterval(async () => {
    if (busy || document.hidden || document.querySelector('.queue-upload[open]')) return;
    let ownsBusy = false;
    try {
      const response = await fetch('/library/context', {credentials: 'same-origin', cache: 'no-store', redirect: 'manual'});
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
  filterQueue();
})();
