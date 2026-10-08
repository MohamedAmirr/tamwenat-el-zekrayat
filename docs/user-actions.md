# Steps only the user can do

Agents cannot or must not run these (cost, approval, or ownership). When the trigger happens, the agent must remind the user in its reply, naming the step and the command. Keep this list current: add a row when a new user-only step appears, remove it when done.

| Trigger | User step | Command / where |
|---|---|---|
| `docs/` changed materially (new doc, rewritten section) | Re-extract docs into the graph (semantic, LLM cost). Needs an LLM key if not run inside Claude Code (e.g. `GEMINI_API_KEY`). | `/graphify . --update` in Claude Code |
| Review finds behavior choices (what counts as a change, what to do for fully delivered orders, which orders a patch marks) | Decide the option before `frappe-dev` fixes it | answer in chat |
| Feature work is done and reviewed | Approve commit, push and PR (agents never commit unasked) | tell `git-ops` |
| GitHub issue or epic content drafted | Approve the shown title, scope and acceptance criteria before creation or edit | answer in chat |
| A patch is added or changed (e.g. #177 `backfill_last_synced_snapshot`) and merged | Run migrate on each real site (dev, prod). Test on `test.localhost` first, never `dev.localhost` for tests. | via `frappe-ops`: `bench --site <site> migrate` (read `.env` for site) |
| Any destructive op (`down -v`, restore over data, force-push, branch delete) | Explicit go-ahead | answer in chat |
| Editing heavily and want live graph refresh | Start the watcher in your own terminal (long-running) | `graphify watch .` |
| Want to share the graph with teammates | Decide whether to stop git-ignoring `graphify-out/` (currently local-only); if yes, also register the merge driver via `graphify hook install` | policy decision |
| Dev bench `bench start` or cloudflared tunnel problems | See memory note "Dev bench ops gotchas"; `bench start` is not supervised | restart manually |
| Code adds a doctype field and is deployed or pulled | Run migrate on each real site BEFORE using the new code; otherwise saving a doc crashes with `AttributeError` (seen 2026-10-08 with `Noon Connection.webhook_url`) | via `frappe-ops`: `bench --site <site> migrate` (read `.env` for site) |
| Noon webhook key or URL created, changed or refreshed on a Noon Connection | Update Noon's Partner Center, Integrator Configuration, Webhook tab by hand: URL, header name `X-Api-Key`, and the key | Partner Center (copy values with the Noon Connection form buttons) |
| Dev bench worker or scheduler code changed (e.g. after a pull) | Restart `bench start`; worker and scheduler processes do not reload Python on their own (also restart if web Python changes don't take effect) | see "Restarting `bench start`" in `docs/dev-environment.md` |
| #181 (outbound platform cancel) merged | Run migrate on each real site: new `Marketplace Order.outbound_cancelled_on` field and `Order Action` option on `Marketplace Sync Log.sync_type`; without it the sweep query and order saves crash on the missing column. Test on `test.localhost` first | via `frappe-ops`: `bench --site <site> migrate` (read `.env` for site) |
