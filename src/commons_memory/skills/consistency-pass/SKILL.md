---
name: consistency-pass
description: The weekly Canon consistency pass. Checks that the memory sync, the Pulse relay and the Canon caches are fresh; resolves conflicts on agent-owned facts by precedence; never resolves owner-only ones and asks the owner instead, batched; flags decisions that changed a product without sets; routes peer findings to a session that can edit code; finds notes that still state retired values; writes a digest.
---

# Weekly consistency pass

Canon is the current truth in Commons: facts on product:<key> (what customers may be told), work:<key> (internal
state), company and vendor:<name>, on one timeline. Kits, caches, peer briefs, llms.txt and phone prompts are copies
rendered from Canon; they never write back. This pass keeps the copies and the notes in line with Canon.

Run it once a week (register it with schedule_report and report each run with schedule_ran), or when the owner asks.
Open it with start_work("weekly consistency pass", project="commons") and close it with finish_work and the digest id.

Precedence, the same everywhere: 1 the owner's words in the current conversation; 2 Canon; 3 the system of record in
its domain (Stripe for charged prices, origin/main for what shipped): a disagreement with Canon is drift to report,
never a silent pick; 4 live pages, Pulse kits and guides; 5 repo docs; 6 notes, memory files, handoffs and chat.

## 1. Check the bridges
Write down each check as ok or broken, with its evidence (an id, a time, a log line).
- Canon itself: canon() answers, generated_at is now, and open_conflicts is a number. Note the count: it is the
  "before" figure for the digest.
- Memory sync: search(query="", tags=["claude-memory"], since="7d") finds memory files imported this week. None
  while the owner's sessions edited memory files means the sync is broken.
- Pulse relay: schedules() shows pulse-commons delivered on its latest slot. For each product with Canon prices,
  the Pulse kit shows the same prices and offer; a mismatch is drift (Canon wins; the kit is a copy).
- Caches: on a machine with the Commons hooks, ~/.commons/state/canon.json has "ok": true and a fetched_at less than
  a day old.
Repair a bridge in this pass only when the repair is yours to make; otherwise it is a finding for the digest.

## 2. Resolve conflicts on agent-owned facts
conflicts() lists the open ones. For each:
- On an owner-only fact (its subject kind and predicate are in company.owner_only, such as product.prices or
  work.direction): never resolve it, however obvious the answer looks. Add it to the owner batch (step 3).
- On anything else: resolve it by precedence. Keep the side backed by the higher source (Canon over notes, Stripe
  over a page for a charged price, origin/main over docs for what shipped) with conflicts(resolve_id, keep_id).
  When Canon itself is wrong on an agent-owned fact, correct it with fact_set(subject, predicate, value,
  evidence=...) first.
- A captured chat line on one side: keep the durable note.

## 3. Ask the owner, batched
Collect every owner-only question of the pass into one batch: one line per item, with the fact, the values in
conflict, their sources and ids. There is no cap on the number of items, but there is one batch per pass:
board_add(kind="question", text=<the batch>). A range approval such as "1-3 ok" does not count for money, public
promises, legal text, reversing a recorded decision or irreversible steps: each of those needs its own answer.
When an answer comes, record it: record_decision(what, why=<the owner's words>, sets=[...], supersedes=[...]).
That settles the conflicts on the facts it sets.

## 4. Flag decisions that changed a product without sets
search(query="", kinds=["decision"], since="7d"). A decision that changed a product's offer, prices, terms,
platforms, features, promises, status or direction, but whose record (expand(id), meta) has no sets, is a finding.
Write the facts it implies: agent-owned ones with fact_set and the decision id as evidence; owner-only ones go into
the owner batch.

## 5. Route peer findings
Findings from peers (Grok Build, Antigravity, Vivi, the Grok bot) that need a code change go to a session that can
edit that repo: submit_task with the repo, the file and line, the evidence and what done looks like. A finding left
only in a note is lost.

## 6. Retired values
For each change in changes(since="7d"): search for the old value, and grep the memory files and the repo for it.
Walk work:<key>.places, which lists every place the customer facts are repeated. Each note that still states the
old value: retire it with record_decision(what, why, supersedes=[ids]), or update or forget your own notes. List what
you could not reach (a live page, a kit) as findings for the session that owns it.

## 7. Digest
Write one digest with remember(title="Consistency pass <date>", tags=["consistency-pass"], content=...):
- bridges: ok or broken, with evidence;
- conflicts: open count before and after, the ones resolved (ids, which side was kept, why);
- the owner batch (board id) and what it asks;
- decisions flagged for missing sets, and the facts written for them;
- peer findings routed, and to whom;
- retired values found, and the notes superseded;
- what is left, and why.
Then finish_work(claim_id, result=<the digest id and a two-line summary>).
