import assert from 'node:assert/strict'
import test from 'node:test'
import { JSDOM } from 'jsdom'
import { renderAnswerText, renderMarkdown } from '../src/utils/markdown.js'

const document = new JSDOM('<!doctype html>').window.document

const parseHtml = (html) => {
  const container = document.createElement('div')
  container.innerHTML = html
  return container
}

test('renders untrusted HTML as text in Markdown output', () => {
  const payload = '<img src=x onerror=alert(document.origin)>'
  const container = parseHtml(renderMarkdown(payload))

  assert.equal(container.querySelector('img'), null)
  assert.equal(container.textContent, payload)
})

test('serializes non-string Markdown input without interpreting HTML', () => {
  const payload = { response: '<img src=x onerror=alert(document.origin)>' }
  const container = parseHtml(renderMarkdown(payload))

  assert.equal(container.querySelector('img'), null)
  assert.equal(container.textContent, JSON.stringify(payload))
})

test('keeps untrusted HTML inert in interview answers while preserving formatting', () => {
  const payload = '<img src=x onerror=alert(document.origin)>'
  const container = parseHtml(renderAnswerText(`${payload}\n**answer**`))

  assert.equal(container.querySelector('img'), null)
  assert.equal(container.querySelector('br') !== null, true)
  assert.equal(container.querySelector('strong')?.textContent, 'answer')
  assert.equal(container.textContent, `${payload}answer`)
})

test('renders blockquotes after escaping Markdown input', () => {
  const container = parseHtml(renderMarkdown('> This is a quote'))

  assert.equal(container.querySelector('blockquote.md-quote')?.textContent, 'This is a quote')
  assert.equal(container.querySelector('p.md-p'), null)
})

test('returns an empty answer for nullish content', () => {
  assert.equal(renderAnswerText(null), '')
  assert.equal(renderAnswerText(undefined), '')
})

test('preserves headings, emphasis, lists, and inline code', () => {
  const container = parseHtml(renderMarkdown('# Heading\n\n**bold** and *italic*\n\n- item\n\n`code`'))

  assert.equal(container.querySelector('h2')?.textContent, 'Heading')
  assert.equal(container.querySelector('strong')?.textContent, 'bold')
  assert.equal(container.querySelector('em')?.textContent, 'italic')
  assert.equal(container.querySelector('ul li')?.textContent, 'item')
  assert.equal(container.querySelector('code.inline-code')?.textContent, 'code')
})

test('keeps HTML-looking code block content as text', () => {
  const payload = '<img src=x onerror=alert(1)>'
  const container = parseHtml(renderMarkdown(`\`\`\`\n${payload}\n\`\`\``))
  const code = container.querySelector('pre code')

  assert.ok(code)
  assert.equal(code.querySelector('img'), null)
  assert.ok(code.textContent.includes(payload))
})
