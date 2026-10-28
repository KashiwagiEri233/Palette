import { expect, it } from 'vitest'
import { DebugInfoStore, scrubDebugInfo } from '../src/main/diagnostics'

it('retains backend codes, stack and QEMU stderr across Electron error serialization', () => {
  const store = new DebugInfoStore()
  const error = Object.assign(new Error('STARTUP_FAILED: QMP channel closed'), {
    code: 'STARTUP_FAILED', details: { stderr: ['-vnc: Failed to bind socket'], detail: 'QMP channel closed' }
  })
  store.capture('root', 'vms.start', error, 'vm')
  expect(store.find('root', "Error: Error invoking remote method 'request': Error: STARTUP_FAILED: QMP channel closed", 'vm')).toMatchObject({
    operation: 'vms.start', vmId: 'vm', error: { code: 'STARTUP_FAILED', stack: expect.stringContaining('STARTUP_FAILED'), details: { stderr: ['-vnc: Failed to bind socket'] } }
  })
  expect(store.find('other-root', error.message, 'vm')).toBeUndefined()
  expect(store.find('root', error.message, 'other-vm')).toBeUndefined()
})

it('redacts credentials, endpoint secrets and chat payloads while preserving useful errors', () => {
  const report = scrubDebugInfo({
    error: 'HTTP 400 https://user:pass@example.com/v1?key=hidden#secret Bearer private-token api_key="private-key" {"password":"private-password"}',
    headers: { Authorization: 'private-token' }, messages: [{ content: 'chat text' }],
    details: { apiKey: 'key', payload: { prompt: 'task text' }, stderr: ['QMP channel closed'] }
  })
  expect(report).toMatchObject({ details: { stderr: ['QMP channel closed'] } })
  const encoded = JSON.stringify(report)
  for (const secret of ['user:', 'pass@', 'hidden', 'private-token', 'private-key', 'private-password', 'chat text', 'task text']) expect(encoded).not.toContain(secret)
  expect(encoded).toContain('https://example.com/v1')
})

it('bounds error history, circular details and string size, and removes unsaved keys', () => {
  const store = new DebugInfoStore()
  const details: Record<string, unknown> = { stderr: ['unsaved-secret'], counter: 2n }
  details.circular = details
  for (let index = 0; index < 21; index++) store.capture('root', 'agent.models.discover', Object.assign(new Error(`failure ${index}`), { details }), undefined, 'unsaved-secret')
  expect(store.find('root', 'failure 0')).toBeUndefined()
  const encoded = JSON.stringify(store.find('root', 'failure 20'))
  expect(encoded).toContain('[depth limit]')
  expect(encoded).toContain('[redacted]')
  expect(encoded).not.toContain('unsaved-secret')
  expect((scrubDebugInfo('x'.repeat(20000)) as string).length).toBe(16000)
})
