"""The Northwind red team (Phase 6, surfaces #13-#15).

Adversarial agents that attack the money path from the outside, the same way
a hostile buyer agent or a hostile catalog entry could: forged/replayed
mandates, poisoned product copy meant to hijack an AI shopping assistant, and
a judge that scores whether an attack actually reached money or was refused
before it got there. None of these surfaces sit on the money path themselves
- they generate adversarial INPUT to it (#13, #14) or grade its OUTPUT (#15).
The thesis this package exists to prove: the merchant's Gate is the defence,
not input sanitisation, prompt hardening, or a well-behaved buyer agent. An
attack here is expected to succeed at fooling an LLM and still fail to move
money.
"""
