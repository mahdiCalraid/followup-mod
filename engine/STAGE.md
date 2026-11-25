# Put approved follow-ups into Gmail as drafts

The owner pressed **Put in Gmail** in the pane. That is approval to **create drafts** for the follow-ups in state `approved`. It is **not** approval to send. Never send, reply or forward: the owner reviews each draft in Gmail and presses Send there. The mod blocks send tools.

## Steps

1. Run `python3 followup_mod/scripts/fu.py stage-list`. For each item you get `id`, `to`, `subject`, `body`, `reply_to_message_id` and `thread_id`.
2. For each item, create the draft with the Gmail connector's `create_draft`:
   - `to`: [`to`]. Exactly the address from `stage-list`; never change it.
   - `subject`: `subject`. If `reply_to_message_id` is set, Gmail keeps the thread's subject.
   - `body`: `body`, plain text, with no Markdown.
   - `replyToMessageId`: `reply_to_message_id`, when present, so the draft sits inside the conversation. Leave it out when it starts with `demo-`: that is fictional demo data, not a real Gmail message.
   - `includeSignature`: `true`, unless `config.json` → `draft.signature_in_gmail` is false.
3. Record each draft:
   ```
   python3 followup_mod/scripts/fu.py staged <id> --draft-id <draft.id> --url <draft.viewUrl>
   ```
4. **Optional labelling.** If `config.json` → `draft.label` is set (for example `Follow-up`), make sure the label exists, creating it once with `create_label` if needed. Then label the thread with `label_thread`.
5. Tell the owner in 2 lines: "N drafts are in Gmail → Drafts. Open each one, check it, and press Send. The pane marks them sent at the next check."

**Outlook** (`mail: outlook`): there's no draft tool. Open Outlook on the web in **your own new Chrome tab**, open the conversation, press Reply, and paste the body. **Don't press Send.** Leave the draft saved. Record it with `staged <id> --draft-id outlook-web`.

**Demo workspace** (all addresses end in `.example`): drafts to `.example` addresses can't be delivered. If the owner is showing a demo, it's fine to create them in their Gmail Drafts so they can see where the drafts land. Ask once first: "Create N demo drafts in your Gmail Drafts folder? They're addressed to fictional .example addresses and are never sent."
