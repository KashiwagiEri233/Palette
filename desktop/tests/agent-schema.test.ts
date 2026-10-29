import { expect, it } from 'vitest'
import { Type } from 'typebox'
import { Compile } from 'typebox/compile'
import { providerToolSchemas } from '../src/main/agent-schema'
import { agentFailureMessage } from '../src/main/agent-errors'

const parameters = Type.Object({
  location: Type.Tuple([Type.Integer(), Type.Integer()]),
  fields: Type.Array(Type.Tuple([Type.Integer({ minimum: 0 }), Type.String({ maxLength: 100 })]))
})

it.each(['completions', 'responses', 'anthropic', 'google'])('sends portable tuple declarations through %s without changing execution validation', api => {
  const declaration = api === 'completions' ? { type: 'function', function: { name: 'app', parameters } }
    : api === 'anthropic' ? { name: 'app', input_schema: parameters }
    : api === 'google' ? { functionDeclarations: [{ name: 'app', parametersJsonSchema: parameters }] }
    : { type: 'function', name: 'app', parameters }
  const messages = [{ role: 'user', content: { items: ['literal', 'unchanged'] } }]
  const payload = api === 'google' ? { contents: messages, config: { tools: [declaration] } } : { messages, tools: [declaration] }
  const result = providerToolSchemas(payload) as typeof payload
  const encoded = JSON.parse(JSON.stringify(result))
  const actual = api === 'completions' ? encoded.tools[0].function.parameters
    : api === 'anthropic' ? encoded.tools[0].input_schema
    : api === 'google' ? encoded.config.tools[0].functionDeclarations[0].parametersJsonSchema
    : encoded.tools[0].parameters
  expect(actual.properties.location).toMatchObject({ type: 'array', minItems: 2, maxItems: 2, items: { type: 'integer' } })
  expect(actual.properties.location).not.toHaveProperty('additionalItems')
  expect(actual.properties.fields.items).toMatchObject({ minItems: 2, maxItems: 2, items: { anyOf: [{ type: 'integer', minimum: 0 }, { type: 'string', maxLength: 100 }] } })
  expect(Array.isArray(parameters.properties.location.items)).toBe(true)
  const validate = Compile(parameters)
  expect(validate.Check({ location: [-10, 20], fields: [[1, 'text']] })).toBe(true)
  expect(validate.Check({ location: [1, 2, 3], fields: [] })).toBe(false)
  expect(validate.Check({ location: [1, 2], fields: [['bad label', 1]] })).toBe(false)
  expect(api === 'google' ? result.contents : result.messages).toBe(messages)
})

it('identifies schema errors without blaming endpoint or credentials and bounds safe error details', () => {
  const detail = '400: {"message":"Invalid schema for function \'app\': items must be boolean or object"}'
  expect(agentFailureMessage(detail)).toContain('工具参数格式不兼容')
  expect(agentFailureMessage(detail)).toContain('HTTP 400')
  expect(agentFailureMessage(detail)).toContain("'app'")
  expect(agentFailureMessage(detail)).not.toContain('检查端点')
  expect(agentFailureMessage('401: unauthorized')).toContain('鉴权失败（HTTP 401）')
  expect(agentFailureMessage('429: limited')).toContain('限流')
  expect(agentFailureMessage('fetch failed')).toContain('fetch failed')
  expect(agentFailureMessage('')).toContain('未返回具体错误信息')
  expect(agentFailureMessage('x'.repeat(5000)).length).toBeLessThan(1300)
  const safe = agentFailureMessage('failed https://user:pass@example.com/api?key=query-secret#fragment Authorization: Bearer header-secret')
  expect(safe).not.toMatch(/user:pass|query-secret|fragment|header-secret/)
})
