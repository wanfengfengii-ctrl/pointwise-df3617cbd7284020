"""Differential fuzz for shared query objects and recursive invocation rings.

Builds a small pool of query objects with explicit reuse (DAG sharing) and
back-edges (rings), then compares the full observable result against the
legacy interpreter across parameter combinations and varied row contents.
The legacy interpreter is loaded from the initial git commit, never imported
from a live module.
"""
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queryflow as new_qf  # noqa: E402


def _load_legacy():
    source = subprocess.run(
        ['git', 'show', 'HEAD:queryflow.py'], cwd=Path(__file__).parent,
        check=True, capture_output=True, text=True).stdout
    module = type(sys)('legacy_qf')
    exec(compile(source, 'legacy_queryflow.py', 'exec'), module.__dict__)
    return module


legacy_qf = _load_legacy()

TABLE_COLS = {'one': ['v'], 'two': ['v'], 'nums': ['n']}
OPS = ['add', 'eq', 'lt', 'and', 'or']


def build(rng, depth):
    # Reusable leaf like samples.shared: sums an imported chain of q_i.v.
    total = ['lit', 0]
    for i in range(depth):
        total = ['op', 'add', total, ['col', 'q' + str(i), 'v']]
    child = {'select': [['v', total]]}
    pool = [child]
    for i in range(depth):
        left = {'from': [{'table': 'one', 'as': 'q' + str(i)}],
                'select': [['v', ['scalar', child]]]}
        right = {'from': [{'table': 'two', 'as': 'q' + str(i)}],
                 'select': [['v', ['scalar', child]]]}
        # Randomly reuse an earlier pool object to add DAG sharing.
        shared = rng.choice(pool)
        node = {'select': [['v', [
            'if', ['param', 'p' + str(i)], ['scalar', left],
            ['if', ['param', 'q' + str(i)], ['scalar', right],
             ['scalar', shared]],
        ]]]}
        pool.extend([left, right, node])
        child = node

    # Add a recursive ring a la samples.recursive, reusing pool objects.
    worker = {'select': []}
    decrement = {'select': [['n', ['op', 'add', ['col', 'r', 'n'],
                                   ['lit', -1]]]]}
    step = {'from': [{'query': decrement, 'as': 'r'}],
            'select': [['v', ['scalar', worker]]]}
    worker['select'] = [['v', [
        'if', ['op', 'eq', ['col', 'r', 'n'], ['lit', 0]], ['lit', 0],
        ['op', 'add', ['col', 'r', 'n'], ['scalar', step]],
    ]]]
    root = {
        'from': [
            {'table': 'nums', 'as': 'r'},
            {'query': child, 'as': 'extra', 'left': bool(rng.randrange(2))},
        ],
        'select': [['total', ['scalar', worker]],
                   ['side', ['scalar', rng.choice(pool)]]],
    }
    pool.extend([worker, decrement, step, root])
    return root, depth


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rng = random.Random(seed)
    for trial in range(trials):
        depth = rng.randrange(1, 8)
        query, _ = build(rng, depth)
        max_n = rng.randrange(0, 6)
        tables = {
            'one': {'columns': ['v'], 'rows': [[rng.randrange(0, 5)]]},
            'two': {'columns': ['v'], 'rows': [[rng.randrange(0, 5)]]},
            'nums': {'columns': ['n'],
                     'rows': [[n] for n in range(max_n)]},
        }
        names = ['p' + str(i) for i in range(depth)] + \
                ['q' + str(i) for i in range(depth)]
        for mask in range(min(4, 1 << min(4, len(names)))):
            params = {}
            for i, name in enumerate(names):
                choice = rng.randrange(3)
                if choice == 0:
                    continue  # missing -> PARAM fault when reached
                params[name] = bool((mask >> (i % 4)) & 1) if choice == 1 \
                    else rng.choice([True, False, None])
            want = legacy_qf.run(query, tables, params)
            got = new_qf.run(query, tables, params)
            if want != got:
                import json
                print('MISMATCH trial', trial, 'mask', mask)
                print('legacy:', json.dumps(want, ensure_ascii=False))
                print('new   :', json.dumps(got, ensure_ascii=False))
                sys.exit(1)
    print('RING_FUZZ_OK', trials)


if __name__ == '__main__':
    main()
