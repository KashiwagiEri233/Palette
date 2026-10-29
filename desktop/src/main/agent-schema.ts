/** Provider-facing schemas are portable; execution keeps the original TypeBox tuples. */
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function schema(value: unknown): unknown {
  if (!object(value)) return value
  const result = { ...value }
  for (const key of ['properties', '$defs', 'definitions', 'patternProperties']) {
    if (object(value[key])) result[key] = Object.fromEntries(Object.entries(value[key]).map(([name, child]) => [name, schema(child)]))
  }
  for (const key of ['anyOf', 'oneOf', 'allOf']) {
    if (Array.isArray(value[key])) result[key] = value[key].map(schema)
  }
  for (const key of ['additionalProperties', 'not', 'contains', 'if', 'then', 'else']) {
    if (object(value[key])) result[key] = schema(value[key])
  }
  if (Array.isArray(value.items)) {
    const variants = [...new Map(value.items.map(item => { const normalized = schema(item); return [JSON.stringify(normalized), normalized] })).values()]
    result.items = variants.length === 0 ? false : variants.length === 1 ? variants[0] : { anyOf: variants }
    if (value.additionalItems === false) result.maxItems = Math.min(typeof value.maxItems === 'number' ? value.maxItems : value.items.length, value.items.length)
    delete result.additionalItems
  } else if (object(value.items)) result.items = schema(value.items)
  return result
}

function tool(value: unknown): unknown {
  if (!object(value)) return value
  const result = { ...value }
  for (const key of ['parameters', 'parametersJsonSchema', 'input_schema']) {
    if (object(value[key])) result[key] = schema(value[key])
  }
  if (object(value.function)) result.function = tool(value.function)
  if (Array.isArray(value.functionDeclarations)) result.functionDeclarations = value.functionDeclarations.map(tool)
  return result
}

export function providerToolSchemas(payload: unknown): unknown {
  if (!object(payload)) return payload
  const result = { ...payload }
  if (Array.isArray(payload.tools)) result.tools = payload.tools.map(tool)
  // Google places declarations inside config; other supported APIs use top-level tools.
  if (object(payload.config) && Array.isArray(payload.config.tools)) result.config = { ...payload.config, tools: payload.config.tools.map(tool) }
  return result
}
