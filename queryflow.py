"""Existing fused query interpreter. Public entry: run(query, tables, params=None)."""
import json


class Fault(Exception):
    pass


def key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def truth(value):
    if value is None:
        return None
    if type(value) is not bool:
        raise Fault('BOOL_REQUIRED')
    return value


def run(query, tables, params=None):
    trace = []
    params = {} if params is None else params

    def column(env, ref):
        for frame in env:
            if len(ref) == 2:
                hits = [row[i] for alias, names, row in frame
                        for i, name in enumerate(names) if name == ref[1]]
            else:
                bindings = [b for b in frame if b[0] == ref[1]]
                if not bindings:
                    continue
                if len(bindings) != 1:
                    raise Fault('AMBIGUOUS:' + '.'.join(ref[1:]))
                _, names, row = bindings[0]
                hits = [row[i] for i, name in enumerate(names) if name == ref[2]]
                if not hits:
                    raise Fault('UNKNOWN:' + '.'.join(ref[1:]))
            if hits:
                if len(hits) != 1:
                    raise Fault('AMBIGUOUS:' + '.'.join(ref[1:]))
                return hits[0]
        raise Fault('UNKNOWN:' + '.'.join(ref[1:]))

    def expression(e, env, group=None, aggregating=False):
        tag = e[0]
        if tag == 'lit':
            return e[1]
        if tag == 'col':
            return column(env, e)
        if tag == 'param':
            if e[1] not in params:
                raise Fault('PARAM:' + e[1])
            return params[e[1]]
        if tag == 'if':
            branch = e[2] if truth(expression(e[1], env, group, aggregating)) is True else e[3]
            return expression(branch, env, group, aggregating)
        if tag == 'coalesce':
            for part in e[1:]:
                value = expression(part, env, group, aggregating)
                if value is not None:
                    return value
            return None
        if tag == 'not':
            v = truth(expression(e[1], env, group, aggregating))
            return None if v is None else not v
        if tag in ('scalar', 'exists', 'in'):
            # IN must evaluate the needle even for an empty right side.
            needle = expression(e[1], env, group, aggregating) if tag == 'in' else None
            sub = e[2] if tag == 'in' else e[1]
            trace.append(['subquery', tag])
            if tag != 'exists' and len(sub['select']) != 1:
                raise Fault('SUBQUERY_WIDTH')
            rows = execute(sub, env, tag == 'exists')
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
            op = e[1]
            values = [expression(e[2], member, None, True) for member in group]
            nonnull = [v for v in values if v is not None]
            if op == 'count':
                return len(nonnull)
            if op == 'sum':
                if any(type(v) is not int for v in nonnull):
                    raise Fault('INT_REQUIRED')
                return sum(nonnull) if nonnull else None
            if op == 'min':
                try:
                    return min(nonnull) if nonnull else None
                except TypeError:
                    raise Fault('COMPARISON')
            raise Fault('AGG:' + op)
        if tag == 'op':
            op = e[1]
            left = expression(e[2], env, group, aggregating)
            if op in ('and', 'or'):
                left = truth(left)
                if op == 'and' and left is False:
                    return False
                if op == 'or' and left is True:
                    return True
                right = truth(expression(e[3], env, group, aggregating))
                if op == 'and':
                    return False if right is False else (None if left is None or right is None else True)
                return True if right is True else (None if left is None or right is None else False)
            right = expression(e[3], env, group, aggregating)
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

    def execute(q, outer, existence=False):
        if q.get('limit') == 0:
            return

        def source(index, frame):
            if index == len(q.get('from', [])):
                env = [frame] + outer
                if truth(expression(q.get('where', ['lit', True]), env)) is True:
                    yield env
                return
            item = q['from'][index]
            if 'table' in item:
                name = item['table']
                trace.append(['scan', name])
                if name not in tables:
                    raise Fault('TABLE:' + name)
                names = tables[name]['columns']
                rows = iter(tables[name]['rows'])
            else:
                names = [pair[0] for pair in item['query']['select']]
                # A lateral relation sees the current prefix, including an empty frame.
                rows = execute(item['query'], [frame] + outer)
            seen = False
            for row in rows:
                seen = True
                binding = (item['as'], names, row)
                yield from source(index + 1, frame + [binding])
            if not seen and item.get('left', False):
                binding = (item['as'], names, [None] * len(names))
                yield from source(index + 1, frame + [binding])

        def candidates():
            if 'group' not in q:
                for env in source(0, []):
                    yield env, None
                return
            groups = {}
            for env in source(0, []):
                values = [expression(e, env) for e in q['group']]
                groups.setdefault(key(values), []).append(env)
            if not q['group'] and not groups:
                groups['[]'] = []
            for members in groups.values():
                if members:
                    env = members[0]
                else:
                    # No physical source is scanned here; only declared column layouts matter.
                    frame = []
                    for item in q.get('from', []):
                        if 'table' in item:
                            name = item['table']
                            if name not in tables:
                                raise Fault('TABLE:' + name)
                            names = tables[name]['columns']
                        else:
                            names = [p[0] for p in item['query']['select']]
                        frame.append((item['as'], names, [None] * len(names)))
                    env = [frame] + outer
                if truth(expression(q.get('having', ['lit', True]), env, members)) is True:
                    yield env, members

        def projected():
            for env, group in candidates():
                if existence:
                    yield []
                else:
                    yield [expression(e, env, group) for _, e in q['select']]

        # EXISTS ignores projection, DISTINCT and ORDER; LIMIT=0 still suppresses it.
        rows = projected()
        if not existence and q.get('distinct', False):
            def unique(upstream):
                seen = set()
                for row in upstream:
                    encoded = key(row)
                    if encoded not in seen:
                        seen.add(encoded)
                        yield row
            rows = unique(rows)
        if not existence and q.get('order'):
            rows = list(rows)
            for index, descending in reversed(q['order']):
                def sort_key(row):
                    v = row[index]
                    return (v is not None, type(v).__name__, v)
                rows.sort(key=sort_key, reverse=descending)
        limit = q.get('limit')
        iterator = iter(rows)
        count = 0
        while limit is None or count < limit:
            row = next(iterator, None)
            if row is None:
                break
            yield row
            count += 1

    try:
        rows = list(execute(query, []))
        return {'columns': [p[0] for p in query['select']], 'rows': rows, 'trace': trace}
    except Fault as exc:
        return {'error': str(exc), 'trace': trace}
