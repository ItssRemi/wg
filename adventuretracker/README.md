# AdventureTracker

Red-DiscordBot cog for recording Dank Memer adventure activity.

## Immediate setup

1. Copy the `adventuretracker` folder into your local cog repository.
2. Run `[p]load adventuretracker`.
3. Run `[p]adventure trackerstatus`.
4. Collection is enabled in every channel by default. To restrict it:
   - `[p]adventure channels #adventures`
5. Use `[p]adventure stats` and `[p]adventure leaderboard` after finalized summaries have been observed.

All timestamps are retained in UTC. There is no destructive midnight reset: commands select rows by UTC date, so the new day starts automatically at 00:00 UTC while older data remains queryable for future period filters.

## Captured data

- Adventure start, selected backpack item IDs, entry-ticket quantity
- Every observed node result and its selected custom ID
- Named item loss with or without adventure ending
- Generic fatal end, named-item fatal end, and lose-all fatal end
- Final summary interaction count and authoritative rewards/backpack
- Won, neutral, and catastrophic-death outcomes, including fatal-on-final-interaction edge cases
- Coins, items, Fish Tokens, Skin Fragments and multipliers
- Tracked and ignored rewards, including ignored Fishing Tools
- Net and market value snapshots at completion
- Ticket and backpack-loss costs
- Adventure Compass uses, proc interactions, doubled components and ROI
- Tentacled Temptation active sessions
- Unknown rewards preserved for later mapping

## Owner tools

- `[p]adventure trackerstatus`
- Attach JSONL logs to `[p]adventure importlog` to backfill/replay them

## 0.1.1 display and run-start corrections

- `/adventure stats` now sends a Components V2 container with a Text Display.
- Adventure catalogue and backpack-selection screens no longer become active runs.
- Pending selections are excluded from active metrics and abandoned staging rows are cleaned up.
- Removed the deprecated `Message.interaction` fallback and use `interaction_metadata` only.
- Updated the compact stats layout and global adventure emojis.

## 0.1.2 stats layout

- Uses the target member's server display name in the title instead of a mention.
- Splits the compact stats response into native Components V2 Text Displays and Separators.
- Shows the highest net-value item contributor for each played adventure.
- Aggregates multiplier rewards by exact type and strength, including total duration and award count.
- Hides the multiplier section when no multiplier was earned.
- Keeps Fish Tokens and Skin Fragments in a compact currency section.

## 0.1.3 adventure reward contributors

- Ranks adventure blocks by net profit.
- Uses buff and debuff emojis on each adventure profit line.
- Shows up to five highest net-value retained item contributors per adventure.
- Dynamically uses five items when 1-3 adventures are shown, four for 4-5 adventures, and three for 6 or more.
- Uses ReplyCont for continuing item rows and Reply for the final item row.


## Global network scope

Adventure records retain their source guild and channel for auditing, but `/adventure stats` and `/adventure leaderboard` aggregate by user across every guild where Nebula records adventures. Existing database rows become visible globally without migration or reset.

## 0.1.5 compact reward formatting

- Item contributor rows use `quantity emoji item name` ordering.
- Multiplier awards are grouped by type and rendered as `+5% for 1h` followed by an inline `x1` count.
- Different strengths or durations of the same multiplier type share one comma-separated line.
- Repeated identical multipliers increment `xN` without incorrectly combining their durations.

## 0.1.9 dynamically paginated gain and loss breakdown

- `/adventure stats` is an owner-locked Components V2 menu with a fixed summary page and dynamically generated detail pages.
- Page 1 shows total profit plus each adventure's Net Gain and Net Loss.
- Detail pages show coins and the top 3-5 retained item contributors for each adventure.
- Detail pages show every distinct backpack loss and the Adventure Ticket cost for each adventure.
- Previous, refresh, and next buttons keep the selected user and refresh against the current UTC day.
- Existing SQLite records remain compatible and require no migration or reset.


## Resumed adventure messages

If a user runs `/adventure` while an adventure is already in progress, Dank Memer may render the current prompt in a new message. AdventureTracker links that message to the original canonical run, including across guilds, so the entry ticket, backpack, prior node results, Compass procs, and final summary are counted once.

If the previous choice already awarded its reward but the user had not clicked Next, Dank may replay the old prompt with `You have already completed this event`. AdventureTracker treats that screen as a continuation marker, not a new result: it preserves the original reward exactly once, does not replace the canonical prompt with the notice, and waits for Next to reveal the actual next node.

## 0.1.10 two-page global leaderboard

- `/adventure leaderboard` now uses an owner-locked Components V2 menu.
- Page 1 ranks the top 10 users by Net Value Profit.
- Page 2 independently ranks the top 10 users by Market Value Profit.
- Leaderboard values omit the leading plus sign and render losses as `- ⏣ amount`.
- Previous, refresh, and next buttons retain the current page and refresh against the current UTC day.

## 0.1.11 five-page personal statistics

- Replaces the expanding gain/loss detail pagination with five fixed Components V2 pages:
  1. Adventure Overview
  2. Adventure Performance
  3. Selected Adventure Details
  4. Adventure Rewards
  5. Adventure Compass
- Page 1 reconciles net and market profit independently from gross rewards, ticket costs, and backpack losses.
- Page 2 shows net and market performance for every played adventure, ranked by net-value profit and separated into individual blocks.
- Only the explicit lose-all catastrophe is counted as `Died`. Other finalized early endings are neutral rather than losses.
- Page 3 keeps one complete adventure title and accounting view at a time, with an adventure select menu beneath it.
- Page 4 shows the five highest net-value retained items and backpack losses, plus coins, item market value, Fish Tokens, and Skin Fragments.
- Page 5 shows Compasses used, proc interactions, doubled rewards, bonus value, cost, and net/market ROI.
- The global leaderboard now reports catastrophic outcomes as `D` rather than counting every neutral early ending as a loss.
- Existing SQLite records require no reset or migration because death is derived from the stored `lose_all_seen` flag.


## 0.1.12 reward ranking and neutral zero values

- Coins now compete with item rewards in the net-value-ranked Top Rewards lists on Pages 3 and 4.
- A large coin payout can therefore rank above lower-value item rewards instead of appearing only in Reward Totals.
- Page 3 ranks the selected adventure's coins and items together and shows the top three reward contributors.
- Page 4 ranks the day's total coins and retained items together and shows the top five reward contributors.
- Zero costs and losses render as `⏣ 0` without a debuff emoji or negative sign.
- Positive costs still render with the debuff emoji and `- ⏣ amount`; positive gross rewards retain the buff emoji.
- Existing SQLite records remain compatible and require no migration or reset.


## v0.1.14 hotfix

- Fixed startup backfill crashing on interactions with no luck contributors.
- Luck backfill failures are now logged without leaving every stats command waiting forever.
- Restored the normal top-level `adventuretracker/` folder in the ZIP package.
