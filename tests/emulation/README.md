# Network Emulation Tests

This directory contains network emulation tests for evaluating the monitoring system under different network conditions.

## Test Conditions

The tests will simulate:

* Packet loss
* Network delay
* High latency
* Unstable connectivity
* Temporary network outages

## Linux Network Emulation

Network conditions can be simulated using `tc` with the `netem` module.

Example:

```bash
# Add 100 ms of network delay
sudo tc qdisc add dev eth0 root netem delay 100ms

# Add 10% packet loss
sudo tc qdisc add dev eth0 root netem loss 10%

# Add both delay and packet loss
sudo tc qdisc add dev eth0 root netem delay 100ms loss 10%

# Remove the network emulation
sudo tc qdisc del dev eth0 root
```

## Purpose

These tests will be used to evaluate whether the monitoring system can:

* Detect degraded network conditions.
* Measure changes in network performance.
* Continue collecting measurements during connectivity problems.
* Store measurements when the backhaul is unavailable.
* Forward stored measurements after connectivity is restored.
