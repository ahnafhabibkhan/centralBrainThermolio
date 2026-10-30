const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('src/central_brain/static/library.js', 'utf8');
const requests = source.slice(source.indexOf('  function retryAfterMilliseconds('), source.indexOf('  function formatBytes('));
const selection = source.slice(source.indexOf('  const reviewKey ='), source.indexOf('  function retryAfterMilliseconds('));
const filter = source.slice(source.indexOf('  function filterQueue('), source.indexOf('  const reviewKey ='));
const review = source.slice(source.indexOf('  async function reviewQueue('), source.indexOf('  async function uploadQueue('));
const refresh = source.slice(source.indexOf('  async function refresh('), source.indexOf('  async function openFolder('));
const itemKey = item => `${item.kind}:${item.id}:${item.index ?? ''}`;
const items = Array.from({length: 9}, (_, index) => ({
  kind: 'archive_file', id: 'original-batch', index, fingerprint: `original-fingerprint-${index}`
}));

async function scenario({respond, refreshError = null, arrivals = [], action = 'approve',
  chosen = items, pending = items, missing = [], initialFilter = 'ready', run = true,
  refreshedItems = null} = {}) {
  let now = 1_000_000;
  let refreshes = 0;
  const calls = [];
  const waits = [];
  const waitStates = [];
  const confirmed = new Set();
  function view(inventory) {
    const button = {disabled: true, textContent: 'Approve selected'};
    const rejectButton = {disabled: true, textContent: 'Reject selected'};
    const all = {checked: false, indeterminate: false, disabled: false,
      matches: selector => selector === '[data-review-select-all]'};
    const form = {dataset: {reviewItems: JSON.stringify(inventory)}, elements: {
      items: {value: '[]'}, csrf_token: {value: 'original-csrf'}
    }};
    const result = {form, button, rejectButton, all, selectionCount: {textContent: ''},
      notice: {textContent: ''}, progress: {hidden: true, max: 1, value: 0}, count: {}, empty: {}};
    result.filters = ['ready', 'upload', 'all'].map(value => ({dataset: {queueFilter: value},
      disabled: false, setAttribute() {}}));
    result.counts = ['ready', 'upload'].map(value => ({dataset: {queueCount: value}}));
    result.rows = inventory.map(item => {
      const row = {hidden: false, dataset: {reviewKey: itemKey(item),
        reviewState: missing.some(candidate => itemKey(candidate) === itemKey(item)) ? 'upload' : 'ready'},
        remove() { result.rows.splice(result.rows.indexOf(this), 1); }};
      row.checkbox = {checked: false, disabled: false, matches: () => false, closest: () => row};
      row.querySelector = selector => selector === '[data-review-select]' ? row.checkbox
        : {textContent: `File ${item.index}`};
      return row;
    });
    result.querySelector = selector => ({
      '[data-queue-progress]': result.progress, '[data-queue-status]': result.notice,
      '.bulk-review': form, '[data-review-select-all]': all,
      '[data-review-selection-count]': result.selectionCount,
      '[data-review-action="approve"]': button, '[data-review-action="reject"]': rejectButton,
      '.approval-count': result.count, '[data-queue-empty]': result.empty
    })[selector] || null;
    result.querySelectorAll = selector => selector === 'button, input'
      ? [button, rejectButton, all, ...result.rows.map(row => row.checkbox), ...result.filters]
      : selector === '[data-queue-filter]' ? result.filters
      : selector === '[data-queue-count]' ? result.counts : result.rows;
    return result;
  }
  let current = view(pending);
  const original = current;
  const document = {
    activeElement: null,
    getElementById: () => current,
    querySelector: selector => selector === '[data-queue-status]' ? current.notice
      : selector === '[data-queue-progress]' ? current.progress : current.form,
    querySelectorAll: () => []
  };
  const selections = new Map(chosen.map(item => [itemKey(item), item.fingerprint]));
  const context = vm.createContext({
    FormData, document, busy: false, reviewingSelection: false, reviewSelections: selections,
    queueFilter: initialFilter, selected: '', dirty: false, revision: 'before',
    dialog: {open: false}, Date: class extends Date { static now() { return now; } },
    selectedTree() {}, expandBranch() {}, message() {},
    folderURL: () => '/library',
    html: async () => {
      refreshes++;
      if (refreshError) throw refreshError;
      return {doc: {getElementById: () => ({dataset: {revision: 'after'}})}};
    },
    replaceRegion: id => {
      if (id === 'approvals') current = view([...(refreshedItems || pending)
        .filter(item => !confirmed.has(itemKey(item))), ...arrivals]);
    },
    setTimeout: (callback, milliseconds) => {
      waits.push(milliseconds);
      waitStates.push({notice: current.notice.textContent, value: current.progress.value});
      now += milliseconds;
      queueMicrotask(callback);
    },
    fetch: async (url, options) => {
      const batch = JSON.parse(options.body.get('items'));
      calls.push({url, batch, body: options.body, time: now, value: current.progress.value});
      const response = await (respond?.({batch, attempt: calls.length, time: now}) ??
        new Response(JSON.stringify({[options.body.get('action') === 'approve' ? 'approved' : 'rejected']: batch.length}), {status: 200}));
      if (response.ok) batch.forEach(item => confirmed.add(itemKey(item)));
      return response;
    }
  });
  vm.runInContext(selection + filter + requests + review + refresh, context);
  vm.runInContext('filterQueue()', context);
  const submit = selectedAction => vm.runInContext('reviewQueue', context)(current.form, selectedAction || action);
  if (run) await submit();
  return {calls, waits, waitStates, context, original, current: () => current, selections, submit,
    choose: (index, checked) => {
      const checkbox = current.rows[index].checkbox;
      checkbox.checked = checked;
      vm.runInContext('changeReviewSelection', context)(checkbox);
    },
    chooseAll: checked => {
      current.all.checked = checked;
      vm.runInContext('changeReviewSelection', context)(current.all);
    },
    filter: value => { context.queueFilter = value; vm.runInContext('filterQueue()', context); },
    refreshes: () => refreshes, refresh: () => vm.runInContext('refresh', context)(true)};
}

