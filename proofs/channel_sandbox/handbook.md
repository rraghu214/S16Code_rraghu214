# Production change handbook

## Normal deployment

The change owner watches error rate, latency, and customer reports for fifteen minutes after deployment.

## Customer-visible errors

When a production change causes customer-visible errors, stop further rollout immediately. The incident
commander decides whether to roll back. Notify support with the affected surface and the next update time,
preserve logs before restarting services, and open an incident timeline owned by someone other than the
engineer performing the rollback.

## Recovery

Close the incident only after the original metrics recover and one independent reviewer verifies the fix.
