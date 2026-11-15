import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { ref } from 'vue'
import { parse } from 'vue/compiler-sfc'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const sfcPath = path.resolve(__dirname, '../src/components/Step3Simulation.vue')
const sfcContent = fs.readFileSync(sfcPath, 'utf8')
const { descriptor } = parse(sfcContent)
const scriptContent = descriptor.scriptSetup.content

// Extract handleSimulationStateUpdate definition
const fnMatch = scriptContent.match(/const handleSimulationStateUpdate = \(([\s\S]*?)\n\s*const fetchRunStatus/)
if (!fnMatch) {
  throw new Error('Could not locate handleSimulationStateUpdate in Step3Simulation.vue')
}
const fnCode = 'const handleSimulationStateUpdate = (' + fnMatch[1]

test('Step3Simulation unwraps actionIds ref when checking and adding unique IDs', () => {
  // In Step3Simulation.vue, actionIds is declared as ref(new Set()).
  // Calling actionIds.has() or actionIds.add() directly on the Ref object throws TypeError.
  assert.equal(
    /actionIds\.(has|add)\(/.test(scriptContent),
    false,
    'Step3Simulation.vue must not call .has() or .add() directly on actionIds Ref; use actionIds.value'
  )
})

test('Step3Simulation references allActions ref instead of undefined actions identifier', () => {
  // In Step3Simulation.vue, actions state is declared as `const allActions = ref([])`.
  // Referencing `actions.value` raises ReferenceError: actions is not defined.
  assert.equal(
    /\bactions\.value\b/.test(scriptContent),
    false,
    'Step3Simulation.vue must not reference undefined actions.value; use allActions.value'
  )
})

test('Step3Simulation wraps recent_actions parsing in try/catch to protect telemetry handler', () => {
  // Action stream parsing block must be wrapped in try/catch so unexpected action payload
  // errors do not crash the telemetry handler before runner_status completion checks.
  const recentActionsBlockMatch = fnCode.match(/if\s*\(Array\.isArray\(data\.recent_actions\)[\s\S]*?\n\s*\}/)
  assert.ok(recentActionsBlockMatch, 'recent_actions block must exist in handleSimulationStateUpdate')
  assert.ok(
    /try\s*\{[\s\S]*?catch/.test(recentActionsBlockMatch[0]),
    'recent_actions processing in handleSimulationStateUpdate must have a try/catch guard'
  )
})

test('handleSimulationStateUpdate processes SSE recent_actions without crashing telemetry handler', () => {
  const allActions = ref([])
  const actionIds = ref(new Set())
  const runStatus = ref({})
  const prevTwitterRound = ref(0)
  const prevRedditRound = ref(0)
  const phase = ref(1)
  const logs = []
  const addLog = (msg) => logs.push(msg)
  const checkPlatformsCompleted = () => false
  let pollingStopped = false
  const stopPolling = () => { pollingStopped = true }
  const emitted = []
  const emit = (event, payload) => {
    emitted.push({ event, payload })
  }

  const runner = new Function(
    'ref', 'allActions', 'actionIds', 'runStatus', 'prevTwitterRound', 'prevRedditRound',
    'phase', 'addLog', 'checkPlatformsCompleted', 'stopPolling', 'emit',
    `${fnCode}\nreturn handleSimulationStateUpdate;`
  )

  const handleSimulationStateUpdate = runner(
    ref, allActions, actionIds, runStatus, prevTwitterRound, prevRedditRound,
    phase, addLog, checkPlatformsCompleted, stopPolling, emit
  )

  const ssePayload = {
    twitter_current_round: 1,
    total_rounds: 5,
    twitter_simulated_hours: 1,
    twitter_actions_count: 1,
    recent_actions: [
      {
        round_num: 1,
        timestamp: '12:00:00',
        agent_id: 'agent_1',
        action_type: 'tweet',
        content: 'Simulation message'
      }
    ],
    runner_status: 'completed'
  }

  // Processing the SSE payload must not throw an uncaught exception
  assert.doesNotThrow(() => {
    handleSimulationStateUpdate(ssePayload)
  }, 'handleSimulationStateUpdate must not throw uncaught error on SSE payload with recent_actions')

  // Action must be tracked in actionIds and allActions
  const uniqueId = '1_12:00:00_agent_1_tweet'
  assert.ok(actionIds.value.has(uniqueId), 'uniqueId must be added to actionIds.value')
  assert.equal(allActions.value.length, 1, 'action must be added to allActions.value')

  // Completion check must execute and emit completion event
  assert.equal(pollingStopped, true, 'polling must be stopped upon runner completion')
  assert.deepEqual(emitted, [{ event: 'update-status', payload: 'completed' }])
})
