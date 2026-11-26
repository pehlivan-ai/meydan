"""meydan.pay: take a payment for something an app sells, from a conversation (experimental).

What lands first: `meydan.pay.checkout`, one attempt to pay through the seller's own provider
(states, the provider protocol, verify-by-fetch, the sweep). The rest of docs/pay.md (dues,
notices that join the turn, the pay page API, the ledger) lands part by part.
"""
