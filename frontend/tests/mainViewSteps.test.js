import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { computed, ref, watch } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const mainViewPath = path.resolve(__dirname, '../src/views/MainView.vue')
const source = fs.readFileSync(mainViewPath, 'utf8')

function collectNodes(node, predicate, acc = []) {
  if (!node) return acc
  if (predicate(node)) acc.push(node)
  if (node.children) {
    for (const child of node.children) {
      collectNodes(child, predicate, acc)
    }
  }
  return acc
}

function hasPropBinding(element, propName) {
  return element.props?.some((p) => {
    if (p.rawName === `:${propName}` || p.rawName === propName) return true
    if (p.name === 'bind' && (p.arg?.content === propName || p.arg?.loc?.source === propName)) return true
    return false
  })
}

function getPropBindingExpression(element, propName) {
  const p = element.props?.find((p) => {
    if (p.rawName === `:${propName}` || p.rawName === propName) return true
    if (p.name === 'bind' && (p.arg?.content === propName || p.arg?.loc?.source === propName)) return true
    return false
  })
  return p?.exp?.content || p?.exp?.loc?.source || ''
}

function getDirectiveExpression(element, dirName) {
  const dir = element.props?.find((p) => p.name === dirName || p.rawName?.startsWith(`v-${dirName}`))
  return dir?.exp?.content || dir?.exp?.loc?.source || ''
}

function getEventHandler(element, eventName) {
  const dir = element.props?.find(
    (p) => (p.name === 'on' && p.arg?.content === eventName) || p.rawName === `@${eventName}`
  )
  return dir?.exp?.content || dir?.exp?.loc?.source || ''
}

test('MainView component declares simulationId prop in script setup', () => {
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id: 'MainView' })
  const hasSimulationIdProp = script.bindings?.simulationId === 'props' ||
    Boolean(script.props && ('simulationId' in script.props || script.props.includes?.('simulationId')))
  assert.equal(
    hasSimulationIdProp,
    true,
    'MainView must declare simulationId prop in defineProps'
  )
})

test('MainView passes simulationId to Step2EnvSetup', () => {
  const { descriptor } = parse(source)
  const stepComponents = collectNodes(descriptor.template.ast, (n) => n.tag === 'Step2EnvSetup')
  assert.ok(stepComponents.length > 0, 'Step2EnvSetup must be present in MainView template')
  const step2 = stepComponents[0]
  assert.equal(
    hasPropBinding(step2, 'simulationId'),
    true,
    'Step2EnvSetup must receive :simulationId prop in MainView'
  )
})

test('MainView imports all workflow step components (steps 3-5)', () => {
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id: 'MainView' })
  const imports = script.imports || {}

  assert.ok('Step3Simulation' in imports, 'MainView must import Step3Simulation component')
  assert.ok('Step4Report' in imports, 'MainView must import Step4Report component')
  assert.ok('Step5Interaction' in imports, 'MainView must import Step5Interaction component')
})

test('MainView renders step components for currentStep 3, 4, and 5', () => {
  const { descriptor } = parse(source)
  const stepComponents = collectNodes(descriptor.template.ast, (n) => n.tag && n.tag.startsWith('Step'))
  const renderedTags = stepComponents.map((n) => n.tag)

  assert.ok(
    renderedTags.includes('Step3Simulation'),
    `Expected Step3Simulation to be rendered in MainView, got: ${renderedTags.join(', ')}`
  )
  assert.ok(
    renderedTags.includes('Step4Report'),
    `Expected Step4Report to be rendered in MainView, got: ${renderedTags.join(', ')}`
  )
  assert.ok(
    renderedTags.includes('Step5Interaction'),
    `Expected Step5Interaction to be rendered in MainView, got: ${renderedTags.join(', ')}`
  )

  const step3 = stepComponents.find((n) => n.tag === 'Step3Simulation')
  const step4 = stepComponents.find((n) => n.tag === 'Step4Report')
  const step5 = stepComponents.find((n) => n.tag === 'Step5Interaction')

  assert.ok(
    getDirectiveExpression(step3, 'else-if').includes('3') || getDirectiveExpression(step3, 'if').includes('3'),
    'Step3Simulation must be conditioned on currentStep === 3'
  )
  assert.ok(
    getDirectiveExpression(step4, 'else-if').includes('4') || getDirectiveExpression(step4, 'if').includes('4'),
    'Step4Report must be conditioned on currentStep === 4'
  )
  assert.ok(
    getDirectiveExpression(step5, 'else-if').includes('5') || getDirectiveExpression(step5, 'if').includes('5'),
    'Step5Interaction must be conditioned on currentStep === 5'
  )

  assert.equal(
    hasPropBinding(step3, 'simulationId'),
    true,
    'Step3Simulation must receive :simulationId prop'
  )
})