const throttled = value => new Response('{"detail":"too many requests"}',
  {status: 429, headers: {'Retry-After': value}});
const failed = (status, detail) => new Response(JSON.stringify({detail}), {status});

test('Midway throttling resumes the exact group after the complete Retry-After delay', async () => {
  const result = await scenario({
    respond: ({attempt}) => attempt === 2 ? throttled('1.2501') : undefined,
    arrivals: [{kind: 'node', id: 'new-arrival', fingerprint: 'new-fingerprint'}]
  });
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)), [[0, 1, 2], [3, 4, 5], [3, 4, 5], [6, 7, 8]]);
  assert.equal(result.calls[1].body, result.calls[2].body);
  assert.deepEqual(result.calls[1].batch, items.slice(3, 6));
  assert.equal(result.calls[1].body.get('csrf_token'), 'original-csrf');
  assert.equal(result.calls[2].time - result.calls[1].time, 1251);
  assert.deepEqual(result.waits, [1000, 251]);
  assert.deepEqual(result.waitStates.map(state => state.value), [3, 3]);
  assert.match(result.waitStates[0].notice, /confirmed 3 approvals.*automatically in 2 seconds/);
  assert.match(result.waitStates[1].notice, /automatically in 1 second\./);
  assert.equal(result.current().progress.value, 9);
  assert.equal(result.current().progress.max, 9);
  assert.equal(result.current().progress.hidden, false);
  assert.match(result.current().notice.textContent, /^9 items were approved/);
  assert.deepEqual(JSON.parse(result.current().form.elements.items.value), []);
  assert.equal(result.current().rows[0].checkbox.checked, false);
  assert.equal(result.current().button.textContent, 'Approve selected');
  assert.equal(result.current().button.disabled, true);
  assert.equal(result.context.busy, false);
});

test('Repeated throttling preserves confirmed progress and can resume again', async () => {
  const result = await scenario({respond: ({attempt}) => [2, 3, 5].includes(attempt) ? throttled('1') : undefined});
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)),
    [[0, 1, 2], [3, 4, 5], [3, 4, 5], [3, 4, 5], [6, 7, 8], [6, 7, 8]]);
  assert.deepEqual(result.waitStates.map(state => state.value), [3, 3, 6]);
  assert.match(result.current().notice.textContent, /^9 items were approved/);
  assert.equal(result.current().button.disabled, true);
});

test('Persistent throttling stops with a recoverable selection after five waits', async () => {
  const result = await scenario({respond: ({attempt}) => attempt > 1 ? throttled('1') : undefined});
  assert.equal(result.calls.length, 7);
  assert.equal(result.waits.length, 5);
  assert.match(result.current().notice.textContent, /^The server confirmed 3 approvals.*Too many requests.*preserved/);
  assert.equal(result.current().progress.value, 3);
  assert.equal(result.current().progress.max, 9);
  assert.equal(result.current().button.disabled, false);
  assert.equal(result.current().button.textContent, 'Approve selected (6)');
  assert.equal(result.context.busy, false);
});

