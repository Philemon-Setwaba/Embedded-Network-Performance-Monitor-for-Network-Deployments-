# TO UPDATE

## 1. Literature Review — Prometheus Scraping vs Remote Write

**Written: 16 September 2026**

### Task

Clarify the distinction between Prometheus scraping and Prometheus Remote Write.

1. **Prometheus scraping:** Prometheus pulls measurements from a target at regular intervals. If the target is unreachable, the scrape fails and `up = 0` is recorded.
2. **Prometheus Remote Write:** This is different from scraping. Remote Write queues and retries measurements that **Prometheus has already collected** when sending them to a remote storage endpoint.
3. **Important limitation:** Remote Write does **not** provide a mechanism for an unreachable target to store measurements locally and resend those measurements when connectivity is restored.
4. **Relevance to this project:** The proposed monitor therefore requires its **own local buffering/store-and-forward mechanism** so that measurements generated during a backhaul outage are retained locally and forwarded when connectivity is restored.

### What needs to be made clear

The literature review should not give the impression that Prometheus Remote Write already solves the project's store-and-forward problem.

The key distinction is:

```text
Prometheus scraping
        ↓
Can Prometheus collect the measurement?
        ↓
Target unreachable → scrape fails
```

versus:

```text
Prometheus Remote Write
        ↓
Prometheus already has the measurement
        ↓
Remote storage temporarily unavailable
        ↓
Queue + retry
```

The project's requirement is different:

```text
Backhaul unavailable
        ↓
Monitor still performs measurements
        ↓
Measurements stored locally
        ↓
Backhaul restored
        ↓
Buffered measurements forwarded
```

This distinction should be clearly supported by the relevant Prometheus documentation/source and connected directly to the identified research gap.

---

## 2. [Next TODO]

**Written: [DATE]**

### Task

*

---

## 3. [Next TODO]

**Written: [DATE]**

### Task

*
