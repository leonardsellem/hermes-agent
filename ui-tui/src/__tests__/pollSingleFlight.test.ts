import { describe, expect, it, vi } from 'vitest'

import { createSessionPolls } from '../app/sessionPolls.js'

// U1 (LS-3828): each periodic read keeps at most one request in flight per connection, so a
// gateway slower than the 1.5 s tick cannot accumulate background polls ahead of an operator
// request. A tick whose poll is unanswered is dropped, never stacked; answered polls refresh.

const deferred = () => {
  let resolve!: (value: unknown) => void

  const promise = new Promise(res => {
    resolve = res
  })

  return { promise, resolve }
}

const rig = ({ isCanonical = true, sid = 'A' as null | string } = {}) => {
  const calls: string[] = []
  const pending: ReturnType<typeof deferred>[] = []

  const gw = {
    isCanonical,
    request: vi.fn((method: string) => {
      calls.push(method)
      const d = deferred()

      pending.push(d)

      return d.promise
    })
  }

  let currentSid: null | string = sid
  const sink = { applyAgent: vi.fn(), applyLiveSessions: vi.fn(), applyProcess: vi.fn() }

  const polls = createSessionPolls(gw as never, () => currentSid, sink)

  return {
    calls,
    gw,
    pending,
    polls,
    setSid: (next: null | string) => {
      currentSid = next
    },
    sink
  }
}

const flush = () =>
  new Promise(resolve => {
    setTimeout(resolve, 0)
  })

describe('session poll single-flight', () => {
  it('unanswered-poll-is-not-resent', () => {
    const { calls, polls } = rig()

    polls.refresh()
    polls.refresh()
    polls.refresh()

    expect(calls.filter(m => m === 'subagent.list')).toHaveLength(1)
    expect(calls.filter(m => m === 'process.list')).toHaveLength(1)
    expect(calls.filter(m => m === 'session.list')).toHaveLength(1)
  })

  it('resolved-poll-refreshes-on-next-tick', async () => {
    const { calls, pending, polls } = rig()

    polls.refresh()
    expect(calls.filter(m => m === 'subagent.list')).toHaveLength(1)
    pending.forEach(d => d.resolve({}))
    await flush()
    polls.refresh()
    expect(calls.filter(m => m === 'subagent.list')).toHaveLength(2)
    expect(calls.filter(m => m === 'process.list')).toHaveLength(2)
  })

  it('stale-reply-for-another-session-is-still-dropped', async () => {
    const { pending, polls, setSid, sink } = rig()

    polls.refresh()
    setSid('B')
    pending.forEach(d => d.resolve({ delegations: [], processes: [], sessions: [], subagents: [] }))
    await flush()
    expect(sink.applyAgent).not.toHaveBeenCalled()
    expect(sink.applyProcess).not.toHaveBeenCalled()
  })

  it('non-canonical-active-list-is-not-resent', () => {
    const { calls, polls } = rig({ isCanonical: false })

    polls.refresh()
    polls.refresh()
    polls.refresh()
    expect(calls.filter(m => m === 'session.active_list')).toHaveLength(1)
    expect(calls.filter(m => m === 'session.list')).toHaveLength(0)
  })
})
