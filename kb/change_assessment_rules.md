# Change Impact Assessment Rules (sample)

## High-criticality objects
Any change to a high-criticality object needs Change Advisory Board approval and a full regression test run.

## Interfaces
Objects with interfaces need an integration test with each connected system before the change can go to production.

## Short notice
A high-criticality change due within 5 business days needs an expedited CAB and sign-off from the client IT head.

## Low-criticality objects
Changes that only touch low-criticality objects without interfaces need business owner sign-off and the standard test suite.

## Unknown objects
If a changed object is not in the CMDB, its dependencies are unknown. The solution architect must review it manually before CAB; it is never assumed to be low impact.

## Who decides
The assistant prepares the impact assessment. The change manager reviews it before it is submitted to CAB, and CAB makes the decision. Approved changes are not reassessed.
