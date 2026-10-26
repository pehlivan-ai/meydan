# meydan architecture

How meydan intends to coordinate people through agents, and why. Grounded in the research
summarized at the end; claims about what's built are in the README.

## Concepts

- **Person**: someone reached through a channel (a phone number on WhatsApp). Identity comes
  from the channel, never from model output.
- **Personal agent**: one per person/conversation. A *delegate*, not a negotiator: it talks with
  the person, elicits preferences, asks for consent, and relays proposals. It does not bargain
  with other people's agents in free text.
- **Planner**: one per instance. Owns group decisions with deterministic logic over shared state:
  matching, constraints, consensus, dropouts. The LLM extracts intent and writes messages; code
  decides and validates every commitment.
- **Plan**: anything that commits people (a pair, a 4-player match, a tournament entry), modelled
  as a state machine:
  `proposed → collecting → confirmed → at_risk → dropped → backfilled → done | no_show`.
- **Autonomy policy**: per person and per action, what the agent may do alone and what needs a
  "yes" first (e.g. "auto-accept Saturday mornings at my club", "always ask before paying").
- **Instance**: one community's deployment (data, planner, agents, channel). Later a **node**
  that can exchange typed messages with other instances.

## Runtime (planned)

```
channel webhook ─► inbound queue (idempotent on message id) ─► 200 OK
                         │
        actor per conversation (one at a time per person, Postgres advisory lock)
           debounce bursts ─► personal agent turn ─► tools / planner intents
                         │
        outbound queue ─► sender (typing indicator, bubbles, channel API, status receipts)
```

Every stage communicates through tables, so one stage can be replaced (or rewritten in another
language) without touching the others. Postgres is the only infrastructure.

## Group dynamics (planned)

- **Consensus over chat**: the planner proposes 2-3 concrete options (time + place) as buttons;
  a rule closes the decision (quorum by a deadline, or least-misery). No open-ended negotiation.
  Which rule works best is an experiment, not an assumption.
- **Matching**: two-sided matching where the problem is two-sided (always has a stable result);
  scored greedy heuristics for any-pair and group formation (stability can't be promised there).
- **Social ties**: who played/met with whom, regular partners, invite/accept history. Negative
  preferences ("rather not with X") are used for matching and never revealed.
- **Consent**: committing another person (adding a partner) needs that person's confirmation.
- **Dropouts**: a ranked waitlist (constraint fit, reliability, fairness to newcomers) is offered
  the spot in order; escalate to an organizer near the deadline.
- **Reliability**: record confirmed-vs-attended per person from day one; the literature has no
  ready model, so each instance learns from its own data.
- **Preferences**: hard constraints separate from soft scored preferences; learn from which
  offered option people pick.

## Phases

1. One instance, in-process: actor runtime, personal agents, planner, Postgres. No
   agent-to-agent protocol.
2. More roles (organizers, venues, sponsors) as separate agents with typed, A2A-shaped
   interfaces, still in-process.
3. Expose A2A/MCP endpoints between instances when there are external peers. Consider
   privacy-preserving or P2P coordination only if parties stop trusting a single operator.

## Why this shape (evidence)

- Personal-agent deployments (Electric Elves, 2000-2002; CALO/PTIME, 2011) found adjustable
  autonomy and privacy mattered more than agent-to-agent mechanics. Scerri, Pynadath & Tambe,
  arXiv:1106.4573; Berry et al., ACM TIST 2011.
- Distributed constraint optimization for meeting scheduling is motivated by privacy without a
  trusted party, mostly evaluated in simulation with honest agents (Maheswaran et al., AAMAS
  2004). No evidence found that it beats a trusted coordinator when one operator holds the data.
- LLM agents are weak at multi-user scheduling and negotiation (Muses-Bench, arXiv:2604.08567;
  NegotiationArena, ICML 2024; arXiv:2309.17234), with model-dependent exceptions
  (arXiv:2602.05302).
- Centralized orchestration contains error amplification best; multi-agent setups degrade on
  sequential tasks (Kim et al., arXiv:2512.08296). Most multi-agent failures are design and
  inter-agent misalignment (Cemri et al., arXiv:2503.13657).
- Group recommendation has no universally best aggregation rule (social-choice limits); choose
  per context and test.

These are summaries of abstracts and secondary sources, not full replications; treat them as
design guidance.