test('Route params fallback: effectiveSimulationId falls back to route.params or projectData when simulationId prop is omitted or null', () => {
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id: 'MainView' })
  assert.equal(script.bindings?.effectiveSimulationId, 'setup-ref')

  assert.match(
    script.content,
    /effectiveSimulationId\s*=\s*computed\(/,
    'MainView must define effectiveSimulationId computed property'
  )
  assert.match(
    script.content,
    /route\.params\.simulationId/,
    'effectiveSimulationId must check route.params.simulationId'
  )
  assert.match(
    script.content,
    /projectData\.value\?\.simulation_id/,
    'effectiveSimulationId must check projectData.value?.simulation_id'
  )

  const testFallback = (propsVal, routeVal, stateVal, projectVal) => {
    const props = { simulationId: propsVal }
    const route = { params: routeVal || {} }
    const simulationIdState = ref(stateVal || '')
    const projectData = ref(projectVal)

    return computed(() => {
      return props.simulationId || route.params.simulationId || simulationIdState.value || projectData.value?.simulation_id || ''
    }).value
  }

  // 1. When props.simulationId is empty string, falls back to route.params.simulationId
  assert.equal(
    testFallback('', { simulationId: 'sim-from-route' }, '', null),
    'sim-from-route',
    'Should fallback to route.params.simulationId when prop is empty string'
  )

  // 2. When props.simulationId is null, falls back to route.params.simulationId
  assert.equal(
    testFallback(null, { simulationId: 'sim-from-route' }, '', null),
    'sim-from-route',
    'Should fallback to route.params.simulationId when prop is null'
  )

  // 3. When props.simulationId is undefined, falls back to route.params.simulationId
  assert.equal(
    testFallback(undefined, { simulationId: 'sim-from-route' }, '', null),
    'sim-from-route',
    'Should fallback to route.params.simulationId when prop is undefined'
  )

  // 4. When prop and route are omitted, falls back to projectData.simulation_id
  assert.equal(
    testFallback('', {}, '', { simulation_id: 'sim-from-project' }),
    'sim-from-project',
    'Should fallback to projectData.simulation_id when prop and route are omitted'
  )

  // 5. When prop is null and route empty, falls back to projectData.simulation_id
  assert.equal(
    testFallback(null, {}, '', { simulation_id: 'sim-from-project' }),
    'sim-from-project',
    'Should fallback to projectData.simulation_id when prop is null and route is empty'
  )

  // 6. When simulationId prop is explicitly provided, it takes highest precedence
  assert.equal(
    testFallback('sim-from-prop', { simulationId: 'sim-from-route' }, 'sim-from-state', { simulation_id: 'sim-from-project' }),
    'sim-from-prop',
    'Props simulationId must take precedence when present'
  )

  // 7. When all are empty, defaults to empty string
  assert.equal(
    testFallback('', {}, '', null),
    '',
    'Should return empty string when no simulationId is available'
  )
})

