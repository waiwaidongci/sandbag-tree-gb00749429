"""签收单与排车批次的配对对账。

业务口径：
- 签收单只有单号（receipt_id）、装车位（loading_bay）、完装时间（finished_at）。
- 排车批次至少含批次编号（batch_id）、装车位、叫车时间（planned_at）。
- 一条配对边成立当且仅当：同一装车位，且 0 <= 完装时间 - 叫车时间 <= 120 分钟。
- 每个批次、每张签收单在任一配法中最多使用一次（二分图匹配）。
- 优化目标分层：先让配成的批次最多，再让总等待分钟（完装 - 叫车）最少。
- 只把所有最优配法共有的配对写入分析台账（固定配对）；在不同最优配法中
  配对对象会变化的标“歧义”；始终无法进入任何最优配对的批次/签收单标“缺配”。
- 所有判定只依赖规范化后的内容（按编号排序、时间转分钟），与两张表的行序无关。

实现用最小费用流求分层最优匹配，再在终态残量网络上求节点位势、只保留约化
费用为 0 的“紧弧”，对紧残量图（含源汇）做强连通分量（SCC）与可达性分析：
一条可配对边属于某个最优匹配，当且仅当它承载流量、与对方处于同一 SCC（等代价
交替环），或存在一条经过该边的完整 source->sink 紧弧路径。固定配对要求该边两端
在所有最优匹配中都只有彼此一个可选对方。整个判定只依赖规范化后的内容，与行序无关。
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

WINDOW_MINUTES = 120
TIME_FORMAT = '%Y-%m-%d %H:%M'

# 分层费用：把“配成批次数”置于远高于任何可能等待总和的权重位。
# 单批等待上限 120 分钟，容量上限 5000，等待总和上界 600000；一百万足以保证
# 最大化配对数始终优先于最小化等待分钟。
PAIR_WEIGHT = 1_000_000

STATUS_FIXED = '固定配对'
STATUS_AMBIGUOUS = '歧义'
STATUS_MISSING = '缺配'


@dataclass(frozen=True)
class _Edge:
    to: int
    rev: int
    cap: int
    cost: int


class MinCostFlow:
    def __init__(self, size):
        self.g = [[] for _ in range(size)]

    def add(self, src, dst, cap, cost):
        forward = _Edge(dst, len(self.g[dst]), cap, cost)
        backward = _Edge(src, len(self.g[src]), 0, -cost)
        self.g[src].append(forward)
        self.g[dst].append(backward)

    def flow(self, source, sink):
        """连续最短路增广，返回 (流量, 费用, 终态邻接表)。费用均为非负，Bellman-Ford 即可。"""
        size = len(self.g)
        total_flow = 0
        total_cost = 0
        while True:
            dist = [None] * size
            prev_v = [-1] * size
            prev_e = [-1] * size
            dist[source] = 0
            while True:
                updated = False
                for v in range(size):
                    if dist[v] is None:
                        continue
                    for index, edge in enumerate(self.g[v]):
                        if edge.cap <= 0:
                            continue
                        candidate = dist[v] + edge.cost
                        if dist[edge.to] is None or candidate < dist[edge.to]:
                            dist[edge.to] = candidate
                            prev_v[edge.to] = v
                            prev_e[edge.to] = index
                            updated = True
                if not updated:
                    break
            if dist[sink] is None:
                break
            v = sink
            while v != source:
                edge = self.g[prev_v[v]][prev_e[v]]
                self.g[prev_v[v]][prev_e[v]] = _Edge(edge.to, edge.rev, edge.cap - 1, edge.cost)
                reverse = self.g[v][edge.rev]
                self.g[v][edge.rev] = _Edge(reverse.to, reverse.rev, reverse.cap + 1, reverse.cost)
                v = prev_v[v]
            total_flow += 1
            total_cost += dist[sink]
        return total_flow, total_cost, self.g


def _parse_time(value, label, index):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'第 {index} 条{label}缺少时间，请用 {TIME_FORMAT} 格式。')
    text = value.strip()
    try:
        timestamp = datetime.strptime(text, TIME_FORMAT)
        if timestamp.strftime(TIME_FORMAT) != text:
            raise ValueError
    except ValueError as error:
        raise ValueError(f'第 {index} 条{label}的时间 {text} 无效，请用本地时间 {TIME_FORMAT}。') from error
    return timestamp


def _normalize(rows, identity, bay_key, time_key, time_label):
    if not isinstance(rows, list) or not rows:
        raise ValueError('对账需要至少一条记录。')
    normalized = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f'第 {index} 条记录格式有误。')
        ident = row.get(identity)
        bay = row.get(bay_key)
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError(f'第 {index} 条记录缺少编号，请补齐后重试。')
        if not isinstance(bay, str) or not bay.strip():
            raise ValueError(f'{ident.strip()} 缺少装车位，请补齐后重试。')
        ident = ident.strip()
        if ident in seen:
            raise ValueError(f'编号 {ident} 重复，请核对源表，每个编号只保留一条有效记录。')
        seen.add(ident)
        timestamp = _parse_time(row.get(time_key), time_label, index)
        # 用固定纪元的朴素分钟数，避免依赖运行容器的本地时区；差值即等待分钟。
        epoch = datetime(1970, 1, 1)
        normalized.append({'id': ident, 'bay': bay.strip(),
                           'time': timestamp, 'minute': int((timestamp - epoch).total_seconds() // 60)})
    # 规范化排序：结果只依赖内容，不依赖原始行序。
    normalized.sort(key=lambda item: item['id'])
    return normalized


def _tarjan_scc(graph):
    """Tarjan 强连通分量。graph 为邻接表（节点 -> 可直达节点列表）。返回每个节点的分量号。"""
    size = len(graph)
    index = 0
    stack = []
    on_stack = [False] * size
    indices = [-1] * size
    low = [0] * size
    component = [-1] * size
    component_count = 0

    def walk(start):
        nonlocal index, component_count
        work = [(start, 0)]
        indices[start] = low[start] = index
        index += 1
        stack.append(start)
        on_stack[start] = True
        while work:
            v, cursor = work[-1]
            if cursor < len(graph[v]):
                w = graph[v][cursor]
                work[-1] = (v, cursor + 1)
                if indices[w] == -1:
                    indices[w] = low[w] = index
                    index += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack[w]:
                    low[v] = min(low[v], indices[w])
            else:
                if low[v] == indices[v]:
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        component[w] = component_count
                        if w == v:
                            break
                    component_count += 1
                work.pop()
                if work:
                    parent, _ = work[-1]
                    low[parent] = min(low[parent], low[v])

    for node in range(size):
        if indices[node] == -1:
            walk(node)
    return component


def reconcile(batches, receipts):
    """对排车批次与签收单做分层最优配对。

    返回结构化结果：指标、分析台账（含固定/歧义/缺配）、固定配对表、歧义边列表与口径说明。
    批次与签收单字段：batch_id/receipt_id、loading_bay、planned_at/finished_at。
    """
    batches = _normalize(batches, 'batch_id', 'loading_bay', 'planned_at', '排车批次')
    receipts = _normalize(receipts, 'receipt_id', 'loading_bay', 'finished_at', '签收单')

    n_b = len(batches)
    n_r = len(receipts)
    # 允许的配对边（同装车位、叫车后两小时内含边界、完装不早于叫车）。
    allowed = defaultdict(list)   # batch index -> [(receipt index, wait minutes)]
    wait_lookup = {}
    for bi, batch in enumerate(batches):
        for ri, receipt in enumerate(receipts):
            if receipt['bay'] != batch['bay']:
                continue
            wait = receipt['minute'] - batch['minute']
            if 0 <= wait <= WINDOW_MINUTES:
                allowed[bi].append((ri, wait))
                wait_lookup[(bi, ri)] = wait

    # 最小费用流：source -> 批次 -> 签收单 -> sink。
    # 配对成功费用 = wait - PAIR_WEIGHT（负），在最小化费用时同时实现
    # “配对数最大化优先、等待总和最小化其次”的分层目标。
    source = n_b + n_r
    sink = source + 1
    mcf = MinCostFlow(sink + 1)
    for bi in range(n_b):
        mcf.add(source, bi, 1, 0)
    for ri in range(n_r):
        mcf.add(n_b + ri, sink, 1, 0)
    for bi, edges in allowed.items():
        for ri, wait in edges:
            mcf.add(bi, n_b + ri, 1, wait - PAIR_WEIGHT)

    flow, cost, residual = mcf.flow(source, sink)
    total_wait = cost + flow * PAIR_WEIGHT

    # 从终态残量网络读出一个最优匹配。节点 0..n_b-1 为批次，n_b..n_b+n_r-1 为签收单。
    matched_receipt = {}   # batch -> receipt
    matched_batch = {}     # receipt -> batch
    for bi in range(n_b):
        for edge in residual[bi]:
            if n_b <= edge.to < n_b + n_r and edge.cap == 0:
                # 原容量 1、当前残量 0，说明该批次-签收单边承载了 1 单位流量（在最优匹配中）。
                ri = edge.to - n_b
                matched_receipt[bi] = ri
                matched_batch[ri] = bi

    # 在终态残量网络上求节点位势 p（以 source 为根的最短距离）。
    # 可达节点得到有限位势；与 source 不连通的已匹配核心先重置距离再做一次 Bellman-Ford。
    size = sink + 1

    def bellman_ford(initial):
        dist = list(initial)
        while True:
            updated = False
            for v in range(size):
                if dist[v] is None:
                    continue
                for edge in residual[v]:
                    if edge.cap <= 0:
                        continue
                    candidate = dist[v] + edge.cost
                    if dist[edge.to] is None or candidate < dist[edge.to]:
                        dist[edge.to] = candidate
                        updated = True
            if not updated:
                return dist

    potential = bellman_ford([0] + [None] * (size - 1))
    if any(potential[v] is None for v in range(size)):
        potential = bellman_ford([0 if potential[v] is None else potential[v]
                                  for v in range(size)])

    # 紧残量图：只保留约化费用为 0 的残量弧。等代价（同配对数、同等待）的最优流互换
    # 全部经由紧弧完成；含正代价的环会改变等待总和，不属于最优解，在此被自然排除。
    tight = [[] for _ in range(size)]
    for v in range(size):
        for edge in residual[v]:
            if edge.cap > 0 and potential[v] is not None and potential[edge.to] is not None \
                    and potential[v] + edge.cost == potential[edge.to]:
                tight[v].append(edge.to)
    component = _tarjan_scc(tight)

    # 判定每条允许的批次-签收单边是否出现在至少一个最优（同配对数、同等待）匹配中。
    # 依据紧残量网络（含源汇）：
    #   - 边已承载流量（当前最优匹配采用）；
    #   - 未承载但两端在同一 SCC（等代价交替环，可整体换入换出）；
    #   - 未承载但存在一条经过该边的完整 source->sink 紧弧路径：
    #     批次可由 source 沿紧弧到达，且签收单可沿紧弧到达 sink。
    reachable_from_source = set()
    stack = [source]
    while stack:
        v = stack.pop()
        if v in reachable_from_source:
            continue
        reachable_from_source.add(v)
        stack.extend(tight[v])
    can_reach_sink = set()
    reverse_tight = [[] for _ in range(size)]
    for v, neighbours in enumerate(tight):
        for w in neighbours:
            reverse_tight[w].append(v)
    stack = [sink]
    while stack:
        v = stack.pop()
        if v in can_reach_sink:
            continue
        can_reach_sink.add(v)
        stack.extend(reverse_tight[v])

    in_optimal = set()
    for bi, edges in allowed.items():
        for ri, _wait in edges:
            receipt_node = n_b + ri
            if matched_receipt.get(bi) == ri:
                in_optimal.add((bi, ri))
            elif component[bi] == component[receipt_node]:
                in_optimal.add((bi, ri))
            elif bi in reachable_from_source and receipt_node in can_reach_sink:
                in_optimal.add((bi, ri))

    # 固定配对：该边是某最优匹配的一部分，且批次与签收单各自都只有这一条可选最优边。
    # 等价地，双方在所有最优配法中都只能选对方。
    partners_b = defaultdict(list)   # 批次 -> 可在某最优匹配中配对的签收单
    partners_r = defaultdict(list)   # 签收单 -> 可在某最优匹配中配对的批次
    for bi, ri in in_optimal:
        partners_b[bi].append(ri)
        partners_r[ri].append(bi)

    edge_status = {}
    for bi, ri in in_optimal:
        if len(partners_b[bi]) == 1 and len(partners_r[ri]) == 1:
            edge_status[(bi, ri)] = STATUS_FIXED
        else:
            edge_status[(bi, ri)] = STATUS_AMBIGUOUS

    def receipt_id(ri):
        return receipts[ri]['id']

    def batch_id(bi):
        return batches[bi]['id']

    ledger = []
    fixed_pairs = []

    # 进入“至少一个最优匹配”的节点集合：有可选最优对方的批次/签收单都不会缺配。
    matched_batches = set(partners_b)
    matched_receipts = set(partners_r)

    # 批次侧台账：当前（或唯一）配对边为固定则标固定，否则标歧义，无任何最优对方为缺配。
    for bi in sorted(range(n_b), key=lambda idx: batches[idx]['id']):
        batch = batches[bi]
        options = sorted(partners_b.get(bi, []))
        if not options:
            reason = _missing_reason(batch, receipts, allowed[bi])
            ledger.append(_ledger_row('批次', batch['id'], batch['bay'], batch['time'],
                                      None, None, None, STATUS_MISSING, reason))
            continue
        chosen_ri = matched_receipt.get(bi, options[0])
        wait = wait_lookup[(bi, chosen_ri)]
        if edge_status[(bi, chosen_ri)] == STATUS_FIXED:
            ledger.append(_ledger_row('批次', batch['id'], batch['bay'], batch['time'],
                                      receipts[chosen_ri]['id'], receipts[chosen_ri]['time'],
                                      wait, STATUS_FIXED, '所有最优配法共有配对'))
            fixed_pairs.append(_pair_row(batch, receipts[chosen_ri], wait))
        else:
            note = '可替换签收单：' + '、'.join(receipt_id(p) for p in options)
            ledger.append(_ledger_row('批次', batch['id'], batch['bay'], batch['time'],
                                      receipts[chosen_ri]['id'], receipts[chosen_ri]['time'],
                                      wait, STATUS_AMBIGUOUS, note))

    # 签收单侧台账：固定性同样按边判定；该签收单在某最优匹配中的批次存在替代时标歧义。
    for ri in sorted(range(n_r), key=lambda idx: receipts[idx]['id']):
        receipt = receipts[ri]
        options = sorted(partners_r.get(ri, []), key=lambda idx: batches[idx]['id'])
        if not options:
            reason = _missing_reason_receipt(
                receipt, batches, [bi for bi in range(n_b) if (bi, ri) in wait_lookup])
            ledger.append(_ledger_row('签收单', receipt['id'], receipt['bay'], None,
                                      None, receipt['time'], None, STATUS_MISSING, reason))
            continue
        chosen_bi = matched_batch.get(ri, options[0])
        wait = wait_lookup[(chosen_bi, ri)]
        if edge_status[(chosen_bi, ri)] == STATUS_FIXED:
            ledger.append(_ledger_row('签收单', receipt['id'], receipt['bay'],
                                      batches[chosen_bi]['time'], batches[chosen_bi]['id'],
                                      receipt['time'], wait, STATUS_FIXED, '所有最优配法共有配对'))
        else:
            note = '可替换批次：' + '、'.join(batch_id(b) for b in options)
            ledger.append(_ledger_row('签收单', receipt['id'], receipt['bay'],
                                      batches[chosen_bi]['time'], batches[chosen_bi]['id'],
                                      receipt['time'], wait, STATUS_AMBIGUOUS, note))

    # 始终未进入任何最优匹配的批次与签收单。
    unmatched_batches = [bi for bi in range(n_b) if bi not in matched_batches]
    unmatched_receipts = [ri for ri in range(n_r) if ri not in matched_receipts]

    fixed_pairs.sort(key=lambda row: (row['批次编号'], row['签收单号']))
    ambiguous_edges = [
        {'批次编号': batches[bi]['id'], '签收单号': receipts[ri]['id'],
         '装车位': batches[bi]['bay'], '等待分钟': wait_lookup[(bi, ri)],
         '状态': edge_status[(bi, ri)]}
        for (bi, ri), status in edge_status.items() if status == STATUS_AMBIGUOUS
    ]
    ambiguous_edges.sort(key=lambda row: (row['批次编号'], row['签收单号']))

    metrics = {
        '排车批次数': n_b,
        '签收单数': n_r,
        '可配对边数': len(wait_lookup),
        '最优配成批次数': flow,
        '最优总等待分钟': total_wait,
        '固定配对数': len(fixed_pairs),
        '歧义配对数': len(ambiguous_edges),
        '缺配批次数': len(unmatched_batches),
        '缺配签收单数': len(unmatched_receipts),
    }
    notes = [
        f'配对窗口：同一装车位，且完装时间在叫车后 0 至 {WINDOW_MINUTES} 分钟内（含两端），'
        '完装早于叫车不予配对。',
        '分层目标：先最大化配成批次数，再在所有最大配对中最小化总等待分钟（实耗 = 完装时间 − 叫车时间）。',
        '每个批次与每张签收单在任一配法中最多使用一次；仅所有最优配法共有的配对写入固定台账，'
        '配对对象在最优配法间变化的标“歧义”，始终无法进入最优匹配的标“缺配”。',
        '判定基于按编号规范化排序后的内容，与两张表的行序无关；换行序重试不新增数据版本。',
        '晚到签收记录与原排车单合并后整体重算并另存为新版本，已发布的旧分析仍绑定原台账版本，不被覆盖。',
    ]
    return {
        'headline': f'最优配成 {flow} 个批次，总等待 {total_wait} 分钟；'
                    f'其中固定配对 {len(fixed_pairs)} 条、歧义边 {len(ambiguous_edges)} 条，'
                    f'缺配批次 {len(unmatched_batches)} 个、缺配签收单 {len(unmatched_receipts)} 张。',
        'metrics': metrics,
        'ledger': ledger,
        'fixed_pairs': fixed_pairs,
        'ambiguous_edges': ambiguous_edges,
        'notes': notes,
        'ledger_columns': ['记录类型', '编号', '装车位', '叫车时间', '配对签收单号',
                           '完装时间', '等待分钟', '状态', '说明'],
        'pair_columns': ['批次编号', '签收单号', '装车位', '叫车时间', '完装时间', '等待分钟'],
    }


def _missing_reason(batch, receipts, candidate_edges):
    if not candidate_edges:
        return '同装车位且在叫车后两小时窗口内无可用签收单'
    return '存在窗口内签收单，但纳入任一最优配对都会减少配成批次数'


def _missing_reason_receipt(receipt, batches, candidate_batches):
    if not candidate_batches:
        return '同装车位且在叫车后两小时窗口内无可用批次'
    return '存在窗口内批次，但纳入任一最优配对都会减少配成批次数'


def _fmt(timestamp):
    return datetime.strftime(timestamp, TIME_FORMAT) if timestamp else None


def _ledger_row(kind, ident, bay, planned_at, receipt_id_value, finished_at, wait, status, note):
    return {
        '记录类型': kind,
        '编号': ident,
        '装车位': bay,
        '叫车时间': _fmt(planned_at),
        '配对签收单号': receipt_id_value,
        '完装时间': _fmt(finished_at),
        '等待分钟': wait,
        '状态': status,
        '说明': note,
    }


def _pair_row(batch, receipt, wait):
    return {
        '批次编号': batch['id'],
        '签收单号': receipt['id'],
        '装车位': batch['bay'],
        '叫车时间': _fmt(batch['time']),
        '完装时间': _fmt(receipt['time']),
        '等待分钟': wait,
    }
