interface ConversationUiState {
  scrollTop: number
  following: boolean
  expanded: Map<string, boolean>
}
// Window-local preferences only. Transcript and task state remain owned by main.
const states = new Map<string, ConversationUiState>()
export function conversationUiState(scope: string): ConversationUiState {
  let state = states.get(scope)
  if (!state) {
    state = { scrollTop: 0, following: true, expanded: new Map() }
    states.set(scope, state)
    if (states.size > 100) states.delete(states.keys().next().value!)
  }
  return state
}