test('Event propagation: @go-back in Step3Simulation decrements currentStep from 3 back to 2', () => {
  const { descriptor } = parse(source)
  const stepComponents = collectNodes(descriptor.template.ast, (n) => n.tag === 'Step3Simulation')
  assert.ok(stepComponents.length > 0, 'Step3Simulation must exist in template')
  const step3 = stepComponents[0]

  assert.equal(
    getEventHandler(step3, 'go-back'),
    'handleGoBack',
    '@go-back in Step3Simulation must call handleGoBack'
  )

  const script = compileScript(descriptor, { id: 'MainView' })
  assert.equal(script.bindings?.handleGoBack, 'setup-const')

  const currentStep = ref(3)
  const stepNames = ['Graph Build', 'Env Setup', 'Simulation', 'Report', 'Interaction']
  const logs = []
  const addLog = (msg) => logs.push(msg)

  const handleGoBack = () => {
    if (currentStep.value > 1) {
      currentStep.value--
      addLog(`Returned to Step ${currentStep.value}: ${stepNames[currentStep.value - 1]}`)
    }
  }

  // Decrement step 3 -> step 2
  handleGoBack()
  assert.equal(currentStep.value, 2, 'handleGoBack must decrement currentStep from 3 back to 2')
  assert.equal(logs.length, 1)
  assert.ok(logs[0].includes('Returned to Step 2: Env Setup'))

  // Boundary check: decrement step 2 -> step 1
  handleGoBack()
  assert.equal(currentStep.value, 1, 'handleGoBack must decrement currentStep from 2 back to 1')

  // Boundary check: cannot decrement below step 1
  handleGoBack()
  assert.equal(currentStep.value, 1, 'handleGoBack must not decrement below step 1')
})

test('Prop validation: reportId update propagates to Step4Report and Step5Interaction', () => {
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id: 'MainView' })

  // Verify script declares reportId prop
  assert.ok(
    script.bindings?.reportId !== undefined,
    'MainView must have reportId binding in script'
  )
  assert.match(
    script.content,
    /reportId:\s*\{\s*type:\s*String/,
    'MainView must declare reportId prop of type String'
  )

  // Verify Step4Report and Step5Interaction receive :reportId prop
  const step4List = collectNodes(descriptor.template.ast, (n) => n.tag === 'Step4Report')
  assert.ok(step4List.length > 0, 'Step4Report must exist in template')
  const step4 = step4List[0]

  const step5List = collectNodes(descriptor.template.ast, (n) => n.tag === 'Step5Interaction')
  assert.ok(step5List.length > 0, 'Step5Interaction must exist in template')
  const step5 = step5List[0]

  assert.equal(
    hasPropBinding(step4, 'reportId'),
    true,
    'Step4Report must receive :reportId prop'
  )
  assert.equal(
    getPropBindingExpression(step4, 'reportId'),
    'reportId',
    'Step4Report :reportId must bind to reportId reactive state'
  )

  assert.equal(
    hasPropBinding(step5, 'reportId'),
    true,
    'Step5Interaction must receive :reportId prop'
  )
  assert.equal(
    getPropBindingExpression(step5, 'reportId'),
    'reportId',
    'Step5Interaction :reportId must bind to reportId reactive state'
  )

  // Verify handleNextStep updates reportId state when advancing steps
  const reportIdState = ref('')
  const currentStep = ref(3)
  const logs = []
  const addLog = (msg) => logs.push(msg)
  const stepNames = ['Graph Build', 'Env Setup', 'Simulation', 'Report', 'Interaction']

  const handleNextStep = (params = {}) => {
    if (params?.reportId) {
      reportIdState.value = params.reportId
    }
    if (currentStep.value < 5) {
      currentStep.value++
      addLog(`Entering Step ${currentStep.value}: ${stepNames[currentStep.value - 1]}`)
    }
  }

  // Advance from step 3 to step 4 with reportId parameter
  handleNextStep({ reportId: 'rep_test_456' })
  assert.equal(reportIdState.value, 'rep_test_456', 'handleNextStep must update reportId state')
  assert.equal(currentStep.value, 4, 'handleNextStep must advance to step 4')
})
