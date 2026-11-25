"""签收单台账的 CSV 解析与版本管理。

签收单只有三列：receipt_id（单号）、loading_bay（装车位）、finished_at（完装时间）。
解析规则与排车台账一致：严格表头、BOM 容忍、单行错误定位；保存时按规范化内容的
SHA-256 去重，因此仅调换行序重新导入不会产生新版本。
"""
import csv
import hashlib
import io
import json
import time
from datetime import datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import receipts
from src.services.reconcile import TIME_FORMAT

RECEIPT_COLUMNS = (
    ('receipt_id', '签收单号'),
    ('loading_bay', '装车位'),
    ('finished_at', '完装时间'),
)


def parse_receipts(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    required = {key for key, _ in RECEIPT_COLUMNS}
    if not fieldnames or len(fieldnames) != len(required) or set(fieldnames) != required:
        raise ValueError('CSV 表头须为 ' + '、'.join(label for _, label in RECEIPT_COLUMNS)
                         + '（列名：' + '、'.join(required) + '）。')
    rows = []
    seen = set()
    try:
        for raw in reader:
            if None in raw:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            ident = (raw.get('receipt_id') or '').strip()
            bay = (raw.get('loading_bay') or '').strip()
            finished = (raw.get('finished_at') or '').strip()
            if not ident:
                raise ValueError(f'第 {reader.line_num} 行签收单号不能为空。')
            if not bay:
                raise ValueError(f'第 {reader.line_num} 行装车位不能为空。')
            if not finished:
                raise ValueError(f'第 {reader.line_num} 行完装时间不能为空。')
            if ident in seen:
                raise ValueError(f'签收单号 {ident} 重复，请核对源表，每个单号只保留一条有效记录。')
            seen.add(ident)
            try:
                # 严格校验并规范化时间格式，拒绝 2026-1-1 这类非定长写法。
                parsed = datetime.strptime(finished, TIME_FORMAT)
                if parsed.strftime(TIME_FORMAT) != finished:
                    raise ValueError
            except ValueError as error:
                raise ValueError(
                    f'第 {reader.line_num} 行完装时间 {finished} 无效，请用本地时间 {TIME_FORMAT}。') from error
            rows.append({'receipt_id': ident, 'loading_bay': bay, 'finished_at': finished})
            if len(rows) > 5000:
                raise ValueError('单份签收台账最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条签收记录。')
    return rows


def save_receipt_revision(name, records, owner):
    if not name.strip():
        raise ValueError('台账名称不能为空。')
    payload = _canonical_payload(records)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    now = time.time()
    existing = select(receipts.c.id).where(receipts.c.checksum == checksum)
    try:
        with engine.begin() as connection:
            found = connection.scalar(existing)
            if found is not None:
                return found, False
            key = connection.execute(insert(receipts).values(
                name=name.strip(), owner=owner, records=records, checksum=checksum, created=now)
            ).inserted_primary_key[0]
            add_event(connection, owner, 'receipt_import', key, now)
            return key, True
    except IntegrityError:
        with engine.connect() as connection:
            found = connection.scalar(existing)
            if found is None:
                raise
            return found, False


def _canonical_payload(records):
    # 排序键固定为单号，保证换行序产生相同字节与相同校验和。
    ordered = sorted(records, key=lambda row: row['receipt_id'])
    return json.dumps(ordered, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def sample_receipt_rows():
    """基于排车样例构造签收样例：多数单可固定配对，穿插歧义与超两小时缺配。"""
    from src.services.domain import sample_rows

    rows = []
    batches = sample_rows()
    for index, batch in enumerate(batches):
        planned = datetime.strptime(batch['planned_at'], TIME_FORMAT)
        # 每 8 张安排一个超出两小时窗口的晚完装，制造缺配。
        if index % 8 == 7:
            finished = planned + timedelta(minutes=150)
        else:
            # 等待在 15 至 95 分钟之间，保证落在两小时窗口内。
            finished = planned + timedelta(minutes=15 + (index * 7) % 81)
        rows.append({
            'receipt_id': 'QS-' + batch['batch_id'].split('-', 1)[1],
            'loading_bay': batch['loading_bay'],
            'finished_at': finished.strftime(TIME_FORMAT),
        })
    # 额外插入两张同装车位、时间贴近的签收单，制造可互换的歧义配对。
    anchor = batches[0]
    planned = datetime.strptime(anchor['planned_at'], TIME_FORMAT)
    rows.append({'receipt_id': 'QS-X-001', 'loading_bay': anchor['loading_bay'],
                 'finished_at': (planned + timedelta(minutes=40)).strftime(TIME_FORMAT)})
    rows.append({'receipt_id': 'QS-X-002', 'loading_bay': anchor['loading_bay'],
                 'finished_at': (planned + timedelta(minutes=55)).strftime(TIME_FORMAT)})
    return rows
