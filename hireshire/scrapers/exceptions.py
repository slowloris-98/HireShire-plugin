class SlugNotFoundError(Exception):
    def __init__(self, platform: str, token: str):
        self.platform = platform
        self.token = token
        super().__init__(f"{platform}: slug {token!r} not found")


class BoardBlockedError(Exception):
    """The board host refused access (e.g. Workday WAF returns 403/401).

    Distinct from SlugNotFoundError only in the status the run records: a block is
    usually IP/edge-based and often transient, so calling it "no board here" would
    misreport a slug that is fine. Both are retried next run — nothing is ever
    skipped on the strength of a past sweep.
    """

    def __init__(self, platform: str, token: str, status_code: int):
        self.platform = platform
        self.token = token
        self.status_code = status_code
        super().__init__(f"{platform}: slug {token!r} blocked (HTTP {status_code})")
