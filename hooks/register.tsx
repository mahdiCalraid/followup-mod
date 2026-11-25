import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CustomerView, FollowupTab, FollowupView, NeedItem, PaneData, QuickBooksStatus } from '../types'

// The Follow-up mod: a pane over the engine's views/pane.json, the /followup
// command, and a guard that blocks sending email and changing QuickBooks.
// The mod talks to the engine only through its CLI (docs/CONTRACT.md §7) and
// reads only views/pane.json (§8).

const PANE = 'followup'
const TITLE = 'Follow-ups'
const POLL_MS = 3000
const DRAFT_DEBOUNCE_MS = 2000
const DEMO_NAME = 'Brightwater Bakery Co.'
const DEMO_EMAIL = 'orders@brightwater.example'

const paneData = atom({ plugin: 'followup', key: 'pane' } as const, null, { shape: 'pane-1' })
const view = atom({ plugin: 'followup', key: 'view' } as const, 'today')
const confirming = atom({ plugin: 'followup', key: 'confirming' } as const, null)
const choice = atom({ plugin: 'followup', key: 'choice' } as const, {}, { shape: 'choice-1' })
const busy = atom({ plugin: 'followup', key: 'busy' } as const, false)
const ready = atom({ plugin: 'followup', key: 'ready' } as const, false)
const qbStatus = atom({ plugin: 'followup', key: 'qb' } as const, null, { shape: 'qb-1' })

// ── Workspace ──────────────────────────────────────────────────────────────
// WS is the `workspace` option if set, else the session's folder. It counts
// as configured if and only if <WS>/followup_mod/config.json exists.

let WS = ''
function setWs(path: string): void {
  const trimmed = path.trim().replace(/[\\/]+$/, '')
  WS = trimmed || path.trim()
}
async function ws($: EngineInterface): Promise<string> {
  if (!WS) {
    try {
      setWs(await $.session.cwd())
    } catch {
      // No session folder known yet: WS stays empty.
    }
  }
  return WS
}
const modDir = () => `${WS}/followup_mod`
const cfgPath = () => `${modDir()}/config.json`
const panePath = () => `${WS}/views/pane.json`
const fuPath = () => `${modDir()}/scripts/fu.py`

let isConfigured = false

async function exists($: EngineInterface, path: string): Promise<boolean> {
  try {
    return await $.fs.exists(path)
  } catch {
    return false
  }
}

async function readJson($: EngineInterface, path: string): Promise<Record<string, unknown> | null> {
  try {
    const data: unknown = JSON.parse(await $.fs.read(path))
    return data && typeof data === 'object' && !Array.isArray(data) ? (data as Record<string, unknown>) : null
  } catch {
    return null
  }
}

async function checkConfigured($: EngineInterface): Promise<boolean> {
  await ws($)
  const ok = WS !== '' && (await exists($, cfgPath()))
  const changed = ok !== isConfigured
  isConfigured = ok
  if ((await read($, ready)) !== ok) await update($, ready, () => ok)
  // The engine asks for the tool list only when something changes: tell it,
  // so the tools that send or change QuickBooks go (or come back) now.
  if (changed) {
    try {
      $.ui.invalidate('tool.list')
    } catch {
      // The guard still refuses every call of one.
    }
  }
  return ok
}

function toast($: EngineInterface, text: string): void {
  try {
    $.ui.toast(text)
  } catch {
    // A toast that can't show is not worth failing for.
  }
}

async function openPane($: EngineInterface): Promise<void> {
  try {
    await $.ui.open({ id: PANE, title: TITLE })
  } catch {
    // The surface may have no panes.
  }
}

// ── Reading views/pane.json ────────────────────────────────────────────────

let paneMtime = -1

function normalize(data: PaneData): PaneData {
  return {
    ...data,
    counts: data.counts ?? {},
    today_items: Array.isArray(data.today_items) ? data.today_items : [],
    pipeline: Array.isArray(data.pipeline) ? data.pipeline : [],
    sent: Array.isArray(data.sent) ? data.sent : [],
    customers: Array.isArray(data.customers) ? data.customers : [],
    last_run: data.last_run ?? null,
  }
}

/** Reads pane.json into the atom when its mtime changed (always with `force`). */
async function refreshPane($: EngineInterface, force = false): Promise<void> {
  if (!WS) return
  const path = panePath()
  const stat = await $.fs.stat(path).catch(() => undefined)
  if (!stat) {
    paneMtime = -1
    if ((await read($, paneData)) !== null) await update($, paneData, () => null)
    return
  }
  if (!force && stat.mtimeMs === paneMtime) return
  let data: PaneData
  try {
    data = JSON.parse(await $.fs.read(path)) as PaneData
  } catch {
    return // Half-written: the next poll reads it again.
  }
  if (!data || typeof data !== 'object') return
  paneMtime = stat.mtimeMs
  await update($, paneData, () => normalize(data))
}

let pollTimer: { cancel: () => void } | null = null
let isPolling = false

let pollCount = 0

async function poll($: EngineInterface): Promise<void> {
  if (isPolling) return
  isPolling = true
  try {
    await checkConfigured($)
    await refreshPane($)
    // A connector can finish connecting after the session started.
    if (++pollCount % QB_POLLS === 0) await refreshQuickBooks($)
  } catch {
    // Try again next tick.
  } finally {
    isPolling = false
  }
}

function startPolling($: EngineInterface): void {
  if (pollTimer) return
  try {
    pollTimer = $.clock.every(POLL_MS, () => {
      void poll($)
    })
  } catch {
    pollTimer = null
  }
}

// ── Python and the engine CLI ──────────────────────────────────────────────

let PY: string[] | null = null

function pythonFromConfig(value: unknown): string[] | null {
  if (Array.isArray(value)) {
    const parts = value.filter((x): x is string => typeof x === 'string' && x.trim() !== '')
    return parts.length > 0 ? parts : null
  }
  if (typeof value !== 'string' || value.trim() === '') return null
  const text = value.trim()
  // A path (which may hold spaces) is one argument; a command like "py -3" is split.
  return /[\\/]/.test(text) ? [text] : text.split(/\s+/)
}

