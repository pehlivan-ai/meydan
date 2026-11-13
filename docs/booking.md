# Booking

`meydan.booking` is a **primitive**: an optional part an app takes only if it books time (`pip install "meydan[booking]"`).
It is appointments as tables and functions over them. It knows nothing about channels or agents; your tools call it
inside your own transaction. An app that books nothing leaves it out and loses nothing.

## Tables

`define_tables(metadata, prefix="bk_")` adds them to your metadata and returns a `BookingTables`. A prefix lets
one database hold several businesses side by side.

- staff, services (minutes, price), shifts (who works when), spaces (rooms or chairs, optional)
- bookings: a member of staff, a service, a time range, a status (`held`, `confirmed`, `cancelled`, `expired`,
  `attended`, `no_show`)
- closures: time taken out for one member or for everyone
- a waiting list, the offers made from it, and a ledger of filled gaps

Two bookings of one member of staff (or of one space) cannot overlap while they are held or confirmed. That is an
exclusion constraint in the database, not a check in code, so it holds under concurrency. It needs the
`btree_gist` extension (`create extension if not exists btree_gist`).

## Functions (`meydan.booking.core`)

Every function takes `now` instead of reading a clock: tests are exact and a month can be simulated in seconds.

| function | does |
|---|---|
| `book` | hold or confirm a time; raises if it is taken |
| `cancel` | free a booking |
| `expire_holds` | let unanswered holds lapse |
| `free_gaps` | free time of a member: shifts minus bookings minus closures |
| `offer_next` | after a time is freed: pick who on the waiting list fits best and make an offer |
| `answer` | a yes books it as a fill, a no frees it for the next person |
| `fills` | the ledger: which freed time was filled, for whom, at what price |
| `utilization` | booked time over worked time |

Filling a freed time: `cancel` → `offer_next` → `answer` or `expire_holds` → `offer_next` again, until nobody fits.

## Policy (`meydan.booking.policy.OfferPolicy`)

When and how a freed time may be offered: how far ahead it must lie, how long a person has to answer, whether the
time is held while they decide or several people are asked at once, quiet hours, and how many offers one person
may get. A waiting-list entry marked `auto_accept` is booked at once and the message is a notice.

## What it does not do

Reminders, payments, an offering → slot layer, calendar sync, and marking attendance by itself (the states exist;
something must set them).
