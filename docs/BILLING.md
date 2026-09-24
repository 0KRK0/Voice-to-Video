# Billing and usage

## The distinction that matters

Rate limits and quotas are constantly confused, and conflating them produces a
system that either throttles a paying customer for being fast or lets a free
account render for a month.

| | Rate limit | Quota |
| --- | --- | --- |
| Protects | the service | the business |
| Unit | requests per second | minutes per month |
| Resets | continuously | on a billing boundary |
| Lives in | `vtv.security.limits` | `vtv.billing` |
| Exceeded → | 429, retry shortly | 402, upgrade or wait |

## Plans are a table

`vtv/billing/plans.py` is a dictionary. No class hierarchy, no `if tier ==`
anywhere in the pipeline. Pricing changes far more often than code should, and
every plan-shaped conditional scattered through business logic is a place a
pricing change can go wrong silently.

Nothing reads a tier name. Code reads a `Plan` and asks whether a number is
under a limit. That is what lets a bespoke enterprise contract be a row rather
than a code path, and what makes "give this one customer SSO" a field change.

`test_allowances_never_decrease_as_the_price_rises` walks every quota across
every tier. A pricing table that inverts is a bug nobody notices until renewal.

### Every plan bounds provider spend

Including Enterprise. `PROVIDER_SPEND_USD` is the circuit breaker between a
runaway loop and a runaway bill, and a contract without a ceiling is a contract
that cannot be capacity-planned.

## Metering: reserve, then settle

```python
verdict, reservation = meter.reserve(...)   # before spending anything
...                                          # the render happens
meter.settle(reservation, actual=4.2, cost_usd=0.31)
```

Three properties this buys, each of which is a real bug otherwise:

**Concurrency cannot overshoot.** Ten simultaneous requests each pass the same
check, and without a reservation all ten proceed. The reservation occupies the
allowance for the length of the window.

**The customer is billed for what was produced.** The estimate is discarded at
settle time. Billing a guess is how a support thread becomes a refund.

**A crash costs an hour, not a month.** Abandoned reservations expire.
`release()` returns the allowance immediately when a job fails, so a failed
render costs the tenant nothing.

## Idempotency is a unique index

```sql
CREATE UNIQUE INDEX usage_idempotency
    ON usage_records (organisation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

A retried job legitimately calls `record()` twice. It must bill once, and a
duplicate must not be an error. An application-level check loses to two workers
racing; the schema does not.

## Cost travels with revenue

Every `UsageRecord` carries `cost_usd` from the `CostLedger` — what the
providers actually charged us for that work. So:

```python
summary.gross_margin_usd   # plan price minus provider cost
```

is a subtraction rather than a reconstruction. A billing system that meters
revenue and not cost cannot tell you which customers are unprofitable, which is
the number that decides whether the company works.

That number is **internal**. `GET /v1/usage` strips it: a customer sees what
they consumed and what it will cost them, never what it cost us.

## Records are immutable

A usage record that can be edited is a usage record a customer cannot trust. The
fix for a mistake is a compensating record, exactly as in double-entry
bookkeeping.

`meter.records()` returns line items with project ids, which is what a customer
disputing an invoice actually needs — a total they cannot decompose is not an
answer.

## What is not built

- **No payment processor.** No network here. `invoice_lines()` produces the
  lines; nothing is ever charged.
- **No proration.** A mid-period plan change is not modelled.
- **No tax.** Jurisdiction-dependent and out of scope for the engine.
- **No dunning.** `Organisation.suspended` exists and is enforced on every
  request; deciding *when* to set it is a commercial policy, not a code path.

A production deployment adds an idempotent invoice writer and a reconciliation
job against the processor's own record of truth. Ours is not the source of
truth for money that has actually moved.
