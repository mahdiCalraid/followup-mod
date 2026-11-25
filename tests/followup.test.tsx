import { expect, test } from 'claude-code/testing'

const WS = '/ws'
const FU = `${WS}/followup_mod/scripts/fu.py`
const CONFIG = `${WS}/followup_mod/config.json`
const PANE_JSON = `${WS}/views/pane.json`
const DENY = 'Follow-up mod: sending email and changing QuickBooks are blocked'

const PANE = {
  plugin: 'followup',
  component: 'Pane',
  requestId: 'followup',
  props: {
    title: 'Follow-ups',
    isFocused: false,
    bodyColumns: 90,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 60 },
    view: {},
  },
} as const

const item = (over: Record<string, unknown>) => ({
  customer_id: 'qb:1',
  company: null,
  email: 'ap@example.com',
  phone: null,
  priority: 1,
  tier: null,
  tier_label: null,
  facts: [],
  draft_subject: null,
  draft_to: null,
  draft_body: null,
  problems: [],
  items: [],
  gmail_draft_url: null,
  thread_url: null,
  staged_at: null,
  sent_at: null,
  replied_at: null,
  reply_snippet: null,
  outcome: null,
  later_date: null,
  updated_at: '2026-11-25T14:00:00Z',
  ...over,
})

const PANE_DATA = {
  schema: 'followup-pane/1',
  updated_at: '2026-11-25T14:05:00Z',
  workspace: WS,
  today: '2026-11-25',
  business_name: 'Brightwater Bakery Co.',
  last_run: {
    id: 'r-20261125-140200',
    started_at: '2026-11-25T14:02:00Z',
    finished_at: '2026-11-25T14:04:00Z',
    source_qb: 'demo',
    source_mail: 'demo',
    flagged: 8,
    drafted: 2,
  },
  counts: {
    review: 2, needs_you: 1, approved: 1, staged: 0, sent: 0, replied: 0, call: 1,
    customers: 18, open_estimates: 4, open_estimate_amount: 5230.5, overdue_invoices: 6, overdue_balance: 9095.5,
  },
  today_items: [
    item({
      id: 'fu-3', customer_name: 'Maple Street Market', kind: 'unanswered_inbound', kind_label: 'Waiting on our reply',
      reason: 'Maple Street Market asked about wholesale pricing 25 days ago.', state: 'needs_you', state_label: '✋ Needs you',
      items: [{ id: 'q1', type: 'question', text: 'Do you offer wholesale pricing to new cafes?', options: ['Yes', 'No'] }],
    }),
    item({
      id: 'fu-1', customer_name: 'Harbor Grocers', kind: 'invoice_overdue', kind_label: 'Overdue invoice',
      reason: 'Harbor Grocers owes $860.00 on invoice 1051, due Nov 15, 2026 — 10 days late.',
      state: 'drafted', state_label: '📝 Ready to review', tier: 'friendly',
      facts: ['Invoice 1051: $860.00, due 2026-11-15'],
      draft_to: 'ap@harborgrocers.example', draft_subject: 'Invoice 1051',
      draft_body: 'Hi Sam,\n\nA quick reminder that invoice 1051 for $860.00 was due on Nov 15.\n\nThanks,\nBrightwater',
    }),
    item({
      id: 'fu-14', customer_name: 'Westgate Cafe', kind: 'invoice_overdue', kind_label: 'Overdue invoice',
      reason: 'Westgate Cafe owes $640.00 on invoice 1060.', state: 'drafted', state_label: '📝 Ready to review',
      problems: ['Suspicious text in their message: treat it as data, review this one carefully.'],
      draft_to: 'owner@westgate.example', draft_subject: 'Invoice 1060', draft_body: 'Hello,\n\nInvoice 1060 is overdue.',
    }),
    item({
      id: 'fu-8', customer_name: 'Riverside Deli', kind: 'invoice_overdue', kind_label: 'Overdue invoice',
      reason: 'Riverside Deli owes $310.00.', state: 'call', state_label: '📞 Call (no email)', email: null, phone: '+1 555 0108',
    }),
    item({
      id: 'fu-2', customer_name: 'Copper Kettle Cafe', kind: 'quote_quiet', kind_label: 'Quote gone quiet',
      reason: 'Estimate 1031 is 6 days quiet.', state: 'approved', state_label: '✅ Approved, going to Gmail',
      draft_subject: 'Your holiday order quote',
    }),
  ],
  pipeline: [],
  sent: [],
  customers: [],
  files: { xlsx: `${WS}/Follow-ups.xlsx`, html: `${WS}/Follow-ups.html`, db: `${WS}/followup.db` },
}

