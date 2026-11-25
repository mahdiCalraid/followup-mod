// The mod's view of the engine's `views/pane.json` (docs/CONTRACT.md §8).
// The pane reads only that file; every change goes through `fu.py` (§7).

export type FollowupState =
  | 'found'
  | 'drafted'
  | 'approved'
  | 'staged'
  | 'sent'
  | 'replied'
  | 'closed'
  | 'revise'
  | 'needs_you'
  | 'skipped'
  | 'snoozed'
  | 'call'
  | 'failed'

/** One thing a follow-up waits on: a question to answer or a task only the owner can do. */
export type NeedItem = {
  id: string
  type: 'question' | 'task'
  text: string
  /** Multiple-choice answers; none means free text. */
  options?: string[]
  key?: string
  answer?: string | null
  done?: boolean
}

export type FollowupView = {
  id: string
  customer_id: string
  customer_name: string
  company?: string | null
  email?: string | null
  phone?: string | null
  kind: string
  kind_label: string
  reason: string
  priority?: number | null
  state: FollowupState
  state_label: string
  tier?: string | null
  tier_label?: string | null
  facts?: string[]
  draft_subject?: string | null
  draft_to?: string | null
  draft_body?: string | null
  problems?: string[]
  items?: NeedItem[]
  gmail_draft_url?: string | null
  thread_url?: string | null
  staged_at?: string | null
  sent_at?: string | null
  replied_at?: string | null
  reply_snippet?: string | null
  outcome?: string | null
  later_date?: string | null
  updated_at?: string | null
}

export type CustomerView = {
  id: string
  name: string
  company?: string | null
  email?: string | null
  phone?: string | null
  type?: string | null
  do_not_contact?: boolean | number | null
  open_estimates?: number | null
  open_estimate_amount?: number | null
  overdue_balance?: number | null
  last_contact_at?: string | null
  last_contact_from?: string | null
  followups_sent?: number | null
  last_outcome?: string | null
}

export type LastRun = {
  id: string
  started_at?: string | null
  finished_at?: string | null
  source_qb?: string | null
  source_mail?: string | null
  flagged?: number | null
  drafted?: number | null
}

export type PaneCounts = {
  review: number
  needs_you: number
  approved: number
  staged: number
  sent: number
  replied: number
  call: number
  customers: number
  open_estimates: number
  open_estimate_amount: number
  overdue_invoices: number
  overdue_balance: number
}

/** `<ws>/views/pane.json`, schema `followup-pane/1`. */
export type PaneData = {
  schema: string
  updated_at?: string
  workspace?: string
  today?: string
  business_name?: string | null
  last_run: LastRun | null
  counts: Partial<PaneCounts>
  today_items: FollowupView[]
  pipeline: FollowupView[]
  sent: FollowupView[]
  customers: CustomerView[]
  files?: { xlsx?: string; html?: string; db?: string }
}

export type FollowupTab = 'today' | 'pipeline' | 'sent' | 'customers'

/** What the mod sees of a QuickBooks connector in this session's tools. */
export type QuickBooksStatus = {
  /** True when some MCP server's tools name or describe QuickBooks. */
  connected: boolean
  /** That server's tools in all. */
  tools: number
  /** Of those, the ones that could change QuickBooks: hidden from Claude in a follow-up workspace. */
  hidden: number
}

declare module 'claude-code' {
  interface PluginState {
    followup: {
      /** The last `views/pane.json` read, or null before there is one. */
      pane: Shaped<PaneData | null>
      /** The tab the pane shows. */
      view: FollowupTab
      /** Follow-up whose "Do not contact" was pressed once and awaits the confirm press. */
      confirming: string | null
      /** Per follow-up: the snooze period picked, or `later` while the Later date picker is open. */
      choice: Shaped<Record<string, string>>
      /** True while an engine command runs. */
      busy: boolean
      /** True once `<ws>/followup_mod/config.json` exists. */
      ready: boolean
      /** The QuickBooks connector as the mod last saw it, or null before the first look. */
      qb: Shaped<QuickBooksStatus | null>
    }
  }
}
