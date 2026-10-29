import { describe, expect, it } from 'vitest'
import { activeVm, isShutdownFailure } from '../src/renderer/lib/vm-lifecycle'
import type { VmRecord } from '../src/shared/types'

describe('VM process state and shutdown recovery', () => {
  it('keeps force stop available for failed guests with a live process', () => {
    const vm = { status: { state: 'FAILED', process_running: true } } as VmRecord
    expect(activeVm(vm)).toBe(true)
    expect(activeVm({ ...vm, status: { ...vm.status, process_running: false } })).toBe(false)
  })

  it.each(['TIMEOUT', 'QMP_UNAVAILABLE', 'QMP_COMMAND_FAILED', 'ACTION_OUTCOME_UNKNOWN'])('recognizes a shutdown error code (%s)', code => {
    expect(isShutdownFailure(new Error(`Error invoking remote method 'request': Error: ${code}: channel unavailable`))).toBe(true)
  })

  it('does not offer force stop for unrelated policy or identity failures', () => {
    expect(isShutdownFailure(new Error('STATE_CONFLICT: instance changed'))).toBe(false)
    expect(isShutdownFailure(new Error('POLICY_DENIED: forbidden'))).toBe(false)
  })
})
