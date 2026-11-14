"""Differential fuzz: refactored run() vs the legacy fused interpreter.

Generates random legal (per CONTRACT) query graphs, including shared query
dicts, rings, duplicate column names, missing tables, nulls and type faults,
and compares the complete observable result (rows, error and ordered trace).

The legacy interpreter is loaded from the initial git commit into a private
module name, so no legacy execution code is kept on the module path.
"""
import importlib.util
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
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader('legacy_qf', loader=None))
    exec(compile(source, 'legacy_queryflow.py', 'exec'), module.__dict__)
    return module


legacy_qf = _load_legacy()

TABLE_DEFS = {
    't1': (['a', 'b'], [[1, 2], [3, None], [1, 5]]),
    't2': (['a', 'a'], [[1, 9], [2, 8], [None, 7]]),   # duplicate columns
    't3': (['k', 'v'], [['x', 10], ['y', None], ['x', 20]]),
    'empty': (['a', 'b'], []),
}
PARAM_NAMES = ['p1', 'p2']
SCALARS = [None, True, False, 0, 1, -2, 7, 'x', 'y']
AGG_OPS = ['count', 'sum', 'min']
BIN_OPS = ['eq', 'lt', 'add', 'div', 'and', 'or']


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.shared = []

    def col_ref(self, aliases):
        if aliases and self.rng.random() < 0.7:
            alias = self.rng.choice(aliases)
            return ['col', alias, self.rng.choice(['a', 'b', 'k', 'v'])]
        return ['col', self.rng.choice(['a', 'b', 'k', 'v'])]

    def expr(self, depth, aliases, allow_agg):
        r = self.rng.random()
        if depth <= 0 or r < 0.28:
            kind = self.rng.randrange(5)
            if kind == 0:
                return ['lit', self.rng.choice(SCALARS)]
            if kind == 1:
                return self.col_ref(aliases)
            if kind == 2:
                return ['param', self.rng.choice(PARAM_NAMES)]
            if kind == 3 and allow_agg:
                return ['agg', self.rng.choice(AGG_OPS),
                        ['lit', self.rng.choice(SCALARS)]]
            return ['not', ['lit', self.rng.choice([True, False, None])]]
        r = self.rng.random()
        if r < 0.25:
            return ['op', self.rng.choice(BIN_OPS),
                    self.expr(depth - 1, aliases, allow_agg),
                    self.expr(depth - 1, aliases, allow_agg)]
        if r < 0.4:
            return ['if', self.expr(depth - 1, aliases, allow_agg),
                    self.expr(depth - 1, aliases, allow_agg),
                    self.expr(depth - 1, aliases, allow_agg)]
        if r < 0.5:
            return ['coalesce'] + [self.expr(depth - 1, aliases, allow_agg)
                                   for _ in range(self.rng.randrange(1, 4))]
        if r < 0.75:
            tag = self.rng.choice(['scalar', 'exists', 'in'])
            sub = self.query(depth - 1, aliases)
            if tag == 'in':
                return ['in', self.expr(depth - 1, aliases, allow_agg), sub]
            return [tag, sub]
        if allow_agg:
            return ['agg', self.rng.choice(AGG_OPS),
                    self.expr(depth - 1, aliases, False)]
        return ['lit', self.rng.choice(SCALARS)]

    def source(self, depth, aliases):
        alias = 's' + str(len(aliases))
        if depth > 0 and self.rng.random() < 0.45:
            item = {'query': self.query(depth - 1, aliases), 'as': alias}
        else:
            name = self.rng.choice(list(TABLE_DEFS) + ['missing'])
            item = {'table': name, 'as': alias}
        if self.rng.random() < 0.3:
            item['left'] = True
        return item

    def query(self, depth, outer_aliases=()):
        # Occasionally reuse an existing query object to build sharing/rings.
        if self.shared and self.rng.random() < 0.18:
            q = self.rng.choice(self.shared)
            return q
        nsrc = self.rng.randrange(0, 3)
        sources = []
        aliases = list(outer_aliases)
        for _ in range(nsrc):
            item = self.source(depth, aliases)
            sources.append(item)
            aliases.append(item['as'])
        grouped = self.rng.random() < 0.35
        select = [['out' + str(i),
                   self.expr(self.rng.randrange(0, depth + 2),
                             aliases, grouped)]
                  for i in range(self.rng.randrange(1, 3))]
        q = {'select': select}
        if sources:
            q['from'] = sources
        if self.rng.random() < 0.4:
            q['where'] = self.expr(self.rng.randrange(0, depth + 2),
                                   aliases, False)
        if grouped:
            q['group'] = [['col', self.rng.choice(['a', 'b', 'k', 'v'])]] \
                if aliases and self.rng.random() < 0.7 else []
            if self.rng.random() < 0.5:
                q['having'] = self.expr(self.rng.randrange(0, depth + 1),
                                        aliases, True)
        if self.rng.random() < 0.3:
            q['distinct'] = True
        if self.rng.random() < 0.3 and select:
            q['order'] = [[self.rng.randrange(len(select)),
                           self.rng.random() < 0.5]]
        if self.rng.random() < 0.3:
            q['limit'] = self.rng.randrange(0, 4)
        self.shared.append(q)
        return q


def main():
    rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1234)
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    for trial in range(trials):
        gen = Gen(rng)
        query = gen.query(rng.randrange(1, 5))
        tables = {name: {'columns': list(cols), 'rows': [list(r) for r in rows]}
                  for name, (cols, rows) in TABLE_DEFS.items()}
        params = {name: rng.choice(SCALARS) for name in PARAM_NAMES}
        # Sometimes omit a parameter to exercise PARAM faults.
        if rng.random() < 0.3:
            del params[rng.choice(PARAM_NAMES)]
        want = legacy_qf.run(query, tables, params)
        got = new_qf.run(query, tables, params)
        if want != got:
            import json
            print('MISMATCH trial', trial)
            print(json.dumps(query, ensure_ascii=False))
            print('legacy:', json.dumps(want, ensure_ascii=False))
            print('new   :', json.dumps(got, ensure_ascii=False))
            sys.exit(1)
    print('FUZZ_OK', trials)


if __name__ == '__main__':
    main()