async function python($: EngineInterface): Promise<string[] | null> {
  const configured = pythonFromConfig((await readJson($, cfgPath()))?.python)
  if (configured) return configured
  if (PY) return PY
  for (const candidate of [['python3'], ['python'], ['py', '-3']]) {
    try {
      const r = await $.process.run([...candidate, '--version'], { timeoutMs: 15000 })
      if (r.exitCode === 0 && /Python 3\./.test(`${r.stdout} ${r.stderr}`)) {
        PY = candidate
        return candidate
      }
    } catch {
      // Not installed under that name.
    }
  }
  return null
}

type FuResult = {
  ok: boolean
  error?: string
  problems?: string[]
  warnings?: string[]
  [key: string]: unknown
}

function parseJson(stdout: string): FuResult | null {
  const text = stdout.trim()
  const tries = [text, text.split('\n').filter(l => l.trim() !== '').pop() ?? '']
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start >= 0 && end > start) tries.push(text.slice(start, end + 1))
  for (const t of tries) {
    try {
      const out: unknown = JSON.parse(t)
      if (out && typeof out === 'object' && 'ok' in out) return out as FuResult
    } catch {
      // Try the next form.
    }
  }
  return null
}

const lastLine = (text: string) =>
  text
    .trim()
    .split('\n')
    .filter(l => l.trim() !== '')
    .pop()
    ?.slice(0, 160) ?? ''

/**
 * A flag and its value as argv, safe when the value starts with "-":
 * argparse would read such a value as another flag.
 */
const flag = (name: string, value: string): string[] =>
  value.startsWith('-') ? [`${name}=${value}`] : [name, value]

type FuCall = { args: string[]; /** A last positional that may start with "-": passed after `--`. */ tail?: string }

async function fuNow($: EngineInterface, call: FuCall): Promise<FuResult | null> {
  await ws($)
  const label = call.args[0] ?? 'command'
  const py = await python($)
  if (!py) {
    toast($, 'Follow-ups: Python 3 was not found. Install it from python.org, or set "python" in followup_mod/config.json.')
    return null
  }
  const argv = [...py, fuPath(), ...call.args]
  if (call.tail === undefined) argv.push('--ws', WS)
  else if (call.tail.startsWith('-')) argv.push('--ws', WS, '--', call.tail)
  else argv.push(call.tail, '--ws', WS)

  let res
  try {
    res = await $.process.run(argv, { cwd: WS, timeoutMs: 180000 })
  } catch (err) {
    toast($, `Follow-ups: ${label} could not run (${String((err as Error)?.message ?? err).slice(0, 120)})`)
    return null
  }
  const out = parseJson(res.stdout)
  await refreshPane($, true).catch(() => undefined)
  if (!out) {
    const why = lastLine(res.stderr)
    toast($, `Follow-ups: ${label} failed${why ? ` — ${why}` : ''}`)
    return null
  }
  if (!out.ok) {
    const why = out.error ?? (Array.isArray(out.problems) ? out.problems[0] : undefined) ?? 'unknown error'
    toast($, `Follow-ups: ${label} failed — ${String(why).slice(0, 160)}`)
  }
  return out
}

// Engine calls run one at a time, in the order pressed.
let queue: Promise<unknown> = Promise.resolve()
let pending = 0

async function setBusy($: EngineInterface, value: boolean): Promise<void> {
  try {
    if ((await read($, busy)) !== value) await update($, busy, () => value)
  } catch {
    // Display only.
  }
}

async function fu($: EngineInterface, args: string[], tail?: string): Promise<FuResult | null> {
  pending++
  if (pending === 1) await setBusy($, true)
  const run = queue.then(() => fuNow($, { args, tail }))
  queue = run.catch(() => undefined)
  try {
    return await run
  } catch {
    return null
  } finally {
    pending--
    if (pending === 0) await setBusy($, false)
  }
}

// ── Engine install ─────────────────────────────────────────────────────────

const TEXT_EXT = /\.(md|py|json)$/i
// Owner-owned files: an install or update never overwrites them.
const isUserOwned = (rel: string) =>
  rel === 'config.json' || rel === 'VOICE.md' || rel === 'learned' || rel.startsWith('learned/')

async function copyTree($: EngineInterface, from: string, to: string, rel: string): Promise<number> {
  let copied = 0
  const entries = await $.fs.list(from).catch(() => [])
  for (const entry of entries) {
    if (entry.isLink) continue
    const r = rel ? `${rel}/${entry.name}` : entry.name
    if (entry.kind === 'dir') {
      if (r === 'tests' || entry.name === '__pycache__' || entry.name.startsWith('.')) continue
      copied += await copyTree($, `${from}/${entry.name}`, `${to}/${entry.name}`, r)
      continue
    }
    if (entry.kind !== 'file' || !TEXT_EXT.test(entry.name)) continue
    const dest = `${to}/${entry.name}`
    if (isUserOwned(r) && (await exists($, dest))) continue
    await $.fs.write(dest, await $.fs.read(`${from}/${entry.name}`))
    copied++
  }
  return copied
}

function demoConfig(template: Record<string, unknown>): Record<string, unknown> {
  const rules = template.rules && typeof template.rules === 'object' ? (template.rules as Record<string, unknown>) : {}
  return {
    ...template,
    business_name: DEMO_NAME,
    sender_name: DEMO_NAME,
    owner_emails: [DEMO_EMAIL],
    rules: { ...rules, max_per_run: 8 },
  }
}

/**
 * Copies the plugin's engine/ into <WS>/followup_mod/ (instructions, scripts,
 * templates; never engine/tests/), then creates config.json and VOICE.md from
 * the templates when they are missing. Returns null when the plugin holds no
 * engine to install.
 */
async function installEngine($: EngineInterface, isDemo = false): Promise<{ copied: number } | null> {
  await ws($)
  const src = `${$.plugin.root}/engine`
  const copied = await copyTree($, src, modDir(), '')
  if (!(await exists($, fuPath()))) {
    toast($, 'Follow-ups: the engine files are missing from the plugin. Reinstall the plugin.')
    return null
  }
  if (!(await exists($, cfgPath()))) {
    const template =
      (await readJson($, `${modDir()}/templates/config.example.json`)) ??
      (await readJson($, `${src}/templates/config.example.json`)) ??
      {}
    const cfg = isDemo ? demoConfig(template) : template
    await $.fs.write(cfgPath(), JSON.stringify(cfg, null, 2) + '\n')
  }
  if (!(await exists($, `${modDir()}/VOICE.md`))) {
    const voice = await $.fs
      .read(`${modDir()}/templates/VOICE.template.md`)
      .catch(() => $.fs.read(`${src}/templates/VOICE.template.md`))
      .catch(() => '')
    if (voice) await $.fs.write(`${modDir()}/VOICE.md`, voice)
  }
  return { copied }
}

