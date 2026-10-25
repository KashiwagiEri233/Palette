import type { VmRecord } from '../../shared/types'

function rawErrorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.replace(/^(?:Error: )?Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '')
}
export function isShutdownTimeout(error: unknown): boolean {
  return rawErrorText(error) === 'TIMEOUT: the guest did not shut down within the configured budget'
}
export function isShutdownFailure(error: unknown): boolean {
  return /^(TIMEOUT|QMP_UNAVAILABLE|QMP_COMMAND_FAILED|ACTION_OUTCOME_UNKNOWN|GUEST_TOOL_UNAVAILABLE):/.test(rawErrorText(error))
}
export function errorText(error: unknown): string {
  const text = rawErrorText(error)
  if (isShutdownTimeout(error)) return '虚拟机未在规定时间内完成正常关机。客户机未响应关机请求，可在虚拟机内关机，或确认后强制停止。'
  return text
}
export const activeVm = (vm: VmRecord): boolean => !!vm.status.process_running || !['STOPPED', 'FAILED'].includes(vm.status.state)

