# 模块职责与依赖边界说明

本轮重构把原 `queryflow.py` 中“名称绑定”和“数据求值”两类职责彻底拆开，
外部入口、请求/结果格式、错误文本、trace 顺序与逐项业务结果保持不变。

## 模块清单与依赖方向

依赖方向严格单向，运行器与编译器互不依赖，二者都不依赖适配层、样例或注册表：

```
run.py / checks.py / boundary_checks.py
        │
        ▼
   queryflow.run（薄适配层）
       ├──► compiler.compile_query   （名称绑定，只依赖标准库）
       └──► runtime.execute_plan     （位置求值，只依赖标准库 json）
```

| 模块 | 职责 | 明确不做的事 |
| --- | --- | --- |
| `queryflow.py` | 仅保留兼容入口 `run(query, tables, params=None)`：由表对象取列名形成 catalog，调用编译器，再把行按 `plan['tables']`、参数按 `plan['parameters']` 排列为位置输入并转发运行器 | 不含任何类、解释器、辅助执行函数或备用路径；不做名称查找；AST 检查保证全模块只有一个 `run` 函数 |
| `compiler.py` | 名称绑定。遍历可能共享/成环的查询对象图，按身份为每个互异查询 dict 生成一个 unit；把列引用绑定为数值槽位 `['slot', 帧偏移, 列]` 或数值导入链接 `['imp', 导入下标]`；产出纯 JSON 计划（`version=1`、`units`、`entry`、有序 `tables`/`parameters`） | 不读行、不执行表达式、不产生业务 trace；不依赖 runtime/queryflow/samples；潜在运行错误不提前失败，而编码为延迟故障节点 `['fault', 文本]` |
| `runtime.py` | 位置求值。只接收计划、按表顺序的行数组 `sources`（无列名）和 `[是否提供, 值]` 形式的 `arguments`；按数值绑定执行，输出既有成功/异常结果与 trace | 不出现列名/别名解析、不保留原始 AST、不读 catalog、不调用编译器或注册表；计划、sources、arguments 均不修改且可复用 |
| `samples.py` / `run.py` | 数据夹具与单行 JSON 适配器，行为不变 | 不承载执行逻辑 |

`compiler.py` 与 `runtime.py` 之间没有共享可变状态，也没有进程内注册表：
边界探针在全新子进程中用 `meta_path` 屏蔽 `compiler`、`queryflow`、`samples`
后，仅靠 JSON 往返后的计划即可由 `runtime` 完成执行；反向也验证了屏蔽
`runtime` 后 `compiler` 仍可独立编译。

## 计划结构（纯 JSON，可 `json.dumps(..., allow_nan=False)` 往返）

* 顶层：`version=1`、`tables`（catalog 全部表名排序）、`parameters`
  （查询图全部 param 名去重排序）、`entry`（入口 unit 下标，恒为 0）、`units`。
* 每个互异输入查询 dict 恰对应一个 unit，unit 之间只用数值下标引用，
  绝不内嵌子查询，也不按祖先作用域或调用路径复制共享单元。
* unit 含 `outputs`、`imports`、`sources`、编译后的 `select/where/group/having`、
  `distinct/order/limit`。`imports` 为 `[别名或 null, 列名]` 的去重有序列表，
  仅作接口声明与诊断；真正的跨单元传值使用其数值下标。
* 表达式节点头只含运行期标签（`lit/param/slot/imp/fault/if/coalesce/not/op/
  agg/scalar/exists/in`），不存在 `col`：所有列引用在编译期已数值化。
  名称仅保留在输出名、trace 事件、错误文本、`imports` 声明以及适配器的
  位置排列中，运行阶段不据此查列。

## 精确自由列接口（imports）如何得到

1. 每个 unit 先按语法收集自身表达式的列引用，并记录两类调用边：
   表达式内 `scalar/exists/in` 调用（可见帧为该 unit 完整本地帧）和
   `from` 中横向派生源调用（可见帧为该源之前的前缀帧，不含其自身别名）。
2. 查列规则与原解释器一致：非限定列在最近含同名列的帧停止，但要统计
   全部同名命中以保留跨帧 `AMBIGUOUS`；限定列在别名出现的帧停止，
   同别名多绑定报歧义、单绑定缺列报 `UNKNOWN`，不退回外层。
3. 能在可见帧内取值或可确定性地判定歧义/缺列的引用不导入；其余按
   `(别名或 null, 列名)` 向外逃逸。对调用边，被调 unit 需要的每个导入，
   若调用点可见帧不能让查列停止，则继续向该 unit 的调用者逃逸。
4. 对所有 unit 的逃逸集合做最小不动点迭代：递归环自然收敛到满足全部
   语法依赖的最小引用集合；冷分支同样计入，不做不可达性证明。