// ── Prompts ────────────────────────────────────────────────────────────────

const runPrompt = () =>
  `Run the follow-up check (Follow-ups pane).\n\nFollow ${WS}/followup_mod/RUN.md exactly. Workspace: ${WS}`
const draftPrompt = () =>
  `Draft the follow-ups waiting in the Follow-ups pane (new ones and any I asked to change).\n\nFollow ${WS}/followup_mod/DRAFT.md exactly. Workspace: ${WS}`
const stagePrompt = () =>
  `Put my approved follow-ups into Gmail as drafts. I pressed "Put in Gmail" in the Follow-ups pane: that is approval to create drafts only, not to send.\n\nFollow ${WS}/followup_mod/STAGE.md exactly. Workspace: ${WS}`
const checkPrompt = () =>
  `Check which follow-ups I sent and which got replies.\n\nFollow ${WS}/followup_mod/CHECK.md exactly. Workspace: ${WS}`
const connectPrompt = (qb: QuickBooksStatus | null) =>
  `Connect QuickBooks for follow-ups, read-only.\n\nFollow ${WS}/followup_mod/CONNECT.md exactly. Workspace: ${WS}\n` +
  (qb?.connected
    ? `The mod sees a QuickBooks connector: ${plural(qb.tools, 'tool')}, of which ${qb.hidden} that could change QuickBooks are hidden from you in this folder.`
    : 'The mod sees no QuickBooks connector in this session yet.')
const setupPrompt = () =>
  `Set up my follow-up workspace.\n\nFollow ${WS}/followup_mod/SETUP.md exactly. Workspace: ${WS}. Ask me the questions in small batches; use only what I tell you.`

// Submits from a timer: a hook can't submit a prompt while it holds the turn.
function submitSoon($: EngineInterface, text: string): void {
  $.clock.after(50, () => {
    $.prompt.submit({ text, asUser: true }).catch(() => undefined)
  })
}

// Several "Ask for a change" within 2 s give one DRAFT prompt.
let draftTimer: { cancel: () => void } | null = null
function draftSoon($: EngineInterface): void {
  draftTimer?.cancel()
  draftTimer = $.clock.after(DRAFT_DEBOUNCE_MS, () => {
    draftTimer = null
    $.prompt.submit({ text: draftPrompt(), asUser: true }).catch(() => undefined)
  })
}

// ── Flows shared by the command and the pane ───────────────────────────────

async function startSetup($: EngineInterface): Promise<string> {
  const installed = await installEngine($)
  if (!installed) return 'The engine files are missing from the plugin; reinstall it.'
  await fu($, ['init'])
  await checkConfigured($)
  await refreshPane($, true)
  startPolling($)
  await openPane($)
  submitSoon($, setupPrompt())
  return `Workspace: ${WS}\nCopied ${installed.copied} engine files into ${modDir()}/. Claude will now ask you a few questions to set it up.`
}

async function startConnect($: EngineInterface): Promise<string> {
  // Set the folder up first, so the guard is on before any QuickBooks tool runs.
  if (!(await checkConfigured($))) {
    const installed = await installEngine($)
    if (!installed) return 'The engine files are missing from the plugin; reinstall it.'
    await fu($, ['init'])
    await checkConfigured($)
    await refreshPane($, true)
  } else {
    // CONNECT.md may be newer than this folder's copy.
    await installEngine($)
  }
  startPolling($)
  await openPane($)
  const qb = await refreshQuickBooks($)
  submitSoon($, connectPrompt(qb))
  return qb?.connected
    ? `QuickBooks is connected. In this folder Claude sees only its read tools (${plural(qb.hidden, 'tool')} that could change QuickBooks are hidden). Claude will now test one read.`
    : 'QuickBooks is not connected yet. Claude will walk you through connecting it; you sign in on Intuit\'s own page.'
}

async function startDemo($: EngineInterface, isInstant: boolean): Promise<string> {
  await ws($)
  // Never load the fictional dataset into a real business's workspace.
  const name = String((await readJson($, cfgPath()))?.business_name ?? '').trim()
  if (name && name !== DEMO_NAME && (await exists($, `${WS}/followup.db`))) {
    return `This folder is ${name}'s workspace. Run the demo in an empty folder instead.`
  }
  const installed = await installEngine($, true)
  if (!installed) return 'The engine files are missing from the plugin; reinstall it.'
  // A demo folder starts clean each time, so the demo can be shown again and
  // again (open follow-ups from the last showing would block new ones). Only
  // the fictional bakery's own database is ever reset here.
  if (name === DEMO_NAME && (await exists($, `${WS}/followup.db`))) await fu($, ['reset', '--yes'])
  await fu($, ['init'])
  const out = await fu($, isInstant ? ['demo', '--with-drafts'] : ['demo'])
  await checkConfigured($)
  await refreshPane($, true)
  startPolling($)
  await openPane($)
  if (!out?.ok) return 'The demo data could not be loaded; see the message in the pane.'
  if (isInstant) return `Demo loaded: ${DEMO_NAME}, with drafts ready to review in the Follow-ups pane.`
  submitSoon($, draftPrompt())
  return `Demo loaded: ${DEMO_NAME}. Claude will now draft the follow-ups; watch them appear in the Follow-ups pane.`
}

async function promptIfReady($: EngineInterface, text: string): Promise<boolean> {
  if (!(await checkConfigured($))) {
    toast($, 'Follow-ups is not set up here yet: run /followup setup or /followup demo.')
    return false
  }
  startPolling($)
  submitSoon($, text)
  return true
}

// ── Opening the spreadsheet and the dashboard ──────────────────────────────

async function openPath($: EngineInterface, path: string): Promise<void> {
  let platform: string | undefined
  try {
    platform = (await $.session.info()).platform
  } catch {
    platform = undefined
  }
  const tries: string[][] =
    platform === 'windows'
      ? [['cmd', '/c', 'start', '', path]]
      : platform === 'macos'
        ? [['open', path]]
        : platform === 'linux' || platform === 'wsl'
          ? [['xdg-open', path]]
          : [['open', path], ['cmd', '/c', 'start', '', path], ['xdg-open', path]]
  for (const argv of tries) {
    try {
      const r = await $.process.run(argv, { timeoutMs: 15000 })
      if (r.exitCode === 0) return
    } catch {
      // Try the next opener.
    }
  }
  toast($, `Follow-ups: could not open ${path}`)
}

