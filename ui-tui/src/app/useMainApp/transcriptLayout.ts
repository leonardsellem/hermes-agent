import { useEffect, useMemo, useRef } from 'react'

import { sectionMode } from '../../domain/details.js'
import { pruneVirtualHeightCache } from '../../hooks/useVirtualHistory.js'
import { composerPromptWidth } from '../../lib/inputMetrics.js'
import type { TranscriptRow, UiState } from '../interfaces.js'

const MAX_HEIGHT_CACHE_BUCKETS = 12

/** Which transcript detail sections render, derived from the `thinking:tools` layout key. */
const detailsVisibility = (detailsLayoutKey: string, ui: Pick<UiState, 'detailsModeCommandOverride' | 'sections'>) => {
  const [thinkingDetailsMode, toolsDetailsMode] = detailsLayoutKey.split(':')
  const thinkingDetailsVisible = thinkingDetailsMode !== 'hidden'
  const toolsDetailsVisible = toolsDetailsMode !== 'hidden'

  const historyThinkingExpanded =
    thinkingDetailsVisible && (ui.detailsModeCommandOverride || ui.sections.thinking === 'expanded')

  const detailsVisible = thinkingDetailsVisible || toolsDetailsVisible

  return { detailsVisible, historyThinkingExpanded, thinkingDetailsVisible, toolsDetailsVisible }
}

// One bucket per layout that changes row heights (width, prompt, compact, inline images, details).
function heightCacheBucket(
  ui: { compact?: boolean; inlineImages?: boolean; sid?: null | string; theme: { brand: { prompt: string } } },
  cols: number,
  detailsLayoutKey: string
): string {
  const flags = `${ui.compact ? '1' : '0'}:${ui.inlineImages ? '1' : '0'}`

  return `${ui.sid ?? 'draft'}:${cols}:${composerPromptWidth(ui.theme.brand.prompt)}:${flags}:${detailsLayoutKey}`
}

/** The transcript-layout phase of `useMainApp`: detail visibility and the per-layout height cache. */
export function useTranscriptLayout(
  ui: UiState,
  cols: number,
  virtualRows: TranscriptRow[],
  historyGeneration: number
) {
  const heightCachesRef = useRef(new Map<string, Map<string, number>>())

  const detailsLayoutKey = useMemo(() => {
    const thinking = sectionMode('thinking', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride)
    const tools = sectionMode('tools', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride)

    return `${thinking}:${tools}`
  }, [ui.detailsMode, ui.detailsModeCommandOverride, ui.sections])

  const { detailsVisible, historyThinkingExpanded, thinkingDetailsVisible, toolsDetailsVisible } = detailsVisibility(
    detailsLayoutKey,
    ui
  )

  const heightCacheKey = heightCacheBucket(ui, cols, detailsLayoutKey)

  // Build a render-local snapshot. Registering/pruning the shared cache is a
  // post-commit transition below, so an abandoned concurrent render cannot
  // delete heights still owned by the committed transcript generation.
  const activeHeightCache = useMemo(() => new Map(heightCachesRef.current.get(heightCacheKey)), [heightCacheKey])

  useEffect(() => {
    pruneVirtualHeightCache(activeHeightCache, virtualRows)
    heightCachesRef.current.delete(heightCacheKey)
    heightCachesRef.current.set(heightCacheKey, activeHeightCache)

    while (heightCachesRef.current.size > MAX_HEIGHT_CACHE_BUCKETS) {
      heightCachesRef.current.delete(heightCachesRef.current.keys().next().value!)
    }
  }, [activeHeightCache, heightCacheKey, historyGeneration, virtualRows])

  return { activeHeightCache, detailsVisible, historyThinkingExpanded, thinkingDetailsVisible, toolsDetailsVisible }
}