5. 缺失表在分析中按零列声明处理，因此编译不会因缺表失败；实际
   `TABLE:` 错误延迟到运行读源时抛出（trace 仍先记 `scan`）。

因此每个 unit 只导入自己真正需要的自由列，从不接收完整环境或全局列槽表；
边界探针断言了 TOTALS 的 `[0,0,1]`、共享 DAG 的 `[18] + range(18)×3`
以及递归环的 `[0,1,1,1]` 等精确导入规模。

## 共享查询的捕获方式

* unit 按查询 dict 的对象身份编号，共享 dict（含被多处 `scalar` 引用、
  被多个横向源引用）只编译一次，调用点只保存 unit 编号与参数链接。
* 每次调用为被调 unit 新建一组**位置导入 thunk**：thunk 闭包绑定调用点
  的环境，内部只含 `slot/imp/fault` 数值引用。同一 unit 在不同调用路径
  下以不同 thunk 复用同一份局部计划，词法捕获按调用点实例化而单元本身
  不展开。`samples.shared(18)` 有 55 个互异对象、2^18 条词法路径，计划仍
  只有 55 个 unit。
* 导入是惰性的：thunk 仅在被调 unit 实际读取该导入时求值。冷分支不读
  的故障导入不会提前触发（已用专用探针验证 `UNKNOWN` 仅在分支选中时抛出）。
* 递归环（`samples.recursive` 的 worker/step/decrement）通过相同的
  unit 编号与 thunk 机制表达，编译期不动点求出最小导入，运行期按原
  解释器的自然递归求值，不新增深度承诺，也不判定不终止程序。

## 延迟错误与错误先后的保持

* 编译期可确定的名称类错误（`UNKNOWN/AMBIGUOUS`）编码为 `fault` 节点，
  仅当求值走到该节点时抛出，保证冷分支错误不提前触发；参数缺失
  （`PARAM:`）、表缺失（`TABLE:`）、宽度（`SUBQUERY_WIDTH`）、基数
  （`SCALAR_MANY`）、类型与除零等仍全部在运行期按原顺序判定。
* 运行器逐行移植原解释器的求值次序：`and/or` 短路与三值逻辑、`if` 仅
  true 取真支、`coalesce` 首个非 null、二元运算左后右且 null 仍求右值、
  `in` 左表达式在 subquery 事件前求值且匹配即停、`scalar` 第二行先投影
  再判基数、`exists` 跳过投影/distinct/order 但保留 `limit=0` 与 having、
  聚合的 `NESTED_AGG/NO_GROUP` 与每成员环境、空组合成 null 帧（缺表仍抛
  `TABLE:` 且不产生 scan 事件）、group/distinct/order 的 key 文本与稳定
  排序、未排序达 limit 即停而排序先全量投影。trace 事件的追加时机与
  原实现逐点一致，故错误前 trace 完全相同。

## 旧行为保持依据与回归结果

* `checks.py` 的原业务断言一条未删，仅在末尾追加 `boundary_checks.
  regression()`；默认 `python3 checks.py` 现同时输出
  `BOUNDARY_REGRESSION_OK` 与 `BUSINESS_REGRESSION_OK`，运行清单
  `.pointwise-runtime.json` 的 verify 仍指向 `python3 checks.py`。
* 除固定探针外，另以 git 中的原始解释器为基准做了差分测试：
  - `fuzz_diff.py`：随机生成含重复列、缺表、null、类型故障、分组、
    横向源、子查询与共享 dict 的查询，逐案比较完整结果（rows/error/
    有序 trace），多个随机种子共约 9000 个用例全部一致；
  - `fuzz_rings.py`：显式构造 DAG 共享与递归环并轮换参数组合，
    多组种子全部一致；
  - 另有限时拉取、全量排序、三值逻辑、null 排序、空组等定向探针。
* 实际执行记录（本环境）：
  - `python3 checks.py` → `BOUNDARY_REGRESSION_OK` 后 `BUSINESS_REGRESSION_OK`
  - `python3 boundary_checks.py` → `BOUNDARY_REGRESSION_OK`
  - `python3 run.py` 与 `python3 run.py REQUEST.json` → TOTALS 真实结果
  - `python3 checks.py demo` → ENRICH/TOTALS、共享查询与递归查询真实结果
  - 差分 fuzz（多种子）→ `FUZZ_OK` / `RING_FUZZ_OK`
  - 隔离模式（`python3 -I` + 屏蔽 compiler/queryflow/samples）下执行
    JSON 文件计划成功，且同一计划可重复用于不同行内容与缺参情况，
    计划与输入均未被修改。
