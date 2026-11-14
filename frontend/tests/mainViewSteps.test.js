import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
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

function getDirectiveExpression(element, dirName) {
  const dir = element.props?.find((p) => p.name === dirName || p.rawName?.startsWith(`v-${dirName}`))
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
