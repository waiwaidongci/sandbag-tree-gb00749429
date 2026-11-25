"""端到端集成测试（零第三方依赖）：用内存假存储模拟 engine，验证对账全链路。

运行：python -I integration_test.py
覆盖：CSV 解析与校验、行序不变版本、提交/认领/完成工作流、结果口径、
晚到补录另存新版本、旧分析沿用原台账、失败路径、审计事件。
"""
import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path('/workspace/backend')))


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [(row,) for row in self._rows]

    def mappings(self):
        outer = self

        class _Mappings:
            def one_or_none(self):
                return outer._rows[0] if outer._rows else None

            def all(self):
                return list(outer._rows)

        return _Mappings()


class FakeConnection:
    def __init__(self, store):
        self.store = store
        self._next = {}

    def scalar(self, statement):
        return statement._fake_run(self.store, scalar=True)

    def execute(self, statement):
        return statement._fake_run(self.store, scalar=False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeEngine:
    def __init__(self, store):
        self.store = store

    def begin(self):
        return FakeConnection(self.store)

    def connect(self):
        return FakeConnection(self.store)


class Select:
    def __init__(self, table, where=None):
        self.table = table
        self.where_clause = where

    def where(self, clause):
        return Select(self.table, clause)

    def order_by(self, *args):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def _fake_run(self, store, scalar):
        rows = store.rows(self.table)
        if self.where_clause is not None:
            rows = [r for r in rows if self.where_clause.matches(r)]
        if scalar:
            return rows[0][store.pk(self.table)] if rows else None
        return FakeResult(rows)


class Insert:
    def __init__(self, table, values):
        self.table = table
        self.values = values

    @property
    def inserted_primary_key(self):
        class Key:
            def __init__(self, value):
                self.value = [value]

            def __getitem__(self, index):
                return self.value[index]

        return Key(self._key)

    def _fake_run(self, store, scalar):
        key = store.insert(self.table, dict(self.values))
        self._key = key
        return self


class Update:
    def __init__(self, table, where, values):
        self.table = table
        self.where_clause = where
        self.values = values
        self.rowcount = 0

    def _fake_run(self, store, scalar):
        self.rowcount = store.update(self.table, self.where_clause, dict(self.values))
        return self


class Clause:
    def __init__(self, **conditions):
        self.conditions = conditions

    def matches(self, row):
        return all(row.get(key) == value for key, value in self.conditions.items())


class And(Clause):
    def __init__(self, *clauses):
        self.clauses = clauses

    def matches(self, row):
        return all(clause.matches(row) for clause in self.clauses)


class IsNone:
    def __init__(self, column):
        self.column = column

    def matches(self, row):
        return row.get(self.column.name) is None


class LessThan:
    def __init__(self, column, value):
        self.column = column
        self.value = value

    def matches(self, row):
        return row[self.column.name] < self.value


class Or:
    def __init__(self, *clauses):
        self.clauses = clauses

    def matches(self, row):
        return any(clause.matches(row) for clause in self.clauses)


class Column:
    def __init__(self, name, table=None):
        self.name = name
        self.table = table

    def is_(self, value):
        if value is None:
            return IsNone(self)
        return Clause(**{self.name: value})

    def __lt__(self, other):
        return LessThan(self, other)

    def __eq__(self, other):
        return Clause(**{self.name: other})

    def __hash__(self):
        return hash(self.name)


class Table:
    def __init__(self, name, pk_name):
        self.table_name = name
        self.c = _Columns(self)
        self._pk_name = pk_name

    @property
    def name(self):
        return self.table_name

    @property
    def primary_key(self):
        return [Column(self._pk_name, self)]


class _Columns:
    def __init__(self, table):
        self._table = table
        self._cache = {}

    def __getattr__(self, name):
        # Column 缓存归属表，便于 select(table.c.col) 反查表对象。
        cache = self.__dict__.setdefault('_cache', {})
        if name not in cache:
            cache[name] = Column(name, self._table)
        return cache[name]


class Store:
    def __init__(self):
        self.tables = {}
        self.audit_log = []
        self._sequences = {}

    def register(self, name, pk_name):
        self.tables[name] = {'pk': pk_name, 'rows': []}

    def pk(self, table):
        return self.tables[table.name]['pk']

    def rows(self, table):
        return self.tables[table.name]['rows']

    def insert(self, table, values):
        bucket = self.tables[table.name]
        key = self._sequences.get(table.name, 0) + 1
        self._sequences[table.name] = key
        values = {**values, bucket['pk']: key}
        bucket['rows'].append(values)
        return key

    def update(self, table, where, values):
        count = 0
        for row in self.rows(table):
            if where.matches(row):
                row.update(values)
                count += 1
        return count

    def log_audit(self, actor, action, subject, at):
        self.audit_log.append({'actor': actor, 'action': action, 'subject': subject, 'at': at})


# ---- 安装假的 sqlalchemy 兼容层（在导入业务模块前）。----
import types  # noqa: E402

fake_sa = types.ModuleType('sqlalchemy')


def _select(target, *rest):
    # 兼容 select(table) 与 select(table.c.col)，统一反查到表对象。
    table = getattr(target, 'table', None) if isinstance(target, Column) else target
    return Select(table)


fake_sa.select = _select
fake_sa.insert = lambda table: _InsertBuilder(table)
fake_sa.update = lambda table: _UpdateBuilder(table)
fake_sa.and_ = And
fake_sa.or_ = Or
fake_sa.create_engine = lambda *a, **k: FakeEngine(STORE)
fake_sa.text = lambda value: value


class _Stub:
    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self


for _name in ('JSON', 'Column', 'Double', 'Integer', 'MetaData', 'String', 'Table',
              'Text', 'UniqueConstraint'):
    setattr(fake_sa, _name, _Stub())


class _InsertBuilder:
    def __init__(self, table):
        self.table = table

    def values(self, **kwargs):
        return Insert(self.table, kwargs)


class _UpdateBuilder:
    def __init__(self, table):
        self.table = table

    def where(self, *clauses):
        clause = clauses[0] if len(clauses) == 1 else And(*clauses)
        return _PendingUpdate(self.table, clause)


class _PendingUpdate:
    def __init__(self, table, clause):
        self.table = table
        self.clause = clause

    def values(self, **kwargs):
        return Update(self.table, self.clause, kwargs)


sys.modules['sqlalchemy'] = fake_sa

fake_exc = types.ModuleType('sqlalchemy.exc')


class IntegrityError(Exception):
    pass


fake_exc.IntegrityError = IntegrityError
sys.modules['sqlalchemy.exc'] = fake_exc

# fastapi / pydantic 的最小 stub，仅满足业务模块导入与数据类校验。
fake_fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


fake_fastapi.HTTPException = HTTPException


class _Depends:
    def __call__(self, *args, **kwargs):
        return None


fake_fastapi.Depends = _Depends
fake_fastapi.APIRouter = lambda *a, **k: None
fake_fastapi.Response = object
sys.modules['fastapi'] = fake_fastapi

fake_pydantic = types.ModuleType('pydantic')


class FieldInfo:
    def __init__(self, default=None, **kwargs):
        self.default = default
        self.kwargs = kwargs


def Field(default=None, **kwargs):
    return FieldInfo(default, **kwargs)


class BaseModel:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


fake_pydantic.BaseModel = BaseModel
fake_pydantic.Field = Field
sys.modules['pydantic'] = fake_pydantic

fake_redis = types.ModuleType('redis')


class _RedisFake:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()

    def set(self, *args, **kwargs):
        return True

    def ping(self):
        return True


fake_redis.Redis = _RedisFake
sys.modules['redis'] = fake_redis

STORE = Store()
for name, pk in [('data_revisions', 'id'), ('receipt_ledgers', 'id'),
                 ('reconcile_runs', 'id'), ('operation_audit', 'id')]:
    STORE.register(name, pk)

import src.models.database as database  # noqa: E402

database.engine = FakeEngine(STORE)


class _FakeCache:
    def set(self, *args, **kwargs):
        return True

    def ping(self):
        return True


database.cache = _FakeCache()
database.add_event = lambda connection, actor, action, subject, at: STORE.log_audit(
    actor, action, subject, at)

from src.models import tables as tables_module  # noqa: E402

# 用假 Table 替换业务里引用的表对象。
_fake_tables = {}
for _name, _pk in [('data_revisions', 'id'), ('receipt_ledgers', 'id'),
                   ('reconcile_runs', 'id'), ('operation_audit', 'id')]:
    _fake = Table(_name, _pk)
    _fake_tables[_name] = _fake
    setattr(tables_module, _name, _fake)

from src.services import data as data_module  # noqa: E402
from src.services import receipts as receipts_module  # noqa: E402

data_module.datasets = _fake_tables['data_revisions']
receipts_module.receipts = _fake_tables['receipt_ledgers']

from src.services import workflow  # noqa: E402
import src.worker as worker  # noqa: E402
from src.services.data import save_revision  # noqa: E402
from src.services.domain import sample_rows  # noqa: E402
from src.services.receipts import parse_receipts, save_receipt_revision  # noqa: E402
from src.services.reconcile import (  # noqa: E402
    STATUS_AMBIGUOUS, STATUS_FIXED, STATUS_MISSING,
)
from src.schemas.requests import ReconcileRun  # noqa: E402

# workflow 通过 `from ...tables import ...` 绑定了 stub 表，这里统一替换。
workflow.datasets = _fake_tables['data_revisions']
workflow.receipts = _fake_tables['receipt_ledgers']
workflow.reconciliations = _fake_tables['reconcile_runs']
workflow.runs = Table('analysis_runs', 'id')
workflow.schedules = Table('report_schedules', 'id')
workflow.deliveries = Table('report_deliveries', 'id')
workflow.engine = database.engine
worker.cache = database.cache

failures = []


def check(condition, message):
    print(('  PASS ' if condition else '  FAIL ') + message)
    if not condition:
        failures.append(message)


# 1) 排车批次版本。
dataset_id, created = save_revision('排车样例', sample_rows(), 'analyst')
check(created, '排车批次版本已创建')

# 2) 签收 CSV 解析。
buffer = io.StringIO()
writer = csv.writer(buffer)
writer.writerow(['receipt_id', 'loading_bay', 'finished_at'])
writer.writerow(['R-001', '东闸一号位', '2026-11-17 07:40'])
writer.writerow(['R-002', '东闸一号位', '2026-11-17 08:25'])
writer.writerow(['R-003', '东闸二号位', '2026-11-17 07:50'])
writer.writerow(['R-LATE', '东闸一号位', '2026-11-18 23:00'])
parsed = parse_receipts(buffer.getvalue())
check(len(parsed) == 4, '签收 CSV 解析出 4 条记录')

# 3) 换行序/换列序内容一致，不新增版本。
shuffled = io.StringIO()
w2 = csv.writer(shuffled)
w2.writerow(['finished_at', 'receipt_id', 'loading_bay'])
for row in reversed(parsed):
    w2.writerow([row['finished_at'], row['receipt_id'], row['loading_bay']])
rid_a, created_a = save_receipt_revision('签收台账', parsed, 'analyst')
rid_b, created_b = save_receipt_revision('换行序', parse_receipts(shuffled.getvalue()), 'analyst')
check(rid_a == rid_b and not created_b, '换行序/换列序重试不新增签收版本')

# 4) 非法数据被拒绝。
bad_cases = [
    'receipt_id,loading_bay,finished_at\n,东闸一号位,2026-11-17 07:40',
    'receipt_id,loading_bay,finished_at\nR-1,东闸一号位,2026-1-1 7:4',
    'receipt_id,loading_bay,finished_at\nR-1,东闸一号位,2026-11-17 07:40\nR-1,东闸一号位,2026-11-17 07:41',
    'receipt_id,loading_bay\nR-1,东闸一号位',
    'receipt_id,loading_bay,finished_at\nR-1,东闸一号位,2026-11-17 07:40\nR-2,,2026-11-17 07:41',
]
for index, text in enumerate(bad_cases):
    try:
        parse_receipts(text)
        check(False, f'非法用例 {index} 应报错')
    except ValueError:
        check(True, f'非法用例 {index} 被拒绝')

# 5) 提交对账 -> worker 认领 -> 计算落库。
queued = workflow.add_reconciliation(ReconcileRun(dataset_id=dataset_id, receipt_id=rid_a), 'analyst')
check(queued['state'] == 'queued', '对账任务已入队')
job = workflow.claim_reconciliation()
check(job is not None and job[0] == queued['id'], 'worker 认领对账任务')
worker.calculate_reconciliation(job)
saved = next(r for r in STORE.rows(_fake_tables["reconcile_runs"]) if r['id'] == queued['id'])
check(saved['state'] == 'completed', '对账任务计算完成')
result = saved['result']
check(result and 'ledger' in result and 'fixed_pairs' in result, '结果含台账与固定配对')
valid = {STATUS_FIXED, STATUS_AMBIGUOUS, STATUS_MISSING}
check(all(row['状态'] in valid for row in result['ledger']), '台账状态取值合法')
check(any(row['状态'] == STATUS_MISSING for row in result['ledger']), '存在缺配记录')
check(result['metrics']['签收单数'] == 4, '指标含全部 4 张签收单')

# 固定配对等待分钟 = 完装 - 叫车，且在窗口内。
fmt = '%Y-%m-%d %H:%M'
for pair in result['fixed_pairs']:
    wait = int((datetime.strptime(pair['完装时间'], fmt)
                - datetime.strptime(pair['叫车时间'], fmt)).total_seconds() // 60)
    check(wait == pair['等待分钟'] and 0 <= wait <= 120,
          f"固定配对 {pair['批次编号']} 等待口径正确")

# 6) 晚到补录合并重算，另存新版本，旧结果不变。
merged = parsed + [{'receipt_id': 'R-NEW', 'loading_bay': '东闸一号位',
                    'finished_at': '2026-11-24 16:00'}]
rid_c, created_c = save_receipt_revision('含晚到补录', merged, 'analyst')
check(created_c and rid_c != rid_a, '晚到补录产生独立新版本')
queued2 = workflow.add_reconciliation(ReconcileRun(dataset_id=dataset_id, receipt_id=rid_c), 'analyst')
worker.calculate_reconciliation(workflow.claim_reconciliation())
old = next(r for r in STORE.rows(_fake_tables["reconcile_runs"]) if r['id'] == queued['id'])
new = next(r for r in STORE.rows(_fake_tables["reconcile_runs"]) if r['id'] == queued2['id'])
check(old['result']['metrics']['签收单数'] == 4, '旧对账沿用原台账（4 张）')
check(new['result']['metrics']['签收单数'] == 5, '新对账基于合并台账（5 张）')

# 7) 审计事件。
actions = {event['action'] for event in STORE.audit_log}
check({'receipt_import', 'reconcile_queue', 'reconcile_completed'} <= actions, '审计事件完整')

# 8) 坏数据失败路径。
rid_bad, _ = save_receipt_revision('坏数据',
                                   [{'receipt_id': 'X', 'loading_bay': '东闸一号位'}], 'analyst')
q3 = workflow.add_reconciliation(ReconcileRun(dataset_id=dataset_id, receipt_id=rid_bad), 'analyst')
worker.calculate_reconciliation(workflow.claim_reconciliation())
failed_job = next(r for r in STORE.rows(_fake_tables["reconcile_runs"]) if r['id'] == q3['id'])
check(failed_job['state'] == 'failed' and failed_job['error'], '坏数据任务标记失败并带错误信息')

# 9) 不存在的版本应被拒绝（工作流抛出 HTTP 风格异常，这里按状态码属性判断）。
try:
    workflow.add_reconciliation(ReconcileRun(dataset_id=999, receipt_id=rid_a), 'analyst')
    check(False, '不存在的批次版本应拒绝')
except Exception as error:  # noqa: BLE001
    check(getattr(error, 'status_code', None) == 404, '不存在的批次版本返回 404')

print()
if failures:
    print(f'{len(failures)} 项失败')
    sys.exit(1)
print('全部集成测试通过。')
