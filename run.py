"""JSON input adapter, deliberately thin even in the legacy project."""
import json
import sys
from queryflow import run
from samples import REQUEST

if __name__ == '__main__':
    request = REQUEST if len(sys.argv) == 1 else json.load(open(sys.argv[1], encoding='utf-8'))
    print(json.dumps(run(request['query'], request['tables'], request.get('params')),
                     ensure_ascii=False, separators=(',', ':')))
