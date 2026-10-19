import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopApi, DesktopMethod } from '../shared/types'
import type { WorkspaceMethod } from '../shared/workspace'
import type { AgentMethod, AgentEvent } from '../shared/agent'

const api: DesktopApi = {
  guestAction: (vmId, instanceId, action, options) => ipcRenderer.invoke('guest-action', vmId, instanceId, action, options),
  cancelGuestAction: vmId => ipcRenderer.invoke('guest-cancel', vmId),
  onGuestProgress: callback => { const listener = (_event: Electron.IpcRendererEvent, event: import('../shared/guest').GuestProgress): void => callback(event); ipcRenderer.on('guest-progress', listener); return () => ipcRenderer.removeListener('guest-progress', listener) },
  platform: process.platform,
  setWindowBackdrop: (opacity, background) => ipcRenderer.invoke('window-backdrop', opacity, background),
  connect: () => ipcRenderer.invoke('connect'),
  request: (method: DesktopMethod | WorkspaceMethod | AgentMethod, vmId?: string, payload?: Record<string, unknown>) => ipcRenderer.invoke('request', method, vmId, payload),
  onAgentEvent: callback => { const listener = (_event: Electron.IpcRendererEvent, event: AgentEvent): void => callback(event); ipcRenderer.on('agent-event', listener); return () => ipcRenderer.removeListener('agent-event', listener) },
  chooseFile: source => ipcRenderer.invoke('choose-file', source),
  chooseRoot: () => ipcRenderer.invoke('choose-root'),
  onClose: callback => { const listener = (): void => callback(); ipcRenderer.on('close-request', listener); return () => ipcRenderer.removeListener('close-request', listener) },
  finishClose: choice => ipcRenderer.invoke('finish-close', choice)
}
contextBridge.exposeInMainWorld('palette', api)
ipcRenderer.on('control-lost', () => window.dispatchEvent(new Event('palette-control-lost')))
