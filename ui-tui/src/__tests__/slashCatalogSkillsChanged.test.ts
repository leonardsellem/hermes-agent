import { beforeEach, describe, expect, it, vi } from 'vitest'

import { refreshSlashCatalogOnSkillsChange } from '../app/slashCatalog.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

// CFG-8: a skill installed from another terminal reaches Ink's palette through the gateway.
describe('refreshSlashCatalogOnSkillsChange', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
  })

  it('refetches commands.catalog from the gateway for the live session', async () => {
    const rpc = vi.fn(async () => ({ canon: { '/new-skill': '/new-skill' }, pairs: [['/new-skill', 'd']], skill_count: 1 }))
    const setCatalog = vi.fn()

    patchUiState({ sid: 'sid-cfg8' })

    // SAFETY: the fake answers only commands.catalog, the one method the refetch calls, with that method's result type.
    expect(refreshSlashCatalogOnSkillsChange({ type: 'skills.changed' }, rpc as never, setCatalog)).toBe(true)
    await vi.waitFor(() => expect(setCatalog).toHaveBeenCalled())

    expect(rpc).toHaveBeenCalledWith('commands.catalog', { session_id: 'sid-cfg8' })
    expect(setCatalog.mock.calls.at(-1)?.[0].canon).toEqual({ '/new-skill': '/new-skill' })
  })

  it('leaves every other event to the main handler', () => {
    const rpc = vi.fn()

    // SAFETY: the fake is never called here; the event is not skills.changed, so no RPC goes out.
    expect(refreshSlashCatalogOnSkillsChange({ type: 'skin.changed' }, rpc as never, vi.fn())).toBe(false)
    expect(rpc).not.toHaveBeenCalled()
  })

  it('three skills.changed events against a failing catalog leave one notice', async () => {
    const request = vi.fn().mockRejectedValue(new Error('request timed out after 120s: commands.catalog'))
    const setCatalog = vi.fn()

    for (let i = 0; i < 3; i++) {
      expect(refreshSlashCatalogOnSkillsChange({ type: 'skills.changed' }, request, setCatalog)).toBe(true)
      await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(i + 1))
    }

    await vi.waitFor(() => expect(getTurnState().activity).toHaveLength(1))
    expect(getTurnState().activity[0]?.text).toContain('command catalog unavailable: Hermes did not answer within 120s')
    expect(setCatalog).not.toHaveBeenCalled()
  })
})
