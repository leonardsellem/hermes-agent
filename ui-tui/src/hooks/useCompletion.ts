import { looksLikeSlashCommand } from '@hermes/shared/slash'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import type { CompletionItem } from '../app/interfaces.js'
import { rankSlashItems } from '../app/slash/fuzzyScore.js'
import { $slashCatalog, $slashCatalogOwner } from '../app/slashCatalog.js'
import { getUiState, sessionCwd } from '../app/uiStore.js'
import { inlineSlashTrigger } from '../domain/slash.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { CompletionResponse } from '../gatewayTypes.js'
import { t } from '../i18n/runtime.js'
import { asRpcResult } from '../lib/rpc.js'
import { listWidgetApps } from '../sdk/registry.js'

/** Client-side widget apps live in the TUI's registry, not the gateway — so
 *  `/` completions merge their title/metadata here. Registry-driven: a new
 *  app surfaces automatically, no hardcoded lists on either side. Matching is
 *  description-aware (ported from grok-cli's slash menu): `/timer` surfaces a
 *  widget whose help text mentions timers, not just id-prefix hits. */
export function mergeWidgetAppItems(input: string, items: CompletionItem[]): CompletionItem[] {
  // Only complete the command NAME position (no args typed yet).
  if (input.includes(' ')) {
    return items
  }

  const local = rankSlashItems(listWidgetApps(), input, app => ({ description: app.help, id: app.id }))
    .filter(app => !items.some(item => item.text === `/${app.id}`))
    .map(app => ({ display: `/${app.id}`, meta: app.help, text: `/${app.id}` }))

  return [...items, ...local]
}

const TAB_PATH_RE = /((?:["']?(?:[A-Za-z]:[\\/]|\.{1,2}\/|~\/|\/|@|[^"'`\s]+\/))[^\s]*)$/

export function completionRequestForInput(
  input: string
):
  | { method: 'complete.path'; params: { word: string; cwd?: string; session_id?: string }; replaceFrom: number }
  | { method: 'complete.slash'; params: { text: string }; replaceFrom: number; skillsOnly?: boolean }
  | null {
  const isSlashCommand = looksLikeSlashCommand(input)
  const pathWord = isSlashCommand ? null : (input.match(TAB_PATH_RE)?.[1] ?? null)

  // `/model` uses the two-step ModelPicker (real curated IDs).
  // Slash completion here only showed short aliases + vendor/family meta.
  if (isSlashCommand && /^\/model(?:\s|$)/.test(input)) {
    return null
  }

  // A `/token` mid-message is a skill reference dropped into prose. Detected
  // BEFORE the leading-command shape because only the first slash can be an
  // invocation — `/help /cle` is a command whose argument names a skill, and
  // routing the whole line to the backend's completer offered nothing at all.
  // It only matches a whitespace-preceded slash sitting at the caret, so
  // ordinary argument completion (`/cron ad`, `/personality alic`) is
  // untouched.
  const inline = inlineSlashTrigger(input)

  if (inline) {
    return {
      method: 'complete.slash',
      params: { text: `/${inline.query}` },
      replaceFrom: inline.start + 1,
      skillsOnly: true
    }
  }

  if (isSlashCommand) {
    return { method: 'complete.slash', params: { text: input }, replaceFrom: 1 }
  }

  if (!pathWord) {
    return null
  }

  const cwd = sessionCwd()
  // The shared gateway lists the session's own folder, so it must know which session is asking.
  const sid = getUiState().sid

  return {
    method: 'complete.path',
    params: { word: pathWord, ...(cwd ? { cwd } : {}), ...(sid ? { session_id: sid } : {}) },
    replaceFrom: input.length - pathWord.length
  }
}

export function useCompletion(input: string, blocked: boolean, gw: GatewayClient) {
  const catalog = useStore($slashCatalog)
  const owner = useStore($slashCatalogOwner)

  const [reply, setReply] = useState<{
    input: string; owner: number; items: CompletionItem[]; replaceFrom: number; failed?: boolean
  } | null>(null)

  const [compIdx, setCompIdx] = useState(0)

  const cached = useMemo(() => {
    const request = completionRequestForInput(input)

    if (!catalog || request?.method !== 'complete.slash' || request.skillsOnly || request.params.text.includes(' ')) {
      return []
    }

    return mergeWidgetAppItems(input, rankSlashItems(catalog.pairs, input, ([name, description]) => ({
      id: name.replace(/^\//, ''), description
    })).map(([name, description]) => ({ text: name, display: name, meta: description })))
  }, [catalog, input])

  useEffect(() => {
    setCompIdx(0)

    if (blocked || getUiState().gatewayConnected === false) {
      return
    }

    const request = completionRequestForInput(input)

    if (!request) {
      return
    }

    let active = true
    const isCurrent = () => active && $slashCatalogOwner.get() === owner

    const timer = setTimeout(() => {
      if (!isCurrent()) {
        return
      }

      // Skill completions are per session: project-local skills follow the
      // session's repo, so the gateway must know which session is asking.
      const sid = getUiState().sid

      const params =
        request.method === 'complete.slash' && sid ? { ...request.params, session_id: sid } : request.params

      gw.request<CompletionResponse>(request.method, params)
        .then(raw => {
          if (!isCurrent()) {
            return
          }

          const r = asRpcResult<CompletionResponse>(raw)

          const fetched =
            request.method === 'complete.slash' ? mergeWidgetAppItems(input, r?.items ?? []) : (r?.items ?? [])

          // Mid-message offers SKILLS only. A built-in like `/model` or `/new`
          // acts on the app, so it's meaningless as a reference inside prose —
          // only a skill reads as "handle this part with X". Filtering here
          // rather than in the gateway keeps one completion source for both
          // shapes.
          const items =
            request.method === 'complete.slash' && request.skillsOnly
              ? fetched.filter(item => item.kind === 'skill')
              : fetched

          // An inline reference replaces its own token, so the gateway's
          // `replace_from` (an offset into the synthetic `/query` it was sent)
          // doesn't apply — the caller already knows where the token starts.
          setReply({ input, owner, items, replaceFrom:
            request.method === 'complete.slash' && !request.skillsOnly ? (r?.replace_from ?? 1) : request.replaceFrom
          })
          setCompIdx(0)
        })
        .catch((e: unknown) => {
          if (!isCurrent()) {
            return
          }

          setReply({ input, owner, failed: true, replaceFrom: request.replaceFrom, items: [
            {
              text: '',
              display: t('libText.completion.unavailable'),
              meta: e instanceof Error && e.message ? e.message : t('libText.completion.unavailableMeta')
            }
          ] })
          setCompIdx(0)
        })
    }, 60)

    return () => { active = false; clearTimeout(timer) }
  }, [blocked, gw, input, owner])

  const current = reply?.input === input && reply.owner === owner ? reply : null
  const useReply = current && (!current.failed || !cached.length)
  const completions = blocked ? [] : useReply ? current.items : cached
  const compReplace = useReply ? current.replaceFrom : cached.length ? 1 : 0

  return { completions, compIdx, setCompIdx, compReplace }
}
