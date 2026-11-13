# 本地时间采样调度器（Local-Time Sampling Scheduler）

按**本地墙钟时间**重复的采样计划，展开为 UTC 时刻列表。核心目标是避免“按秒差递增”
在夏令时（DST）切换日造成误采：所有递推都在日历值（年月日、本地时分秒）上进行，
每个实例独立地经时区转换得到 UTC，绝不通过累加 epoch 秒生成下一个实例。

## 运行方式

仅依赖 Python 3.11+ 标准库；时区数据库 `tzdata==2025.2` 已内置在 `vendor/lib/tzdata`，
无需联网或安装第三方包。

```bash
# 输出到文件（原子替换：失败时旧文件原样保留）
python3 -I main.py plan.json -o schedule.json

# 输出到标准输出
python3 -I main.py plan.json

# 从标准输入读取
cat plan.json | python3 -I main.py -
```

`-I` 为隔离模式，确保不加载工作目录或环境变量中的任何外部代码。

## 独立验收

```bash
python3 -I tests/run_acceptance.py
```

全部用例仅使用标准库 `unittest`，覆盖日历递推、DST 间隙/重叠、BYSETPOS、COUNT/UNTIL、
overrides 改期与取消、半开窗口、严格输入校验、JSON 字节级报错以及 CLI 原子写。

## 输入 JSON 结构

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `tz` | string | IANA 时区名，经内置 tzdata 2025.2 解析 |
| `start` | string | 本地时刻 `YYYY-MM-DDTHH:MM:SS`，禁闰秒；必须符合规则且不晚于 UNTIL |
| `rule` | string | RRULE 内容行（见下） |
| `overrides` | object | 以原实例 rid 为键，值为新本地时刻（改期）或 `null`（取消） |
| `window` | [int, int] | UTC epoch 秒的**非空半开**区间 `[lo, hi)`；整数，禁止布尔 |

未知顶层成员、重复 JSON 成员、缺失成员一律拒绝。窗口边界必须是真正的整数
（`true`/`false` 是布尔，不会被当作 1/0）。

## 支持的 RRULE 子集（RFC 5545 §3.3.10，自写实现）

```
FREQ=DAILY|WEEKLY|MONTHLY
INTERVAL=<正整数>            （默认 1）
COUNT=<正整数> | UNTIL=<UTC: YYYYMMDDTHHMMSSZ>   二者恰有一个
BYDAY=[±n]?MO|TU|WE|TH|FR|SA|SU （可逗号多个）
BYMONTHDAY=±1..±31           （可逗号多个，0 非法）
BYSETPOS=<非零整数>          （可逗号多个）
```

* 周始固定星期一（不支持 `WKST`，出现即作为未知规则部分拒绝）；
* 其它任何规则部分（`BYMONTH`、`BYHOUR`、`YEARLY` 等）一律拒绝；
* `BYMONTHDAY` 仅允许在 MONTHLY 下；带序号的 BYDAY（如 `2MO`）仅允许在 MONTHLY 下；
* `BYSETPOS` 必须配合 `BYDAY` 或 `BYMONTHDAY` 使用。

## 语义约定

1. **无效日期剔除**：如 `BYMONTHDAY=31` 在 30 天月份不存在，该候选直接丢弃。
2. **缺失时刻剔除**：本地时间落在春令时前跳间隙（不存在的墙钟时间）时丢弃该实例，
   且**不消耗 COUNT**。
3. **处理顺序（每个周期内）**：先展开候选日期 → 剔除无效日期 → 剔除缺失时刻 →
   再按 `BYSETPOS` 在幸存者中取位 → 最后用 UNTIL/COUNT 截断。被跳过的项不计 COUNT。
4. **重叠时刻**：回拨产生的歧义本地时间，取**较早的 UTC 瞬间**。
5. **显式缺失时刻拒绝**：`start` 或 override 目标若落在间隙，直接报错，不做静默平移。
6. **年份范围**：任何产生的日期超出 1～9999 年即拒绝（报 `$.rule`）。
7. **rid**：实例的原本地时刻字符串；改期保留原 rid，取消不补足 COUNT。
   override 的键必须由原递推真实产生，否则拒绝。
8. **窗口筛选在改期之后**：先应用 overrides，再按目标 UTC 时刻筛选；原实例在窗外、
   但改期后落入窗内仍会被保留。
9. **输出**：`[{"rid": ..., "utc": <整数秒>}, ...]`，按 `(utc, rid)` 升序；
   同一 UTC 时刻的不同 rid 都保留；无结果为 `[]`。

## 错误报告

* JSON 语法错误：报**零基字节偏移**，如 `syntax error at byte 23 ($["start"]): ...`；
* 语义/结构错误：报字段路径与数组零基下标，如 `$.window[0]`、
  `$.overrides["2024-01-01T00:00:00"]`；
* RRULE 错误：报规则内零基字节偏移，如 `$.rule (byte 5): ...`。

## 代码组织

```
sampling/
  jsonparser.py   从零实现的严格 JSON 解析器（字节偏移、重复键、bool/int 区分）
  rrule_parser.py RRULE 子集解析与合法性校验
  recurrence.py   日历值上的周期扩展、间隙剔除、BYSETPOS、COUNT/UNTIL 截断
  timeutil.py     tzdata 装载、本地时间解析、DST 间隙/重叠分类与 UTC 转换
  scheduler.py    顶层编排、schema 校验、overrides、窗口与排序
  cli.py          命令行与原子输出
main.py           隔离模式下的自举入口
tests/            独立验收（unittest，无第三方测试框架）
vendor/lib/tzdata 内置 tzdata==2025.2 时区数据库
```

## 设计边界

不使用任何递推引擎（dateutil/rrule、icalendar 等）；仅用 `zoneinfo` 作为时区数据库的
读取前端，递推规则全部自行实现。
