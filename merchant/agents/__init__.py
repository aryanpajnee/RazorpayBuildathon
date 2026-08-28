"""The Northwind merchant agent org (surfaces #1-#6).

Each module here is one LLM agent surface. They PROPOSE and EXPLAIN; none of
them sign a mandate, compute an authoritative total, call the Gate, touch
Razorpay, or write to the ledger. Where an agent proposes a cart it returns
SKUs and quantities only - never a price - and the merchant re-derives every
price from its own catalog and enforces the ceiling at the Gate. This is the
same money-path boundary buyer/ nodes respect, applied merchant-side.
"""