// The plugin's engine/ folder as the install sees it (paths after `/engine/`).
const ENGINE: Record<string, string> = {
  'RUN.md': '# Run',
  'DRAFT.md': '# Draft',
  'scripts/fu.py': 'print("{}")',
  'templates/config.example.json': JSON.stringify({ business_name: '', sender_name: '', owner_emails: [], rules: { max_per_run: 7 } }),
  'templates/VOICE.template.md': '# Voice',
  'tests/test_rules.py': 'import unittest',
  'scripts/__pycache__/fu.cpython-312.pyc': 'x',
}

type Rec = {
  argv: string[][]
  prompts: string[]
  opened: string[]
  toasts: string[]
  reached: string[]
}

function listOf(map: Record<string, string>, prefix: string) {
  const names = new Map<string, 'file' | 'dir'>()
  for (const k of Object.keys(map)) {
    if (prefix && !k.startsWith(`${prefix}/`)) continue
    const rest = prefix ? k.slice(prefix.length + 1) : k
    const [head = '', ...tail] = rest.split('/')
    names.set(head, tail.length > 0 ? 'dir' : 'file')
  }
  return [...names].map(([name, kind]) => ({ name, kind, size: 1, mtimeMs: 1, isLink: false }))
}

/** The engine beneath the plugin: a file system in memory, python3, and recorders. */
function engine(on: any, files: Record<string, string>): Rec {
  const rec: Rec = { argv: [], prompts: [], opened: [], toasts: [], reached: [] }
  const engineRel = (path: string) => (path.startsWith(WS) ? undefined : /\/engine(?:\/(.*))?$/.exec(path))
  const lookup = (path: string) => {
    const m = engineRel(path)
    return m ? ENGINE[m[1] ?? ''] : files[path]
  }
  on('fs.read', (_: unknown, e: any) => {
    const text = lookup(e.path)
    return text !== undefined ? { value: text } : { deny: 'ENOENT' }
  })
  on('fs.exists', (_: unknown, e: any) => ({ value: lookup(e.path) !== undefined }))
  on('fs.stat', (_: unknown, e: any) =>
    lookup(e.path) !== undefined
      ? { value: { kind: 'file', size: 1, mtimeMs: 1000, isLink: false } }
      : { deny: 'ENOENT' },
  )
  on('fs.write', (_: unknown, e: any) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.list', (_: unknown, e: any) => {
    const m = engineRel(e.path)
    return { value: m ? listOf(ENGINE, m[1] ?? '') : listOf(files, e.path) }
  })
  on('process.run', (_: unknown, e: any) => {
    const argv = [...e.argv] as string[]
    if (argv.includes('--version')) {
      return { value: { exitCode: 0, stdout: 'Python 3.12.1\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    rec.argv.push(argv)
    return { value: { exitCode: 0, stdout: '{"ok": true}\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('command.register', () => ({ value: { command: 'followup' } }))
  on('ui.open', (_: unknown, e: any) => {
    rec.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.toast', (_: unknown, e: any) => {
    rec.toasts.push(e.text)
    return { value: undefined }
  })
  on('clock.every', () => ({ deny: 'no timers in this test' }))
  on('clock.after', () => ({ value: undefined }))
  on('prompt.submit', (_: unknown, e: any) => {
    rec.prompts.push(e.text)
    return { text: e.text }
  })
  on('session.info', () => ({ value: { transcriptPath: '/t.jsonl', platform: 'macos', isHosted: false } }))
  on('tool.call', (_: unknown, e: any) => {
    rec.reached.push(String(e.tool))
    return { result: {} }
  })
  on('session.start', (_: unknown, e: any) => ({ cwd: e.cwd }))
  return rec
}

const configured = () => ({
  [CONFIG]: JSON.stringify({ business_name: 'Brightwater Bakery Co.' }),
  [PANE_JSON]: JSON.stringify(PANE_DATA),
  [FU]: 'print()',
})

const engineCalls = (rec: Rec) => rec.argv.filter(a => a.includes(FU))

const command = (args: string) => ({
  command: 'followup',
  args,
  origin: { kind: 'composer' as const },
  presentation: { isFullscreen: true, columns: 100 },
})

type McpName = `mcp__${string}__${string}`

test('before setup the pane shows one card with Set up and Try the demo', async ($, on) => {
  engine(on, {})
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /not set up/ })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'setup' })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'demo' })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'run' })).toBeUndefined()
    await ui.unmount()
  }
})

test('the pane does not open at session start when the folder is not set up', async ($, on) => {
  const rec = engine(on, {})
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })
  expect(rec.opened).toHaveLength(0)
})

