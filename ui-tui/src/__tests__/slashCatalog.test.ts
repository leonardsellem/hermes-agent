import { beforeEach, describe, expect, it, vi } from 'vitest'

import { loadSlashCatalog } from '../app/slashCatalog.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { setRpcErrorLogSink } from '../app/userMessages.js'
import type { CommandsCatalogResponse } from '../gatewayTypes.js'
import type { SlashCatalog } from '../types.js'

const REPO_SKILL = '/repo-probe'
type CatalogRequest = Parameters<typeof loadSlashCatalog>[0]

const catalogFor = (sessionId: string | undefined): CommandsCatalogResponse => {
  const skills = sessionId === 'repo' ? [REPO_SKILL] : []

  return {
    canon: Object.fromEntries(skills.map(skill => [skill, skill])),
    categories: [],
    pairs: skills.map(skill => [skill, 'skill']),
    skill_count: skills.length,
    sub: {}
  }
}

describe('loadSlashCatalog', () => {
  let catalog: null | SlashCatalog

  const setCatalog = (next: null | SlashCatalog) => {
    catalog = next
  }

  beforeEach(() => {
    resetTurnState()
    resetUiState()
    catalog = null
  })

  it("binds the catalog to the live session, so each session's project skills replace the last one's", async () => {
    const requested: (string | undefined)[] = []

    const fake = async (method: string, params: { session_id?: string } = {}) => {
      expect(method).toBe('commands.catalog')
      requested.push(params.session_id)

      return catalogFor(params.session_id)
    }

    // SAFETY: the fake answers only commands.catalog, the one method loadSlashCatalog calls, with that method's result type.
    const rpc = fake as CatalogRequest

    await loadSlashCatalog(rpc, null, setCatalog)
    expect(catalog?.canon[REPO_SKILL]).toBeUndefined()

    patchUiState({ sid: 'repo' })
    await loadSlashCatalog(rpc, 'repo', setCatalog)
    expect(catalog?.canon[REPO_SKILL]).toBe(REPO_SKILL)
    expect(catalog?.skillCount).toBe(1)

    patchUiState({ sid: 'elsewhere' })
    await loadSlashCatalog(rpc, 'elsewhere', setCatalog)
    expect(catalog?.canon[REPO_SKILL]).toBeUndefined()

    expect(requested).toEqual([undefined, 'repo', 'elsewhere'])
  })

  it('drops a reply that arrives after the live session has changed', async () => {
    let release = () => {}

    const gate = new Promise<void>(resolve => {
      release = resolve
    })

    const fake = async (_method: string, params: { session_id?: string } = {}) => {
      await gate

      return catalogFor(params.session_id)
    }

    // SAFETY: the fake answers only commands.catalog, the one method loadSlashCatalog calls, with that method's result type.
    const rpc = fake as CatalogRequest

    patchUiState({ sid: 'repo' })
    const pending = loadSlashCatalog(rpc, 'repo', setCatalog)

    patchUiState({ sid: 'elsewhere' })
    release()
    await pending

    expect(catalog).toBeNull()
  })

  it('a background load that times out keeps the catalog and records one notice', async () => {
    const previous = { canon: {}, categories: [], pairs: [['/help', 'help']], skillCount: 0, sub: {} } satisfies SlashCatalog
    const setCatalog = vi.fn(next => { catalog = next })
    catalog = previous
    const sink = vi.fn()
    const request = vi.fn().mockRejectedValue(new Error('request timed out after 120s: commands.catalog'))
    setRpcErrorLogSink(sink)

    try {
      await loadSlashCatalog(request, null, setCatalog)

      expect(setCatalog).not.toHaveBeenCalled()
      expect(catalog).toBe(previous)
      expect(getTurnState().activity).toHaveLength(1)
      expect(getTurnState().activity[0]).toMatchObject({
        text: expect.stringContaining('command catalog unavailable: Hermes did not answer within 120s'), tone: 'info'
      })
      expect(sink).toHaveBeenCalledExactlyOnceWith('[rpc] request timed out after 120s: commands.catalog')
    } finally {
      setRpcErrorLogSink(null)
    }
  })

  it('a slow reply inside the deadline is applied with its warning', async () => {
    const pairs: [string, string][] = [['/repo-probe', 'skill']]
    const setCatalog = vi.fn()

    const request = vi.fn(async () => {
      await new Promise(resolve => setTimeout(resolve, 50))

      return { pairs, warning: 'plugin discovery unavailable: x' }
    })

    // SAFETY: this fake answers only commands.catalog with its response shape.
    const pending = loadSlashCatalog(request as CatalogRequest, null, setCatalog)
    expect(setCatalog).not.toHaveBeenCalled()
    await pending

    expect(setCatalog).toHaveBeenCalledExactlyOnceWith({ canon: {}, categories: [], pairs, skillCount: 0, sub: {} })
    expect(getTurnState().activity).toMatchObject([{ text: 'plugin discovery unavailable: x', tone: 'warn' }])

    // Retained debt: objects without pairs and non-object replies are silently dropped, not validated.
    for (const malformed of [{ warning: 'ignored' }, null, 'not an object', 0]) {
      await loadSlashCatalog(vi.fn().mockResolvedValue(malformed), null, setCatalog)
    }

    expect(setCatalog).toHaveBeenCalledTimes(1)
    expect(getTurnState().activity).toHaveLength(1)
  })
})
