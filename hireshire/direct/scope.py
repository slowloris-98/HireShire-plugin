"""Which countries the direct portals search, derived from `scraper.location_filter`.

The multi-tenant boards return every job and `scraper.py` filters them after the
fact. The direct portals cannot work that way: they are read newest-first and
capped at `direct_max_pages`, so a search the portal is not scoped for spends
those pages on jobs the filter then throws away. So the portal is asked for the
user's countries up front — derived from the list setup already writes, never
from a setting of its own, because there is one location list and the scraper
owns it.

The list mixes countries, states and cities ("united states", "georgia",
"bay area", "remote - us"), so each term is resolved to a country against
`portal_locations.COUNTRIES`. **Any term that does not resolve widens the scope
to everywhere** rather than being skipped. The two ways of being wrong are not
symmetric: a scope narrowed past a term the user wrote hides their jobs with no
sign anything is missing, while an unscoped search only spends pages. An empty
list and a bare "remote" mean everywhere too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hireshire.direct.portal_locations import BY_NAME, COUNTRIES, US_STATE_ABBREVS

WORLDWIDE = "Worldwide"

_LOOKUP: dict[str, str] = {}
for _c in COUNTRIES:
    for _term in (_c.name.lower(), *_c.aliases, *_c.regions, *_c.cities):
        # First writer wins, and the United States comes first, so "georgia" is
        # the state. Never consult a portal's own lookup for a user term: Apple's
        # answers "georgia" with the Republic of Georgia.
        _LOOKUP.setdefault(_term, _c.name)
_ABBREVS = {a.lower() for a in US_STATE_ABBREVS}
_REMOTE = re.compile(r"\bremote\b")
_PUNCT = re.compile(r"[\s\-–—/()\[\]:]+")


@dataclass(frozen=True)
class Scope:
    """`countries` is None when the portals should not be scoped at all."""
    countries: frozenset[str] | None
    unresolved: tuple[str, ...] = ()

    @property
    def ordered(self) -> list[str]:
        """Countries in table order, so a scope always renders the same URL."""
        if self.countries is None:
            return []
        return [c.name for c in COUNTRIES if c.name in self.countries]

    def for_portal(self, column: str | None) -> list[str] | None:
        """This portal's values for the scope, or None to search it unscoped.

        None when the scope is everywhere, when the portal has no column at all
        (Meta returns its whole board, so there is nothing to scope), and when
        any country in it has no value in that portal's column — narrowing to
        the countries it does know would silently drop the rest.
        """
        if self.countries is None or column is None:
            return None
        values = [getattr(BY_NAME[name], column) for name in self.ordered]
        return None if any(v is None for v in values) else values

    def placeholder(self, column: str | None) -> str:
        """The location to give a job whose list entry does not say where it is.

        Names what the portal was actually searched for, so it is only as wide
        as that search.
        """
        if self.for_portal(column) is None:
            return WORLDWIDE
        return " | ".join(self.ordered)

    def describe(self, column: str | None) -> str:
        return "everywhere" if self.for_portal(column) is None else ", ".join(self.ordered)


EVERYWHERE = Scope(countries=None)


def _resolve_term(term: str) -> str | None:
    t = term.strip().lower()
    if not t:
        return None
    if t in _LOOKUP:
        return _LOOKUP[t]

    # "remote - us", "remote (us", "us-remote": the country is what is left.
    bare = _PUNCT.sub(" ", _REMOTE.sub(" ", t)).strip(" ,")
    if bare and bare != t and bare in _LOOKUP:
        return _LOOKUP[bare]

    # "cambridge, ma", "austin, tx", "pune, india": resolve the parts, and accept
    # them only when every part that resolves names the same country.
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if len(parts) > 1:
        found = {_LOOKUP.get(p) or ("United States" if p in _ABBREVS else None)
                 for p in parts}
        found.discard(None)
        if len(found) == 1:
            return found.pop()
    return None


def resolve_scope(location_filter: list[str] | None) -> Scope:
    terms = [t for t in (location_filter or []) if t and t.strip()]
    if not terms:
        return EVERYWHERE

    countries, unresolved = set(), []
    for term in terms:
        country = _resolve_term(term)
        if country is None:
            unresolved.append(term.strip())
        else:
            countries.add(country)

    if unresolved:
        return Scope(countries=None, unresolved=tuple(unresolved))
    return Scope(countries=frozenset(countries))
