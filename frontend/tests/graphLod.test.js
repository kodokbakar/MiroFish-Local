import assert from 'node:assert/strict'
import test from 'node:test'
import {
  calculateNodeDegrees,
  filterVisibleGraph,
  searchOffscreenNodes,
  shouldShowElementLabel
} from '../src/utils/graphLod.js'

test('calculateNodeDegrees counts incident edges for nodes', () => {
  const nodes = [{ uuid: 'n1' }, { uuid: 'n2' }, { uuid: 'n3' }]
  const edges = [
    { source_node_uuid: 'n1', target_node_uuid: 'n2' },
    { source_node_uuid: 'n2', target_node_uuid: 'n3' },
    { source_node_uuid: 'n1', target_node_uuid: 'n3' },
    { source_node_uuid: 'n1', target_node_uuid: 'n1' } // self-loop counts src & tgt
  ]

  const degrees = calculateNodeDegrees(nodes, edges)
  assert.equal(degrees['n1'], 4)
  assert.equal(degrees['n2'], 2)
  assert.equal(degrees['n3'], 2)
})

test('filterVisibleGraph retains all nodes when cap is null, Infinity, or 0', () => {
  const nodes = [{ uuid: 'a' }, { uuid: 'b' }, { uuid: 'c' }]
  const edges = [{ source_node_uuid: 'a', target_node_uuid: 'b' }]

  const resNull = filterVisibleGraph({ nodes, edges, cap: null })
  assert.equal(resNull.visibleNodeCount, 3)
  assert.equal(resNull.offscreenNodes.length, 0)
  assert.equal(resNull.visibleEdges.length, 1)

  const resInf = filterVisibleGraph({ nodes, edges, cap: Infinity })
  assert.equal(resInf.visibleNodeCount, 3)

  const resZero = filterVisibleGraph({ nodes, edges, cap: 0 })
  assert.equal(resZero.visibleNodeCount, 3)
})

test('filterVisibleGraph caps nodes by degree descending and prunes dangling edges', () => {
  // A has degree 3, B degree 2, C degree 1, D degree 0
  const nodes = [
    { uuid: 'D', name: 'Node D' },
    { uuid: 'C', name: 'Node C' },
    { uuid: 'B', name: 'Node B' },
    { uuid: 'A', name: 'Node A' }
  ]
  const edges = [
    { source_node_uuid: 'A', target_node_uuid: 'B' },
    { source_node_uuid: 'A', target_node_uuid: 'C' },
    { source_node_uuid: 'A', target_node_uuid: 'B' }
  ]

  const res = filterVisibleGraph({ nodes, edges, cap: 2 })
  assert.equal(res.totalNodeCount, 4)
  assert.equal(res.visibleNodeCount, 2)
  const visibleIds = res.visibleNodes.map(n => n.uuid)
  assert.deepEqual(visibleIds, ['A', 'B'])
  assert.equal(res.offscreenNodes.length, 2)

  // Edge A-C should be pruned because C is not visible. Only A-B edges remain (2).
  assert.equal(res.visibleEdges.length, 2)
  assert.ok(res.visibleEdges.every(e => visibleIds.includes(e.source_node_uuid) && visibleIds.includes(e.target_node_uuid)))
})

test('filterVisibleGraph retains explicitly injected nodes even if outside top cap', () => {
  const nodes = [
    { uuid: 'A', name: 'Node A' },
    { uuid: 'B', name: 'Node B' },
    { uuid: 'C', name: 'Node C' },
    { uuid: 'D', name: 'Node D' }
  ]
  // Degree: A: 2, B: 2, C: 0, D: 0
  const edges = [
    { source_node_uuid: 'A', target_node_uuid: 'B' }
  ]

  const res = filterVisibleGraph({
    nodes,
    edges,
    cap: 2,
    injectedNodeIds: ['D']
  })

  const visibleIds = new Set(res.visibleNodes.map(n => n.uuid))
  assert.equal(res.visibleNodeCount, 3)
  assert.ok(visibleIds.has('A'))
  assert.ok(visibleIds.has('B'))
  assert.ok(visibleIds.has('D'))
  assert.ok(!visibleIds.has('C'))
  assert.equal(res.offscreenNodes.length, 1)
  assert.equal(res.offscreenNodes[0].uuid, 'C')
})

test('searchOffscreenNodes finds matching nodes not already visible', () => {
  const nodes = [
    { uuid: '1', name: 'Alpha Agent' },
    { uuid: '2', name: 'Beta Agent' },
    { uuid: '3', name: 'Gamma Tool' }
  ]
  const visibleNodeIds = ['1']

  const resEmpty = searchOffscreenNodes(nodes, visibleNodeIds, '')
  assert.deepEqual(resEmpty, [])

  const resAgent = searchOffscreenNodes(nodes, visibleNodeIds, 'agent')
  assert.equal(resAgent.length, 1)
  assert.equal(resAgent[0].uuid, '2')

  const resId = searchOffscreenNodes(nodes, visibleNodeIds, '3')
  assert.equal(resId.length, 1)
  assert.equal(resId[0].uuid, '3')
})

test('shouldShowElementLabel returns true when scale >= 0.8', () => {
  assert.equal(shouldShowElementLabel({ scale: 1.0 }), true)
  assert.equal(shouldShowElementLabel({ scale: 0.8 }), true)
  assert.equal(shouldShowElementLabel({ scale: 0.85 }), true)
})

test('shouldShowElementLabel at scale < 0.8 hides secondary labels unless hovered/selected/highDegree', () => {
  assert.equal(shouldShowElementLabel({ scale: 0.7 }), false)
  assert.equal(shouldShowElementLabel({ scale: 0.5, isHovered: true }), true)
  assert.equal(shouldShowElementLabel({ scale: 0.5, isSelected: true }), true)
  assert.equal(shouldShowElementLabel({ scale: 0.5, isHighDegree: true }), true)
  assert.equal(shouldShowElementLabel({ scale: 0.3, isHovered: false, isSelected: false, isHighDegree: false }), false)
})
