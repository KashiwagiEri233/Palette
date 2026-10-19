export interface GuestStatus {
  qga: { enabled: boolean; online: boolean; version?: string; hostname?: { 'host-name'?: string }; os?: Record<string, string>; interfaces?: { name: string; 'ip-addresses'?: { 'ip-address': string }[] }[]; commands: string[]; detail?: string }
  desktop: { clipboard: boolean; resize: boolean; reason: string }
  shares: { available: boolean; url: string; folders: { tag: string; read_only: boolean }[] }
}
export type GuestAction = 'copy' | 'paste' | 'text' | 'sync' | 'upload' | 'download' | 'share' | 'unshare'
export interface GuestProgress { vmId: string; bytes: number; phase: 'running' | 'done' }
