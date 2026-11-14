"""Compatibility entry point.

``run`` is a deliberately thin adapter: it builds the column-only catalog,
asks the name-binding compiler for a JSON data plan, rearranges the request
into positional sources/arguments and forwards everything to the plan
runtime.  It performs no name lookup and contains no interpreter, helper or
fallback execution path.
"""
import compiler
import runtime


def run(query, tables, params=None):
    params = {} if params is None else params
    catalog = {name: table['columns'] for name, table in tables.items()}
    plan = compiler.compile_query(query, catalog)
    sources = [tables[name]['rows'] for name in plan['tables']]
    arguments = [[name in params, params.get(name)]
                 for name in plan['parameters']]
    return runtime.execute_plan(plan, sources, arguments)
