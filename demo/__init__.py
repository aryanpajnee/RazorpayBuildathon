"""The autonomous web-shopping demo layer (1 Sep pivot).

Additive to the frozen money path: the buyer becomes a real tool-calling agent
that searches the live web under a signed budget and settles through the same
merchant + Gate + Razorpay pipeline. This package holds the NEW pieces —
`search` (the web-discovery fallback chain), and, from Day 2, `tools`, `agent`,
`orchestrator`, and `events`. Nothing here touches core/ or the merchant money
path; it only ever feeds candidate data into the existing, unchanged Gate.
"""
