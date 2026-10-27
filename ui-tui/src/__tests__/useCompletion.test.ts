import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { loadSlashCatalog, setSlashCatalog, useSlashCatalog } from '../app/slashCatalog.js'
import { resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import type { CommandsCatalogResponse, CompletionResponse } from '../gatewayTypes.js'
import { completionRequestForInput, useCompletion } from '../hooks/useCompletion.js'

describe('completionRequestForInput', () => {
  it('routes real slash commands to slash completion', () => {
    expect(completionRequestForInput('/help')).toMatchObject({
      method: 'complete.slash',
      params: { text: '/help' },
      replaceFrom: 1
    })
  })

  it('does not route absolute paths through slash completion', () => {
    expect(
      completionRequestForInput('/home/d/Desktop/agenda/CrimsonRed/.hermes/plans/2026-05-04-HANDOFF-NEXT.md')
    ).toMatchObject({
      method: 'complete.path',
      params: { word: '/home/d/Desktop/agenda/CrimsonRed/.hermes/plans/2026-05-04-HANDOFF-NEXT.md' },
      replaceFrom: 0
    })
  })

  it('keeps path completion for trailing absolute path tokens', () => {
    expect(completionRequestForInput('read /home/d/Desktop/file.md')).toMatchObject({
      method: 'complete.path',
      params: { word: '/home/d/Desktop/file.md' },
      replaceFrom: 5
    })
  })

  it('leaves plain text alone', () => {
    expect(completionRequestForInput('hello there')).toBeNull()
  })

  // The shared gateway's own cwd is its daemon folder: the listing must name the session's.
  it('lists paths in the session folder, else the launch folder', () => {
    try {
      vi.stubEnv('HERMES_CWD', '/launch/dir')
      patchUiState({ info: { cwd: '/work/repo' } as never })
      expect(completionRequestForInput('read @file:sr')).toMatchObject({
        method: 'complete.path',
        params: { word: '@file:sr', cwd: '/work/repo' }
      })
      resetUiState()
      expect(completionRequestForInput('./sr')).toMatchObject({ params: { word: './sr', cwd: '/launch/dir' } })
    } finally {
      resetUiState()
      vi.unstubAllEnvs()
    }
  })

  it('names the asking session, whose folder the shared gateway lists', () => {
    try {
      patchUiState({ sid: 'sess-1' })
      expect(completionRequestForInput('read @file:sr')).toMatchObject({
        method: 'complete.path',
        params: { word: '@file:sr', session_id: 'sess-1' }
      })
      resetUiState()
      expect(completionRequestForInput('./sr')?.params).not.toHaveProperty('session_id')
    } finally {
      resetUiState()
      vi.unstubAllEnvs()
    }
  })
})

describe('useCompletion cached slash menu', () => {
  const mounted: Array<() => void> = []

  const catalog: CommandsCatalogResponse = {
    canon: { '/help': '/help' }, categories: [], skill_count: 1, sub: {},
    pairs: [['/help', 'Show available commands'], ['/private-skill', 'Private project skill']]
  }

  beforeEach(() => {
    vi.useFakeTimers()
    resetTurnState()
    resetUiState()
    setSlashCatalog(null)
    patchUiState({ sid: 'repo', storedSid: 'stored-repo', gatewayConnected: true,
      info: { cwd: '/private/repo', profile_name: 'owner' } as never })
  })
  afterEach(() => {
    for (const unmount of mounted.splice(0)) { unmount() }
    vi.useRealTimers()
  })

  const seed = () => loadSlashCatalog(vi.fn().mockResolvedValue(catalog), 'repo', setSlashCatalog)

  const mount = (input: string) => {
    const replies: Array<{ resolve: (value: CompletionResponse) => void; reject: (error: Error) => void }> = []

    const gw = { request: vi.fn((_method: string, _params: unknown) => new Promise<CompletionResponse>((resolve, reject) => {
      replies.push({ resolve, reject })
    })) }

    let value!: ReturnType<typeof useCompletion>
    let setter!: ReturnType<typeof useSlashCatalog>['setCatalog']
    const requestCatalog = vi.fn(() => new Promise<never>(() => {}))

    function Harness({ input, blocked = false }: { input: string; blocked?: boolean }) {
      setter = useSlashCatalog(requestCatalog).setCatalog
      value = useCompletion(input, blocked, gw as never)

      return null
    }

    const stream = () => Object.assign(new PassThrough(), { columns: 120, isTTY: false, rows: 30 })

    const instance = renderSync(React.createElement(Harness, { input }), {
      patchConsole: false, stdout: stream() as never, stdin: stream() as never, stderr: stream() as never
    })

    mounted.push(() => { instance.unmount(); instance.cleanup() })

    return { gw, replies, value: () => value, setter: () => setter,
      input: (input: string, blocked = false) => instance.rerender(React.createElement(Harness, { input, blocked })) }
  }

  it('offers cached slash names while pending and failed without losing authoritative remote refresh', async () => {
    await seed()
    const view = mount('/h')
    await vi.advanceTimersByTimeAsync(0)

    expect(view.value().completions).toEqual([{ text: '/help', display: '/help', meta: 'Show available commands' }])
    expect(view.value().compReplace).toBe(1)
    await vi.advanceTimersByTimeAsync(60)
    expect(view.gw.request).toHaveBeenCalledExactlyOnceWith('complete.slash', { text: '/h', session_id: 'repo' })
    view.replies[0]!.resolve({ items: [{ text: 'help', display: '/help refreshed', meta: 'Fresh remote description' }], replace_from: 1 })
    await vi.waitFor(() => expect(view.value().completions[0]?.meta).toBe('Fresh remote description'))

    view.input('/help')
    await vi.advanceTimersByTimeAsync(60)
    view.replies[1]!.reject(new Error('request timed out after 30s: complete.slash'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(view.gw.request).toHaveBeenCalledTimes(2)
    expect(view.value().completions[0]?.text).toBe('/help')

    view.input('/h')
    await vi.advanceTimersByTimeAsync(60)
    view.replies[2]!.resolve({ items: [], replace_from: 1 })
    await vi.waitFor(() => expect(view.value().completions).toEqual([]))
  })

  it('preserves path argument inline-skill and changed-input completion boundaries', async () => {
    await seed()
    const view = mount('clean /pr')
    await vi.advanceTimersByTimeAsync(60)
    expect(view.value().completions).toEqual([])
    expect(view.gw.request).toHaveBeenLastCalledWith('complete.slash', { text: '/pr', session_id: 'repo' })
    view.replies[0]!.resolve({ items: [
      { text: 'help', display: '/help', kind: 'command' },
      { text: 'private-skill', display: '/private-skill', kind: 'skill' }
    ], replace_from: 1 })
    await vi.waitFor(() => expect(view.value().completions.map(item => item.text)).toEqual(['private-skill']))
    expect(view.value().compReplace).toBe(7)

    view.input('/cron ad')
    await vi.advanceTimersByTimeAsync(60)
    expect(view.value().completions).toEqual([])
    view.replies[1]!.resolve({ items: [{ text: 'add', display: 'add' }], replace_from: 6 })
    await vi.waitFor(() => expect(view.value().completions[0]?.text).toBe('add'))
    expect(view.value().compReplace).toBe(6)

    view.input('/home/work/')
    await vi.advanceTimersByTimeAsync(60)
    expect(view.gw.request).toHaveBeenLastCalledWith('complete.path', {
      word: '/home/work/', cwd: '/private/repo', session_id: 'repo'
    })
    view.input('ordinary text')
    await vi.advanceTimersByTimeAsync(0)
    view.replies[2]!.resolve({ items: [{ text: '/home/work/late', display: 'late' }] })
    await vi.advanceTimersByTimeAsync(0)
    expect(view.value().completions).toEqual([])

    view.input('/help', true)
    await vi.advanceTimersByTimeAsync(100)
    expect(view.value().completions).toEqual([])
    expect(view.gw.request).toHaveBeenCalledTimes(3)
  })

  it('never reuses cached or delayed project skills across session cwd profile or reconnect ownership', async () => {
    await seed()
    const view = mount('/')

    for (const change of ['session', 'cwd', 'profile', 'reconnect'] as const) {
      patchUiState({ sid: 'repo', storedSid: 'stored-repo', gatewayConnected: true,
        info: { cwd: '/private/repo', profile_name: 'owner' } as never })
      await seed()
      await vi.advanceTimersByTimeAsync(60)
      expect(view.value().completions.some(item => item.text === '/private-skill')).toBe(true)
      const oldReply = view.replies.at(-1)!
      const oldSetter = view.setter()
      let release!: (value: CommandsCatalogResponse) => void

      const lateCatalog = loadSlashCatalog(vi.fn(() => new Promise<CommandsCatalogResponse>(resolve => { release = resolve })) as never,
        'repo', setSlashCatalog)

      if (change === 'session') {
        patchUiState({ sid: 'other', storedSid: 'stored-other' })
        patchUiState({ sid: 'repo', storedSid: 'stored-repo' })
      } else if (change === 'cwd') {
        patchUiState({ info: { cwd: '/other/repo', profile_name: 'owner' } as never })
      } else if (change === 'profile') {
        patchUiState({ info: { cwd: '/private/repo', profile_name: 'other-owner' } as never })
      } else {
        patchUiState({ gatewayConnected: false })
        patchUiState({ gatewayConnected: true })
      }

      await vi.advanceTimersByTimeAsync(0)
      expect(view.value().completions.some(item => item.text.includes('private-skill'))).toBe(false)
      oldReply.resolve({ items: [{ text: 'private-skill', display: '/private-skill', kind: 'skill' }] })
      release(catalog)
      await lateCatalog
      oldSetter({ canon: {}, categories: [], pairs: [['/private-skill', 'Private project skill']], skillCount: 1, sub: {} })
      await vi.advanceTimersByTimeAsync(0)
      expect(view.value().completions.some(item => item.text.includes('private-skill'))).toBe(false)
    }
  })
})
