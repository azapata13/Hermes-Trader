"""V1 Pro analytics: shadow / missed setups, outcome statistics, historical similarity, LLM context.

Read-only consumers of recorded setup rows (``setups.jsonl`` / ``setups.json``) and of the approval
journal. Nothing here is imported by the decision layer, nothing here can create, change or approve
a candidate, and nothing here has an order path. Every statistic is a SHADOW measurement unless a
row says ``execution == "REAL"``, which V1 never produces (it has no execution layer).
"""
