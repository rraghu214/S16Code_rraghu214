# Retrieval runbook

When relevance drops but latency is healthy, first inspect embedding drift, then increase Atlas efSearch in a canary. Add Borealis only if the quality gain justifies its measured 38 ms latency cost. Roll back if p95 exceeds 240 ms.
