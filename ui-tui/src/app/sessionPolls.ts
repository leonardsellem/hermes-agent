// The TUI's 1.5 s background polls (subagents, processes, live sessions), each with at most one
// request in flight per connection: a tick whose previous answer has not arrived is dropped, never
// stacked, so a gateway slower than the tick cannot accumulate reads ahead of an operator request.
import type { GatewayClient } from '../gatewayClient.js'
import type { SessionActiveItem, SessionActiveListResponse, SubagentListResponse } from '../gatewayTypes.js'
import { asRpcResult } from '../lib/rpc.js'

import { requestLiveSessions } from './liveSessions.js'
import type { ProcessEntry } from './processRoster.js'

export interface SessionPollSink {
  applyAgent: (sid: string, result: SubagentListResponse) => void
  applyLiveSessions: (sessions: SessionActiveItem[], currentSid: null | string) => void
  applyProcess: (sid: string, processes: ProcessEntry[]) => void
}

export const createSessionPolls = (
  gw: Pick<GatewayClient, 'isCanonical' | 'request'>,
  currentSid: () => null | string,
  sink: SessionPollSink
) => {
  const inFlight = new Set<string>()
  let stopped = false

  const poll = <T>(key: string, send: () => Promise<unknown>, apply: (result: T) => void) => {
    if (stopped || inFlight.has(key)) {
      return
    }

    inFlight.add(key)
    send()
      .then(raw => {
        const result = asRpcResult(raw)

        if (!stopped && result) {
          apply(result as T)
        }
      })
      .catch(() => {})
      .finally(() => inFlight.delete(key))
  }

  const refresh = () => {
    const sid = currentSid()

    if (!sid) {
      return
    }

    poll<SubagentListResponse>('subagent.list', () => gw.request('subagent.list', { session_id: sid }), result => {
      if (currentSid() === sid) {
        sink.applyAgent(sid, result)
      }
    })
    poll<{ processes: ProcessEntry[] }>('process.list', () => gw.request('process.list', { session_id: sid }), result => {
      if (currentSid() === sid) {
        sink.applyProcess(sid, result.processes ?? [])
      }
    })
    const liveMethod = gw.isCanonical ? 'session.list' : 'session.active_list'

    poll<SessionActiveListResponse>(liveMethod, () => requestLiveSessions(gw, sid), result => {
      if (result.sessions) {
        sink.applyLiveSessions(result.sessions, currentSid())
      }
    })
  }

  return {
    refresh,
    stop: () => {
      stopped = true
    }
  }
}
