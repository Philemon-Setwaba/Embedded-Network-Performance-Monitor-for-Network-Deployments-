# Embedded Network Performance Monitor

## Project Overview

This project develops a low-cost network performance monitoring system using a Raspberry Pi 5. The system is designed to monitor networks with intermittent or unreliable backhaul connectivity.

The monitor measures key network performance metrics, provides a local dashboard for viewing results, and temporarily stores measurements when the connection to the backhaul is unavailable. Once connectivity is restored, stored measurements can be forwarded to the remote destination.

## Project Aim

The aim of the project is to develop and evaluate a low-cost network monitoring system capable of:

* Measuring network performance.
* Detecting connectivity problems.
* Displaying measurements through a local dashboard.
* Buffering measurements during connectivity outages.
* Forwarding buffered measurements after connectivity is restored.

## Project Structure

| Directory/File            | Purpose                                      |
| ------------------------- | -------------------------------------------- |
| `docs/`                   | Project documentation and deliverables       |
| `docs/literature-review/` | Literature review                            |
| `docs/requirements/`      | Requirements analysis                        |
| `docs/design/`            | System architecture and hardware design      |
| `docs/deployment/`        | Deployment documentation and recommendations |
| `src/measurement/`        | Network measurement functionality            |
| `src/dashboard/`          | Local monitoring dashboard                   |
| `src/reporting/`          | Data buffering, forwarding and eviction      |
| `tests/emulation/`        | Network impairment and outage testing        |
| `scripts/`                | Device setup and provisioning scripts        |
| `reports/`                | Project report drafts                        |
| `README.md`               | Project overview and repository structure    |
| `.gitignore`              | Files excluded from Git tracking             |

## Status

Project structure established. Measurement, dashboard, reporting and testing functionality will be developed as the project progresses.