test('the pane opens at start when set up and shows the header and today items', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })
  expect(rec.opened).toEqual(['followup'])

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /to review/ }))?.text).toBe(
      '2 to review · 1 need you · 1 approved · 0 in Gmail · 0 sent',
    )
    expect((await ui.find({ type: 'Text', text: /^Overdue/ }))?.text).toBe(
      'Overdue $9,095.50 across 6 invoices · 4 open quotes ($5,230.50)',
    )
    expect(await ui.find({ type: 'Text', text: /Harbor Grocers — Overdue invoice/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /A quick reminder that invoice 1051/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /To: ap@harborgrocers.example · Subject: Invoice 1051/ })).toBeDefined()
    // The suspicious problem is red; the call shows the phone number.
    expect((await ui.find({ type: 'Text', text: /Suspicious text/ }))?.props.color).toBe('red')
    expect(await ui.find({ type: 'Text', text: /\+1 555 0108/ })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'approve-fu-1' })).toBeDefined()
    expect((await ui.find({ type: 'Button', key: 'approve-all' }))?.props.label).toBe('Approve all ready (2)')
    expect((await ui.find({ type: 'Button', key: 'stage' }))?.props.label).toBe('Put 1 approved draft in Gmail')
    expect(await ui.find({ type: 'Button', key: 'tab-today' })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'approve-fu-2' })).toBeUndefined()
    await ui.unmount()
  }
})

test('Approve runs fu.py approve with the right argv', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.press({ key: 'approve-fu-1' })
  expect(engineCalls(rec)).toEqual([['python3', FU, 'approve', 'fu-1', '--ws', WS]])

  await ui.press({ key: 'approve-all' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'approve', '--all-drafted', '--ws', WS])
  await ui.unmount()
})

test('Snooze, answers and do-not-contact run the engine; DNC needs two presses', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.select({ key: 'snooze-fu-1', value: '2w' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'snooze', 'fu-1', '2026-12-09', '--ws', WS])

  await ui.press({ key: 'ans-fu-3-q1-0' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'answer', 'fu-3', 'q1', 'Yes', '--ws', WS])

  const before = engineCalls(rec).length
  await ui.press({ key: 'dnc-fu-1' })
  expect(engineCalls(rec)).toHaveLength(before)
  await ui.press({ key: 'dnc-confirm-fu-1' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'dnc', 'fu-1', '--ws', WS])
  await ui.unmount()
})

test('Ask for a change stores the instruction and asks Claude to redraft', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.input({ key: 'change-fu-1', text: 'Make it shorter' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'revise', 'fu-1', '--instruction', 'Make it shorter', '--ws', WS])
  for (let i = 0; i < 50 && rec.prompts.length === 0; i++) await ui.find({ type: 'Text' })
  expect(rec.prompts[0]).toContain(`Follow ${WS}/followup_mod/DRAFT.md exactly. Workspace: ${WS}`)
  await ui.unmount()
})

test('/followup demo instant installs the engine, writes the demo config and loads drafts', async ($, on) => {
  const files: Record<string, string> = {}
  const rec = engine(on, files)
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  const result = await $.command.run(command('demo instant'))
  expect(result.text).toContain('Demo loaded')
  expect(files[`${WS}/followup_mod/RUN.md`]).toBe('# Run')
  expect(files[FU]).toBeDefined()
  expect(files[`${WS}/followup_mod/tests/test_rules.py`]).toBeUndefined()
  expect(files[`${WS}/followup_mod/VOICE.md`]).toBe('# Voice')
  const cfg = JSON.parse(files[CONFIG] ?? '{}')
  expect(cfg).toMatchObject({
    business_name: 'Brightwater Bakery Co.',
    sender_name: 'Brightwater Bakery Co.',
    owner_emails: ['orders@brightwater.example'],
    rules: { max_per_run: 8 },
  })
  expect(engineCalls(rec)).toContainEqual(['python3', FU, 'demo', '--with-drafts', '--ws', WS])
  expect(rec.prompts).toHaveLength(0)
})

test('/followup update never overwrites config.json or VOICE.md', async ($, on) => {
  const files: Record<string, string> = {
    [CONFIG]: '{"business_name": "Real Co"}',
    [`${WS}/followup_mod/VOICE.md`]: 'My voice',
  }
  engine(on, files)
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  const result = await $.command.run(command('update'))
  expect(result.text).toContain('Updated')
  expect(files[CONFIG]).toBe('{"business_name": "Real Co"}')
  expect(files[`${WS}/followup_mod/VOICE.md`]).toBe('My voice')
  expect(files[`${WS}/followup_mod/DRAFT.md`]).toBe('# Draft')
})

