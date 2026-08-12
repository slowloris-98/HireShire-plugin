"""Matcher-entry funnel: a code + encoder relevance gate that also lazily hydrates
list-only Workday/BambooHR jobs (fetching their description on demand) before the
job reaches the LLM scorer.

Import `Funnel` from `hireshire.funnel.funnel` directly — this package __init__ is
kept import-light on purpose: `hireshire.matcher.config` imports `funnel.config`, so
pulling `funnel.funnel` (which imports `matcher.config`) in here would cycle.
"""
