# Audit Engine — Integration Guide

`audit.py` is a self-contained mixin.  Three small edits to `waifugami.py`
connect everything.

---

## 1  Import the mixin at the top of waifugami.py

Add after the existing local import block (anywhere before the `Waifugami`
class definition):

```python
from .audit import AuditMixin
```

---

## 2  Add AuditMixin to the class declaration

Change:

```python
class Waifugami(commands.Cog):
```

to:

```python
class Waifugami(AuditMixin, commands.Cog):
```

---

## 3  Initialise the mixin in __init__

Inside `Waifugami.__init__`, after the existing instance-variable
setup (near the end of __init__, before the context-menu line), add:

```python
        # ---- audit engine (from AuditMixin) ----
        self._audit_init()
```

---

## 4  Hook into on_message_edit_cards

Inside `on_message_edit_cards`, add ONE line right after the
`if after.author.id != WAIFUGAMI_ID:` guard returns, before
any other processing:

```python
    @commands.Cog.listener(name="on_message_edit")
    async def on_message_edit_cards(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.id != WAIFUGAMI_ID:
            return

        # ---- audit engine hook ----
        if await self.audit_on_message_edit(before, after):
            return          # consumed by an audit harvest; skip normal logic
        # ---- end audit hook ----

        if after.id in self._scan_list_owners:
            ...  # rest of original method unchanged
```

---

## 5  Register wgaudit under the wg group (optional but recommended)

At the bottom of the "Unified wg command tree" section, add:

```python
    # ---- audit engine ----

    @wg.group(name="audit", invoke_without_command=True)
    async def wg_audit(self, ctx: commands.Context) -> None:
        """List Audit & Cleanup Engine.  Same as [p]wgaudit / [p]wga."""
        await self._wg_audit_dispatch(ctx)

    @wg_audit.command(name="start")
    async def wg_audit_start(self, ctx: commands.Context) -> None:
        """Begin an audit session."""
        await self.wgaudit_start.callback(self, ctx)

    @wg_audit.command(name="classify")
    async def wg_audit_classify(self, ctx: commands.Context) -> None:
        """Classify without harvesting event cards."""
        await self.wgaudit_classify.callback(self, ctx)

    @wg_audit.command(name="sell")
    async def wg_audit_sell(self, ctx: commands.Context, *, args: str = "") -> None:
        """Browse SELL candidates."""
        await self.wgaudit_sell.callback(self, ctx, args=args)

    @wg_audit.command(name="review")
    async def wg_audit_review(self, ctx: commands.Context) -> None:
        """Inspect REVIEW / UNKNOWN cards."""
        await self.wgaudit_review.callback(self, ctx)

    @wg_audit.command(name="confirm")
    async def wg_audit_confirm(self, ctx: commands.Context, *, ids_str: str = "") -> None:
        """Execute removal."""
        await self.wgaudit_confirm.callback(self, ctx, ids_str=ids_str)

    @wg_audit.command(name="cancel")
    async def wg_audit_cancel(self, ctx: commands.Context) -> None:
        """Cancel the active audit."""
        await self.wgaudit_cancel.callback(self, ctx)

    @wg_audit.command(name="status")
    async def wg_audit_status(self, ctx: commands.Context) -> None:
        """Show audit session status."""
        await self.wgaudit_status.callback(self, ctx)
```

---

## Summary of new commands

| Command | Alias | What it does |
|---|---|---|
| `..wg audit start` | `..wgaudit start`, `..wga start` | Begin a session; instruct user to run `.l -event all` |
| `..wg audit classify` | `..wgaudit classify` | Classify without event harvesting |
| `..wg audit sell` | | Show all SELL candidates |
| `..wg audit sell α` | | Filter by rarity |
| `..wg audit sell dupes` | | Duplicates only |
| `..wg audit sell α dupes` | | Rarity + duplicates |
| `..wg audit review` | | Show REVIEW / UNKNOWN cards |
| `..wg audit confirm` | | Remove all selected SELL cards |
| `..wg audit confirm 15 82 140` | | Remove specific IDs only |
| `..wg audit cancel` | | Abort without removing anything |
| `..wg audit status` | | Show session state |
