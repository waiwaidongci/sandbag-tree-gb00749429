import json
import logging
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.config.settings import ARTIFACTS
from src.models.database import cache
from src.services.domain import compute
from src.services.reconcile import reconcile
from src.services.workflow import (
    claim_reconciliation, claim_run, finish_reconciliation, finish_run, publish_due,
)
from src.utils.exports import write_csv


def calculate(job):
    key, claim, rows, options = job
    try:
        report = compute(rows, options)
        report['columns'] = list(dict.fromkeys(key for row in report['rows'] for key in row))
        report['metric_order'] = list(report['metrics'])
        json.dumps(report, allow_nan=False)
        _write_artifact(f'run-{key}-{claim.hex()}.csv', write_csv(report['rows'], report['columns']))
    except ValueError as error:
        finish_run(key, claim, None, str(error))
    except Exception:
        logging.exception('Analysis %s failed', key)
        finish_run(key, claim, None, '计算失败，请核对数据并重新提交。')
    else:
        finish_run(key, claim, report, None)


def calculate_reconciliation(job):
    key, claim, batches, receipt_rows = job
    try:
        report = reconcile(batches, receipt_rows)
        # 分析台账为主工件，另存固定配对表，列顺序由算法固定，与输入行序无关。
        report['columns'] = report['ledger_columns']
        report['metric_order'] = list(report['metrics'])
        json.dumps(report, allow_nan=False)
        _write_artifact(f'reconcile-{key}-{claim.hex()}.csv',
                        write_csv(report['ledger'], report['ledger_columns']))
        _write_artifact(f'reconcile-{key}-{claim.hex()}-fixed.csv',
                        write_csv(report['fixed_pairs'], report['pair_columns']))
    except ValueError as error:
        finish_reconciliation(key, claim, None, str(error))
    except Exception:
        logging.exception('Reconciliation %s failed', key)
        finish_reconciliation(key, claim, None, '对账失败，请核对签收单与批次台账后重新提交。')
    else:
        finish_reconciliation(key, claim, report, None)


def _write_artifact(filename, data):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=ARTIFACTS, delete=False) as temporary:
        temporary.write(data)
    Path(temporary.name).replace(ARTIFACTS / filename)


def work():
    while True:
        try:
            job = claim_run()
            if job:
                calculate(job)
            else:
                reconciliation = claim_reconciliation()
                if reconciliation:
                    calculate_reconciliation(reconciliation)
            publish_due()
            cache.set('worker:heartbeat', str(time.time()), ex=25)
        except Exception:
            logging.exception('Worker iteration failed')
        time.sleep(0.8)


if __name__ == '__main__':
    work()