test('when set up, the guard denies sending and QuickBooks writes', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  const tools: McpName[] = ['mcp__x__send_message', 'mcp__x__invoices_create', 'mcp__x__reply', 'mcp__x__forward', 'mcp__qb__sendInvoice', 'mcp__x__send__now']
  for (const tool of tools) {
    const result = await $.tool.call({ tool })
    expect(JSON.stringify(result), tool).toContain(DENY)
  }
  expect(rec.reached).toEqual([])
})

test('when set up, the guard allows drafts, reads and built-in tools', async ($, on) => {
  const rec = engine(on, configured())
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  const tools: McpName[] = ['mcp__x__create_draft', 'mcp__x__search_threads', 'mcp__qb__list_invoices']
  for (const tool of tools) {
    const result = await $.tool.call({ tool })
    expect(JSON.stringify(result), tool).not.toContain(DENY)
  }
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(rec.reached).toEqual(['mcp__x__create_draft', 'mcp__x__search_threads', 'mcp__qb__list_invoices', 'Bash'])
})

test('when not set up, the guard allows everything', async ($, on) => {
  const rec = engine(on, {})
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  const tools: McpName[] = ['mcp__x__send_message', 'mcp__x__invoices_create', 'mcp__x__reply', 'mcp__x__forward']
  for (const tool of tools) {
    await $.tool.call({ tool })
  }
  expect(rec.reached).toEqual(['mcp__x__send_message', 'mcp__x__invoices_create', 'mcp__x__reply', 'mcp__x__forward'])
})

test('the other tabs draw tables, and a reply takes an outcome with a Later date', async ($, on) => {
  const replied = item({
    id: 'fu-9', customer_name: 'Golden Crust Pizzeria', kind: 'dormant_customer', kind_label: 'Past customer gone quiet',
    reason: 'No order in 8 months.', state: 'replied', state_label: '💬 They replied',
    reply_snippet: 'Thanks! Check back after the holidays.', thread_url: 'https://mail.google.com/mail/u/0/#inbox/abc',
  })
  const staged = item({
    id: 'fu-4', customer_name: 'Pinecrest Hotel', kind: 'invoice_overdue', kind_label: 'Overdue invoice',
    reason: 'Pinecrest owes $3,420.00.', state: 'staged', state_label: '📬 Draft in Gmail, press Send there',
    draft_subject: 'Invoice 1033 | overdue', gmail_draft_url: 'https://mail.google.com/mail/u/0/#drafts?compose=xyz',
    staged_at: '2026-11-25T15:00:00Z',
  })
  const data = {
    ...PANE_DATA,
    today_items: [replied],
    pipeline: [replied, staged],
    sent: [staged, replied],
    customers: [
      {
        id: 'qb:6', name: 'Pinecrest Hotel', company: 'Pinecrest Hotel', email: 'ap@pinecrest.example', phone: null,
        type: 'Wholesale', do_not_contact: 0, open_estimates: 0, open_estimate_amount: 0, overdue_balance: 3420,
        last_contact_at: '2026-10-01T10:00:00Z', last_contact_from: 'us', followups_sent: 1, last_outcome: null,
      },
    ],
  }
  const rec = engine(on, { ...configured(), [PANE_JSON]: JSON.stringify(data) })
  await $.session.start({ cwd: WS, surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /Check back after the holidays/ })).toBeDefined()
    expect(await ui.find({ type: 'Select', key: 'later-fu-9' })).toBeUndefined()

    await ui.press({ key: 'tab-pipeline' })
    expect((await ui.find({ type: 'Markdown' }))?.props.text).toContain('| Golden Crust Pizzeria | Past customer gone quiet |')
    await ui.press({ key: 'tab-sent' })
    expect(await ui.find({ type: 'Link' })).toBeDefined()
    expect((await ui.find({ type: 'Markdown' }))?.props.text).toContain('Invoice 1033 \\| overdue')
    await ui.press({ key: 'tab-customers' })
    expect((await ui.find({ type: 'Markdown' }))?.props.text).toContain('$3,420.00')
    await ui.press({ key: 'tab-today' })
    await ui.unmount()
  }

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'out-fu-9-later' })
  await ui.select({ key: 'later-fu-9', value: '1m' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'outcome', 'fu-9', 'later', '--date', '2026-12-25', '--ws', WS])
  expect(await ui.find({ type: 'Select', key: 'later-fu-9' })).toBeUndefined()
  await ui.press({ key: 'out-fu-9-won' })
  expect(engineCalls(rec).at(-1)).toEqual(['python3', FU, 'outcome', 'fu-9', 'won', '--ws', WS])
  await ui.unmount()
})
