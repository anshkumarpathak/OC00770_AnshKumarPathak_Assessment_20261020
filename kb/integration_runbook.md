# Integration Recovery Runbook (sample)

## IDOC51-VENDOR vendor not found
The IDoc failed because the vendor does not exist in S/4HANA. The master data team must replicate the vendor first. Once the vendor exists, the IDocs can be reprocessed in transaction BD87.

## TIMEOUT-POS target timeout
The POS sales upload timed out because S/4HANA was slow during the store close batch. Check free work processes in transaction SM50. If the system health is green, the messages can be reprocessed.

## MT940-DUP duplicate bank statement
The bank sent the same statement twice. Never import it again. Treasury confirms the duplicate and the second file is discarded.

## PAY-AUTH payment file rejected
The bank rejected the payment file signature. Treasury must renew the signing certificate. Payment files are never resent by the support team.

## Reprocessing approval
The integration lead approves every reprocessing request. One request covers one error type and at most 50 messages.
