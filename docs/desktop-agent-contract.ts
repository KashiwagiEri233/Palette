/** Design contract only. Not wired into the application. Both design documents share this draft. */
export type Id = string
export type IsoTime = string
export type VmState = 'STOPPED' | 'STARTING' | 'RUNNING' | 'STOPPING' | 'PAUSED' | 'FAILED'
export type RunState =
  | 'idle' | 'starting' | 'observing' | 'acting' | 'waiting_approval'
  | 'taking_over' | 'human_control' | 'paused' | 'resuming' | 'stopping'
  | 'completed' | 'cancelled' | 'failed' | 'interrupted'
export type ControlState = 'view_only' | 'agent' | 'acquiring' | 'human' | 'releasing' | 'faulted'
export type DisplayState = 'connecting' | 'live' | 'disconnected' | 'failed'
export type ReasoningEffort = 'low' | 'medium' | 'high'
export interface ErrorInfo {
  code: 'NOT_RUNNING' | 'VM_BUSY' | 'STALE_CONTEXT' | 'APPROVAL_EXPIRED'
    | 'POLICY_DENIED' | 'CONTROL_FAULTED' | 'UNSUPPORTED' | 'DISCONNECTED'
    | 'RESULT_UNKNOWN' | 'INVALID_ARGUMENT' | 'PROVIDER_ERROR' | 'NOT_FOUND'
  message: string
  retryable: boolean
  ownerConversationId?: Id
}
export type Result<T> = { ok: true; value: T } | { ok: false; error: ErrorInfo }
export interface Epochs {
  instanceId: Id
  bootEpoch: number
  displayEpoch: number
  controllerEpoch: number
}
export interface Conversation {
  id: Id
  vm_id: Id
  title: string
  pinned: boolean
  archivedAt: IsoTime | null
  createdAt: IsoTime
  updatedAt: IsoTime
  revision: number
  activeRunId: Id | null
  draft: string
}
export interface AttachmentRef {
  id: Id
  name: string
  kind: 'image' | 'file'
  mime: string
  sizeBytes: number
  // Opaque main-owned reference; no arbitrary host path or data URL.
}
export interface Message {
  id: Id
  revision: number
  conversationId: Id
  vm_id: Id
  runId: Id | null
  role: 'user' | 'assistant' | 'system'
  status: 'pending' | 'streaming' | 'complete' | 'failed' | 'interrupted'
  content: string
  attachments: AttachmentRef[]
  createdAt: IsoTime
  elapsedMs?: number
  // Public progress summaries only; never private chain-of-thought.
  progress?: { id: Id; title: string; state: 'pending' | 'active' | 'complete' }[]
  toolStepIds: Id[]
  approvalIds: Id[]
  feedback?: 'up' | 'down' | null
  result?: { outcome: 'completed' | 'cancelled' | 'failed'; stepCount: number; takeoverCount: number }
}
export interface ScreenshotRef extends Epochs {
  id: Id
  width: number
  height: number
  capturedAt: IsoTime
  // Resolve via screenshots.get to a scoped palette-media URL.
}
export interface ToolStep {
  id: Id
  vm_id: Id
  conversationId: Id
  runId: Id
  index: number
  revision: number
  kind: 'observe' | 'click' | 'move' | 'scroll' | 'type' | 'key' | 'wait'
  summary: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'
  startedAt: IsoTime | null
  finishedAt: IsoTime | null
  before: ScreenshotRef | null
  after: ScreenshotRef | null
  point?: { x: number; y: number; width: number; height: number; displayEpoch: number }
  // Text and key descriptions must be redacted where sensitive.
  publicInput?: string
  error?: ErrorInfo
  actionSeq?: number
}
export interface ApprovalRequest extends Epochs {
  id: Id
  vm_id: Id
  conversationId: Id
  runId: Id
  stepId: Id
  revision: number
  title: string
  risks: string[]
  publicPlan: string[]
  actionDigest: string
  policyRevision: number
  scope: 'single_action'
  status: 'pending' | 'allowed' | 'denied' | 'expired' | 'cancelled'
  createdAt: IsoTime
  expiresAt: IsoTime
  decidedAt: IsoTime | null
}
export interface AgentRun {
  id: Id
  vm_id: Id
  conversationId: Id
  state: RunState
  revision: number
  providerId: Id
  modelId: Id
  reasoningEffort: ReasoningEffort | null
  maxSteps: number
  stepCount: number
  takeoverCount: number
  pauseReason?: 'user' | 'control_lost' | 'display_lost' | 'approval_denied' | 'recovery'
  error?: ErrorInfo
}
export interface ControlView {
  vm_id: Id
  state: ControlState
  revision: number
  ownerConversationId: Id | null
  ownerRunId: Id | null
  epochs: Epochs | null
  releaseConfirmed: boolean
  // Renderer receives no lease bearer token. Main owns heartbeat and lease ID.
}
export interface VmAgentView {
  vm_id: Id
  name: string
  state: VmState
  arch: string | null
  displayState: DisplayState
  resolution: { width: number; height: number } | null
  control: ControlView
  activeRun: AgentRun | null
  metrics: { cpuPercent: number | null; memoryBytes: number | null; sampledAt: IsoTime | null }
  features: { agent: boolean; pauseResume: boolean; networkIsolation: boolean; clipboard: boolean }
}
export interface ModelOption {
  id: Id
  name: string
  reasoningEfforts: ReasoningEffort[]
}
export interface AgentSettings {
  providers: { id: Id; name: string; credentialConfigured: boolean }[]
  providerId: Id | null
  modelId: Id | null
  credentialConfigured: boolean
  reasoningEffort: ReasoningEffort | null
  maxSteps: number
  models: ModelOption[]
  permission: {
    revision: number
    requireApproval: ('delete' | 'submit' | 'shutdown' | 'network' | 'clipboard')[]
    allowNetwork: boolean
    allowClipboard: boolean
  }
}
export interface Snapshot {
  streamEpoch: Id
  cursor: number
  conversations: Conversation[]
  messages: Message[]
  steps: ToolStep[]
  approvals: ApprovalRequest[]
  runs: AgentRun[]
  vms: VmAgentView[]
  settings: AgentSettings
  // Only selected conversation history is included; other history is paginated.
}
type EventPayload =
  | { type: 'conversation.upsert'; conversation: Conversation; historyReset?: boolean }
  | { type: 'conversation.deleted'; conversationId: Id }
  | { type: 'message.upsert'; message: Message }
  | { type: 'message.delta'; messageId: Id; revision: number; offset: number; text: string }
  | { type: 'step.upsert'; step: ToolStep }
  | { type: 'approval.upsert'; approval: ApprovalRequest }
  | { type: 'run.upsert'; run: AgentRun }
  | { type: 'control.changed'; control: ControlView }
  | { type: 'vm.upsert'; vm: VmAgentView }
  | { type: 'settings.changed'; settings: AgentSettings }
  | { type: 'stream.reset_required'; reason: 'expired' | 'epoch_changed' | 'overflow' }
