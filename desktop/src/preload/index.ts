import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopApi, DesktopMethod } from '../shared/types'
import type { WorkspaceMethod } from '../shared/workspace'

const api: DesktopApi = {
  platform: process.platform,
  setWindowBackdrop: (opacity, background) => ipcRenderer.invoke('window-backdrop', opacity, background),
  connect: () => ipcRenderer.invoke('connect'),
  request: (method: DesktopMethod | WorkspaceMethod, vmId?: string, payload?: Record<string, unknown>) => ipcRenderer.invoke('request', method, vmId, payload),
  chooseFile: source => ipcRenderer.invoke('choose-file', source),
  chooseRoot: () => ipcRenderer.invoke('choose-root'),
  onClose: callback => { const listener = (): void => callback(); ipcRenderer.on('close-request', listener); return () => ipcRenderer.removeListener('close-request', listener) },
  finishClose: choice => ipcRenderer.invoke('finish-close', choice)
}
contextBridge.exposeInMainWorld('palette', api)
ipcRenderer.on('control-lost', () => window.dispatchEvent(new Event('palette-control-lost')))
