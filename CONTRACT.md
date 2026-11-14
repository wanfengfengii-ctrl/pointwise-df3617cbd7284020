# 嵌套转换查询基线与重构交付契约

本项目将有序的多重集源表转换为有序结果，不连接数据库。
当前文件为 queryflow.py、samples.py、run.py、checks.py、boundary_checks.py 和运行清单。
queryflow.run(query, tables, params=None) 是既有 Python 调用入口。
python3 run.py 展示 samples.REQUEST 的真实结果；python3 run.py REQUEST.json 读取一个请求文件。
请求为 {"query":查询对象,"tables":源表映射,"params":参数映射}，params 可省略。
tables 的每项为 {"columns":[列名…],"rows":[行数组…]}，行长度等于列数，保持输入顺序，列名可重复。
数据值限 None/JSON null、布尔、整数及字符串；参数和 lit 也只接受这些标量。
请求树结构、操作名称和索引保证合法；源表缺失、列不存在、歧义、参数缺失和表达式运行错误属于可观察业务结果，不得提前拒绝。
查询对象图有限；Python 调用允许共享查询 dict、表达式 list 和经子查询调用形成的引用环，输入对象不能被修改。
递归用例的实际求值保证在基线正常完成；不要求判定不终止程序，不新增递归深度承诺。
文件 JSON 不表达对象共享或环，samples.shared 和 samples.recursive 展示 Python 入口的实际调用。
成功返回 {"columns":[输出名…],"rows":[行数组…],"trace":[事件…]}；业务异常返回 {"error":错误字符串,"trace":[此前事件…]}，不返回部分 rows。
适配入口只打印一行 JSON；对象字段排列不构成兼容要求，数组顺序和重复次数构成兼容要求。

## 已有查询格式与行为

查询含必填 select，其余键可省略：
* select 是 [输出名,表达式] 列表；输出名允许重复。
* from 是源项列表；省略即单个空行。源项是 {"table":表名,"as":别名} 或 {"query":查询对象,"as":别名}，均可附 "left":true。
* from 从左向右嵌套枚举。派生源是 LATERAL：每次使用之前源项的当前行以及外层环境执行，当前源自己的别名尚未入作用域。源无行且 left=true 时补一行该源声明宽度的 null。
* where 默认为 true，只保留严格布尔 true；不使用一般 Python 真值转换。
* 出现 group 键才聚合，键值是表达式列表。按行遇到顺序创建组，同组成员保持输入顺序；键由 queryflow.key 的标准 JSON 字节文本确定。group=[] 在输入为空时仍创建一个空组。
* having 默认为 true，仅在聚合查询应用；在 select 前执行。非聚合查询不执行 having。
* 聚合组中的普通列使用首个成员；空组使用所有当前源声明列都为 null 的合成环境。外层环境保持不变。
* distinct=true 按 queryflow.key 的文本去重，保留首行。
* order 是 [从0计的输出列索引,是否降序] 列表；最后一项先作稳定排序。升序按 (值不是null, Python类型名, 值) 排，降序反向。
* limit 为非负整数，省略不截断；limit=0 在任何源读取、表达式求值前返回空。未排序时达到 limit 即停止拉取；order 则先完成全部投影和排序。

表达式是下列列表：
* ["lit",值]；["param",参数名]；["col",列名]；["col",别名,列名]。
* ["if",条件,真支,其他支]；["coalesce",表达式…]；["not",表达式]。
* ["op",操作,左,右]，操作为 eq、lt、add、div、and、or。
* ["scalar",查询]；["exists",查询]；["in",左表达式,查询]。
* ["agg",操作,表达式]，操作为 count、sum、min。

查列从最近环境帧开始。非限定列只在该帧具有同名列时停止搜索，多个匹配报 AMBIGUOUS:列名。
限定列只要该帧出现别名就停止搜索：该别名有多个绑定报 AMBIGUOUS:别名.列名，单个绑定缺列报 UNKNOWN:别名.列名，重复列也报 AMBIGUOUS；不得退回同别名外层。
找不到任何匹配报 UNKNOWN:引用文本，缺参数报 PARAM:参数名。派生源返回的重复输出名也参与歧义判断。
and/or/not/if 条件只接受布尔或 null，否则 BOOL_REQUIRED；null 为未知。
and 的 false、or 的 true 均短路；未知左值仍求右值，false and unknown=false，true or unknown=true，其余未知传播。
if 仅 true 选真支，false/null 选其他支；coalesce 只求值到首个非 null。
其他二元运算左后右；任一为 null 则为 null，但右操作数仍被求值。eq 用 Python 标量相等，lt 用 Python 比较，不可比报 COMPARISON。
add/div 只接受精确 int 类型，否则 INT_REQUIRED；div 为向下整除，零除报 DIV_ZERO。
每次执行表源先追加 ["scan",表名]；即使缺表也先记事件再报 TABLE:表名。行内容不记入 trace。
执行 scalar/exists/in 时先追加 ["subquery",种类]；in 的左表达式在追加事件前求值。
scalar/in 要求恰一输出列，否则在读源前报 SUBQUERY_WIDTH；exists 不检查宽度。
scalar 空结果为 null；拉到第二行才报 SCALAR_MANY，第二行投影本身出错时先报该错，不读第三行。
in 按源序读取，匹配即停止；无匹配但发生 null 比较返回 null，空结果始终 false。
exists 只检查有无通过 where/having 的候选，跳过 select、distinct、order，但保留 limit=0 的抑制。
agg 使用当前查询的组，不能捕获外层组；无当前组报 NO_GROUP。参数表达式按每个成员环境求值，忽略 null。
count 返回非 null 数量；sum 要求精确整数，空集合为 null；min 用 Python 最小值，不可比报 COMPARISON，空集合为 null。
同一查询直接嵌套 agg 报 NESTED_AGG；进入子查询后聚合上下文重新建立。所有错误及错误前 trace 的精确先后保持 queryflow.py 的既有行为。

