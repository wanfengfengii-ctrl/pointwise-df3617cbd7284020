"""Positional plan runtime: evaluates JSON data plans without name lookup.

The runtime owns *data evaluation* only.  Every column reference in a plan
has already been bound by the compiler to a numeric local slot
(``['slot', frame-offset, column]``) or a numeric import link
(``['imp', import-index]``); deferred compile-time faults arrive as
``['fault', message]`` nodes.  Execution therefore never sees column names,
aliases or an original AST, never searches a catalog and never consults a
compiler or registry.  Units reference each other solely by numeric index
and receive per-call positional import thunks, so shared and recursive
units execute as a single compiled unit rather than per-call expansions.

Inputs are positional: ``sources`` are row arrays ordered by
``plan['tables']`` (no column names) and ``arguments`` are
``[provided, value]`` pairs ordered by ``plan['parameters']``.  The plan,
sources and arguments are never modified and may be reused across runs.

Standard library only; no dependency on compiler, queryflow or samples.
"""

import json


class Fault(Exception):
    pass


def key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def _truth(value):
    if value is None:
        return None
    if type(value) is not bool:
        raise Fault('BOOL_REQUIRED')
    return value


class _Env:
    """One invocation's local frame and its positional import thunks.

    Outer scopes are not walked at runtime: each import thunk closes over
    the caller environment that the compiler linked it to.
    """

    __slots__ = ('frame', 'thunks')

    def __init__(self, frame, thunks):
        self.frame = frame    # flat slot list: binding rows concatenated
        self.thunks = thunks  # zero-argument callables, one per import

    def read(self, ref):
        kind = ref[0]
        if kind == 'slot':
            return self.frame[ref[1] + ref[2]]
        if kind == 'imp':
            return self.thunks[ref[1]]()
        # ['fault', message]: a binding error whose trigger is evaluation.
        raise Fault(ref[1])


def _thunk(ref, env):
    def resolve():
        return env.read(ref)
    return resolve


def execute_plan(plan, sources, arguments):
    """Execute *plan* over positional *sources* and *arguments*.

    Returns the legacy success/error result shape.  None of the three
    inputs is mutated; the call is repeatable with different contents.
    """
    runner = _Runner(plan, sources, arguments)
    entry = plan['entry']
    entry_unit = plan['units'][entry]
    entry_thunks = [runner._entry_fault(pair) for pair in entry_unit['imports']]
    try:
        rows = list(runner._execute(entry, entry_thunks, False))
        return {'columns': list(entry_unit['outputs']),
                'rows': rows, 'trace': runner.trace}
    except Fault as exc:
        return {'error': str(exc), 'trace': runner.trace}


