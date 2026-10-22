# Finance Runbook (sample)

## Posting period not open
The error "Posting period not open" means the period is closed for posting. Posting periods are opened by the client finance controller in transaction OB52. If month-end close is blocked, raise a P2 ticket.

## Invoice blocked for payment
An invoice blocked for price variance means the invoice price is higher than the purchase order price beyond the tolerance. An invoice blocked for missing goods receipt means no goods receipt has been posted against the purchase order. Price variance blocks are reviewed by the client procurement team within 2 business days. Missing goods receipt blocks clear automatically once the goods receipt is posted in transaction MIGO.

## Releasing payment blocks
Payment blocks are released only by the client accounts payable lead in transaction MRBR. The support assistant cannot release payment blocks.

## Vendor bank details
Changes to vendor bank details need dual control by the client finance team. The support desk never changes vendor bank details.
