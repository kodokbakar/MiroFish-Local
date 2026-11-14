/**
 * Pure utility functions for Graph Level of Detail (LOD) and rendering cap.
 */

/**
 * Calculates degree (number of incident edges) for each node id.
 *
 * @param {Array<{ uuid?: string, id?: string }>} nodes
 * @param {Array<{ source_node_uuid?: string, target_node_uuid?: string, source?: any, target?: any }>} edges
 * @returns {Record<string, number>} mapping node id -> degree
 */
export function calculateNodeDegrees(nodes = [], edges = []) {
  const degrees = {}
  for (const n of nodes) {
    const id = n.uuid ?? n.id
    if (id != null) {
      degrees[id] = 0
    }
  }

  for (const e of edges) {
    const src = e.source_node_uuid ?? (typeof e.source === 'object' && e.source !== null ? (e.source.uuid ?? e.source.id) : e.source)
    const tgt = e.target_node_uuid ?? (typeof e.target === 'object' && e.target !== null ? (e.target.uuid ?? e.target.id) : e.target)

    if (src != null) {
      degrees[src] = (degrees[src] || 0) + 1
    }
    if (tgt != null) {
      degrees[tgt] = (degrees[tgt] || 0) + 1
    }
  }

  return degrees
}

/**
 * Filters visible nodes and edges based on cap and injected ids.
 *
 * @param {Object} params
 * @param {Array} params.nodes
 * @param {Array} params.edges
 * @param {number|null} params.cap
 * @param {Array<string>|Set<string>} [params.injectedNodeIds]
 * @returns {{
 *   visibleNodes: Array,
 *   visibleEdges: Array,
 *   totalNodeCount: number,
 *   visibleNodeCount: number,
 *   offscreenNodes: Array
 * }}
 */
export function filterVisibleGraph({ nodes = [], edges = [], cap = null, injectedNodeIds = [] }) {
  const totalNodeCount = nodes.length
  const injectedSet = new Set(injectedNodeIds || [])
  const degrees = calculateNodeDegrees(nodes, edges)

  // Rank nodes by degree descending; break ties by stable name/id
  const sortedNodes = [...nodes].sort((a, b) => {
    const idA = a.uuid ?? a.id
    const idB = b.uuid ?? b.id
    const degDiff = (degrees[idB] || 0) - (degrees[idA] || 0)
    if (degDiff !== 0) return degDiff
    return String(a.name || idA).localeCompare(String(b.name || idB))
  })

  let visibleNodes = []
  let offscreenNodes = []

  const numericCap = (cap === null || cap === undefined || cap === Infinity || cap <= 0) ? null : Number(cap)

  if (numericCap === null || sortedNodes.length <= numericCap) {
    visibleNodes = sortedNodes
    offscreenNodes = []
  } else {
    const selectedIds = new Set()
    for (let i = 0; i < sortedNodes.length && selectedIds.size < numericCap; i++) {
      const node = sortedNodes[i]
      const id = node.uuid ?? node.id
      selectedIds.add(id)
    }

    // Retain explicitly injected nodes
    for (const injectedId of injectedSet) {
      selectedIds.add(injectedId)
    }

    visibleNodes = []
    offscreenNodes = []

    for (const node of sortedNodes) {
      const id = node.uuid ?? node.id
      if (selectedIds.has(id)) {
        visibleNodes.push(node)
      } else {
        offscreenNodes.push(node)
      }
    }
  }

  // Prune edges: both source and target must be in visible nodes
  const visibleIdSet = new Set(visibleNodes.map(n => n.uuid ?? n.id))

  const visibleEdges = edges.filter(e => {
    const src = e.source_node_uuid ?? (typeof e.source === 'object' && e.source !== null ? (e.source.uuid ?? e.source.id) : e.source)
    const tgt = e.target_node_uuid ?? (typeof e.target === 'object' && e.target !== null ? (e.target.uuid ?? e.target.id) : e.target)
    return visibleIdSet.has(src) && visibleIdSet.has(tgt)
  })

  return {
    visibleNodes,
    visibleEdges,
    totalNodeCount,
    visibleNodeCount: visibleNodes.length,
    offscreenNodes
  }
}

/**
 * Searches offscreen nodes matching query (case-insensitive name or id).
 *
 * @param {Array} nodes
 * @param {Array<string>|Set<string>} visibleNodeIds
 * @param {string} query
 * @returns {Array} matching offscreen nodes
 */
export function searchOffscreenNodes(nodes = [], visibleNodeIds = [], query = '') {
  if (!query || !query.trim()) return []
  const q = query.trim().toLowerCase()
  const visibleSet = new Set(visibleNodeIds || [])

  return nodes.filter(node => {
    const id = node.uuid ?? node.id
    if (visibleSet.has(id)) return false
    const name = String(node.name || '').toLowerCase()
    const strId = String(id || '').toLowerCase()
    return name.includes(q) || strId.includes(q)
  })
}

/**
 * LOD visibility logic for labels.
 * Scale < 0.8 hides secondary labels unless hovered/selected/highDegree.
 *
 * @param {Object} params
 * @param {number} params.scale
 * @param {boolean} [params.isHovered]
 * @param {boolean} [params.isSelected]
 * @param {boolean} [params.isHighDegree]
 * @returns {boolean}
 */
export function shouldShowElementLabel({ scale = 1, isHovered = false, isSelected = false, isHighDegree = false }) {
  if (scale >= 0.8) return true
  return Boolean(isHovered || isSelected || isHighDegree)
}
