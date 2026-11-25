"""暴力枚举对账结果，与 services.reconcile 的 SCC 算法交叉验证。

运行：python -I verify_reconcile.py
不依赖项目内任何模块路径技巧，直接用文件路径导入。
"""
import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from src.services.reconcile import (  # noqa: E402
    STATUS_AMBIGUOUS, STATUS_FIXED, STATUS_MISSING, WINDOW_MINUTES, reconcile,
)

TIME_FMT = '%Y-%m-%d %H:%M'


def brute_force(batches, receipts):
    n_b, n_r = len(batches), len(receipts)
    edges = []
    wait = {}
    for bi, b in enumerate(batches):
        for ri, r in enumerate(receipts):
            if b['loading_bay'] != r['loading_bay']:
                continue
            from datetime import datetime
            bt = datetime.strptime(b['planned_at'], TIME_FMT)
            rt = datetime.strptime(r['finished_at'], TIME_FMT)
            w = int((rt - bt).total_seconds() // 60)
            if 0 <= w <= WINDOW_MINUTES:
                edges.append((bi, ri))
                wait[(bi, ri)] = w
    adj = {bi: [ri for (b, ri) in edges if b == bi] for bi in range(n_b)}
    # 枚举每个批次“不配对或选一个未占用签收单”的所有匹配。
    optimal = []
    best_flow = -1
    best_wait = None

    def recurse(bi, chosen, used_r, flow, total_wait):
        nonlocal optimal, best_flow, best_wait
        if bi == n_b:
            if flow > best_flow or (flow == best_flow and
                                    (best_wait is None or total_wait < best_wait)):
                best_flow, best_wait = flow, total_wait
                optimal = [dict(chosen)]
            elif flow == best_flow and total_wait == best_wait:
                optimal.append(dict(chosen))
            return
        recurse(bi + 1, chosen, used_r, flow, total_wait)
        for ri in adj.get(bi, []):
            if ri not in used_r:
                chosen[bi] = ri
                used_r.add(ri)
                recurse(bi + 1, chosen, used_r, flow + 1, total_wait + wait[(bi, ri)])
                used_r.remove(ri)
                del chosen[bi]

    recurse(0, {}, set(), 0, 0)

    pair_counts = {}
    batch_ever = set()
    receipt_ever = set()
    for matching in optimal:
        for bi, ri in matching.items():
            pair_counts[(bi, ri)] = pair_counts.get((bi, ri), 0) + 1
            batch_ever.add(bi)
            receipt_ever.add(ri)
    fixed = {key for key, count in pair_counts.items() if count == len(optimal)}
    flexible_edges = set(pair_counts)
    return {
        'flow': best_flow,
        'wait': best_wait,
        'fixed': fixed,
        'flexible_edges': flexible_edges,
        'batch_ever': batch_ever,
        'receipt_ever': receipt_ever,
        'edges': set(edges),
    }


def make_case(rng):
    bays = ['一号位', '二号位']
    n_b = rng.randint(1, 6)
    n_r = rng.randint(1, 6)
    base = 60 * 24 * 0  # 分钟相对值，靠统一基准生成
    batches = []
    for i in range(n_b):
        minute = base + rng.randint(0, 600)
        batches.append({
            'batch_id': f'B{i:02d}',
            'loading_bay': rng.choice(bays),
            'planned_at': f'2026-11-24 {minute // 60:02d}:{minute % 60:02d}',
        })
    receipts = []
    for i in range(n_r):
        minute = base + rng.randint(0, 600)
        receipts.append({
            'receipt_id': f'R{i:02d}',
            'loading_bay': rng.choice(bays),
            'finished_at': f'2026-11-24 {minute // 60:02d}:{minute % 60:02d}',
        })
    return batches, receipts


def main():
    rng = random.Random(20261125)
    trials = 4000
    for trial in range(trials):
        batches, receipts = make_case(rng)
        expected = brute_force(batches, receipts)
        got = reconcile(batches, receipts)

        assert got['metrics']['最优配成批次数'] == expected['flow'], (trial, 'flow')
        assert got['metrics']['最优总等待分钟'] == expected['wait'], (trial, 'wait')

        # 直接用编号比对，避免与 reconcile 内部规范化排序的下标约定耦合。
        got_fixed = {(row['批次编号'], row['签收单号']) for row in got['fixed_pairs']}
        expect_fixed = {(batches[bi]['batch_id'], receipts[ri]['receipt_id'])
                        for bi, ri in expected['fixed']}
        assert got_fixed == expect_fixed, (trial, 'fixed', got_fixed, expect_fixed)

        got_amb = {(row['批次编号'], row['签收单号']) for row in got['ambiguous_edges']}
        expect_amb = {(batches[bi]['batch_id'], receipts[ri]['receipt_id'])
                      for (bi, ri) in (expected['flexible_edges'] - expected['fixed'])}
        assert got_amb == expect_amb, (trial, 'ambiguous', got_amb, expect_amb)

        # 台账状态核对：批次侧固定行应与暴力枚举的固定配对一致；签收单侧也应有对应固定行。
        ledger_fixed_pairs = {
            (row['编号'], row['配对签收单号']) for row in got['ledger']
            if row['状态'] == STATUS_FIXED and row['记录类型'] == '批次'
        }
        assert ledger_fixed_pairs == expect_fixed
        ledger_fixed_receipts = {
            (row['编号'],) for row in got['ledger']
            if row['状态'] == STATUS_FIXED and row['记录类型'] == '签收单'
        }
        assert ledger_fixed_receipts == {(receipts[ri]['receipt_id'],)
                                         for (_b, ri) in expected['fixed']}  # noqa: E501

        missing_batches = {row['编号'] for row in got['ledger']
                           if row['记录类型'] == '批次' and row['状态'] == STATUS_MISSING}
        expect_missing_b = {batches[bi]['batch_id'] for bi in range(len(batches))
                            if bi not in expected['batch_ever']}
        assert missing_batches == expect_missing_b, (trial, 'missing batch')

        missing_receipts = {row['编号'] for row in got['ledger']
                            if row['记录类型'] == '签收单' and row['状态'] == STATUS_MISSING}
        expect_missing_r = {receipts[ri]['receipt_id'] for ri in range(len(receipts))
                            if ri not in expected['receipt_ever']}
        assert missing_receipts == expect_missing_r, (trial, 'missing receipt')

        # 每个进入最优匹配但非固定的节点都应在台账中出现歧义条目。
        amb_batches = {row['编号'] for row in got['ledger'] if row['状态'] == STATUS_AMBIGUOUS
                       and row['记录类型'] == '批次'}
        amb_receipts = {row['编号'] for row in got['ledger'] if row['状态'] == STATUS_AMBIGUOUS
                        and row['记录类型'] == '签收单'}
        flex_b = {batches[bi]['batch_id'] for (bi, ri) in (expected['flexible_edges'] - expected['fixed'])}
        flex_r = {receipts[ri]['receipt_id'] for (bi, ri) in (expected['flexible_edges'] - expected['fixed'])}
        assert amb_batches == flex_b
        assert amb_receipts == flex_r

        # 行序无关性：打乱两张表顺序，结果必须一致。
        shuffled_b = batches[:]
        shuffled_r = receipts[:]
        rng.shuffle(shuffled_b)
        rng.shuffle(shuffled_r)
        again = reconcile(shuffled_b, shuffled_r)
        assert again['metrics'] == got['metrics']
        assert again['fixed_pairs'] == got['fixed_pairs']
        assert again['ambiguous_edges'] == got['ambiguous_edges']
        assert [(row['记录类型'], row['编号'], row['状态']) for row in again['ledger']] == \
               [(row['记录类型'], row['编号'], row['状态']) for row in got['ledger']]

    print(f'OK: {trials} random cases match brute force, order-invariance verified.')

    # 一个手工构造的确定性场景：同时覆盖固定配对、歧义配对与缺配。
    manual_b = [
        {'batch_id': 'B1', 'loading_bay': '一号位', 'planned_at': '2026-11-24 08:00'},
        {'batch_id': 'B2', 'loading_bay': '一号位', 'planned_at': '2026-11-24 08:10'},
        {'batch_id': 'B3', 'loading_bay': '一号位', 'planned_at': '2026-11-24 10:00'},
        {'batch_id': 'B4', 'loading_bay': '二号位', 'planned_at': '2026-11-24 09:00'},
    ]
    manual_r = [
        {'receipt_id': 'R1', 'loading_bay': '一号位', 'finished_at': '2026-11-24 08:30'},
        {'receipt_id': 'R2', 'loading_bay': '一号位', 'finished_at': '2026-11-24 08:40'},
        {'receipt_id': 'R3', 'loading_bay': '一号位', 'finished_at': '2026-11-24 10:20'},
        {'receipt_id': 'R4', 'loading_bay': '二号位', 'finished_at': '2026-11-24 11:30'},
    ]
    result = reconcile(manual_b, manual_r)
    print(result['headline'])
    for row in result['ledger']:
        print(row)
    statuses = {row['状态'] for row in result['ledger']}
    assert {STATUS_FIXED, STATUS_AMBIGUOUS, STATUS_MISSING} <= statuses
    # B4-R4 完装晚于叫车 150 分钟，超出两小时窗口，双方均缺配。
    assert any(row['编号'] == 'B4' and row['状态'] == STATUS_MISSING for row in result['ledger'])
    assert any(row['编号'] == 'R4' and row['状态'] == STATUS_MISSING for row in result['ledger'])
    # B3 仅能配 R3（B1/B2 与 10:20 间隔超过两小时），R3 仅能配 B3，为固定配对。
    assert ('B3', 'R3') in {(row['批次编号'], row['签收单号']) for row in result['fixed_pairs']}


if __name__ == '__main__':
    main()
