from central_brain.derived_cache import reconcile


def test_change_invalidation_deletion_scope_and_untrusted_output():
    docs = [{'id': 'a', 'version': 1, 'path': '/A', 'text': 'Logo rules use approved colours.'},
            {'id': 'b', 'version': 1, 'path': '/B', 'text': 'Payroll runs monthly.'}]
    good = lambda text: '{"category":"brand","evidence":"Logo rules"}'
    first, stats = reconcile(docs[:1], {}, good, 'model-v1', 'actor-a')
    assert stats['processed'] == 1
    second, stats = reconcile(docs[:1], first, lambda text: 1 / 0, 'model-v1', 'actor-a')
    assert stats['processed'] == 0 and stats['reused'] == 1
    docs[0]['version'] = 2
    changed, stats = reconcile(docs[:1], second, good, 'model-v1', 'actor-a')
    assert stats['processed'] == 1
    cleared, stats = reconcile([], changed, good, 'model-v1', 'actor-a')
    assert stats['removed'] == 1 and not cleared['entries']
    _, stats = reconcile(docs[:1], changed, good, 'model-v1', 'actor-b')
    assert stats['processed'] == 1
    rejected, stats = reconcile(docs[:1], {}, lambda text: '{"category":"brand","evidence":"Invented quote"}', 'model-v1', 'actor-a')
    assert stats['rejected'] == 1 and rejected['entries']['a']['derived']['needs_review']
