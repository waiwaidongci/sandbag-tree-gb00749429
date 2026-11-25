from fastapi import HTTPException
from sqlalchemy import select

from src.models.database import engine, fetch_one
from src.models.tables import audit, datasets, deliveries, receipts, reconciliations, runs, schedules


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions():
    columns = [field for field in datasets.c if field.name != 'records']
    return list_rows(datasets, columns)


def receipt_revisions():
    columns = [field for field in receipts.c if field.name != 'records']
    return list_rows(receipts, columns)


def require_receipt(key):
    with engine.connect() as connection:
        row = fetch_one(connection, receipts, key)
    if row is None:
        raise HTTPException(404, '签收台账版本不存在。')
    row = dict(row)
    records = row.pop('records')
    return {**row, 'count': len(records), 'records': records[:100]}


def reconciliation_jobs():
    return list_rows(reconciliations, [field for field in reconciliations.c if field.name != 'result'])


def require_reconciliation(key):
    return require_row(reconciliations, key, '对账任务')


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    return list_rows(runs, [field for field in runs.c if field.name != 'result'])


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def recent_activity():
    return list_rows(audit, limit=20)