test('HTTP-date Retry-After is respected and excessively long waits do not retry early', async () => {
  const dateResult = await scenario({
    respond: ({attempt, time}) => attempt === 1 ? throttled(new Date(time + 3000).toUTCString()) : undefined
  });
  assert.equal(dateResult.calls[1].time - dateResult.calls[0].time, 3000);
  const longResult = await scenario({respond: () => throttled('301')});
  assert.equal(longResult.calls.length, 1);
  assert.equal(longResult.waits.length, 0);
  assert.match(longResult.current().notice.textContent, /Wait 301 seconds/);
});

test('Ambiguous transport, invalid confirmation, and session failures do not retry approvals', async t => {
  for (const failure of ['transport', 'json', 'count', '401', '403', '500', 'redirect']) {
    await t.test(failure, async () => {
      const result = await scenario({respond: ({attempt}) => {
        if (attempt !== 2) return;
        if (failure === 'transport') throw new Error('Connection interrupted.');
        if (failure === 'json') return new Response('unconfirmed', {status: 200});
        if (failure === 'count') return new Response('{"approved":1}', {status: 200});
        if (failure === 'redirect') return {type: 'opaqueredirect'};
        return failed(Number(failure), 'The operation failed.');
      }});
      assert.equal(result.calls.length, 2);
      assert.equal(result.waits.length, 0);
      assert.match(result.current().notice.textContent, /^The server confirmed 3 approvals/);
      assert.equal(result.current().progress.value, 3);
      assert.equal(result.context.busy, false);
      assert.equal(result.refreshes(), 1);
    });
  }
});

test('Stale groups isolate conflicts without changing fingerprints or approving new arrivals', async () => {
  const result = await scenario({respond: ({batch}) => batch.some(item => item.index === 4)
    ? failed(409, 'A proposal changed. Refresh and review it.') : undefined});
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)),
    [[0, 1, 2], [3, 4, 5], [3], [4], [5], [6, 7, 8]]);
  assert.equal(result.calls.flatMap(call => call.batch).every(item =>
    item.fingerprint === `original-fingerprint-${item.index}`), true);
  assert.equal(result.waits.length, 0);
  assert.match(result.current().notice.textContent, /^8 items were approved.*1 item still needs attention/);
  assert.equal(result.current().progress.value, 8);
  assert.deepEqual(JSON.parse(result.current().form.elements.items.value).map(item => item.index), [4]);
});

test('A failed reconciliation retains confirmed progress and removes confirmed selections', async () => {
  const result = await scenario({refreshError: new Error('Refresh is temporarily unavailable.')});
  assert.equal(result.current(), result.original);
  assert.match(result.current().notice.textContent, /^9 items were approved.*Reload the workspace/);
  assert.deepEqual(JSON.parse(result.current().form.elements.items.value), []);
  assert.equal(result.current().progress.value, 9);
  assert.equal(result.current().button.disabled, true);
});

test('Later workspace refreshes preserve the approval outcome and confirmed progress', async () => {
  const result = await scenario();
  const notice = result.current().notice.textContent;
  await result.refresh();
  assert.equal(result.current().notice.textContent, notice);
  assert.deepEqual(result.current().progress, {max: 9, value: 9, hidden: false});
});

test('Ordinary mutations do not inherit automatic approval retries', async () => {
  let calls = 0;
  const context = vm.createContext({Date, FormData, fetch: async () => { calls++; return throttled('1'); }});
  vm.runInContext(requests, context);
  await assert.rejects(vm.runInContext('postJSON', context)('/library/folders', new FormData()),
    error => error.status === 429);
  assert.equal(calls, 1);
});


test('No proposal is selected initially and both review actions require explicit selection', async () => {
  const result = await scenario({chosen: []});
  assert.equal(result.calls.length, 0);
  assert.equal(result.current().button.disabled, true);
  assert.equal(result.current().rejectButton.disabled, true);
  assert.equal(result.current().all.checked, false);
  assert.equal(result.current().all.indeterminate, false);
  assert.equal(result.selections.size, 0);
});

test('Select all applies only to visible rows and changing filters clears hidden selections', async () => {
  const result = await scenario({run: false, chosen: [], missing: items.slice(6)});
  result.chooseAll(true);
  assert.equal(result.current().all.checked, true);
  assert.equal(result.current().all.indeterminate, false);
  assert.equal(result.selections.size, 6);
  result.filter('upload');
  assert.equal(result.selections.size, 0);
  result.chooseAll(true);
  assert.equal(result.current().button.disabled, true);
  assert.equal(result.current().rejectButton.textContent, 'Reject selected (3)');
  await result.submit('reject');
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)), [[6, 7, 8]]);
  assert.equal(result.calls[0].body.get('action'), 'reject');
  assert.equal(result.calls[0].url, '/library/review-selected');
  assert.match(result.current().notice.textContent, /^3 items were rejected/);
});

