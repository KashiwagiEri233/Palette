export type InputEvent =
  | { type: 'move'; x: number; y: number }
  | { type: 'button'; button: 'left' | 'middle' | 'right'; down: boolean }
  | { type: 'key'; key: string; down: boolean }
  | { type: 'wheel'; direction: 'up' | 'down' | 'left' | 'right'; steps: number }

export type DesktopMethod = 'profiles' | 'doctor' | 'image.info' | 'hardware.capabilities' | 'vms.settings.get' | 'vms.disk.resize' | 'vms.list' | 'vms.create' | 'vms.update' | 'vms.start' | 'vms.stop' | 'vms.restart' | 'vms.reset' | 'vms.delete' | 'display.ticket' | 'display.sync' | 'control.acquire' | 'control.input' | 'control.heartbeat' | 'control.release'
export interface OwnerInfo { owner_kind: 'embedded' | 'daemon'; config_root: string; runtime_root: string; version: string; features: string[] }
export interface Profile { profile_id: string; qemu: { arch: string; accelerator: string }; resources: { vcpus: number; memory_mib: number }; images: { size_gib: number }; network: { mode: string; model?: string | null; mac?: string | null }; devices: { display_model: string; default_resolution: boolean }; display: { width: number; height: number }; audio: { enabled: boolean } }
export interface PortForward { protocol: 'tcp' | 'udp'; host_port: number; guest_port: number }
export interface HardwareSettings { display: { model: string; width: number; height: number; default_resolution: boolean }; network: { model: 'virtio-net-pci' | 'e1000' | 'rtl8139' | null; mac: string | null; forwards: PortForward[] }; audio_enabled: boolean; iso_path: string | null; boot_order: 'auto' | 'disk' | 'cdrom' }
export interface HardwareCapabilities { available: boolean; reason: string; displays: { model: string; resolution: boolean }[]; network_models: string[]; audio_available: boolean; audio_backend: string | null; host: { vcpus: number | null; memory_mib: number } }
export interface VmDraft { name: string; profile_id: string; source: 'image' | 'iso'; path: string; image_format: 'qcow2' | 'raw'; vcpus: number; memory_mib: number; disk_gib: number; network: 'offline' | 'restricted-lab' | 'nat'; settings?: HardwareSettings | null }
export interface VmSettingsResult { revision: number; draft: VmDraft; settings: HardwareSettings; iso_attached: boolean; editable: boolean; capabilities: HardwareCapabilities; disk: { configured_gib: number; virtual_bytes: number | null; actual_bytes: number | null; allocated: boolean }; preserved_network: { extra_options: string | null; hostfwd: string[] } }
export interface VmStatus { state: string; instance_id?: string; guest_arch?: string; accelerator?: string; control_owner?: 'agent' | 'operator'; control_state?: string; display_epoch?: number; boot_epoch?: number; controller_epoch?: number; action_seq?: number; queued_actions?: number; qmp_ready?: boolean; screen_ready?: boolean; desktop_ready?: boolean | null; input_ledger?: { keys: string[]; buttons: string[]; release_confirmed: boolean }; display?: { width: number; height: number }; last_error?: { code: string; message: string } }
export interface VmRecord { vm_id: string; name: string; persistent: boolean; definition: { draft: VmDraft; iso_attached: boolean; revision: number } | null; status: VmStatus }
export interface ControlLease { lease_id: string; instance_id: string; controller_epoch: number; boot_epoch: number; display_epoch: number }
export interface DoctorReport { ok: boolean; checks: { name: string; ok: boolean; blocking?: boolean; detail?: unknown }[] }
export interface DesktopApi {
  readonly platform: string
  setWindowBackdrop(opacity: number): Promise<void>
  connect(): Promise<OwnerInfo>
  request(method: DesktopMethod, vmId?: string, payload?: Record<string, unknown>): Promise<unknown>
  chooseFile(source: 'image' | 'iso'): Promise<string | null>
  chooseRoot(): Promise<OwnerInfo | null>
  onClose(callback: () => void): () => void
  finishClose(choice: 'background' | 'shutdown' | 'cancel'): Promise<void>
}
declare global { interface Window { palette: DesktopApi } }
