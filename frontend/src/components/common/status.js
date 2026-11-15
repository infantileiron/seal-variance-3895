import { STATUS } from '../../constants/enums.js';

export const badge = a =>
  `<span class=badge data-state=${a.approved ? 'approved' : a.status}>${a.approved ? '已签核' : STATUS[a.status] || a.status}</span>`;

// 认领 / 接手留痕：worker 身份、执行轮次与恢复原因均来自数据库评估记录。
export const claimInfo = a => {
  if (!a || !(a.worker_id || a.recovery_note)) return '';
  const rows = [];
  if (a.worker_id) rows.push(`<div><small>当前/末次认领进程</small><b>${String(a.worker_id).slice(0, 48)}</b></div>`);
  rows.push(`<div><small>执行轮次</small><b>${a.attempts || 1}</b></div>`);
  if (a.heartbeat_at) rows.push(`<div><small>最近心跳 (UTC)</small><b>${escTime(a.heartbeat_at)}</b></div>`);
  const note = a.recovery_note ? `<p class=recovery-note>↻ ${a.recovery_note}</p>` : '';
  return `<div class=claim-info>${rows.join('')}${note}</div>`;
};

function escTime(v) {
  return String(v).slice(0, 19).replace('T', ' ');
}
