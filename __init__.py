from redbot.core.bot import Red

from .waifugami import Waifugami


async def setup(bot: Red) -> None:
    await bot.add_cog(Waifugami(bot))