class _Runner:
    def __init__(self, plan, sources, arguments):
        self.plan = plan
        self.sources = sources
        self.arguments = arguments
        self.trace = []

    def _entry_fault(self, pair):
        alias, column = pair
        message = ('UNKNOWN:' + column if alias is None
                   else 'UNKNOWN:' + alias + '.' + column)

        def fail():
            raise Fault(message)
        return fail

    # ------------------------------------------------------------- execution
    def _execute(self, uid, thunks, existence):
        unit = self.plan['units'][uid]
        if unit['limit'] == 0:
            return

        def source(index, frame):
            if index == len(unit['sources']):
                env = _Env(frame, thunks)
                where = unit['where']
                if where is None or _truth(self._eval(where, env)) is True:
                    yield env
                return
            item = unit['sources'][index]
            if item['kind'] == 'table':
                name = item['table']
                self.trace.append(['scan', name])
                table_index = item['table_index']
                if table_index < 0:
                    raise Fault('TABLE:' + name)
                rows = iter(self.sources[table_index])
            else:
                prefix_env = _Env(frame, thunks)
                child_thunks = [_thunk(ref, prefix_env)
                                for ref in item['args']]
                rows = self._execute(item['unit'], child_thunks, False)
            seen = False
            for row in rows:
                seen = True
                # Flat concatenation of binding rows matches the compiler's
                # cumulative column-width slot offsets.
                yield from source(index + 1, frame + list(row))
            if not seen and item['left']:
                yield from source(index + 1,
                                  frame + [None] * item['width'])

        def candidates():
            if unit['group'] is None:
                for env in source(0, []):
                    yield env, None
                return
            groups = {}
            for env in source(0, []):
                values = [self._eval(expr, env) for expr in unit['group']]
                groups.setdefault(key(values), []).append(env)
            if not unit['group'] and not groups:
                groups['[]'] = []
            for members in groups.values():
                if members:
                    env = members[0]
                else:
                    # No physical source is scanned here; only declared
                    # column layouts matter for the synthetic null frame,
                    # but a missing table still raises its TABLE fault.
                    frame = []
                    for item in unit['sources']:
                        if item['kind'] == 'table' and \
                                item['table_index'] < 0:
                            raise Fault('TABLE:' + item['table'])
                        frame.extend([None] * item['width'])
                    env = _Env(frame, thunks)
                having = unit['having']
                # HAVING runs in the unit's aggregate context: top-level aggs
                # see the members, while a nested agg still hits NESTED_AGG
                # because member expressions evaluate with aggregating=True.
                if having is None or _truth(
                        self._eval(having, env, members)) is True:
                    yield env, members

        def projected():
            for env, group in candidates():
                if existence:
                    yield []
                else:
                    yield [self._eval(expr, env, group)
                           for expr in unit['select']]

        # EXISTS ignores projection, DISTINCT and ORDER; LIMIT=0 already
        # suppressed the whole unit above.
        rows = projected()
        if not existence and unit['distinct']:
            seen = set()

            def unique(upstream):
                for row in upstream:
                    encoded = key(row)
                    if encoded not in seen:
                        seen.add(encoded)
                        yield row
            rows = unique(rows)
        if not existence and unit['order'] is not None:
            rows = list(rows)
            for index, descending in reversed(unit['order']):
                rows.sort(
                    key=lambda row, i=index:
                        (row[i] is not None, type(row[i]).__name__, row[i]),
                    reverse=descending)
        limit = unit['limit']
        iterator = iter(rows)
        count = 0
        while limit is None or count < limit:
            row = next(iterator, None)
            if row is None:
                break
            yield row
            count += 1

    # ------------------------------------------------------------ evaluation
    def _eval(self, node, env, group=None, aggregating=False):
        tag = node[0]
        if tag == 'lit':
            return node[1]
        if tag in ('slot', 'imp', 'fault'):
            return env.read(node)
        if tag == 'param':
            provided, value = self.arguments[node[1]]
            if not provided:
                raise Fault('PARAM:' + node[2])
            return value
        if tag == 'if':
            branch = node[2] if _truth(
                self._eval(node[1], env, group, aggregating)) is True \
                else node[3]
            return self._eval(branch, env, group, aggregating)
        if tag == 'coalesce':
            for part in node[1:]:
                value = self._eval(part, env, group, aggregating)
                if value is not None:
                    return value
            return None
        if tag == 'not':
            value = _truth(self._eval(node[1], env, group, aggregating))
            return None if value is None else not value
        if tag in ('scalar', 'exists', 'in'):
            callee = node[1]
            needle = self._eval(node[3], env, group, aggregating) \
                if tag == 'in' else None
            self.trace.append(['subquery', tag])
            if tag != 'exists' and \
                    len(self.plan['units'][callee]['outputs']) != 1:
                raise Fault('SUBQUERY_WIDTH')
            child_thunks = [_thunk(ref, env) for ref in node[2]]
            rows = self._execute(callee, child_thunks, tag == 'exists')
            if tag == 'exists':
                return next(rows, None) is not None
            if tag == 'scalar':
                first = next(rows, None)
                if first is None:
                    return None
                if next(rows, None) is not None:
                    raise Fault('SCALAR_MANY')
                return first[0]
            unknown = False
            for row in rows:
                if needle is None or row[0] is None:
                    unknown = True
                elif needle == row[0]:
                    return True
            return None if unknown else False
        if tag == 'agg':
            if aggregating:
                raise Fault('NESTED_AGG')
            if group is None:
                raise Fault('NO_GROUP')
            op = node[1]
            values = [self._eval(node[2], member, None, True)
                      for member in group]
            nonnull = [value for value in values if value is not None]
            if op == 'count':
                return len(nonnull)
            if op == 'sum':
                if any(type(value) is not int for value in nonnull):
                    raise Fault('INT_REQUIRED')
                return sum(nonnull) if nonnull else None
            if op == 'min':
                try:
                    return min(nonnull) if nonnull else None
                except TypeError:
                    raise Fault('COMPARISON')
            raise Fault('AGG:' + op)
        if tag == 'op':
            op = node[1]
            left = self._eval(node[2], env, group, aggregating)
            if op in ('and', 'or'):
                left = _truth(left)
                if op == 'and' and left is False:
                    return False
                if op == 'or' and left is True:
                    return True
                right = _truth(self._eval(node[3], env, group, aggregating))
                if op == 'and':
                    return False if right is False else (
                        None if left is None or right is None else True)
                return True if right is True else (
                    None if left is None or right is None else False)
            right = self._eval(node[3], env, group, aggregating)
            if left is None or right is None:
                return None
            if op == 'eq':
                return left == right
            if op in ('add', 'div'):
                if type(left) is not int or type(right) is not int:
                    raise Fault('INT_REQUIRED')
                if op == 'div' and right == 0:
                    raise Fault('DIV_ZERO')
                return left + right if op == 'add' else left // right
            if op == 'lt':
                try:
                    return left < right
                except TypeError:
                    raise Fault('COMPARISON')
            raise Fault('OP:' + op)
        raise Fault('EXPR:' + tag)
