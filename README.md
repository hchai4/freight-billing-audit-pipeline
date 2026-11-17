# Logistics Billing Anomaly Agent

[![CI](https://github.com/hchai4/logistics-billing-anomaly-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/hchai4/logistics-billing-anomaly-agent/actions/workflows/ci.yml)

Freight-audit system that cross-examines warehouse scale scans, UPS EDI 210 invoices, and unstructured portal/email PDFs. Overcharges are quantified in PostgreSQL by dbt, then surfaced in a Streamlit workflow that helps teams identify, triage, and dispute billing exceptions.

![Enterprise Logistics Audit & Dispute Hub overview](docs/images/logistics-billing-anomaly-agent-overview.png)

[Open the live demo](https://logistics-billing-anomaly-agent-rkngeowwvh6lkeep9gszt4.streamlit.app)

![3-Way Reconciliation Mart dashboard](docs/images/logistics-billing-anomaly-agent-reconciliation.png)

## Quick start

Needs Docker Desktop and an OpenAI key (Tab 1 extract only).
