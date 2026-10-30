import test from 'node:test';
import assert from 'node:assert/strict';
import { CHECKS, parsePhoenixEvent, checkCommand, selectCommand, createAlertObserver,
  canPrepare, prepareCommand, resumeCommand } from '../integrations/voyager/phoenix-client.mjs';

test('malformed/unrelated/future-version events are ignored', () => {
  for (const line of [null, 'hello', '// phoenix:event {', '// phoenix:event {"api_version":2}']) {
    assert.equal(parsePhoenixEvent(line), null);
  }
  assert.equal(parsePhoenixEvent('// phoenix:event {"api_version":1,"event":"ready","phase":"detected"}').phase, 'detected');
});
test('command builders refuse injected strings and invalid indexes', () => {
  assert.throws(() => checkCommand('bed_attached\nG28', true));
  assert.throws(() => checkCommand('bed_attached', 'yes'));
  for (const index of [-1, 1.5, NaN, '0\nG28']) assert.throws(() => selectCommand(index));
  assert.equal(selectCommand(2), 'PHOENIX_SELECT INDEX=2');
});
test('all checklist conditions and reviewed profile required', () => {
  const status = { api_version: 1, phase: 'detected', profile_ready: true, busy: false,
    checks: Object.fromEntries(CHECKS.map((key) => [key, true])) };
  assert.equal(prepareCommand(status), 'PHOENIX_PREPARE');
  for (const key of CHECKS) assert.equal(canPrepare({ ...status, checks: { ...status.checks, [key]: false } }), false);
  assert.equal(canPrepare({ ...status, busy: true }), false);
  assert.equal(canPrepare({ ...status, profile_ready: false }), false);
});
test('resume is a separate explicit action', () => {
  assert.throws(() => resumeCommand({ api_version: 1, phase: 'prepared' }, false, true));
  assert.throws(() => resumeCommand({ api_version: 1, phase: 'detected' }, true, true));
  assert.equal(resumeCommand({ api_version: 1, phase: 'prepared' }, true, true),
    'PHOENIX_RESUME CLEANED=1 PATH_CLEAR=1');
});
test('reconnect does not spam, printer and job changes still notify', () => {
  const alerts = [], observe = createAlertObserver((event) => alerts.push(event));
  const status = { api_version: 1, phase: 'detected', job_id: 'a' };
  observe('printer-1', status);
  observe('printer-1', status);
  observe('printer-2', status);
  observe('printer-1', { ...status, job_id: 'b' });
  observe('printer-1', { ...status, phase: 'recording' });
  assert.equal(alerts.length, 3);
  assert.equal(alerts[0].action, 'open_phoenix');
  assert.equal(alerts[0].title, 'Interrupted print detected');
});
