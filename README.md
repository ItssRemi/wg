# Waifugami

A single merged Red-DiscordBot cog combining the former **WaifugamiCards**
and **WaifugamiListener** cogs:

* passive Waifugami card tracking, team analysis, and the Components V2
  card browser (`/wgcards`)
* public spawn name assistance and the right-click **Name** message
  context menu
* seasonal event role pings and manual event-tier overrides
* owner/admin catalog updates from `.sc` series list replies
* completion tracking, watched series, and tier-alert DMs

This merge is **structural only** — it does not change any behaviour.
Every command still does exactly what it did before. What changed is how
they're organized and discovered.

## Everything lives under `[p]help wg` now

Run `[p]help wg` (or `[p]wg` on its own) to see the whole command list in
one place, instead of having to know a command exists ahead of time.

```
[p]wg                    — shows this help
[p]wg track              — enable/disable/clear card tracking
  [p]wg track enable
  [p]wg track disable
  [p]wg track clear
[p]wg cd                 — show your cooldowns
[p]wg status             — spawn-tracking config & catalog stats
[p]wg trackid <id>       — track a character id for completion
[p]wg untrackid <id>
[p]wg watch <series>     — watch a series for spawns
[p]wg unwatch <series>
[p]wg tracked            — your watched series / tracked ids
[p]wg trackedids <series>
[p]wg series             — on/off/status for series display on names
[p]wg event              — admin: manual event-tier overrides
[p]wg debug              — admin: debug log channel
[p]wg channels           — admin: spawn channel list
[p]wg tieralert          — subscribe to rare-tier private DMs
```

## Legacy names still work

Every old command name and alias (`wgstatus`, `wgwatch`, `wgtrack`,
`waifugamitrack`, `wgseries`, `wgevent`, `wgdebug`, `wgchannels`,
`wgtieralert`, `wgtrackid`, `wguntrackid`, `wgunwatch`, `wgtracked`,
`wgtrackedids`, `wgcd`) keeps working exactly as before — nothing that
used to work has been removed. The `[p]wg ...` forms above are just a
second, more discoverable way to reach the same commands.

One name did shift: the old bare `[p]wg enable/disable/clear` group is
now primarily reached as `[p]wg track enable/disable/clear`, because
`wg` itself is now the master group. Its aliases `[p]wgtrack enable` and
`[p]waifugamitrack enable` are unchanged and still work exactly as they
always have.

## Things intentionally left alone

* `[p]wgupdate` and `[p]wgscan` are reply-triggered commands (you reply
  to a message and type the bare command). They're left at the top
  level, unchanged — nesting them under `wg` wouldn't make them any
  easier to use.
* The right-click **Name** context menu is a Discord message context
  menu, not a text command, so it's unaffected either way.
* `/wgcards` and `/wgcd` remain standalone slash commands. Discord's
  slash-command tree is separate from the prefix command tree used by
  `[p]help`, so they're unaffected by this refactor.

## Data & config

The two original cogs used separate Red `Config` identifiers, and this
merge keeps both of them (`self.config` for card tracking,
`self.listener_config` for spawn tracking/series/tier alerts). This
means **no data migration is needed** — all previously stored per-user
card data, tracking preferences, watched series, and tier-alert
subscriptions carry over unchanged.

The catalog JSON files (`series_catalog.json`, `waifu_hash_map.json`,
`waifugami_catalog_lookup.json`, `waifus.json`, `wgevent_overrides.json`)
are unchanged and ship with the cog as before.

## What's next

This was phase 1: merge + a unified, discoverable command tree, with no
behaviour changes. Renaming/reworking individual old commands (the ones
you mentioned wanting to change) is a good phase 2 now that everything
lives in one place and is easy to find.
