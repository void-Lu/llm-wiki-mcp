import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { apply as applyFilesystemSkillProvider } from '@deepseek-ai/dsh-skill-filesystem'
import {
  createAdapter,
  normalizeMcpServer,
  PLUGIN_NAME,
} from './adapter.mjs'

const pluginRoot = fileURLToPath(new URL('.', import.meta.url))
const guidance = readFileSync(new URL('./session-guidance.md', import.meta.url), 'utf8')
const mcpConfig = JSON.parse(readFileSync(new URL('./mcp.json', import.meta.url), 'utf8'))
const server = normalizeMcpServer(mcpConfig, { pluginRoot })
const skillRoot = fileURLToPath(new URL('./skills', import.meta.url))

const adapter = createAdapter({
  guidance,
  server,
  installSkillProvider: ctx => applyFilesystemSkillProvider(ctx, {
    providerName: 'llm-wiki-bundled-skills',
    includeDefaultRoots: false,
    bundledSkillDir: skillRoot,
  }),
})

export const name = PLUGIN_NAME
export const inject = ['mcpManager', 'skills', 'systemPrompt']
export const apply = adapter.apply
