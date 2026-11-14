"""Executable business regressions. Expected results do not call production evaluation."""
from copy import deepcopy
import json
import sys
from queryflow import run
from samples import TABLES, ENRICH, TOTALS, shared, recursive


def select(expr, **parts):
    return dict(parts, select=[['v', expr]])


def regression():
    assert run(ENRICH, TABLES) == {
        'columns': ['site', 'adjusted'],
        'rows': [['A', 12], ['A', 12], ['B', None], ['C', 5]],
        'trace': [['scan', 'readings']] + [['scan', 'calibration']] * 4,
    }
    assert run(TOTALS, TABLES) == {
        'columns': ['site', 'n', 'total'],
        'rows': [['A', 2, 24], ['B', 0, None], ['C', 1, 5]],
        'trace': [['scan', 'readings']] + [['scan', 'calibration']] * 4,
    }
    assert TABLES['readings']['rows'][0] == ['A', 10]
    # Empty-input aggregate still produces one group; grouped empty input produces none.
    empty = {'from': [{'table': 'empty', 'as': 'e'}], 'group': [],
             'select': [['n', ['agg', 'count', ['col', 'e', 'value']]],
                        ['s', ['agg', 'sum', ['col', 'e', 'value']]],
                        ['v', ['col', 'e', 'value']]]}
    assert run(empty, TABLES) == {
        'columns': ['n', 's', 'v'], 'rows': [[0, None, None]],
        'trace': [['scan', 'empty']],
    }
    empty['group'] = [['col', 'e', 'site']]
    assert run(empty, TABLES)['rows'] == []
    # Projection duplicates are a bag until DISTINCT; LIMIT pulls no extra row.
    q = select(['col', 'r', 'site'], **{'from': [{'table': 'readings', 'as': 'r'}]})
    assert run(q, TABLES)['rows'] == [['A'], ['A'], ['B'], ['C']]
    q.update(distinct=True, order=[[0, True]], limit=2)
    assert run(q, TABLES)['rows'] == [['C'], ['B']]
    # Conditional, boolean and coalesce evaluation must retain short-circuit faults.
    bad = ['op', 'div', ['lit', 1], ['lit', 0]]
    for expr, expected in [
        (['if', ['lit', None], bad, ['lit', 8]], 8),
        (['op', 'and', ['lit', False], bad], False),
        (['op', 'or', ['lit', True], bad], True),
        (['op', 'and', ['lit', None], ['lit', False]], False),
        (['op', 'or', ['lit', None], ['lit', False]], None),
        (['coalesce', ['lit', 4], ['col', 'missing']], 4),
    ]:
        assert run(select(expr), TABLES) == {
            'columns': ['v'], 'rows': [[expected]], 'trace': [],
        }
    for expr, error in [
        (['op', 'and', ['lit', None], bad], 'DIV_ZERO'),
        (['col', 'missing'], 'UNKNOWN:missing'),
        (['param', 'missing'], 'PARAM:missing'),
        (['not', ['lit', 1]], 'BOOL_REQUIRED'),
        (['agg', 'sum', ['lit', 1]], 'NO_GROUP'),
        (['op', 'lt', ['lit', 1], ['lit', 'x']], 'COMPARISON'),
    ]:
        assert run(select(expr), TABLES) == {'error': error, 'trace': []}
    # EXISTS skips bad projection, but not HAVING. LIMIT=0 skips even source lookup.
    projected_bad = select(bad)
    assert run(select(['exists', projected_bad]), TABLES) == {
        'columns': ['v'], 'rows': [[True]], 'trace': [['subquery', 'exists']],
    }
    zero = select(bad, **{'from': [{'table': 'absent', 'as': 'x'}], 'limit': 0})
    assert run(select(['exists', zero]), TABLES) == {
        'columns': ['v'], 'rows': [[False]], 'trace': [['subquery', 'exists']],
    }
    with_having = select(['lit', 1], group=[], having=bad)
    assert run(select(['exists', with_having]), TABLES) == {
        'error': 'DIV_ZERO', 'trace': [['subquery', 'exists']],
    }
    # A scalar subquery evaluates its second projected row before cardinality rejection.
    values = {'t': {'columns': ['v'], 'rows': [[1], [0], [0]]}}
    sub = select(['op', 'div', ['lit', 1], ['col', 't', 'v']],
                 **{'from': [{'table': 't', 'as': 't'}]})
    assert run(select(['scalar', sub]), values) == {
        'error': 'DIV_ZERO', 'trace': [['subquery', 'scalar'], ['scan', 't']],
    }
    sub['select'][0][1] = ['col', 't', 'v']
    assert run(select(['scalar', sub]), values)['error'] == 'SCALAR_MANY'
    sub['limit'] = 1
    assert run(select(['scalar', sub]), values)['rows'] == [[1]]
    sub['limit'] = 0
    assert run(select(['scalar', sub]), values)['rows'] == [[None]]
    # IN stops at a match; NULL needle on empty relation is False.
    one_col = select(['col', 't', 'v'], **{'from': [{'table': 't', 'as': 't'}]})
    for needle, rows, expected in [
        (None, [], False), (None, [[1]], None), (2, [[None], [2]], True),
        (2, [[None], [1]], None), (2, [[1]], False),
    ]:
        table = {'t': {'columns': ['v'], 'rows': rows}}
        assert run(select(['in', ['lit', needle], one_col]), table)['rows'] == [[expected]]
    # Qualified alias shadowing must not fall back if the inner alias lacks the column.
    leaf = select(['col', 'r', 'value'])
    middle = select(['scalar', leaf], **{'from': [{'table': 'calibration', 'as': 'r'}]})
    outer = select(['scalar', middle], **{'from': [{'table': 'readings', 'as': 'r'}]})
    assert run(outer, TABLES) == {
        'error': 'UNKNOWN:r.value',
        'trace': [['scan', 'readings'], ['subquery', 'scalar'],
                  ['scan', 'calibration'], ['subquery', 'scalar']],
    }
    # Unqualified lookup can instead pass through a frame having no matching column.
    leaf['select'][0][1] = ['col', 'value']
    middle['limit'] = 1
    assert run(outer, TABLES)['rows'] == [[10], [10], [None], [5]]
    # The empty current prefix of a lateral source does not hide an outer scope.
    lateral = select(['col', 'x', 'v'], **{'from': [{'query': leaf, 'as': 'x'}]})
    outer['select'][0][1] = ['scalar', lateral]
    assert run(outer, TABLES)['rows'] == [[10], [10], [None], [5]]
    # Duplicate output names propagate ambiguity through a lateral relation.
    dup = {'select': [['v', ['lit', 1]], ['v', ['lit', 2]]]}
    use = select(['col', 'x', 'v'], **{'from': [{'query': dup, 'as': 'x'}]})
    assert run(use, TABLES) == {'error': 'AMBIGUOUS:x.v', 'trace': []}
    # The prefix used while evaluating a lateral source excludes that source's own alias.
    same = select(['col', 'r', 'value'])
    q = select(['col', 'r', 'v'], **{'from': [{'query': same, 'as': 'r'}]})
    q = select(['scalar', q], **{'from': [{'table': 'readings', 'as': 'r'}]})
    assert run(q, TABLES)['rows'] == [[10], [10], [None], [5]]
    # Two bindings with the same alias in one frame are ambiguous, even if only one has v.
    q = select(['col', 'a', 'v'], **{'from': [
        {'table': 'one', 'as': 'a'}, {'table': 'calibration', 'as': 'a'}]})
    tables = deepcopy(TABLES)
    tables['one'] = {'columns': ['v'], 'rows': [[9]]}
    assert run(q, tables)['error'] == 'AMBIGUOUS:a.v'
    # Correlation inside aggregate arguments follows each member, not the representative.
    leaf = select(['col', 'r', 'value'])
    q = select(['agg', 'sum', ['scalar', leaf]], **{
        'from': [{'table': 'readings', 'as': 'r'}], 'group': []})
    assert run(q, TABLES) == {
        'columns': ['v'], 'rows': [[25]], 'trace': [['scan', 'readings']] +
        [['subquery', 'scalar']] * 4,
    }
    nested = select(['agg', 'sum', ['agg', 'count', ['lit', 1]]], group=[])
    assert run(nested, TABLES)['error'] == 'NESTED_AGG'
    # A subquery owns a new aggregate context.
    local = select(['agg', 'count', ['lit', 1]], group=[])
    q['select'][0][1] = ['agg', 'sum', ['scalar', local]]
    assert run(q, TABLES)['rows'] == [[4]]
    # A reused DAG has exponentially many lexical contexts, but one selected execution path.
    for depth in (1, 8, 18):
        q, tables, params = shared(depth)
        result = run(q, tables, params)
        assert result['rows'] == [[depth + depth // 2]]
        assert len(result['trace']) == 3 * depth
        params = {name: not value for name, value in params.items()}
        assert run(q, tables, params)['rows'] == [[2 * depth - depth // 2]]
    # A finite query object graph may contain a recursive invocation cycle.
    q, tables, params = recursive()
    result = run(q, tables, params)
    assert result['rows'] == [[0], [6], [21]]
    assert result['columns'] == ['total']
    assert result['trace'] == [['scan', 'counters']] + [['subquery', 'scalar']] * 21
    print('BUSINESS_REGRESSION_OK')


def demo():
    print(json.dumps({'enriched': run(ENRICH, TABLES),
                      'totals': run(TOTALS, TABLES)}, ensure_ascii=False))
    q, tables, params = shared(8)
    print(json.dumps({'shared_query': run(q, tables, params)}, ensure_ascii=False))
    q, tables, params = recursive()
    print(json.dumps({'recursive_query': run(q, tables, params)}, ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'demo':
        demo()
    else:
        regression()
