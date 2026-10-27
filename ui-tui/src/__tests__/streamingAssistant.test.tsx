import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import { stripAnsi } from '@hermes/shared/ansi'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { loadSlashCatalog } from '../app/slashCatalog.js'
import { getTurnState, patchTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { StreamingAssistant } from '../components/streamingAssistant.js'
import type { SectionVisibility } from '../types.js'

const mounted: Array<() => void> = []

beforeEach(() => {
  resetTurnState()
  resetUiState()
})
afterEach(() => {
  for (const unmount of mounted.splice(0)) { unmount() }
})

const mount = (sections?: SectionVisibility) => {
  const stream = () => Object.assign(new PassThrough(), { columns: 240, isTTY: false, rows: 30 })
  const stdout = stream()
  let output = ''
  stdout.on('data', chunk => { output += stripAnsi(chunk.toString()) })

  const instance = renderSync(
    <StreamingAssistant cols={240} detailsMode="collapsed" detailsModeCommandOverride={false}
      progress={{ showProgressArea: true }} sections={sections} />,
    { patchConsole: false, stdout: stdout as never, stdin: stream() as never, stderr: stream() as never }
  )

  const unmount = () => { instance.unmount(); instance.cleanup() }

  mounted.push(unmount)

  return { output: () => output, unmount }
}

const catalogTimeout = () => loadSlashCatalog(
  vi.fn().mockRejectedValue(new Error('request timed out after 30s: commands.catalog')), null, vi.fn()
)

it('an idle or live catalog timeout visibly renders one deduplicated activity notice', async () => {
  for (const busy of [false, true]) {
    resetTurnState()
    patchUiState({ busy })

    if (busy) { patchTurnState({ streaming: 'Live reply is still arriving.' }) }

    await catalogTimeout()
    await catalogTimeout()
    await catalogTimeout()

    const view = mount({ activity: 'expanded' })

    await vi.waitFor(() => expect(view.output()).toContain('command catalog unavailable: Hermes did not answer within 30s'))
    expect(view.output().match(/command catalog unavailable:/g)).toHaveLength(1)
    expect(getTurnState().activity).toHaveLength(1)

    if (busy) { expect(view.output()).toContain('Live reply is still arriving.') }

    view.unmount()
    mounted.pop()
  }
})

it('catalog activity respects hidden collapsed and expanded section preferences', async () => {
  await catalogTimeout()

  for (const mode of [undefined, 'hidden', 'collapsed', 'expanded'] as const) {
    const view = mount(mode ? { activity: mode } : undefined)
    await new Promise(resolve => setTimeout(resolve, 60))

    expect(view.output().includes('command catalog unavailable:')).toBe(mode === 'expanded')

    if (mode === 'collapsed') { expect(view.output()).toContain('Activity') }

    view.unmount()
    mounted.pop()
  }
})