function exportPath(out: FuResult | null, kind: 'xlsx' | 'html'): string | undefined {
  if (!out) return undefined
  const files = (out.files ?? out.paths) as Record<string, unknown> | undefined
  const value = out[kind] ?? files?.[kind]
  return typeof value === 'string' && value ? value : undefined
}

async function openExport($: EngineInterface, kind: 'xlsx' | 'html', isFresh = false): Promise<string | undefined> {
  await ws($)
  const pane = await read($, paneData)
  let path = pane?.files?.[kind] || `${WS}/${kind === 'xlsx' ? 'Follow-ups.xlsx' : 'Follow-ups.html'}`
  if (isFresh || !(await exists($, path))) {
    const out = await fu($, ['export'])
    if (!out?.ok) return undefined
    for (const w of out.warnings ?? []) toast($, `Follow-ups: ${w}`)
    path = exportPath(out, kind) ?? path
  }
  await openPath($, path)
  return path
}

// ── Formatting ─────────────────────────────────────────────────────────────

function money(n: unknown): string {
  const v = typeof n === 'number' && Number.isFinite(n) ? n : Number(n) || 0
  const [whole = '0', cents = '00'] = Math.abs(v).toFixed(2).split('.')
  return `${v < 0 ? '-' : ''}$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${cents}`
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function when(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${p(d.getHours())}:${p(d.getMinutes())}`
}

const isoDay = (iso?: string | null) => (iso ? iso.slice(0, 10) : '')

function localToday(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** `base` (YYYY-MM-DD) plus 1w, 2w, 1m or 3m, as YYYY-MM-DD. */
function addPeriod(base: string, period: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(base) ?? /^(\d{4})-(\d{2})-(\d{2})/.exec(localToday())
  const y = Number(m?.[1])
  const mo = Number(m?.[2]) - 1
  const d = Number(m?.[3])
  let t: number
  if (period.endsWith('w')) {
    t = Date.UTC(y, mo, d + 7 * (Number.parseInt(period, 10) || 1))
  } else {
    const months = Number.parseInt(period, 10) || 1
    const first = new Date(Date.UTC(y, mo + months, 1))
    const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate()
    t = Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), Math.min(d, last))
  }
  return new Date(t).toISOString().slice(0, 10)
}

const SNOOZE = [
  { value: '1w', label: '1 week' },
  { value: '2w', label: '2 weeks' },
  { value: '1m', label: '1 month' },
] as const
const LATER = [
  { value: '2w', label: '2 weeks' },
  { value: '1m', label: '1 month' },
  { value: '3m', label: '3 months' },
] as const
const periodLabel = (v: string) => [...SNOOZE, ...LATER].find(o => o.value === v)?.label ?? v

const cell = (v: unknown) =>
  String(v ?? '')
    .replace(/\r?\n/g, ' ')
    .replace(/\|/g, '\\|')
    .trim() || '—'
const mdUrl = (url: string) => url.replace(/ /g, '%20').replace(/\(/g, '%28').replace(/\)/g, '%29').replace(/\|/g, '%7C')

function table(head: string[], rows: string[][], cap: number): string {
  const lines = [`| ${head.join(' | ')} |`, `|${head.map(() => '---').join('|')}|`]
  for (const r of rows.slice(0, cap)) lines.push(`| ${r.join(' | ')} |`)
  if (rows.length > cap) lines.push('', `…and ${rows.length - cap} more in the spreadsheet.`)
  return lines.join('\n')
}

function pipelineMarkdown(list: readonly FollowupView[]): string {
  if (list.length === 0) return '_No open follow-ups._'
  return table(
    ['Customer', 'Follow-up', 'State', 'Updated'],
    list.map(f => [cell(f.customer_name), cell(f.kind_label), cell(f.state_label), cell(isoDay(f.updated_at))]),
    200,
  )
}

function sentMarkdown(list: readonly FollowupView[]): string {
  if (list.length === 0) return '_Nothing sent yet._'
  return table(
    ['Customer', 'Subject', 'State', 'Date', 'Gmail'],
    list.map(f => {
      const url = f.state === 'staged' ? (f.gmail_draft_url ?? f.thread_url) : (f.thread_url ?? f.gmail_draft_url)
      const linkText = f.state === 'staged' ? 'draft' : 'thread'
      return [
        cell(f.customer_name),
        cell(f.draft_subject),
        cell(f.state_label),
        cell(isoDay(f.replied_at ?? f.sent_at ?? f.staged_at ?? f.updated_at)),
        url ? `[${linkText}](${mdUrl(url)})` : '—',
      ]
    }),
    100,
  )
}

function customersMarkdown(list: readonly CustomerView[]): string {
  if (list.length === 0) return '_No customers yet._'
  return table(
    ['Customer', 'Email', 'Phone', 'Open quotes', 'Overdue', 'Last contact', 'Sent', 'Last outcome'],
    list.map(c => [
      cell(`${c.company && c.company !== c.name ? `${c.company} (${c.name})` : c.name}${c.do_not_contact ? ' 🚫' : ''}`),
      cell(c.email),
      cell(c.phone),
      c.open_estimates ? cell(`${c.open_estimates} · ${money(c.open_estimate_amount)}`) : '—',
      c.overdue_balance ? cell(money(c.overdue_balance)) : '—',
      cell(isoDay(c.last_contact_at)),
      cell(c.followups_sent ?? 0),
      cell(c.last_outcome),
    ]),
    300,
  )
}

const isResolved = (i: NeedItem): boolean => (i.type === 'task' ? !!i.done : !!(i.answer && String(i.answer).trim()))
const allResolved = (f: FollowupView): boolean => (f.items?.length ?? 0) > 0 && (f.items ?? []).every(isResolved)
const isSuspicious = (problem: string) => /suspicious/i.test(problem)

// ── The guard ──────────────────────────────────────────────────────────────
// Active only in a configured workspace. Built-in tools are untouched.

const ALWAYS_ALLOW = new Set([
  'create_draft',
  'update_draft',
  'list_drafts',
  'get_draft',
  'create_label',
  'list_labels',
  'label_thread',
  'label_message',
  'search_threads',
  'get_thread',
  'get_message',
  // The browser opening and closing its own tab (QuickBooks reports, path B).
  'tabs_create',
  'tabs_create_mcp',
  'tabs_close',
  'tabs_close_mcp',
])
// The desktop app's own tools (task list, sessions, panes) reach neither mail nor QuickBooks.
const isHostServer = (server: string) => server.startsWith('ccd_')
// Any word here in a tool's name blocks it. QuickBooks connectors name their
// changes with these verbs (`invoices_create`, `quickbooks-transaction-import`,
// `book_bank_transaction`, `batch_entity_operations` ...).
const DENY_TOKENS = new Set([
  'send', 'reply', 'forward', 'trash', 'delete', 'spam', 'create', 'update', 'void', 'import', 'upload',
  'categorize', 'reconcile', 'approve', 'book', 'apply', 'remove', 'write', 'post', 'pay', 'refund',
  'transfer', 'receive', 'edit', 'set',
  'add', 'save', 'submit', 'record', 'match', 'unmatch', 'merge', 'sync', 'archive', 'batch', 'bulk',
  'assign', 'cancel', 'close', 'deposit', 'charge', 'move', 'duplicate', 'attach', 'issue', 'schedule',
  'undo', 'restore', 'exclude', 'classify', 'reclassify', 'link', 'unlink', 'mark', 'share', 'invite',
  'activate', 'deactivate', 'enable', 'disable', 'convert', 'adjust', 'accept', 'reject', 'finalize',
  'publish', 'modify', 'change', 'replace', 'insert', 'put', 'patch',
  'begin', 'finish', 'start', 'complete', 'select', 'connect', 'disconnect', 'consent',
  // Whole areas a follow-up never needs (QuickBooks Labs): kept out of reach.
  'reconciliation', 'payroll', 'lending', 'bank', 'tax',
])
// Browser tools that could type into a QuickBooks form or run code in its page.
const DENY_NAMES = new Set(['form_input', 'javascript_tool', 'javascript_exec', 'file_upload', 'upload_image'])
const DENY_TEXT =
  'Follow-up mod: sending email and changing QuickBooks are blocked in this workspace. Drafts only — the owner presses Send in Gmail.'

/**
 * The MCP tool's own name split into lowercase words: `mcp__gmail__sendMessage`
 * → send, message. The bare name is everything after `mcp__<server>__`, which is
 * the part after the last `__` for every ordinary name, and also catches a name
 * that itself holds `__` (`mcp__srv__send__now`).
 */
function toolTokens(tool: string): string[] {
  const bare = tool.split('__').slice(2).join('_')
  return bare
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
    .toLowerCase()
    .split(/[_\-]+/)
    .filter(t => t !== '')
}

function isBlockedMcpTool(tool: string): boolean {
  if (!tool.startsWith('mcp__') || isHostServer(tool.split('__')[1] ?? '')) return false
  const tokens = toolTokens(tool)
  const bare = tokens.join('_')
  if (ALWAYS_ALLOW.has(bare)) return false
  return DENY_NAMES.has(bare) || tokens.some(t => DENY_TOKENS.has(t))
}

// ── QuickBooks: what the session's tools show ─────────────────────────────
// Connector servers have opaque names, so a server counts as QuickBooks when
// one of its tools names or describes QuickBooks.

const QB_RE = /quick\s*books|\bqbo\b/i
const QB_POLLS = 10 // every 10th poll, about 30 s

const serverOf = (tool: string) => tool.split('__')[1] ?? ''

type ListedTool = { name: string; description: string; mcp: boolean }
// What the tool.list hook last took out: a mod's own $.tool.list() may not show it.
let hiddenByHook: ListedTool[] = []

async function refreshQuickBooks($: EngineInterface): Promise<QuickBooksStatus | null> {
  try {
    return await lookForQuickBooks($)
  } catch {
    return null
  }
}

async function lookForQuickBooks($: EngineInterface): Promise<QuickBooksStatus | null> {
  let list: ListedTool[]
  try {
    list = await $.tool.list()
  } catch {
    return read($, qbStatus)
  }
  const seen = new Set(list.map(t => t.name))
  const all = [...list, ...hiddenByHook.filter(t => !seen.has(t.name))]
  const mcp = all.filter(t => t.name.startsWith('mcp__'))
  const servers = new Set(mcp.filter(t => QB_RE.test(t.name) || QB_RE.test(t.description ?? '')).map(t => serverOf(t.name)))
  const tools = mcp.filter(t => servers.has(serverOf(t.name)))
  const status: QuickBooksStatus = {
    connected: tools.length > 0,
    tools: tools.length,
    hidden: tools.filter(t => isBlockedMcpTool(t.name)).length,
  }
  const before = await read($, qbStatus)
  if (JSON.stringify(before) !== JSON.stringify(status)) await update($, qbStatus, () => status)
  return status
}

function qbLine(qb: QuickBooksStatus | null): string {
  if (!qb) return 'QuickBooks: checking…'
  if (!qb.connected) return 'QuickBooks: not connected'
  return qb.hidden > 0
    ? `QuickBooks: connected, read-only here (${plural(qb.hidden, 'tool')} that could change it hidden)`
    : 'QuickBooks: connected, read-only here'
}

// ── Registration ───────────────────────────────────────────────────────────

const USAGE = [
  'Usage:',
  '/followup — open the Follow-ups pane',
  '/followup setup — set up this folder for your business',
  '/followup connect — connect QuickBooks, read-only, and test it',
  '/followup demo — load the Brightwater Bakery demo and let Claude draft it',
  '/followup demo instant — the demo with ready-made drafts',
  '/followup run — check QuickBooks and mail for follow-ups now',
  '/followup stage — put approved follow-ups into Gmail as drafts',
  '/followup check — check what was sent and who replied',
  '/followup update — refresh the engine files (your settings are kept)',
  '/followup export — refresh and open the spreadsheet',
].join('\n')

const NOT_SET_UP = 'Follow-ups is not set up in this folder yet. Run /followup setup, or /followup demo to try it first.'

export const register: Register = (on, options) => {
  const fixed = String(options?.workspace ?? '').trim()
  if (fixed) setWs(fixed)

  on('session.start', async ($, e, next) => {
    if (!WS) setWs(e.cwd)
    try {
      await $.command.register({
        name: 'followup',
        description:
          'Follow-ups: /followup opens the pane; setup, connect, demo, demo instant, run, stage, check, update, export.',
        argumentHint: '[setup | connect | demo | demo instant | run | stage | check | update | export]',
      })
    } catch {
      // Registered again on the next start.
    }
    try {
      await setBusy($, false)
      // Only a configured workspace gets a pane (and polling) at start.
      if (await checkConfigured($)) {
        await refreshPane($, true)
        startPolling($)
        void openPane($)
        void refreshQuickBooks($)
      }
    } catch {
      // The pane stays closed; /followup opens it.
    }
    return next(e)
  })

  on('command.run', { command: 'followup' }, async ($, e) => {
    await ws($)
    const arg = e.args.trim().toLowerCase().replace(/\s+/g, ' ')

    if (arg === '') {
      const ok = await checkConfigured($)
      await refreshPane($, true)
      startPolling($)
      await openPane($)
      void refreshQuickBooks($)
      return { text: ok ? 'Follow-ups pane opened.' : `Follow-ups pane opened. ${NOT_SET_UP}` }
    }
    if (arg === 'setup') return { text: await startSetup($) }
    if (arg === 'connect' || arg === 'connect quickbooks') return { text: await startConnect($) }
    if (arg === 'demo') return { text: await startDemo($, false) }
    if (arg === 'demo instant') return { text: await startDemo($, true) }
    if (arg === 'run' || arg === 'stage' || arg === 'check') {
      if (!(await checkConfigured($))) return { text: NOT_SET_UP }
      await openPane($)
      const text = arg === 'run' ? runPrompt() : arg === 'stage' ? stagePrompt() : checkPrompt()
      await promptIfReady($, text)
      return {
        text:
          arg === 'run'
            ? 'Starting the follow-up check; results appear in the Follow-ups pane.'
            : arg === 'stage'
              ? 'Putting approved follow-ups into Gmail as drafts (nothing is sent).'
              : 'Checking what was sent and who replied.',
      }
    }
    if (arg === 'update') {
      const installed = await installEngine($)
      if (!installed) return { text: 'The engine files are missing from the plugin; reinstall it.' }
      await checkConfigured($)
      return {
        text: `Updated ${installed.copied} engine files in ${modDir()}/ (config.json, VOICE.md and learned/ were not touched).`,
      }
    }
    if (arg === 'export') {
      if (!(await checkConfigured($))) return { text: NOT_SET_UP }
      const path = await openExport($, 'xlsx', true)
      return { text: path ? `Spreadsheet refreshed and opened: ${path}` : 'The export failed; see the message above.' }
    }
    return { text: `Unknown option: ${arg}\n${USAGE}` }
  })

  // The guard: in a configured workspace, no MCP tool may send mail or change
  // QuickBooks. A guard that fails denies (the .catch), never lets through.
  on('tool.call', async ($, e, next) => {
    const tool = String(e.tool)
    if (!isBlockedMcpTool(tool)) return next(e)
    await ws($)
    let isActive = isConfigured
    if (WS) {
      try {
        isActive = await $.fs.exists(cfgPath())
      } catch {
        isActive = isConfigured
      }
    }
    return isActive ? { deny: DENY_TEXT } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : { deny: DENY_TEXT }))

  // In a configured workspace Claude never even sees the tools that could send
  // mail or change QuickBooks: they are taken out of the session's tool list.
  // The guard above still refuses any call of one.
  on('tool.list', async ($, e, next) => {
    const listed = await next(e)
    if (!isConfigured || listed.value === undefined) {
      hiddenByHook = []
      return listed
    }
    hiddenByHook = listed.value.filter(t => isBlockedMcpTool(t.name))
    return { value: listed.value.filter(t => !isBlockedMcpTool(t.name)) }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input, Select, Markdown, Link } = $.ui.resolve(e)
    const isReady = await read($, ready)
    const pane = await read($, paneData)
    const tab = await read($, view)
    const confirmingId = await read($, confirming)
    const choices = await read($, choice)
    const isBusy = await read($, busy)
    const qb = await read($, qbStatus)

    // ── Not set up: one card, two buttons ──
    if (!isReady) {
      return (
        <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
          <Text bold>Follow-ups</Text>
          <Text>
            {`Follow-ups is not set up in ${WS || 'this folder'} yet: set it up for your business, or try it first on a fictional bakery's data.`}
          </Text>
          <Box marginTop={1}>
            <Button
              key="setup"
              variant="primary"
              label="Set up"
              onPress={async () => toast($, (await startSetup($)).split('\n').pop() ?? '')}
            />
            <Text> </Text>
            <Button key="demo" label="Try the demo" onPress={async () => toast($, await startDemo($, false))} />
          </Box>
        </Box>
      )
    }

    const c = pane?.counts ?? {}
    const n = (v: number | undefined) => v ?? 0
    const countsLine = `${n(c.review)} to review · ${n(c.needs_you)} need you · ${n(c.approved)} approved · ${n(c.staged)} in Gmail · ${n(c.sent)} sent`
    const moneyLine = `Overdue ${money(c.overdue_balance)} across ${plural(n(c.overdue_invoices), 'invoice')} · ${plural(n(c.open_estimates), 'open quote')} (${money(c.open_estimate_amount)})`
    const run = pane?.last_run
    const lastRunLine = run
      ? `Last run ${when(run.started_at)}${run.finished_at ? '' : ' (still running)'} · ${n(run.flagged ?? undefined)} flagged · ${n(run.drafted ?? undefined)} drafted`
      : 'No run yet.'
    const title = pane?.business_name || 'Follow-ups'

    // A minimized pane: a glance only.
    if (e.props.placement === 'widget') {
      return (
        <Box flexDirection="column">
          <Text bold>{title}</Text>
          <Text>{countsLine}</Text>
        </Box>
      )
    }

    const header = (
      <Box flexDirection="column" marginBottom={1}>
        <Text bold>{title}</Text>
        <Text>{countsLine}</Text>
        <Text>{moneyLine}</Text>
        <Text dimColor>{lastRunLine}</Text>
        <Box flexWrap="wrap">
          <Text color={qb?.connected ? 'green' : qb ? 'yellow' : undefined} dimColor={!qb}>
            {qbLine(qb)}
          </Text>
          {qb && !qb.connected && <Text> · </Text>}
          {qb && !qb.connected && (
            <Button key="connect" plain label="Connect QuickBooks" onPress={async () => toast($, await startConnect($))} />
          )}
        </Box>
        {isBusy && <Text color="yellow">Working…</Text>}
        <Box flexWrap="wrap">
          <Button key="run" variant="primary" label="Run now" onPress={() => promptIfReady($, runPrompt())} />
          <Text> </Text>
          <Button key="check" label="Check sent & replies" onPress={() => promptIfReady($, checkPrompt())} />
          <Text> </Text>
          <Button key="open-xlsx" plain label="Open spreadsheet" onPress={() => openExport($, 'xlsx')} />
          <Text> · </Text>
          <Button key="open-html" plain label="Open dashboard" onPress={() => openExport($, 'html')} />
        </Box>
      </Box>
    )

    const todayItems = pane?.today_items ?? []
    const TABS: readonly (readonly [FollowupTab, string])[] = [
      ['today', `Today (${todayItems.length})`],
      ['pipeline', 'Pipeline'],
      ['sent', 'Sent'],
      ['customers', 'Customers'],
    ]
    const tabs = (
      <Box marginBottom={1} flexWrap="wrap">
        {TABS.map(([v, label]) => (
          <Button
            key={`tab-${v}`}
            plain
            variant={tab === v ? 'primary' : undefined}
            label={tab === v ? `[ ${label} ]` : `  ${label}  `}
            onPress={() => update($, view, () => v)}
          />
        ))}
      </Box>
    )

    // ── Actions ──
    const outcome = (f: FollowupView, value: string, date?: string) =>
      fu($, date ? ['outcome', f.id, value, '--date', date] : ['outcome', f.id, value])
    const clearChoice = (key: string) =>
      update($, choice, m => {
        const next = { ...m }
        delete next[key]
        return next
      })
    const baseDay = pane?.today || localToday()

    const needItemView = (f: FollowupView, item: NeedItem) => {
      const done = isResolved(item)
      if (item.type === 'task') {
        return (
          <Box key={`item-${f.id}-${item.id}`} flexWrap="wrap">
            <Text color={done ? 'green' : undefined}>{`${done ? '☑' : '☐'} ${item.text} `}</Text>
            {!done && (
              <Button
                key={`task-${f.id}-${item.id}`}
                plain
                variant="primary"
                label="Done"
                onPress={() => fu($, ['answer', f.id, item.id], 'done')}
              />
            )}
          </Box>
        )
      }
      return (
        <Box key={`item-${f.id}-${item.id}`} flexDirection="column">
          <Text color={done ? 'green' : 'yellow'}>{`${done ? '✔' : '❓'} ${item.text}`}</Text>
          {item.options && item.options.length > 0 ? (
            <Box flexWrap="wrap">
              {item.options.map((o, i) => (
                <Button
                  key={`ans-${f.id}-${item.id}-${i}`}
                  plain
                  variant={item.answer === o ? 'primary' : undefined}
                  label={item.answer === o ? `● ${o}` : `○ ${o}`}
                  onPress={() => fu($, ['answer', f.id, item.id], o)}
                />
              ))}
            </Box>
          ) : (
            <Input
              key={`ans-${f.id}-${item.id}`}
              label="Answer"
              value={item.answer ?? ''}
              placeholder="Type your answer and press Enter"
              submitLabel="save"
              onSubmit={value => {
                if (value.trim()) void fu($, ['answer', f.id, item.id], value.trim())
              }}
            />
          )}
        </Box>
      )
    }

    const outcomeButtons = (f: FollowupView) => {
      const isPickingLater = choices[f.id] === 'later'
      const wonValue = f.kind === 'invoice_overdue' ? 'paid' : 'won'
      return (
        <Box flexDirection="column">
          <Box flexWrap="wrap">
            <Button key={`out-${f.id}-interested`} label="Interested" onPress={() => outcome(f, 'interested')} />
            <Text> </Text>
            <Button
              key={`out-${f.id}-later`}
              label="Later…"
              onPress={() => update($, choice, m => ({ ...m, [f.id]: 'later' }))}
            />
            <Text> </Text>
            <Button key={`out-${f.id}-not_interested`} label="Not interested" onPress={() => outcome(f, 'not_interested')} />
            <Text> </Text>
            <Button key={`out-${f.id}-won`} label="Won / paid" onPress={() => outcome(f, wonValue)} />
            <Text> </Text>
            <Button key={`out-${f.id}-no_reply`} label="No real answer" onPress={() => outcome(f, 'no_reply')} />
          </Box>
          {isPickingLater && (
            <Box flexWrap="wrap">
              <Select
                key={`later-${f.id}`}
                label="Follow up again in"
                options={LATER}
                onSelect={async value => {
                  const date = addPeriod(baseDay, value)
                  const out = await outcome(f, 'later', date)
                  await clearChoice(f.id)
                  if (out?.ok) toast($, `${f.customer_name}: follow up again on ${date}`)
                }}
              />
              <Button key={`later-cancel-${f.id}`} plain label="cancel" onPress={() => clearChoice(f.id)} />
            </Box>
          )}
        </Box>
      )
    }

    const draftedView = (f: FollowupView) => {
      const isConfirming = confirmingId === f.id
      const snoozed = choices[`snooze:${f.id}`]
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text dimColor>{`To: ${f.draft_to || '(none)'} · Subject: ${f.draft_subject || '(none)'}`}</Text>
          <Box marginTop={1} marginBottom={1} paddingX={1} borderStyle="single" borderDimColor>
            <Text wrap="wrap">{f.draft_body || ''}</Text>
          </Box>
          <Box flexWrap="wrap">
            <Button key={`approve-${f.id}`} variant="primary" label="Approve" onPress={() => fu($, ['approve', f.id])} />
            <Text> </Text>
            <Button key={`skip-${f.id}`} label="Skip" onPress={() => fu($, ['skip', f.id])} />
            <Text> </Text>
            {!isConfirming && (
              <Button
                key={`dnc-${f.id}`}
                plain
                dimColor
                label="Do not contact"
                onPress={() => update($, confirming, () => f.id)}
              />
            )}
          </Box>
          {isConfirming && (
            <Box flexDirection="column">
              <Text color="red">
                {`Never contact ${f.customer_name} again? This closes the follow-up and marks the customer do-not-contact.`}
              </Text>
              <Box>
                <Button
                  key={`dnc-confirm-${f.id}`}
                  label="Yes, do not contact"
                  onPress={async () => {
                    await update($, confirming, () => null)
                    await fu($, ['dnc', f.id])
                  }}
                />
                <Text> </Text>
                <Button key={`dnc-cancel-${f.id}`} label="Cancel" onPress={() => update($, confirming, () => null)} />
              </Box>
            </Box>
          )}
          <Select
            key={`snooze-${f.id}`}
            label="Snooze"
            options={SNOOZE}
            value={snoozed}
            onSelect={async value => {
              await update($, choice, m => ({ ...m, [`snooze:${f.id}`]: value }))
              const date = addPeriod(baseDay, value)
              const out = await fu($, ['snooze', f.id, date])
              await clearChoice(`snooze:${f.id}`)
              if (out?.ok) toast($, `${f.customer_name}: snoozed ${periodLabel(value)}, until ${date}`)
            }}
          />
          <Input
            key={`change-${f.id}`}
            label="Ask for a change"
            placeholder="e.g. shorter, friendlier, mention the holiday order"
            submitLabel="send to Claude"
            onSubmit={async value => {
              const text = value.trim()
              if (!text) return
              const out = await fu($, ['revise', f.id, ...flag('--instruction', text)])
              if (out?.ok) {
                toast($, 'Change noted: Claude will redraft it.')
                draftSoon($)
              }
            }}
          />
        </Box>
      )
    }

    const card = (f: FollowupView) => {
      const problems = f.problems ?? []
      const hasSuspicious = problems.some(isSuspicious)
      const border =
        hasSuspicious ? 'red' : f.state === 'needs_you' ? 'yellow' : f.state === 'drafted' ? 'blue' : f.state === 'replied' ? 'green' : 'gray'
      const answered = allResolved(f)
      return (
        <Box key={`card-${f.id}`} flexDirection="column" marginBottom={1} borderStyle="round" borderColor={border} paddingX={1}>
          <Text bold>
            {`${f.customer_name} — ${f.kind_label}  `}
            <Text color={f.state === 'needs_you' ? 'yellow' : undefined}>{f.state_label}</Text>
          </Text>
          {f.reason ? <Text>{f.reason}</Text> : null}
          {(f.facts ?? []).map(fact => (
            <Text dimColor>{`• ${fact}`}</Text>
          ))}
          {problems.map(p => (
            <Text color={isSuspicious(p) ? 'red' : 'yellow'}>{`⚠ ${p}`}</Text>
          ))}
          {f.state === 'drafted' && draftedView(f)}
          {f.state === 'needs_you' && (
            <Box flexDirection="column" marginTop={1}>
              {(f.items ?? []).map(item => needItemView(f, item))}
              {answered && (
                <Button key={`redraft-${f.id}`} variant="primary" label="Done — redraft" onPress={() => submitSoon($, draftPrompt())} />
              )}
            </Box>
          )}
          {f.state === 'replied' && (
            <Box flexDirection="column" marginTop={1}>
              {f.reply_snippet ? <Text italic>{`“${f.reply_snippet}”`}</Text> : null}
              {outcomeButtons(f)}
            </Box>
          )}
          {f.state === 'call' && (
            <Box flexDirection="column" marginTop={1}>
              <Text bold>{f.phone ? `📞 ${f.phone}` : 'No phone number on file.'}</Text>
              {outcomeButtons(f)}
              <Box>
                <Button key={`skip-${f.id}`} plain label="Skip" onPress={() => fu($, ['skip', f.id])} />
              </Box>
            </Box>
          )}
          {(f.state === 'found' || f.state === 'revise') &&
            (f.state === 'revise' && answered ? (
              <Box flexDirection="column">
                <Text dimColor>Answers saved.</Text>
                <Button key={`redraft-${f.id}`} variant="primary" label="Done — redraft" onPress={() => submitSoon($, draftPrompt())} />
              </Box>
            ) : (
              <Text dimColor>Claude is drafting…</Text>
            ))}
        </Box>
      )
    }

    // ── Tabs ──
    let body
    if (tab === 'pipeline') {
      body = <Markdown key="pipeline-table" text={pipelineMarkdown(pane?.pipeline ?? [])} />
    } else if (tab === 'sent') {
      const staged = (pane?.sent ?? []).filter(f => f.state === 'staged' && f.gmail_draft_url)
      body = (
        <Box flexDirection="column">
          {staged.length > 0 && (
            <Box flexDirection="column" marginBottom={1}>
              <Text bold>In Gmail, waiting for you to press Send:</Text>
              {staged.map(f => (
                <Link href={f.gmail_draft_url ?? ''} label={`${f.customer_name} — ${f.draft_subject || 'draft'}`} />
              ))}
            </Box>
          )}
          <Markdown key="sent-table" text={sentMarkdown(pane?.sent ?? [])} />
        </Box>
      )
    } else if (tab === 'customers') {
      body = <Markdown key="customers-table" text={customersMarkdown(pane?.customers ?? [])} />
    } else {
      const approved = todayItems.filter(f => f.state === 'approved')
      const drafted = todayItems.filter(f => f.state === 'drafted')
      const rest = todayItems.filter(f => f.state !== 'approved')
      body = (
        <Box flexDirection="column">
          {approved.length > 0 && (
            <Box flexDirection="column" marginBottom={1} borderStyle="round" borderColor="green" paddingX={1}>
              <Button
                key="stage"
                variant="primary"
                label={`Put ${plural(approved.length, 'approved draft')} in Gmail`}
                onPress={() => promptIfReady($, stagePrompt())}
              />
              {approved.map(f => (
                <Text dimColor>{`✅ ${f.customer_name} — ${f.kind_label}${f.draft_subject ? `: ${f.draft_subject}` : ''}`}</Text>
              ))}
            </Box>
          )}
          {drafted.length > 0 && (
            <Box marginBottom={1}>
              <Button
                key="approve-all"
                label={`Approve all ready (${drafted.length})`}
                onPress={() => fu($, ['approve', '--all-drafted'])}
              />
            </Box>
          )}
          {todayItems.length === 0 && (
            <Text dimColor>{pane ? 'Nothing waiting on you. Press Run now to check QuickBooks and mail.' : 'No data yet. Press Run now to check QuickBooks and mail.'}</Text>
          )}
          {rest.map(card)}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {header}
        {tabs}
        {body}
      </Box>
    )
  })
}
