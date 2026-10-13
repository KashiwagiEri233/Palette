export type InputEvent =
  | { type: 'move'; x: number; y: number }
  | { type: 'button'; button: 'left' | 'middle' | 'right'; down: boolean }
  | { type: 'key'; key: string; down: boolean }
  | { type: 'wheel'; direction: 'up' | 'down' | 'left' | 'right'; steps: number }

export type DesktopMethod = 'profiles' | 'doctor' | 'image.info' | 'vms.list' | 'vms.create' | 'vms.update' | 'vms.start' | 'vms.stop' | 'vms.restart' | 'vms.reset' | 'vms.delete' | 'display.ticket' | 'display.sync' | 'control.acquire' | 'control.input' | 'control.heartbeat' | 'control.release'
export interface OwnerInfo { owner_kind: 'embedded' | 'daemon'; config_root: string; runtime_root: string; version: string }
export interface Profile { profile_id: string; qemu: { arch: string; accelerator: string }; resources: { vcpus: number; memory_mib: number }; images: { size_gib: number }; network: { mode: string } }
export interface VmDraft { name: string; profile_id: string; source: 'image' | 'iso'; path: string; image_format: 'qcow2' | 'raw'; vcpus: number; memory_mib: number; disk_gib: number; network: 'offline' | 'restricted-lab' | 'nat' }
export interface VmStatus { state: string; instance_id?: string; guest_arch?: string; accelerator?: string; control_owner?: 'agent' | 'operator'; control_state?: string; display_epoch?: number; boot_epoch?: number; controller_epoch?: number; action_seq?: number; queued_actions?: number; qmp_ready?: boolean; screen_ready?: boolean; desktop_ready?: boolean | null; input_ledger?: { keys: string[]; buttons: string[]; release_confirmed: boolean }; display?: { width: number; height: number }; last_error?: { code: string; message: string } }
export interface VmRecord { vm_id: string; name: string; persistent: boolean; definition: { draft: VmDraft; iso_attached: boolean } | null; status: VmStatus }
export interface ControlLease { lease_id: string; instance_id: string; controller_epoch: number; boot_epoch: number; display_epoch: number }
export interface DoctorReport { ok: boolean; checks: { name: string; ok: boolean; blocking?: boolean; detail?: unknown }[] }
export interface DesktopApi {
  connect(): Promise<OwnerInfo>
  request(method: DesktopMethod, vmId?: string, payload?: Record<string, unknown>): Promise<unknown>
  chooseFile(source: 'image' | 'iso'): Promise<string | null>
  chooseRoot(): Promise<OwnerInfo | null>
  onClose(callback: () => void): () => void
  finishClose(choice: 'background' | 'shutdown' | 'cancel'): Promise<void>
}
declare global { interface Window { palette: DesktopApi } }
