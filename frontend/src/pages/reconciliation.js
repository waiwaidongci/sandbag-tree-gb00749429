import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {states} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

function reconcileReport(result) {
  const metrics = (result.metric_order || Object.keys(result.metrics))
    .map(key => [key, result.metrics[key]]);
  const ledgerColumns = (result.ledger_columns || []).map(key => ({key, label: key}));
  const pairColumns = (result.pair_columns || []).map(key => ({key, label: key}));
  return `<div class="reading"><h2>${esc(result.headline)}</h2>`
    + `<dl class="metrics">${metrics.map(([key, value]) =>
      `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`
    + `<h3>分析台账（固定 / 歧义 / 缺配）</h3>${table(result.ledger, ledgerColumns)}`
    + `<h3>固定配对（写入分析台账的共有配对）</h3>${table(result.fixed_pairs, pairColumns)}`
    + (result.ambiguous_edges?.length ? `<h3>歧义配对边</h3>${table(result.ambiguous_edges)}` : '')
    + `<h3>口径与限制</h3><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></div>`;
}

export async function reconciliation() {
  const isAnalyst = state.account.role === 'analyst';
  const [datasets, receiptVersions, jobs] = await Promise.all([
    api('/datasets'), api('/receipts'), api('/reconciliations'),
  ]);
  const selectedId = jobs.some(job => job.id === state.runId) ? state.runId : jobs[0]?.id;
  const active = selectedId ? await api(`/reconciliations/${selectedId}`) : null;

  const uploader = isAnalyst ? `<form id="receipt-upload" class="inline-form">`
    + '<label>签收台账文件（单号、装车位、完装时间）<input name="file" type="file" accept=".csv,text/csv" required></label>'
    + `<button type="submit">保存签收版本</button>${errorSlot}</form>` : '';

  const versionBlock = receiptVersions.length
    ? `<section><h2>签收台账版本</h2><p class="hint">按规范化内容 SHA-256 去重，换行序重试不新增版本。</p>`
      + table(receiptVersions.map(row => ({
        版本: {link: `#6/${row.id}`, text: `#${row.id}`}, 名称: row.name,
        归属: row.owner, 导入时间: time(row.created), 校验和: row.checksum.slice(0, 12),
      }))) + `<button type="button" id="receipt-example" class="secondary">下载签收样例 CSV</button>`
      + '<p id="receipt-download-error" role="alert" class="error"></p></section>'
    : empty('请先导入签收台账 CSV（三列：receipt_id、loading_bay、finished_at）。');

  const queueForm = isAnalyst && datasets.length && receiptVersions.length
    ? `<form id="reconcile-queue" class="inline-form"><label>排车批次版本<select name="dataset_id">`
      + datasets.map(row => `<option value="${row.id}" ${row.id === state.datasetId ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')
      + '</select></label><label>签收台账版本<select name="receipt_id">'
      + receiptVersions.map(row => `<option value="${row.id}">${esc(row.name)} · #${row.id}</option>`).join('')
      + `</select></label><button type="submit">提交对账</button>${errorSlot}</form>`
    : empty(isAnalyst ? '需要先有排车批次版本和签收台账版本。' : '分析员可提交对账。', '#1', '查看排车台账');

  const jobTable = jobs.length ? table(jobs.map(job => ({
    任务: {link: `#6/${job.id}`, text: `#${job.id}`},
    排车版本: job.dataset_id, 签收版本: job.receipt_id,
    状态: states[job.state] || job.state, 提交时间: time(job.created),
  }))) : '<p class="empty">还没有对账任务。</p>';
  const picker = jobs.length
    ? `<label>对账任务<select id="pick-reconcile">${jobs.map(job =>
      `<option value="${job.id}" ${job.id === selectedId ? 'selected' : ''}>#${job.id} · ${esc(states[job.state] || job.state)} · ${esc(time(job.created))}</option>`).join('')}</select></label>`
    : '';

  const resultBlock = active
    ? (active.result
      ? reconcileReport(active.result)
        + `<p><button type="button" class="secondary ledger-download" data-id="${active.id}">下载分析台账 CSV</button> `
        + `<button type="button" class="secondary fixed-download" data-id="${active.id}">下载固定配对 CSV</button></p>`
      : `<p ${active.error ? 'class="error"' : 'data-poll'}>${esc(active.error || states[active.state])}</p>`)
    : '';

  return `<section><h2>签收台账</h2>${uploader}</section>${versionBlock}`
    + `<section><h2>提交配对对账</h2><p class="hint">同装车位、叫车后两小时内可配对；先最大化配成批次数，再最小化总等待分钟。</p>${queueForm}</section>`
    + `<section><h2>对账记录</h2>${picker}${jobTable}</section>`
    + (active ? `<section><h2>对账结果 #${active.id}</h2>${resultBlock}</section>` : '');
}

export function bindReconciliation(root, refresh) {
  bindForm(root, '#receipt-upload', async form => {
    const file = form.elements.file.files[0];
    const imported = await api('/receipts', {name: file.name, csv: await file.text()});
    state.status = imported.created
      ? `已保存签收版本 #${imported.id}（${imported.rows} 行）。`
      : '内容与已有签收版本一致，已选中该版本。';
  }, refresh);

  bindForm(root, '#reconcile-queue', async form => {
    const data = Object.fromEntries(new FormData(form));
    await api('/reconciliations', {
      dataset_id: Number(data.dataset_id), receipt_id: Number(data.receipt_id),
    });
    state.status = '对账任务已提交，稍后自动刷新结果。';
  }, refresh);

  root.querySelector('#receipt-example')?.addEventListener('click', async () => {
    try {
      await download('/receipt-example.csv', '签收台账样例.csv');
    } catch (error) {
      root.querySelector('#receipt-download-error').textContent = error.message;
    }
  });

  root.querySelector('#pick-reconcile')?.addEventListener('change', event => {
    location.hash = `6/${event.target.value}`;
  });

  root.querySelector('.ledger-download')?.addEventListener('click', event => {
    const id = event.currentTarget.dataset.id;
    download(`/reconciliations/${id}/ledger.csv`, `签收对账分析台账-${id}.csv`);
  });
  root.querySelector('.fixed-download')?.addEventListener('click', event => {
    const id = event.currentTarget.dataset.id;
    download(`/reconciliations/${id}/fixed.csv`, `签收对账固定配对-${id}.csv`);
  });
}
