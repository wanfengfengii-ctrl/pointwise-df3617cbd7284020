"""Refactoring contract probes, enabled after compiler.py and runtime.py exist."""
import ast
from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess
import sys

from samples import TABLES, TOTALS, shared, recursive


def query_count(root):
    seen = set()
    count = 0

    def visit(value):
        nonlocal count
        if not isinstance(value, (dict, list)):
            return
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            if 'select' in value:
                count += 1
            for child in value.values():
                visit(child)
        else:
            for child in value:
                visit(child)
    visit(root)
    return count


def graph_stamp(root):
    """Inspect sharing as well as values without expanding or looping through the query graph."""
    seen, nodes = {}, []

    def visit(value):
        if not isinstance(value, (dict, list)):
            return ['value', value]
        if id(value) in seen:
            return ['ref', seen[id(value)]]
        index = len(nodes)
        seen[id(value)] = index
        nodes.append(None)
        if isinstance(value, dict):
            body = ['dict', [[k, visit(v)] for k, v in sorted(value.items())]]
        else:
            body = ['list', [visit(v) for v in value]]
        nodes[index] = body
        return ['ref', index]
    visit(root)
    return nodes


def prepare(query, tables, params):
    compiler = importlib.import_module('compiler')
    stamp = graph_stamp(query)
    catalog = {name: table['columns'][:] for name, table in tables.items()}
    catalog_before = deepcopy(catalog)
    plan = compiler.compile_query(query, catalog)
    assert stamp == graph_stamp(query), 'compiler mutated query or sharing'
    assert catalog == catalog_before, 'compiler mutated catalog'
    assert type(plan) is dict and plan['version'] == 1
    assert type(plan['units']) is list and len(plan['units']) == query_count(query)
    assert type(plan['entry']) is int and 0 <= plan['entry'] < len(plan['units'])
    for unit in plan['units']:
        imports = unit['imports']
        assert type(imports) is list
        assert all(type(pair) is list and len(pair) == 2 and
                   (pair[0] is None or type(pair[0]) is str) and type(pair[1]) is str
                   for pair in imports)
        assert len(imports) == len({tuple(pair) for pair in imports})
    assert plan['tables'] == sorted(tables)
    assert len(plan['parameters']) == len(set(plan['parameters']))
    assert plan['parameters'] == sorted(plan['parameters'])
    sources = [tables[name]['rows'] for name in plan['tables']]
    arguments = [[name in params, params.get(name)] for name in plan['parameters']]
    plan = json.loads(json.dumps(plan, allow_nan=False))
    return plan, sources, arguments


def detached(plan, sources, arguments):
    # A fresh process has neither query, catalog, compiler nor process-local plan registry.
    script = """
import builtins, importlib.abc, json, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'compiler', 'queryflow', 'samples'}:
            raise AssertionError('runtime dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from runtime import execute_plan
p, s, a = json.load(sys.stdin)
print(json.dumps(execute_plan(p, s, a), allow_nan=False))
"""
    result = subprocess.run([sys.executable, '-B', '-c', script],
                            input=json.dumps([plan, sources, arguments]),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def check_runtime_purity():
    # Import compiler separately with its execution-side imports unavailable.
    script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'runtime', 'queryflow', 'samples'}:
            raise AssertionError('compiler dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from compiler import compile_query
p = compile_query({'select': [['v', ['lit', 7]]]}, {})
assert p['version'] == 1
"""
    subprocess.run([sys.executable, '-B', '-c', script], check=True)


def regression():
    facade = ast.parse(Path('queryflow.py').read_text(encoding='utf-8'))
    assert not any(isinstance(n, ast.ClassDef) for n in ast.walk(facade))
    functions = [n for n in ast.walk(facade) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(functions) == 1 and functions[0].name == 'run', 'facade contains execution helpers'
    check_runtime_purity()
    plan, sources, arguments = prepare(TOTALS, TABLES, {})
    frozen = deepcopy([plan, sources, arguments])
    assert sorted(len(unit['imports']) for unit in plan['units']) == [0, 0, 1]
    assert [u['imports'] for u in plan['units'] if u['imports']] == [[['r', 'site']]]
    assert detached(plan, sources, arguments) == {
        'columns': ['site', 'n', 'total'],
        'rows': [['A', 2, 24], ['B', 0, None], ['C', 1, 5]],
        'trace': [['scan', 'readings']] + [['scan', 'calibration']] * 4,
    }
    assert frozen == [plan, sources, arguments], 'test-side data changed'
    # The same compiled unit graph is reused under different environments and parameters.
    q, tables, params = shared(18)
    plan, sources, arguments = prepare(q, tables, params)
    assert sorted(len(u['imports']) for u in plan['units']) == sorted(
        [18] + [n for n in range(18) for _ in range(3)])
    assert detached(plan, sources, arguments)['rows'] == [[27]]
    changed = [[present, not value] for present, value in arguments]
    assert detached(plan, sources, changed)['rows'] == [[27]]
    # Asymmetric branch choices expose accidental hard-wiring of captures.
    assert detached(plan, sources, [[True, True] for _ in arguments])['rows'] == [[18]]
    assert detached(plan, sources, [[True, False] for _ in arguments])['rows'] == [[36]]
    q, tables, params = recursive()
    plan, sources, arguments = prepare(q, tables, params)
    assert len(plan['units']) == 4
    assert sorted(len(u['imports']) for u in plan['units']) == [0, 1, 1, 1]
    assert all(u['imports'] == [['r', 'n']] for u in plan['units'] if u['imports'])
    assert detached(plan, sources, arguments) == {
        'columns': ['total'], 'rows': [[0], [6], [21]],
        'trace': [['scan', 'counters']] + [['subquery', 'scalar']] * 21,
    }
    assert detached(plan, [[[2], [4]]], arguments)['rows'] == [[3], [10]]
    # A dormant unresolved reference remains an execution fault only if selected.
    q = {'select': [['v', ['if', ['param', 'use'], ['lit', 9], ['col', 'x']]]]}
    plan, sources, arguments = prepare(q, {}, {'use': True})
    assert detached(plan, sources, arguments) == {'columns': ['v'], 'rows': [[9]], 'trace': []}
    assert detached(plan, sources, [[True, False]]) == {'error': 'UNKNOWN:x', 'trace': []}
    assert detached(plan, sources, [[False, None]]) == {'error': 'PARAM:use', 'trace': []}
    # Same executable plan, different rows: catalog and values are not baked together.
    q = {'from': [{'table': 't', 'as': 'r'}], 'select': [['v', ['col', 'r', 'v']]]}
    plan, sources, arguments = prepare(q, {'t': {'columns': ['v'], 'rows': [[3]]}}, {})
    assert detached(plan, [[[17], [None]]], arguments)['rows'] == [[17], [None]]
    runtime = importlib.import_module('runtime')
    arguments = []
    frozen = deepcopy([plan, sources, arguments])
    for _ in range(2):
        assert runtime.execute_plan(plan, sources, arguments)['rows'] == [[3]]
        assert frozen == [plan, sources, arguments], 'runtime mutated its inputs'
    print('BOUNDARY_REGRESSION_OK')


if __name__ == '__main__':
    regression()
