# Validation against supplied logs

The parser and SQLite accounting were replay-tested against the supplied JSONL samples.

Confirmed classifications:

- Out West 13/13: won
- Out West 11/13 with lose-all ending: lost, provisional rewards forfeited
- Vacation 8/8: won, two Compass proc interactions in one adventure
- Down Under 14/14: won
- Space 13/13: won, live-verified `16:0` reward corrected to one Meteorite
- Brazil 7/7: won with two nonfatal backpack losses
- Fishing 9/9: won with one nonfatal backpack loss and Fish Tokens
- Fishing premature fatal ending: lost while retained rewards remain credited
- Compass base quantity above one: bonus is `actual - base`, not always one

The collector deduplicates repeated `on_message`, `on_message_edit`, and raw-edit states by message ID plus a stable state hash.

## 0.1.2 display aggregation

- User stats aggregate exact multiplier type/strength pairs from finalized run JSON.
- Multiplier totals retain duration seconds and award counts.
- Top item contributor is selected per adventure by summed frozen net value.
- Stats title uses the target member's guild display name.
- Native Components V2 separators are used when supported.

## 0.1.3 display aggregation

- Item contributors remain grouped by adventure and item across all finalized UTC-day runs.
- Contributors are ordered by frozen net-value total, then quantity and name.
- The storage response retains both `top_items` and the legacy singular `top_item` field.
- Adventure performance is ordered by net profit, highest first.

## 0.1.5 formatting validation

- Exact multiplier aggregation key is type, strength, and duration.
- Repeated identical awards retain the original duration and increment count.
- Different awards of one multiplier type render as comma-separated entries on one line.
- Item contributor rows place quantity before the developer emoji.

## v0.1.6 display validation

- Page 1: aggregate Net Profit, Net Gain, and Net Loss reconcile per adventure.
- Detail pages: retained item contributors use frozen run-time net values.
- Detail pages: ticket costs and backpack losses are grouped by adventure and completion UTC day.
- Stats navigation uses native Components V2 buttons and is restricted to the command invoker.

## Resumed-message edge case

Validated against `masterlog_1509650103219261441_20260714_083522.jsonl`:

- Original canonical message: `1526507397462626396`
- `/adventure` continuation message: `1526507581600825414`
- The continuation repeated the exact active Fishing prompt.
- The continuation and its final summary resolve to the original canonical run.
- Only one adventure row and one ticket cost remain.
- Prior node rewards, backpack selections/losses, and Compass attribution remain attached.
- Cross-guild continuation is supported because matching is global by user and adventure, not guild.


## Completed-event replay edge case

Validated against `masterlog_1509650103219261441_20260714_084735.jsonl`:

- Original canonical message: `1526510652984655883`
- The original node awarded 2 Lucky Horseshoes before Next was clicked.
- `/adventure` then rendered message `1526510746450661456` with the same prompt plus `You have already completed this event`.
- The replay message is linked to the canonical run using the prompt before the notice.
- The notice is not stored as a node result and does not duplicate the Lucky Horseshoes.
- The notice does not overwrite `current_prompt_text`.
- Clicking Next on the replay message advances the same canonical run to the next Fishing node.
- The final 9-interaction summary remains attached to one run with one ticket cost.

## v0.1.10 leaderboard validation

- Page 1 queries `leaderboard(day, "net", 10, 0)`.
- Page 2 queries `leaderboard(day, "market", 10, 0)`.
- Each metric is ranked independently and can therefore have a different user order.
- Positive and zero values render as `⏣ amount`; negative values render as `- ⏣ amount`.
- User mentions are displayed without sending mention notifications.

## v0.1.11 five-page stats validation

- Personal stats always render as five pages with fixed navigation state.
- Page 2 remains below Discord's 4,000-character display-text limit with all nine adventure types present.
- Page 3 uses one selected adventure and a maximum of nine select options, with a native separator beneath the select menu.
- Summary and per-adventure `Died` totals count only rows where `lose_all_seen=1`; ordinary premature endings remain neutral.
- Net and market gain/loss totals are queried independently for each adventure.
- Page 4 excludes Adventure Tickets from backpack-loss rankings and totals retained item market value as `Items Earned`.
- Compass ROI is calculated as bonus value minus Compass cost for both net and market valuations.
- The supplied completed Museum run parses as 9/9 interactions, ⏣250,000, and exactly 35 Skin Fragments. The explanatory duplicate-skin embed is not counted a second time.
- Storage aggregation, leaderboard death counts, syntax compilation, and synthetic five-page render budgets were tested successfully.


## v0.1.12 reward ranking validation

- A synthetic Page 3 result with ⏣4,350,000 coins and item rewards worth ⏣800,000, ⏣500,000, and ⏣400,000 ranks Coins first.
- The same synthetic data ranks Coins first on Page 4 and retains only the next four highest item rewards in the five-row list.
- Coin rewards use the Coins emoji and do not affect the `Items Retained` quantity or `Items Earned` market-value total.
- Zero ticket, backpack-loss, and Compass-cost values render as `⏣ 0` with no buff/debuff emoji and no minus sign.
- Positive costs still render as debuffs with `- ⏣ amount`.
- Python syntax compilation and archive verification completed successfully.
