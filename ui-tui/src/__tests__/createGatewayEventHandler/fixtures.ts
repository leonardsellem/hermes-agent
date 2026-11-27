import { vi } from 'vitest'

import { createServerRequestHandler } from '../../app/createServerRequestHandler.js'
import { patchUiState } from '../../app/uiStore.js'
import type { Msg } from '../../types.js'

export const ref = <T>(current: T) => ({ current })

export const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn(async () => null) },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      sys: vi.fn()
    },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: (title: string, sections: any[]) =>
        appended.push({ kind: 'panel', panelData: { sections, title }, role: 'system', text: '' }),
      setHistoryItems: vi.fn()
    },
    voice: {
      setProcessing: vi.fn(),
      setRecording: vi.fn(),
      setVoiceEnabled: vi.fn()
    }
  }) as any

/** Deliver one server→client request (`tui_gateway/server_requests.py`) to the TUI's request handler. */
export const serverRequest = (method: string, params: Record<string, unknown>, id = `srq-${method}`) => {
  const respond = vi.fn()

  const handled = createServerRequestHandler({
    ringPromptBell: vi.fn(),
    setStatus: status => patchUiState({ status })
  })({
    fail: vi.fn(),
    id,
    method,
    params,
    respond
  })

  return { handled, respond }
}
