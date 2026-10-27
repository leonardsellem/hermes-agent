import { randomUUID } from 'node:crypto'
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { captureDestination } from '../app/submissionDestination.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useQueue } from '../hooks/useQueue.js'
import { loadPendingInputs, savePendingInput } from '../lib/pendingInputs.js'

// A retained local record keeps its text and its place without holding the messages behind it: the
// drain sends the first item that is genuinely sendable and leaves a dead attempt where it was.

let home: string

beforeEach(() => {
  home = realpathSync(mkdtempSync(join(tmpdir(), 'ink-queue-head-')))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  patchUiState({
    sid: 'owner',
    info: { model: 'test', profile_name: 'default', skills: {}, stored_session_id: 'S', tools: {} } as never,
    busy: false,
    status: 'ready'
  })
})

afterEach(() => {
  resetUiState()
  vi.unstubAllEnvs()
  rmSync(home, { recursive: true, force: true })
})

const item = (text: string, { createdAt: offset = 0, ...patch }: Record<string, unknown> = {}) => ({
  createdAt: Date.parse('2026-10-27T10:00:00Z') + Number(offset),
  destination: captureDestination(),
  display: text,
  submissionId: randomUUID(),
  text,
  ...patch
})

function mount() {
  let queue!: ReturnType<typeof useQueue>

  function Harness() {
    queue = useQueue()

    return React.createElement(Text, null, String(queue.queuedDisplay.length))
  }

  const instance = renderSync(React.createElement(Harness), {
    stdin: new PassThrough() as never,
    stdout: Object.assign(new PassThrough(), { columns: 80, isRow: false, isTTY: false, rows: 20 }) as never,
    stderr: new PassThrough() as never,
    patchConsole: false
  })

  return { get queue() { return queue }, cleanup() { instance.unmount() } }
}

it('failed-ambiguous-head-does-not-block-later-items', () => {
  savePendingInput(item('ambiguous head', { controlMethod: 'session.steer', executionGeneration: 1, inFlight: true, legacyAttempted: true, queued: true }) as never)
  savePendingInput(item('first later', { createdAt: 1 }) as never)
  savePendingInput(item('second later', { createdAt: 2 }) as never)

  const h = mount()

  try {
    const sent = [h.queue.dequeue()?.text, h.queue.dequeue()?.text]

    expect(sent).toEqual(['first later', 'second later'])
    // Its place and its text survive both sends.
    expect(h.queue.queueRef.current.map(entry => entry.text)).toContain('ambiguous head')
  } finally { h.cleanup() }
})

it('ambiguous-head-preserved-and-not-sent', () => {
  savePendingInput(item('ambiguous head', { controlMethod: 'session.steer', executionGeneration: 1, inFlight: true, legacyAttempted: true, queued: true }) as never)
  savePendingInput(item('first later', { createdAt: 1 }) as never)

  const h = mount()

  try {
    expect(h.queue.dequeue()?.text).toBe('first later')
    expect(h.queue.dequeue()).toBeUndefined()

    const head = h.queue.queueRef.current[0]!
    expect(head).toMatchObject({ failed: true, text: 'ambiguous head' })
    expect(loadPendingInputs(captureDestination()).map(record => record.text)).toEqual(['ambiguous head', 'first later'])
  } finally { h.cleanup() }
})

it('plain-failed-item-is-still-retryable-under-its-own-identity', () => {
  const failed = item('plain failed', { createdAt: -1 })
  savePendingInput({ ...failed, inFlight: true } as never)
  savePendingInput(item('later', { createdAt: 1 }) as never)

  const h = mount()

  try {
    // Not auto-sent, but it does not hold the row behind it either.
    expect(h.queue.dequeue()?.text).toBe('later')

    const retried = h.queue.dequeue(true)!
    expect(retried.text).toBe('plain failed')
    expect(retried.submissionId).toBe(failed.submissionId)
    expect(retried.controlMethod).toBeUndefined()
  } finally { h.cleanup() }
})

it('retry-readmits-new-identity', () => {
  const head = item('ambiguous head', { controlMethod: 'session.steer', executionGeneration: 1, inFlight: true, legacyAttempted: true, queued: true })
  savePendingInput(head as never)

  const h = mount()

  try {
    const retried = h.queue.dequeue(true)!

    expect(retried.text).toBe('ambiguous head')
    expect(retried.submissionId).not.toBe(head.submissionId)
    expect(retried.controlMethod).toBeUndefined()
    expect(retried.executionGeneration).toBeUndefined()
    expect(retried.legacyAttempted).toBeFalsy()
    // One record, under the new identity: the dead one is gone, nothing was sent twice.
    const records = loadPendingInputs(captureDestination())
    expect(records).toHaveLength(1)
    expect(records[0]).toMatchObject({ text: 'ambiguous head' })
    expect(records[0]!.submissionId).toBe(retried.submissionId)
  } finally { h.cleanup() }
})
