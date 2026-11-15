import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { ref } from "vue"
import { parse } from "vue/compiler-sfc"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const sfcPath = path.resolve(__dirname, "../src/components/Step3Simulation.vue")
const sfcContent = fs.readFileSync(sfcPath, "utf8")
const { descriptor } = parse(sfcContent)
const scriptContent = descriptor.scriptSetup.content

// Extract handleSimulationStateUpdate definition
const fnMatch = scriptContent.match(/const handleSimulationStateUpdate = \(([\s\S]*?)\n\s*const fetchRunStatus/)
if (!fnMatch) {
  throw new Error("Could not locate handleSimulationStateUpdate in Step3Simulation.vue")
}
const fnCode = "const handleSimulationStateUpdate = (" + fnMatch[1]

function createHandler(overrides = {}) {
  const allActions = overrides.allActions || ref([])
  const actionIds = overrides.actionIds || ref(new Set())
  const runStatus = overrides.runStatus || ref({})
  const prevTwitterRound = overrides.prevTwitterRound || ref(0)
  const prevRedditRound = overrides.prevRedditRound || ref(0)
  const phase = overrides.phase || ref(1)
  const logs = overrides.logs || []
  const addLog = overrides.addLog || ((msg) => logs.push(msg))
  const checkPlatformsCompleted = overrides.checkPlatformsCompleted || (() => false)
  let pollingStopped = false
  const stopPolling = overrides.stopPolling || (() => { pollingStopped = true })
  const emitted = overrides.emitted || []
  const emit = overrides.emit || ((event, payload) => {
    emitted.push({ event, payload })
  })

  const runner = new Function(
    "ref", "allActions", "actionIds", "runStatus", "prevTwitterRound", "prevRedditRound",
    "phase", "addLog", "checkPlatformsCompleted", "stopPolling", "emit",
    `${fnCode}\nreturn handleSimulationStateUpdate;`
  )

  const handleSimulationStateUpdate = runner(
    ref, allActions, actionIds, runStatus, prevTwitterRound, prevRedditRound,
    phase, addLog, checkPlatformsCompleted, stopPolling, emit
  )

  return {
    handleSimulationStateUpdate,
    allActions,
    actionIds,
    runStatus,
    prevTwitterRound,
    prevRedditRound,
    phase,
    logs,
    emitted,
    isPollingStopped: () => pollingStopped
  }
}

test("Step3Simulation unwraps actionIds ref when checking and adding unique IDs", () => {
  // In Step3Simulation.vue, actionIds is declared as ref(new Set()).
  // Calling actionIds.has() or actionIds.add() directly on the Ref object throws TypeError.
  assert.equal(
    /actionIds\.(has|add)\(/.test(scriptContent),
    false,
    "Step3Simulation.vue must not call .has() or .add() directly on actionIds Ref; use actionIds.value"
  )
})

test("Step3Simulation references allActions ref instead of undefined actions identifier", () => {
  // In Step3Simulation.vue, actions state is declared as \`const allActions = ref([])\`.
  // Referencing \`actions.value\` raises ReferenceError: actions is not defined.
  assert.equal(
    /\bactions\.value\b/.test(scriptContent),
    false,
    "Step3Simulation.vue must not reference undefined actions.value; use allActions.value"
  )
})

test("Step3Simulation wraps recent_actions parsing in try/catch to protect telemetry handler", () => {
  // Action stream parsing block must be wrapped in try/catch so unexpected action payload
  // errors do not crash the telemetry handler before runner_status completion checks.
  const recentActionsBlockMatch = fnCode.match(/if\s*\(Array\.isArray\(data\.recent_actions\)[\s\S]*?\n\s*\}/)
  assert.ok(recentActionsBlockMatch, "recent_actions block must exist in handleSimulationStateUpdate")
  assert.ok(
    /try\s*\{[\s\S]*?catch/.test(recentActionsBlockMatch[0]),
    "recent_actions processing in handleSimulationStateUpdate must have a try/catch guard"
  )
})

test("handleSimulationStateUpdate processes SSE recent_actions without crashing telemetry handler", () => {
  const harness = createHandler()

  const ssePayload = {
    twitter_current_round: 1,
    total_rounds: 5,
    twitter_simulated_hours: 1,
    twitter_actions_count: 1,
    recent_actions: [
      {
        round_num: 1,
        timestamp: "12:00:00",
        agent_id: "agent_1",
        action_type: "tweet",
        content: "Simulation message"
      }
    ],
    runner_status: "completed"
  }

  // Processing the SSE payload must not throw an uncaught exception
  assert.doesNotThrow(() => {
    harness.handleSimulationStateUpdate(ssePayload)
  }, "handleSimulationStateUpdate must not throw uncaught error on SSE payload with recent_actions")

  // Action must be tracked in actionIds and allActions
  const uniqueId = "1_12:00:00_agent_1_tweet"
  assert.ok(harness.actionIds.value.has(uniqueId), "uniqueId must be added to actionIds.value")
  assert.equal(harness.allActions.value.length, 1, "action must be added to allActions.value")

  // Completion check must execute and emit completion event
  assert.equal(harness.isPollingStopped(), true, "polling must be stopped upon runner completion")
  assert.deepEqual(harness.emitted, [{ event: "update-status", payload: "completed" }])
})

test("handleSimulationStateUpdate deduplicates duplicate actions across subsequent SSE events using actionIds", () => {
  const harness = createHandler()
  const action1 = {
    round_num: 1,
    timestamp: "12:00:00",
    agent_id: "agent_1",
    action_type: "tweet",
    content: "First action"
  }
  const action2 = {
    round_num: 1,
    timestamp: "12:00:05",
    agent_id: "agent_2",
    action_type: "reply",
    content: "Second action"
  }

  // First SSE event containing action1
  harness.handleSimulationStateUpdate({
    recent_actions: [action1],
    runner_status: "running"
  })
  assert.equal(harness.allActions.value.length, 1)
  assert.equal(harness.actionIds.value.size, 1)

  // Subsequent SSE event containing duplicate action1 and new action2
  harness.handleSimulationStateUpdate({
    recent_actions: [action1, action2],
    runner_status: "running"
  })
  assert.equal(harness.allActions.value.length, 2, "duplicate action1 should not be added again")
  assert.equal(harness.actionIds.value.size, 2)
  assert.deepEqual(harness.allActions.value[0], action2)
  assert.deepEqual(harness.allActions.value[1], action1)

  // Another SSE event containing action2 again
  harness.handleSimulationStateUpdate({
    recent_actions: [action2],
    runner_status: "running"
  })
  assert.equal(harness.allActions.value.length, 2, "duplicate action2 should not be added again")
  assert.equal(harness.actionIds.value.size, 2)
})

test("handleSimulationStateUpdate caps allActions length at 200 via allActions.value.pop()", () => {
  const harness = createHandler()
  const actions = []
  for (let i = 0; i < 220; i++) {
    actions.push({
      round_num: i,
      timestamp: `12:${String(i % 60).padStart(2, "0")}:00`,
      agent_id: `agent_${i}`,
      action_type: "tweet",
      content: `Action ${i}`
    })
  }

  harness.handleSimulationStateUpdate({
    recent_actions: actions,
    runner_status: "running"
  })

  assert.equal(harness.allActions.value.length, 200, "allActions array must be capped at 200 items")
  assert.equal(harness.actionIds.value.size, 220, "all action IDs must still be tracked in actionIds")
  // Newest action is unshifted to front (index 0)
  assert.equal(harness.allActions.value[0].content, "Action 219")
  // 20 oldest actions (Action 0 to Action 19) were popped
  assert.equal(harness.allActions.value[199].content, "Action 20")
})

test("handleSimulationStateUpdate handles empty, null, or undefined recent_actions without exception", () => {
  const harness = createHandler()

  const payloads = [
    { recent_actions: [] },
    { recent_actions: null },
    { recent_actions: undefined },
    {},
    { recent_actions: "invalid_string" },
    { recent_actions: 12345 }
  ]

  for (const payload of payloads) {
    assert.doesNotThrow(() => {
      harness.handleSimulationStateUpdate(payload)
    }, `Payload ${JSON.stringify(payload)} should not throw error`)
  }

  assert.equal(harness.allActions.value.length, 0)
  assert.equal(harness.actionIds.value.size, 0)
})

test("handleSimulationStateUpdate recovers from exceptions during recent_actions processing and continues status checks", () => {
  const harness = createHandler()
  const warnings = []
  const originalWarn = console.warn
  console.warn = (...args) => {
    warnings.push(args)
  }

  try {
    // An action that throws when accessing properties (null action)
    const malformedPayload = {
      recent_actions: [null],
      runner_status: "completed"
    }

    assert.doesNotThrow(() => {
      harness.handleSimulationStateUpdate(malformedPayload)
    }, "should catch error and not throw")

    // Verify warning was logged
    assert.equal(warnings.length, 1, "should log console.warn on error")
    assert.equal(warnings[0][0], "Failed to process recent actions:")
    assert.ok(warnings[0][1] instanceof Error, "second argument should be the caught error")

    // Verify simulation status progression checks still ran
    assert.equal(harness.isPollingStopped(), true, "stopPolling should still be called")
    assert.equal(harness.phase.value, 2, "phase should transition to 2")
    assert.deepEqual(harness.emitted, [{ event: "update-status", payload: "completed" }])
  } finally {
    console.warn = originalWarn
  }
})

test("handleSimulationStateUpdate recovers from exception during recent_actions and executes failure status checks", () => {
  const harness = createHandler()
  const warnings = []
  const originalWarn = console.warn
  console.warn = (...args) => {
    warnings.push(args)
  }

  try {
    const malformedPayload = {
      recent_actions: [undefined],
      runner_status: "failed",
      error: "Backend failure reason"
    }

    assert.doesNotThrow(() => {
      harness.handleSimulationStateUpdate(malformedPayload)
    })

    assert.equal(warnings.length, 1)
    assert.equal(harness.isPollingStopped(), true)
    assert.equal(harness.phase.value, 2)
    assert.deepEqual(harness.emitted, [{ event: "update-status", payload: "error" }])
    assert.ok(harness.logs.some(log => log.includes("Simulation failed: Backend failure reason")))
  } finally {
    console.warn = originalWarn
  }
})
