# Procurement Runbook (sample)

## Purchase order approval
Purchase orders above the release limit go to the approver shown on the purchase order. If a purchase order has been awaiting approval for more than 3 business days, the assistant raises a P3 ticket to the client procurement lead.

## Purchase order statuses
Awaiting approval means the release workflow has not finished. Approved means the purchase order can be sent to the vendor. Goods received means a goods receipt has been posted. Closed means the purchase order is fully delivered and invoiced.

## Goods receipt
Goods receipts are posted by the warehouse in transaction MIGO. An invoice for goods that have no goods receipt is blocked for payment.
