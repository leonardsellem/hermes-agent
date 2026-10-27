import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect, useMemo } from 'react'

import type { CommandsCatalogResponse } from '../gatewayTypes.js'
import { t } from '../i18n/runtime.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { SlashCatalog } from '../types.js'

import { turnController } from './turnController.js'
import { $uiState } from './uiStore.js'

type CatalogRequest = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

/** A revision, not just a sid: switching away and back must still fence old replies. */
export const $slashCatalogOwner = atom(0)
export const $slashCatalog = atom<null | SlashCatalog>(null)

$uiState.listen((state, previous) => {
  if (state.sid !== previous.sid || state.storedSid !== previous.storedSid ||
      state.info?.cwd !== previous.info?.cwd || state.info?.profile_name !== previous.info?.profile_name ||
      state.gatewayConnected !== previous.gatewayConnected) {
    $slashCatalog.set(null)
    $slashCatalogOwner.set($slashCatalogOwner.get() + 1)
  }
})

/** `commands.catalog` bound to `sid` when there is one: project-local skills follow the session's repo,
 *  so the catalog is reloaded whenever the live session changes. A reply for a session that is no longer
 *  the live one is dropped. */
export const loadSlashCatalog = (rpc: CatalogRequest, sid: null | string, setCatalog: (catalog: null | SlashCatalog) => void) => {
  const owner = $slashCatalogOwner.get()

  return rpc<CommandsCatalogResponse>('commands.catalog', sid ? { session_id: sid } : {})
    .then(r => {
      if (!r?.pairs || $uiState.get().sid !== sid || $slashCatalogOwner.get() !== owner) {
        return
      }

      setCatalog({
        canon: r.canon ?? {},
        categories: r.categories ?? [],
        pairs: r.pairs,
        skillCount: r.skill_count ?? 0,
        sub: r.sub ?? {}
      })

      if (r.warning) {
        turnController.pushActivity(r.warning, 'warn')
      }
    })
    .catch(e => turnController.pushActivity(t('gatewayMsg.startup.commandCatalogUnavailable', rpcErrorMessage(e)), 'info'))
}

/** `skills.changed`: a skill was installed or removed from another terminal. Refetch the palette
 *  from the gateway, which rescans; this client never reads the skills dir itself. True when handled. */
export function refreshSlashCatalogOnSkillsChange(
  ev: { type: string },
  rpc: CatalogRequest,
  setCatalog: (catalog: null | SlashCatalog) => void
): boolean {
  if (ev.type !== 'skills.changed') {
    return false
  }

  void loadSlashCatalog(rpc, $uiState.get().sid, setCatalog)

  return true
}

export const setSlashCatalog = (next: null | SlashCatalog) => $slashCatalog.set(next)

/** The slash catalog, reloaded for each live session while connected; the gateway-ready load covers the
 *  pre-session one. */
export function useSlashCatalog(rpc: CatalogRequest) {
  const catalog = useStore($slashCatalog)
  const owner = useStore($slashCatalogOwner)
  const { gatewayConnected, sid } = useStore($uiState)

  const setCatalog = useMemo(() => (next: null | SlashCatalog) => {
    if ($slashCatalogOwner.get() === owner) { setSlashCatalog(next) }
  }, [owner])

  useEffect(() => {
    if (sid && gatewayConnected !== false) {
      void loadSlashCatalog(rpc, sid, setCatalog)
    }
  }, [rpc, sid, gatewayConnected, setCatalog])

  return { catalog, setCatalog }
}
