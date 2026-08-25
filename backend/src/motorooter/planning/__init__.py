"""The planning pipeline.

Five distinct stages — parameters, route search, discovery, enrichment, export — kept in
separate modules so each is testable without the others. Stages 3 and 4 touch an LLM and
run only on an explicit replan; route search and single-leg edits stay synchronous.
"""
