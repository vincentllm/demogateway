# Requests for Discussion (RFDs)

This directory contains the architectural decision records for AI Gateway.

Each RFD follows a lightweight RFC-style format:

- **Status**: proposed / accepted / deprecated
- **Context**: what problem are we solving
- **Goals / Non-Goals**: scope boundaries
- **Options Considered**: at least two alternatives
- **Decision**: the chosen approach
- **Consequences**: tradeoffs, limitations, and future work

## Index

| RFD | Title | Status |
|-----|-------|--------|
| [RFD-001](rfd-001-technology-stack.md) | Technology Stack: Python + FastAPI | accepted |
| [RFD-002](rfd-002-configuration-approach.md) | Configuration: Static YAML | accepted |
| [RFD-003](rfd-003-rate-limiting-architecture.md) | Rate Limiting Architecture: Facade + Composable Policies | accepted |
| [RFD-004](rfd-004-rate-limiting-algorithm-and-storage.md) | Rate Limiting Algorithm and Storage: Fixed Window + In-Memory | accepted |
| [RFD-005](rfd-005-token-counting-strategy.md) | Token Counting Strategy: Dummy Tokenizer | accepted |
| [RFD-006](rfd-006-backend-health-and-load-sensing.md) | Backend Health and Load Sensing | accepted |
| [RFD-007](rfd-007-provider-abstraction.md) | Provider Abstraction and OpenAI Passthrough | accepted |
| [RFD-008](rfd-008-degradation-and-disaster-recovery.md) | Degradation and Disaster Recovery | accepted |
| [RFD-009](rfd-009-testing-strategy.md) | Testing Strategy: MockTransport-Based Integration Tests | accepted |