export type AgentEvent = EventPayload & {
  subscriptionId: Id
  streamEpoch: Id
  seq: number
  emittedAt: IsoTime
}
export interface Mutation {
  requestId: Id
}
export type InputEvent =
  | { type: 'move'; x: number; y: number }
  | { type: 'button'; button: 'left' | 'middle' | 'right'; down: boolean }
  | { type: 'key'; key: string; down: boolean }
  | { type: 'wheel'; direction: 'up' | 'down' | 'left' | 'right'; steps: number }
interface Route<P, R, V extends Id | undefined = Id> { payload: P; result: R; vmId: V }
export interface DesktopAgentRoutes {
  'agent.bootstrap': Route<{ conversationId?: Id }, Snapshot, undefined>
  'conversations.create': Route<Mutation & { title?: string }, Conversation>
  'conversations.list': Route<{ cursor?: string; limit: number; includeArchived: boolean }, { items: Conversation[]; cursor: string | null }, Id | undefined>
  'conversations.history': Route<{ conversationId: Id; before?: string; limit: number }, { messages: Message[]; steps: ToolStep[]; approvals: ApprovalRequest[]; before: string | null }>
  'conversations.update': Route<Mutation & { conversationId: Id; expectedRevision: number; patch: Partial<Pick<Conversation, 'title' | 'pinned' | 'archivedAt' | 'draft'>> }, Conversation>
  'conversations.clear': Route<Mutation & { conversationId: Id; expectedRevision: number }, Conversation>
  'conversations.delete': Route<Mutation & { conversationId: Id; expectedRevision: number }, { deleted: true }>
  'conversations.export': Route<{ conversationId: Id; format: 'markdown' | 'json' }, { saved: boolean }>
  'messages.feedback': Route<Mutation & { messageId: Id; feedback: 'up' | 'down' | null }, { saved: true }>
  'agent.start': Route<Mutation & { conversationId: Id; text: string; attachmentIds: Id[]; modelId: Id; reasoningEffort: ReasoningEffort | null; expectedInstanceId: Id }, AgentRun>
  'agent.stop': Route<Mutation & { runId: Id }, AgentRun>
  'agent.resume': Route<Mutation & { runId: Id; expectedRevision: number }, AgentRun>
  'approvals.decide': Route<Mutation & { approvalId: Id; expectedRevision: number; actionDigest: string; decision: 'allow' | 'deny' }, ApprovalRequest>
  'control.takeover': Route<Mutation & { conversationId: Id | null; expectedInstanceId: Id }, ControlView>
  'control.return': Route<Mutation & { resumeRun: boolean; expectedRevision: number }, ControlView>
  'control.input': Route<Mutation & { controlRevision: number; seq: number; displayEpoch: number; width: number; height: number; events: InputEvent[] }, { seq: number }>
  'control.release': Route<Mutation & { reason: 'user' | 'blur' | 'fullscreen_exit' | 'navigation' | 'replay' }, ControlView>
  'screenshots.get': Route<{ screenshotId: Id; variant: 'thumbnail' | 'full' }, { url: string; expiresAt: IsoTime }>
  'attachments.choose': Route<{ conversationId: Id; kind: 'file' | 'image' | 'current_screen' }, AttachmentRef[]>
  'agent.settings.get': Route<Record<string, never>, AgentSettings, undefined>
  'agent.settings.update': Route<Mutation & { settings: Pick<AgentSettings, 'providerId' | 'modelId' | 'reasoningEffort' | 'maxSteps'> }, AgentSettings, undefined>
  'permissions.update': Route<Mutation & { expectedRevision: number; permission: AgentSettings['permission'] }, AgentSettings, undefined>
  'credentials.configure': Route<{ providerId: Id; mode: 'native_entry' | 'import_file' | 'remove' }, { configured: boolean }, undefined>
  'window.command': Route<{ action: 'minimize' | 'maximize_toggle' | 'close' | 'back' | 'forward' | 'menu' }, { accepted: boolean }, undefined>
  // Proposed backend extensions. Must negotiate support before enabling controls.
  'vms.pause': Route<Mutation & { expectedInstanceId: Id }, { accepted: boolean }>
  'vms.resume': Route<Mutation & { expectedInstanceId: Id }, { accepted: boolean }>
}
export interface SubscribeOptions {
  subscriptionId: Id
  streamEpoch: Id
  afterSeq: number
}
export interface AgentSubscription {
  // Resolves once replay and following live delivery have been installed atomically.
  ready: Promise<Result<{ throughSeq: number }>>
  unsubscribe(): void
}
export interface DesktopAgentApiDraft {
  request<K extends keyof DesktopAgentRoutes>(
    method: K,
    vmId: DesktopAgentRoutes[K]['vmId'],
    payload: DesktopAgentRoutes[K]['payload']
  ): Promise<Result<DesktopAgentRoutes[K]['result']>>
  subscribeAgent(options: SubscribeOptions, listener: (event: AgentEvent) => void): AgentSubscription
}
// Design skeleton: use one app-level subscription, not one listener per message.
export async function mountAgentStore(
  api: DesktopAgentApiDraft,
  install: (snapshot: Snapshot) => void,
  apply: (event: AgentEvent) => void,
  recover: () => void
): Promise<() => void> {
  const result = await api.request('agent.bootstrap', undefined, {})
  if (!result.ok) throw new Error(result.error.message)
  install(result.value)
  const subscription = api.subscribeAgent({
    subscriptionId: crypto.randomUUID(),
    streamEpoch: result.value.streamEpoch,
    afterSeq: result.value.cursor
  }, apply)
  const ready = await subscription.ready
  if (!ready.ok) { subscription.unsubscribe(); recover() }
  return () => subscription.unsubscribe()
}
