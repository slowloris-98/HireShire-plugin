"""Per-company handlers for the single-tenant `direct` board.

Each module exposes:

    async def fetch_list(ctx, token) -> list[Job]
    async def fetch_detail(ctx, job) -> Job      # optional; omitted when the
                                                 # list payload already carries
                                                 # the description (Apple)

`ctx` is the `DirectScraper` itself, used for its shared `_get` helper (retry +
rate limiter) and its settings.
"""
