import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setSlashCatalog } from '../app/slashCatalog.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { useMainApp } from '../app/useMainApp.js'

let unmount = () => {}

beforeEach(() => {
  resetTurnState()
  resetUiState()
  setSlashCatalog(null)
})

afterEach(() => {
  unmount()
  vi.useRealTimers()
})

it('session catalog load uses a stable rejecting request across rerenders', async () => {
  vi.useFakeTimers()

  const gw = Object.assign(new EventEmitter(), {
    attached: true, isCanonical: true, drain: () => {}, getLogTail: () => '', kill: () => {}, recordLog: () => {},
    request: vi.fn((method: string) => method === 'commands.catalog'
      ? Promise.reject(new Error('request timed out after 120s: commands.catalog'))
      : new Promise(() => {}))
  })

  let app!: ReturnType<typeof useMainApp>
  const renderedStatuses: string[] = []

  function Harness() {
    app = useMainApp(gw as never)
    renderedStatuses.push(getUiState().status)

    return null
  }

  const stream = () => Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 })
  const stdin = Object.assign(new PassThrough(), { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })

  const instance = renderSync(React.createElement(Harness), {
    patchConsole: false, stderr: stream() as never, stdin: stdin as never, stdout: stream() as never
  })

  unmount = () => { instance.unmount(); instance.cleanup() }

  patchUiState({ sid: 'owner', gatewayConnected: true })
  await vi.advanceTimersByTimeAsync(0)

  for (const status of ['unrelated one', 'unrelated two', 'unrelated three']) {
    patchUiState({ status })
    await vi.advanceTimersByTimeAsync(0)
    expect(renderedStatuses).toContain(status)
  }

  expect(gw.request.mock.calls.filter(([method]) => method === 'commands.catalog')).toHaveLength(1)
  expect(app.appTranscript.historyItems.filter(item => item.text.includes('did not answer'))).toEqual([])
  expect(getTurnState().activity.filter(item => item.text.startsWith('command catalog unavailable:'))).toHaveLength(1)
})