样例 ENRICH 输出 [["A",12],["A",12],["B",null],["C",5]]。
样例 TOTALS 输出 [["A",2,24],["B",0,null],["C",1,5]]，列为 ["site","n","total"]。
samples.shared(18) 的输入含55个互异查询对象，存在2的18次方条词法调用路径；一次执行仅选择一条，默认及反转交替参数均为 [[27]]，全true为 [[18]]，全false为 [[36]]。

samples.recursive 的4个查询对象含调用环，输入 n=[0,3,6] 输出 [[0],[6],[21]]；以横向派生源逐轮把 n 减一，再累计 1 到原 n。

## 本轮结构交付

只重构职责与依赖边界，不新增查询语义或优化策略。
保留 queryflow.run、请求和结果格式、所有原断言及运行清单；queryflow.py 只含兼容 run 函数和导入，不保留解释器、辅助执行函数或备用执行路径。
新增 compiler.compile_query(query,catalog)，catalog 是表名到完整有序列名列表的映射，不含行或参数值。编译不读行、不执行表达式、不产生业务 trace，也不能因潜在运行错误提前失败。
返回纯 JSON 数据计划，含 version=1、units 列表、entry 单元索引、tables 和 parameters 两个有序名称列表。tables 必须为 catalog 全部表名排序，parameters 为查询图中全部 param 名去重排序。
每个互异输入查询 dict 恰对应一个 unit，包括冷分支和递归环内节点；单元间通过编号引用，不内嵌子查询，不能按祖先作用域或调用路径复制共享查询单元及其局部计划。
每个 unit 还声明 imports，元素为 [别名或null,列名]，不重复，仅作接口说明和诊断；执行引用使用其数值索引。imports 必须是该单元的精确自由列接口，不得向所有单元传递全局列槽表、完整环境或不相关列。
自由列接口按语法依赖定义：自身表达式的列引用，以及每个子查询调用所需的自由引用，先按该使用点的完整本地帧或横向源前缀作查列；能本地取值或确定歧义/缺列错误的不导入，其余向外导入。所有分支均计入，不做不可达性证明；递归环取满足这些依赖的最小引用集合。缺失表在此分析中的声明列为空，其实际读源错误仍须延迟到执行。
跨作用域列引用在编译期绑定为数值槽位或数值导入链接，允许表示延迟触发的错误；运行阶段不得用列名、别名或原始 AST 作解析、查找、重绑或回退搜索。
允许保留名称用于输出名、trace、错误文本及适配器将源表和参数排列为位置输入，不能借此在运行阶段查列。
新增 runtime.execute_plan(plan,sources,arguments)。sources 为按 plan.tables 顺序的行数组列表，不含任何列名；arguments 为按 plan.parameters 顺序的 [是否提供,值] 列表。
execute_plan 返回既有成功或异常结果；计划、sources、arguments 均不得修改，单计划可反复用于不同内容、不同参数及缺参数情况。
编译器和运行器只依赖标准库及无执行行为的共享数据定义；不得依赖彼此、queryflow、samples 或进程内注册表。原始查询、catalog、编译器都不存在的全新进程仍能执行 JSON 往返后的计划。
run 只负责形成 catalog、调用编译器、排列位置输入并转发给运行器；不能把旧解释器移入适配器或共享模块。
不要求提供 SQL 文本解析器、数据库、网络服务、增量维护、性能提速或暂停状态协议。

## 验证与交付入口

python3 checks.py 核对现有业务，必须退出0并输出 BUSINESS_REGRESSION_OK。
python3 checks.py demo 展示真实转换结果和共享查询结果。
python3 boundary_checks.py 核对本轮接口、计划共享、独立导入、JSON脱离编译器执行和输入不变性，重构后必须退出0并输出 BOUNDARY_REGRESSION_OK。
boundary_checks.py 当前因目标模块尚未实现而不能通过，不是既有业务故障；本轮不是修 bug。
原 checks.py 可增加但不能删除或削弱断言；其默认入口最终须同时调用原业务回归与 boundary_checks.regression，运行清单 verify 继续指向 python3 checks.py。
边界探针只是自动化证据，仍须核对实际源码的数值绑定、精确自由列接口、依赖方向及无展开共享；不能把原 AST 换个字段名塞进计划或把动态名字解释器藏到通用模块。
交付重构代码与 RESPONSIBILITIES.md，说明各模块职责、共享查询捕获方式、延迟错误及旧行为保持依据，列出真实执行的回归结果。