test('Individual checkboxes update the mixed Select all state and can clear the selection', async () => {
  const result = await scenario({run: false, chosen: []});
  result.choose(0, true);
  assert.equal(result.current().all.checked, false);
  assert.equal(result.current().all.indeterminate, true);
  assert.equal(result.current().button.textContent, 'Approve selected (1)');
  result.chooseAll(true);
  assert.equal(result.current().all.checked, true);
  result.choose(0, false);
  assert.equal(result.current().all.indeterminate, true);
  assert.equal(result.selections.size, 8);
  result.chooseAll(false);
  assert.equal(result.current().all.indeterminate, false);
  assert.equal(result.selections.size, 0);
});

test('Only ready selected files are approved and the missing selection remains for rejection', async () => {
  const result = await scenario({initialFilter: 'all', missing: items.slice(6)});
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)), [[0, 1, 2], [3, 4, 5]]);
  assert.equal(result.current().button.disabled, true);
  assert.equal(result.current().rejectButton.textContent, 'Reject selected (3)');
  assert.match(result.current().selectionCount.textContent, /3 items are selected.*3 selected items.*only be rejected/);
  assert.equal(result.selections.size, 3);
});

test('Refresh preserves unchanged selections but clears changed fingerprints and leaves arrivals unchecked', async () => {
  const changed = {...items[1], fingerprint: 'changed-after-selection'};
  const arriving = {kind: 'node', id: 'new', fingerprint: 'new-proposal'};
  const result = await scenario({run: false, chosen: items.slice(0, 3),
    refreshedItems: [items[0], changed, ...items.slice(2)], arrivals: [arriving]});
  await result.refresh();
  assert.deepEqual([...result.selections.keys()], [itemKey(items[0]), itemKey(items[2])]);
  assert.equal(result.current().rows[1].checkbox.checked, false);
  assert.equal(result.current().rows.at(-1).checkbox.checked, false);
  assert.equal(result.current().all.indeterminate, true);
  await result.submit();
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)), [[0, 2]]);
});

test('Reject selected retries only the same confirmed rate-limited group and preserves its action', async () => {
  const result = await scenario({action: 'reject',
    respond: ({attempt}) => attempt === 2 ? throttled('2') : undefined});
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)),
    [[0, 1, 2], [3, 4, 5], [3, 4, 5], [6, 7, 8]]);
  assert.equal(result.calls[1].body, result.calls[2].body);
  assert.equal(result.calls.every(call => call.body.get('action') === 'reject'), true);
  assert.match(result.waitStates[0].notice, /confirmed 3 rejections.*Rejection will continue automatically in 2 seconds/);
  assert.match(result.current().notice.textContent, /^9 items were rejected/);
  assert.equal(result.current().progress.value, 9);
});

test('Reject selected isolates a stale proposal without rejecting its changed replacement', async () => {
  const replacement = {...items[4], fingerprint: 'latest-replacement'};
  const result = await scenario({action: 'reject', refreshedItems: items.map(item => item.index === 4 ? replacement : item),
    respond: ({batch}) => batch.some(item => item.index === 4)
      ? failed(409, 'A proposal changed. Refresh and review it.') : undefined});
  assert.deepEqual(result.calls.map(call => call.batch.map(item => item.index)),
    [[0, 1, 2], [3, 4, 5], [3], [4], [5], [6, 7, 8]]);
  assert.match(result.current().notice.textContent, /^8 items were rejected.*1 item still needs attention/);
  assert.equal(result.current().rows[0].checkbox.checked, false);
  assert.equal(result.selections.size, 0);
});

test('Rejected selections do not retry an ambiguous transport failure', async () => {
  const result = await scenario({action: 'reject', respond: ({attempt}) => {
    if (attempt === 2) throw new Error('Connection interrupted.');
  }});
  assert.equal(result.calls.length, 2);
  assert.equal(result.waits.length, 0);
  assert.match(result.current().notice.textContent, /^The server confirmed 3 rejections/);
  assert.equal(result.current().rejectButton.textContent, 'Reject selected (6)');
});

test('Invalid actions and empty selected groups never submit a review', async () => {
  const result = await scenario({run: false});
  await result.submit('delete');
  assert.equal(result.calls.length, 0);
});
