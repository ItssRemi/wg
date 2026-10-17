from .adventuretracker import AdventureTracker


async def setup(bot):
    await bot.add_cog(AdventureTracker(bot))
