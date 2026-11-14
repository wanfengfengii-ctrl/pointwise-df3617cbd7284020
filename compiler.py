"""Name-binding compiler: query object graph -> pure JSON data plan.

The compiler owns *names* only.  It reads the query graph and a catalog of
table column names (never rows), binds every column reference to a numeric
slot or to a numeric import link, and serialises the result as plain JSON
data (``version=1``).  It never evaluates an expression, never produces a
business trace and never raises a business error: every error that may only
fire on a selected execution path (including references in cold branches)
is represented in the plan as a deferred fault node, so compilation cannot
fail early for a potential runtime fault.

The module depends solely on the Python standard library and has no
execution-side dependency (no runtime, queryflow, samples or registry).
"""

VERSION = 1
_SUBQUERY_TAGS = ('scalar', 'exists', 'in')


def compile_query(query, catalog):
    """Compile *query* against *catalog* (table name -> ordered column names).

    Returns a JSON-serialisable plan dict.  Neither argument is mutated.
    """
    return _Compiler(catalog).build(query)


class _Compiler:
    def __init__(self, catalog):
        self._catalog = catalog
        self._table_index = {name: i for i, name in enumerate(sorted(catalog))}
        # One entry per distinct input query dict, in discovery order; the
        # entry query is discovered first and therefore gets unit index 0.
        self._order = []
        self._ids = {}
        self._params = []
        self._param_seen = set()
        self._analysis_cache = {}

    # ------------------------------------------------------------- discovery
    def build(self, root):
        self._discover(root)
        analysis = {uid: self._analyze(uid)
                    for uid in range(len(self._order))}
        # Parameter names are gathered while analysing every unit.
        self._param_order = {name: i for i, name in
                             enumerate(sorted(self._params))}
        import_sets = self._fix_imports(analysis)

        units = []
        for uid in range(len(self._order)):
            units.append(self._build_unit(uid, analysis, import_sets))
        return {
            'version': VERSION,
            'tables': sorted(self._catalog),
            'parameters': sorted(self._params),
            'entry': 0,
            'units': units,
        }

    def _discover(self, root):
        """Walk the possibly cyclic/shared object graph once, by identity."""
        stack = [root]
        while stack:
            node = stack.pop()
            if not isinstance(node, (dict, list)) or id(node) in self._ids:
                continue
            if isinstance(node, dict):
                if 'select' in node:
                    self._ids[id(node)] = len(self._order)
                    self._order.append(node)
                stack.extend(node.values())
            else:
                stack.extend(node)

    # --------------------------------------------------------- unit analysis
    def _analyze(self, uid):
        cached = self._analysis_cache.get(uid)
        if cached is not None:
            return cached
        q = self._order[uid]

        frames = []
        for item in q.get('from', []):
            if 'table' in item:
                names = list(self._catalog.get(item['table'], ()))
            else:
                names = [pair[0] for pair in item['query']['select']]
            frames.append({'alias': item.get('as'), 'names': names,
                           'offset': 0})
        # Flat slot offsets across concatenated binding rows: a slot reference
        # ['slot', offset, column] addresses frame[offset + column] directly.
        offset = 0
        for frame in frames:
            frame['offset'] = offset
            offset += len(frame['names'])

        free = set()
        expr_edges = []   # (callee id, full local frames)
        source_edges = []  # (callee id, visible lateral prefix frames)
        for expr in self._unit_expressions(q):
            self._scan_expr(expr, frames, free, expr_edges)
        for index, item in enumerate(q.get('from', [])):
            if 'query' in item:
                source_edges.append(
                    (self._ids[id(item['query'])], frames[:index]))

        result = {'frames': frames, 'free': free,
                  'expr_edges': expr_edges, 'source_edges': source_edges}
        self._analysis_cache[uid] = result
        return result

    def _unit_expressions(self, q):
        for _, expr in q['select']:
            yield expr
        if 'where' in q:
            yield q['where']
        if 'group' in q:
            for expr in q['group']:
                yield expr
        if 'having' in q:
            yield q['having']

    def _scan_expr(self, expr, frames, free, expr_edges):
        """Collect this unit's free column pairs and expression call edges.

        Bodies of subqueries are not recursed into here: each distinct query
        dict is its own unit and analysed independently, which also keeps a
        shared/cyclic graph traversal finite.
        """
        tag = expr[0]
        if tag == 'lit':
            return
        if tag == 'col':
            if not self._matches_locally(
                    (None if len(expr) == 2 else expr[1],
                     expr[1] if len(expr) == 2 else expr[2]), frames):
                free.add((None, expr[1]) if len(expr) == 2
                         else (expr[1], expr[2]))
            return
        if tag == 'param':
            if expr[1] not in self._param_seen:
                self._param_seen.add(expr[1])
                self._params.append(expr[1])
            return
        if tag == 'if':
            self._scan_expr(expr[1], frames, free, expr_edges)
            self._scan_expr(expr[2], frames, free, expr_edges)
            self._scan_expr(expr[3], frames, free, expr_edges)
        elif tag == 'coalesce':
            for part in expr[1:]:
                self._scan_expr(part, frames, free, expr_edges)
        elif tag == 'not':
            self._scan_expr(expr[1], frames, free, expr_edges)
        elif tag == 'op':
            self._scan_expr(expr[2], frames, free, expr_edges)
            self._scan_expr(expr[3], frames, free, expr_edges)
        elif tag == 'agg':
            self._scan_expr(expr[2], frames, free, expr_edges)
        elif tag in _SUBQUERY_TAGS:
            sub = expr[2] if tag == 'in' else expr[1]
            expr_edges.append((self._ids[id(sub)], frames))
            if tag == 'in':
                self._scan_expr(expr[1], frames, free, expr_edges)

    @staticmethod
    def _pair_key(pair):
        # None (unqualified) sorts before string aliases without comparing
        # heterogeneous tuple elements.
        return (0, '', pair[1]) if pair[0] is None else (1, pair[0], pair[1])

    @staticmethod
    def _matches_locally(pair, frames):
        """Whether the interpreter's column search stops inside *frames*.

        Mirrors the stop rule (nearest-frame unqualified stop, qualified
        alias stop) without deciding the eventual value: ambiguity and
        qualified unknown-column faults are local outcomes too.
        """
        alias, column = pair
        if alias is None:
            return any(column in frame['names'] for frame in frames)
        return any(frame['alias'] == alias for frame in frames)

    # --------------------------------------------------- free-column fixpoint
    def _fix_imports(self, analysis):
        """Least fixed point of each unit's exact free-column interface.

        A pair belongs to a unit's interface when its own expressions need it
        locally-unresolved, or a callee demands the pair and no visible local
        frame stops the search at the call site.  Recursive rings converge on
        the smallest satisfying set.
        """
        imports = {uid: set(analysis[uid]['free'])
                   for uid in range(len(self._order))}
        changed = True
        while changed:
            changed = False
            for uid in range(len(self._order)):
                wanted = set(imports[uid])
                info = analysis[uid]
                edges = list(info['expr_edges']) + list(info['source_edges'])
                for callee, visible in edges:
                    for pair in imports[callee]:
                        if not self._matches_locally(pair, visible):
                            wanted.add(pair)
                if wanted != imports[uid]:
                    imports[uid] = wanted
                    changed = True
        return imports

    # ------------------------------------------------------------ unit build
    def _build_unit(self, uid, analysis, import_sets):
        frames = analysis[uid]['frames']
        ordered_imports = sorted(import_sets[uid], key=self._pair_key)
        import_index = {pair: i for i, pair in enumerate(ordered_imports)}

        def resolve_pair(pair, visible_frames):
            """Bind an (alias-or-None, column) pair against visible frames.

            Returns a ['slot', ...] numeric link, an ['imp', ...] link when
            the pair is this unit's own import, or a deferred ['fault', ...].
            Unqualified lookups use the nearest-frame stop rule while still
            counting every same-named hit to preserve cross-frame ambiguity.
            """
            alias, column = pair
            if alias is None:
                nearest = next(
                    (frame for frame in reversed(visible_frames)
                     if column in frame['names']), None)
                if nearest is None:
                    if pair in import_index:
                        return ['imp', import_index[pair]]
                    return ['fault', 'UNKNOWN:' + column]
                hits = [(frame['offset'], i)
                        for frame in visible_frames
                        for i, name in enumerate(frame['names'])
                        if name == column]
                if len(hits) != 1:
                    return ['fault', 'AMBIGUOUS:' + column]
                offset, position = hits[0]
                return ['slot', offset, position]
            bindings = [frame for frame in visible_frames
                        if frame['alias'] == alias]
            if not bindings:
                if pair in import_index:
                    return ['imp', import_index[pair]]
                return ['fault', 'UNKNOWN:' + alias + '.' + column]
            if len(bindings) != 1:
                return ['fault', 'AMBIGUOUS:' + alias + '.' + column]
            frame = bindings[0]
            hits = [i for i, name in enumerate(frame['names'])
                    if name == column]
            if not hits:
                return ['fault', 'UNKNOWN:' + alias + '.' + column]
            if len(hits) != 1:
                return ['fault', 'AMBIGUOUS:' + alias + '.' + column]
            return ['slot', frame['offset'], hits[0]]

        def bind_col(expr, visible_frames):
            pair = (None, expr[1]) if len(expr) == 2 else (expr[1], expr[2])
            return resolve_pair(pair, visible_frames)

        def compile_expr(expr, visible_frames):
            tag = expr[0]
            if tag == 'lit':
                return ['lit', expr[1]]
            if tag == 'param':
                return ['param', self._param_order[expr[1]], expr[1]]
            if tag == 'col':
                return bind_col(expr, visible_frames)
            if tag == 'if':
                return ['if', compile_expr(expr[1], visible_frames),
                        compile_expr(expr[2], visible_frames),
                        compile_expr(expr[3], visible_frames)]
            if tag == 'coalesce':
                return ['coalesce'] + [compile_expr(part, visible_frames)
                                       for part in expr[1:]]
            if tag == 'not':
                return ['not', compile_expr(expr[1], visible_frames)]
            if tag == 'op':
                return ['op', expr[1],
                        compile_expr(expr[2], visible_frames),
                        compile_expr(expr[3], visible_frames)]
            if tag == 'agg':
                return ['agg', expr[1], compile_expr(expr[2], visible_frames)]
            if tag in _SUBQUERY_TAGS:
                sub = expr[2] if tag == 'in' else expr[1]
                callee = self._ids[id(sub)]
                callee_imports = sorted(import_sets[callee],
                                        key=self._pair_key)
                args = [compile_arg(pair, visible_frames)
                        for pair in callee_imports]
                node = [tag, callee, args]
                if tag == 'in':
                    node.append(compile_expr(expr[1], visible_frames))
                return node
            raise ValueError('EXPR:' + tag)

        def compile_arg(pair, visible_frames):
            """Numeric link supplying callee import *pair* at this call site."""
            return resolve_pair(pair, visible_frames)

        sources = []
        for index, item in enumerate(self._order[uid].get('from', [])):
            prefix = frames[:index]
            if 'table' in item:
                name = item['table']
                sources.append({
                    'kind': 'table',
                    'alias': item.get('as'),
                    'table': name,
                    'table_index': self._table_index.get(name, -1),
                    'width': len(self._catalog.get(name, ())),
                    'left': bool(item.get('left', False)),
                })
            else:
                callee = self._ids[id(item['query'])]
                callee_imports = sorted(import_sets[callee],
                                        key=self._pair_key)
                sources.append({
                    'kind': 'query',
                    'alias': item.get('as'),
                    'unit': callee,
                    'args': [compile_arg(pair, prefix)
                             for pair in callee_imports],
                    'width': len(self._order[callee]['select']),
                    'left': bool(item.get('left', False)),
                })

        q = self._order[uid]
        return {
            'outputs': [pair[0] for pair in q['select']],
            'imports': [list(pair) for pair in ordered_imports],
            'sources': sources,
            'select': [compile_expr(expr, frames)
                       for _, expr in q['select']],
            'where': compile_expr(q['where'], frames)
                if 'where' in q else None,
            'group': [compile_expr(expr, frames)
                      for expr in q['group']] if 'group' in q else None,
            'having': compile_expr(q['having'], frames)
                if 'having' in q else None,
            'distinct': bool(q.get('distinct', False)),
            'order': [list(pair) for pair in q['order']]
                if 'order' in q else None,
            'limit': q.get('limit'),
        }
