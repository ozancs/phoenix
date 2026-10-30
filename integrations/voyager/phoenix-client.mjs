// Phoenix API v1 helpers. No network connection, Vue state or printer credentials.
// Voyager supplies its existing Moonraker transport and printer-scoped store.
export const CHECKS = Object.freeze([
  'bed_attached', 'axes_unmoved', 'plate_unmoved', 'nozzle_free',
  'path_clear', 'uncertainty_accepted',
]);

export function parsePhoenixEvent(line) {
  const prefix = '// phoenix:event ';
  if (typeof line !== 'string' || !line.startsWith(prefix) || line.length > 4096) return null;
  try {
    const event = JSON.parse(line.slice(prefix.length));
    return event?.api_version === 1 && typeof event.event === 'string'
      && typeof event.phase === 'string' ? event : null;
  } catch {
    return null;
  }
}

export function checkCommand(item, checked) {
  if (!CHECKS.includes(item) || typeof checked !== 'boolean') throw new Error('Invalid checklist input');
  return `PHOENIX_CHECK ITEM=${item} VALUE=${checked ? 1 : 0}`;
}

export function selectCommand(index) {
  if (!Number.isSafeInteger(index) || index < 0) throw new Error('Invalid checkpoint index');
  return `PHOENIX_SELECT INDEX=${index}`;
}

export function canPrepare(status) {
  return status?.api_version === 1 && status.phase === 'detected' && !status.busy
    && status.profile_ready && CHECKS.every((key) => status.checks?.[key] === true);
}

export function canResume(status, cleaned, pathClear) {
  return status?.api_version === 1 && status.phase === 'prepared' && !status.busy
    && cleaned === true && pathClear === true;
}

export function prepareCommand(status) {
  if (!canPrepare(status)) throw new Error('Recovery preparation is not ready');
  return 'PHOENIX_PREPARE';
}

export function resumeCommand(status, cleaned, pathClear) {
  if (!canResume(status, cleaned, pathClear)) throw new Error('Recovery resume is not ready');
  return 'PHOENIX_RESUME CLEANED=1 PATH_CLEAR=1';
}

export function createAlertObserver(onAlert) {
  const seen = new Set();
  return (printerKey, status) => {
    if (!printerKey || status?.api_version !== 1) return;
    if (!['detected', 'blocked'].includes(status.phase)) return;
    const key = JSON.stringify([printerKey, status.job_id, status.phase, status.error]);
    if (seen.has(key)) return;
    seen.add(key);
    onAlert({
      key, printerKey, jobId: status.job_id, phase: status.phase,
      title: status.phase === 'blocked' ? 'Recovery needs attention' : 'Interrupted print detected',
      hint: status.error || 'Possible power loss or restart. Review the print before recovery.',
      // The caller opens its wizard. A notification must NEVER run a printer command.
      action: 'open_phoenix',
    });
  };
}
