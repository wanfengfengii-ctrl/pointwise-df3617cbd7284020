"""Public data fixtures and transformations; no external data source required."""
from copy import deepcopy

TABLES = {
    'readings': {
        'columns': ['site', 'value'],
        'rows': [['A', 10], ['A', 10], ['B', None], ['C', 5]],
    },
    'calibration': {
        'columns': ['site', 'bias'],
        'rows': [['A', 2], ['B', None]],
    },
    'empty': {'columns': ['site', 'value'], 'rows': []},
}
ENRICH = {
    'from': [
        {'table': 'readings', 'as': 'r'},
        {'query': {
            'from': [{'table': 'calibration', 'as': 'c'}],
            'where': ['op', 'eq', ['col', 'c', 'site'], ['col', 'r', 'site']],
            'select': [['bias', ['col', 'c', 'bias']]],
        }, 'as': 'x', 'left': True},
    ],
    'select': [
        ['site', ['col', 'r', 'site']],
        ['adjusted', ['op', 'add', ['col', 'r', 'value'],
                      ['coalesce', ['col', 'x', 'bias'], ['lit', 0]]]],
    ],
}
TOTALS = {
    'from': [{'query': deepcopy(ENRICH), 'as': 'e'}],
    'group': [['col', 'e', 'site']],
    'select': [
        ['site', ['col', 'e', 'site']],
        ['n', ['agg', 'count', ['col', 'e', 'adjusted']]],
        ['total', ['agg', 'sum', ['col', 'e', 'adjusted']]],
    ],
    'order': [[0, False]],
}
REQUEST = {'query': TOTALS, 'tables': TABLES, 'params': {}}

def shared(depth=12):
    """O(depth) query objects, 2**depth lexical invocation paths."""
    total = ['lit', 0]
    for i in range(depth):
        total = ['op', 'add', total, ['col', 'q' + str(i), 'v']]
    child = {'select': [['v', total]]}
    for i in range(depth):
        left = {
            'from': [{'table': 'one', 'as': 'q' + str(i)}],
            'select': [['v', ['scalar', child]]],
        }
        right = {
            'from': [{'table': 'two', 'as': 'q' + str(i)}],
            'select': [['v', ['scalar', child]]],
        }
        child = {'select': [['v', ['if', ['param', 'p' + str(i)],
                                           ['scalar', left], ['scalar', right]]]]}
    tables = {
        'one': {'columns': ['v'], 'rows': [[1]]},
        'two': {'columns': ['v'], 'rows': [[2]]},
    }
    params = {'p' + str(i): i % 2 == 0 for i in range(depth)}
    return child, tables, params

def recursive():
    """Recursive reusable conversion: sum 1..n through a lateral decrement."""
    worker = {'select': []}
    decrement = {'select': [['n', ['op', 'add', ['col', 'r', 'n'], ['lit', -1]]]]}
    step = {
        'from': [{'query': decrement, 'as': 'r'}],
        'select': [['v', ['scalar', worker]]],
    }
    worker['select'] = [['v', [
        'if', ['op', 'eq', ['col', 'r', 'n'], ['lit', 0]], ['lit', 0],
        ['op', 'add', ['col', 'r', 'n'], ['scalar', step]],
    ]]]
    root = {
        'from': [{'table': 'counters', 'as': 'r'}],
        'select': [['total', ['scalar', worker]]],
    }
    tables = {'counters': {'columns': ['n'], 'rows': [[0], [3], [6]]}}
    return root, tables, {}
