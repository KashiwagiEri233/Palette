import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopApi } from '../shared/types'

const api: DesktopApi = {
  platform: process.platform,
  setWindowBackdrop: opacity => ipcRenderer.invoke('window-backdrop', opacity),
  connect: () => ipcRenderer.invoke('connect'),
  request: (method, vmId, payload) => ipcRenderer.invoke('request', method, vmId, payload),
  chooseFile: source => ipcRenderer.invoke('choose-file', source),
  chooseRoot: () => ipcRenderer.invoke('choose-root'),
  onClose: callback => { const listener = (): void => callback(); ipcRenderer.on('close-request', listener); return () => ipcRenderer.removeListener('close-request', listener) },
  finishClose: choice => ipcRenderer.invoke('finish-close', choice)
}
contextBridge.exposeInMainWorld('palette', api)
ipcRenderer.on('control-lost', () => window.dispatchEvent(new Event('palette-control-lost')))
