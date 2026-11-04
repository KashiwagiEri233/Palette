import type { DisplaySettings } from '../../shared/types'

export interface DisplaySettingsState {
  settings: DisplaySettings | null
  busy: boolean
  error: string
  saved: boolean
}

type DisplaySettingsRequest = (method: 'display.settings.get' | 'display.settings.update', payload?: { mode: 'low_latency' | 'compatibility' }) => Promise<DisplaySettings>

/** Keeps a stale response from a previous backend or settings page out of the UI. */
export class DisplaySettingsController {
  private state: DisplaySettingsState = { settings: null, busy: false, error: '', saved: false }
  private operation = 0
  private listeners = new Set<() => void>()

  constructor(private request: DisplaySettingsRequest) {}

  snapshot = (): DisplaySettingsState => this.state
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  cancel(): void { this.operation++ }

  async load(): Promise<void> { await this.run('display.settings.get') }

  async update(enabled: boolean): Promise<void> {
    if (this.state.busy || !this.state.settings) return
    await this.run('display.settings.update', { mode: enabled ? 'low_latency' : 'compatibility' })
  }

  private publish(state: DisplaySettingsState): void {
    this.state = state
    for (const listener of this.listeners) listener()
  }

  private async run(method: 'display.settings.get' | 'display.settings.update', payload?: { mode: 'low_latency' | 'compatibility' }): Promise<void> {
    const operation = ++this.operation
    this.publish({ ...this.state, busy: true, error: '', saved: false })
    try {
      const settings = await this.request(method, payload)
      if (operation === this.operation) this.publish({ settings, busy: false, error: '', saved: method === 'display.settings.update' })
    } catch (error) {
      if (operation === this.operation) this.publish({ ...this.state, busy: false, error: error instanceof Error ? error.message : String(error), saved: false })
    }
  }
}
