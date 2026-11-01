# Bosta Integration Specification for ERPNext
## Shipping and Returns Management Through Delivery Note and Sales Return

**Version:** 1.0  
**Status:** Business Specification, supplied by the user on 2026-10-31. It replaces the earlier Sales Order-first and parked Delivery Note-first Bosta designs. Epic #413.  
**Current scope:** Bosta only  
**Language:** English

---

## 1. Objective and Scope

The objective is to implement an end-to-end shipping workflow inside ERPNext while allowing employees to work with standard ERPNext documents, without manually creating or managing a separate `Shipment` document.

Core principles:

- `Delivery Note` is the primary operational document for outbound sales shipments.
- `Sales Return` is the primary operational document for customer returns after delivery.
- Shipment creation, cancellation, and tracking are handled through the backend integration with Bosta API.
- Inventory movements remain controlled by ERPNext.
- Bosta is the source of truth for the logistical status of its shipments.
- An external operation must not be treated as successful merely because a request was sent. The system must receive confirmation or resolve the result through a lookup when necessary.

### Out of Scope for This Phase

- Noon integration.
- Internal delivery representatives.
- Uber and inDrive.
- Other delivery providers or methods.

These can be designed later. The current design should still allow the Shipping Method / Provider options to be extended.

---

## 2. Agreed Principles and Decisions

1. Employees work through `Delivery Note` for sales shipping and do not need to open or create a separate `Shipment` document.
2. Shipping information and actions appear in a section named `Shipping` inside the `Details` tab of Delivery Note.
3. Selecting Bosta triggers shipment creation through Bosta API.
4. A Delivery Note must not be submitted unless Bosta has confirmed successful shipment creation.
5. If the result of shipment creation is unknown, the Delivery Note remains Draft until the result is resolved.
6. AWB, Tracking Number, and other values returned by Bosta are stored in Read Only fields.
7. The Airway Bill / Shipping Label PDF is saved in the Delivery Note's native ERPNext Attachments.
8. Shipment status is updated through Webhooks, with periodic reconciliation for open shipments.
9. A Delivery Note must not be cancelled before the related Bosta shipment has been successfully cancelled, if a shipment was created.
10. If any part of the cancellation workflow fails, the system displays a Dialog explaining what succeeded and what failed, with a Retry action.
11. A customer return after delivery is managed through Sales Return, with an optional Bosta Customer Return Pickup using `type = 25`.
12. A shipment returned because the customer did not receive it does not require a new Customer Return Pickup. When Bosta reports Returned, the system automatically creates a Draft Sales Return and notifies an employee.
13. Returned goods must not be added back to sellable stock merely because the shipment status changed to Returned. Physical receipt and inspection must be confirmed.
14. All Create, Cancel, Refresh, and Webhook operations must be traceable, and duplicate operations must be prevented.

---

## 3. Delivery Note Interface

### 3.1 Section Location

Add a section named **Shipping** inside the **Details** tab of Delivery Note.

### 3.2 Suggested Fields

| Field | Description | Editable? |
|---|---|---|
| Shipping Method | Selected delivery/shipping method | Yes, before shipment creation |
| Shipping Provider | Shipping company, e.g. Bosta | Depends on design |
| Shipping Cost / Estimated Shipping Cost | Expected shipping cost | Subject to permissions |
| Actual Shipping Cost | Final cost returned by Bosta, if available | Read Only |
| COD Amount | Amount Bosta is expected to collect from the customer | Before shipment creation |
| Tracking Number | Tracking identifier | Read Only |
| AWB Number | Airway Bill number | Read Only |
| Bosta Delivery ID / External Reference | External shipment identifier and integration reference | Read Only |
| Shipping Status | Latest known logistical status from Bosta | Read Only |
| Integration Status | Status of the integration operation itself | Read Only |
| Last Sync At | Time of the latest synchronization | Read Only |
| Shipping Label | PDF or link to the shipping label | Attachment |
| Last Integration Error | Short description of the latest technical error | Read Only |

Final field names (`fieldname`) will be defined during implementation and should remain stable for reporting and API use.

### 3.3 Shipping Method and Provider

The current design includes a Shipping Method dropdown. Potential future options include:

- Bosta
- Noon
- Internal Delivery Representative
- Uber
- inDrive
- Other

**Only Bosta is in scope for this phase.** Selecting another option must not trigger the Bosta API.

Conceptually, it is preferable to distinguish:
- `Shipping Method`: how the delivery is carried out.
- `Shipping Provider`: the company or service performing the shipment.

The detailed workflows for other methods are deferred.

### 3.4 Address Data

When Bosta is selected:

- Customer and address details are sourced from the Sales Order / Customer Address linked to the transaction.
- City, address, recipient phone number, and other fields required by Bosta are populated.
- The data that will be sent to Bosta should be visible for review, according to the final UI design.
- The system validates all mandatory Bosta fields before sending the request.
- The implementation must identify the actual shipping address selected for the Sales Order. It must not assume that the default Customer Address is always the correct address, especially when multiple addresses exist.

### 3.5 Shipping Rule and Cost

- Expected shipping cost is calculated using ERPNext Shipping Rules.
- The following values must remain separate:
  - **Estimated Shipping Cost:** internally calculated expected cost.
  - **Actual Shipping Cost:** final cost returned by Bosta or obtained from its settlement data.
  - **COD Amount:** amount Bosta is instructed to collect from the customer.
- Shipping Cost must never be treated as COD.
- If Bosta does not return the actual shipping cost during shipment creation, Actual Shipping Cost remains unset until reliable data becomes available.

---

## 4. Create a Bosta Shipment When Submitting Delivery Note

### 4.1 Normal Workflow

1. The employee creates a Delivery Note from a Sales Order.
2. The employee selects Bosta as the Shipping Method / Provider.
3. Customer and address details are populated from the Sales Order / Customer data.
4. Estimated Shipping Cost is calculated using the applicable Shipping Rule.
5. The employee reviews the address, shipment details, quantities, and COD amount.
6. The employee clicks Submit.
7. The system starts the shipment creation process with Bosta.
8. A clear waiting state is displayed, for example:
   - `Sending to Bosta...`
   - Loading indicator.
9. Once Bosta confirms successful shipment creation:
   - Save the Bosta Delivery ID / External Reference.
   - Save the AWB Number and Tracking Number.
   - Save other relevant values returned by the API.
   - Save the Airway Bill PDF in the Delivery Note's Attachments.
   - Complete the Delivery Note submission.
10. The submitted Delivery Note remains linked to the external Bosta shipment.

### 4.2 Submission Requirement

**A Delivery Note must not become Submitted unless Bosta has confirmed successful shipment creation.**

- If shipment creation succeeds, continue with Delivery Note submission.
- If Bosta confirms that creation failed and no shipment was created, keep the document in Draft.
- If the result is unknown, keep the document in Draft and resolve the result before allowing another creation attempt.

### 4.3 Transaction and Implementation Consideration

Bosta is an external system and does not participate in ERPNext's database transaction. Therefore, a single atomic transaction across both systems cannot be assumed.

The workflow must be implemented as an orchestrated process with each stage recorded explicitly.

Important failure case: Bosta confirms shipment creation, but ERPNext fails to complete the Delivery Note submission or save the document. The system must retain the external shipment ID and the data needed for reconciliation, and must prevent another shipment from being created for the same operation. It should resume the local submission or handle the external shipment according to its state, without repeating Create.

---

## 5. Create Shipment Errors and Unknown Results

### 5.1 Result Types

| Result | Meaning | Action |
|---|---|---|
| Confirmed Success | Bosta confirmed creation and returned shipment identifiers | Save data and complete submission |
| Confirmed Failure | Bosta clearly rejected creation and did not create a shipment | Show error and allow Retry after correction |
| Unknown | Connection dropped, request timed out, or no conclusive response was received | Query Bosta before attempting another Create |

### 5.2 User Experience for Unknown Results

If the connection drops or no conclusive response is received:

- Show a Dialog explaining that the shipment creation result is unknown.
- Do not submit the Delivery Note.
- Provide a button such as `Check with Bosta` or `Refresh from Bosta`.
- Send a lookup request to Bosta to determine whether the shipment was created.
- If the shipment is found:
  - Link it to the Delivery Note.
  - Save AWB, Tracking Number, Label, and other returned data.
  - Complete Delivery Note submission if it has not already been completed.
- If Bosta confirms that no shipment was created:
  - Show a clear message.
  - Provide a `Retry` button to create the shipment.
- If the result remains inconclusive:
  - Explain that Bosta has not provided a definitive result.
  - Do not blindly create another shipment.

### 5.3 Duplicate Prevention and Idempotency

- Generate a stable, unique External Reference for each Delivery Note / creation operation.
- Use an Idempotency Key if Bosta supports it.
- Record the Request ID, request time, and result.
- Do not send another Create request after an Unknown result until the previous attempt has been checked.
- If Bosta does not support lookup by External Reference or idempotency, a reliable alternative must be defined before implementation. Do not assume these capabilities exist.

### 5.4 Error Dialog

The Dialog should explain:
- Which operation failed.
- Whether the failure is confirmed or the result is unknown.
- A user-friendly error message.
- The available action: Retry or Check with Bosta.
- Short technical details when useful.

Bosta API credentials and secrets must never be exposed in user-facing errors.

---

## 6. Shipment Data and Attachments

After shipment creation is confirmed:

- Tracking Number is stored in a Read Only field in the Shipping section.
- AWB Number is stored in a Read Only field.
- Bosta Delivery ID / External Reference is saved for linking, lookup, and cancellation.
- Other data returned by Bosta that does not require user input is saved in Read Only fields.
- The Airway Bill / Shipping Label PDF is saved in the Delivery Note's native ERPNext Attachments.
- The system must verify that the PDF was downloaded and saved successfully. If PDF retrieval fails after shipment creation, retry fetching the label independently; do not create another shipment.

---

## 7. Shipment Status Updates

### 7.1 Source of Truth

Bosta is the source of truth for logistical shipment status. Under the normal workflow, users must not manually edit Shipping Status.

### 7.2 Webhooks

- Receive Webhooks from Bosta when shipment status changes.
- Verify the message source and its signature or the authentication mechanism supported by Bosta.
- Record the incoming event before applying it.
- Update Shipping Status on the Delivery Note.
- Store the event time and receipt time.
- Prevent the same event from being processed more than once.
- Handle events received out of order.
- Do not allow an older event to move a shipment backwards from a newer confirmed state.
- If events conflict or the correct state is unclear, query Bosta for the current state.

### 7.3 Nightly Synchronization

Run a Scheduled Job every day at **11:59 PM**, using the system's configured local timezone.

The job queries shipments that remain open or require review, such as:
- Shipments that have not reached a final status.
- Shipments whose Webhook may have been missed.
- Shipments with conflicting statuses.
- Creation or cancellation operations with Unknown results.
- Shipments whose previous synchronization failed.

Normally, final shipments such as Delivered, Cancelled, or Returned do not need to be queried every day unless a settlement issue or status conflict requires it.

### 7.4 Manual Refresh

- Provide a `Refresh from Bosta` button in Delivery Note when needed.
- Show it only for documents with a Bosta Delivery ID or an unresolved operation.
- Update status and shipment data using Bosta's response.
- Record who initiated the refresh, when it happened, and its result.

### 7.5 Keep Different Status Dimensions Separate

The system must distinguish:

1. **Shipping Status:** logistical status reported by Bosta.
2. **Integration Status:** state of the ERPNext–Bosta integration, e.g. Created, Creation Unknown, or Failed.
3. **Return Receipt Status:** whether the goods physically arrived at the warehouse and were inspected.
4. **COD / Collection Status:** whether the amount was collected, remitted, and reconciled.

A single field must not represent all these separate processes.

---

## 8. Shipment Cancellation and Delivery Note Cancellation

### 8.1 General Rule

If a Bosta shipment has been created, the Delivery Note must not be cancelled before the system has handled cancellation of the Bosta shipment.

The system must check the shipment status and Bosta's rules to determine whether cancellation is allowed.

If the shipment cannot be cancelled:
- Do not cancel the Delivery Note.
- Show a Dialog explaining that the Delivery Note cannot be cancelled because Bosta does not allow the shipment to be cancelled.
- Show the shipment status and the reason for rejection, if available.

### 8.2 Cancellation Outcomes

| Bosta State / Result | Action |
|---|---|
| No shipment has been created | Delivery Note can be cancelled according to ERPNext rules |
| Bosta confirms successful cancellation | Continue with Delivery Note cancellation |
| Bosta responds Already Cancelled | Treat the shipment as cancelled and continue with Delivery Note cancellation |
| Bosta clearly responds Shipment Not Found | May be treated as successful cancellation only if the documented meaning confirms that the shipment is absent/cancelled |
| Bosta returns a temporary error | Do not cancel Delivery Note; show Dialog with Retry |
| Bosta says cancellation is not allowed | Do not cancel Delivery Note; show the rejection reason |
| Bosta cancellation succeeds but ERPNext cancellation fails | Retry the local ERPNext cancellation; do not send another external cancellation request |

### 8.3 Cancel Unknown and Retry Decision

The agreed UX is to keep this simple: if cancellation fails or the connection drops, show a Dialog explaining what happened and provide a `Retry` button.

When Retry is clicked:
- The system sends the cancellation request to Bosta again.
- If Bosta responds that the shipment does not exist, and this response is documented to mean that the shipment is absent or already cancelled, the system may treat cancellation as successful.
- If Bosta responds Already Cancelled, treat cancellation as successful.
- If Bosta returns a temporary error, show Retry again.
- If Bosta says the shipment cannot be cancelled, do not cancel the Delivery Note.

Do not interpret every generic Error, Invalid Request, or Authentication Failure as Shipment Not Found.

### 8.4 Track External and Internal Cancellation Separately

Store two separate results:
- `External Cancellation Status`: result of cancelling the shipment at Bosta.
- `ERPNext Cancellation Status`: result of cancelling the Delivery Note locally.

This is essential when Bosta cancellation succeeds but ERPNext cancellation fails. The system can then retry the local operation without sending another cancellation request to Bosta.

*Epic #413 decision (2026-10-31): Integration Status carries the external result and the native docstatus carries the ERPNext result, so no second field is added.*

---

## 9. Editing a Delivery Note After Shipment Creation

After a Bosta shipment has been created, key shipping data already sent to Bosta must not be freely edited in Delivery Note. Examples include:
- Shipping address.
- Phone number.
- Recipient details.
- COD Amount.
- Package details or quantities that affect the shipment.

If a user needs to change these values:

1. Check whether Bosta supports updating the relevant shipment data in its current state.
2. If supported, perform the update and record its result.
3. If not supported, consider Cancel + Create New, but only after the original shipment has been successfully cancelled and the current shipment status allows cancellation.
4. Never create a replacement shipment before resolving the original shipment's status.

Keep a snapshot of the data sent to Bosta so that later changes can be compared with the original shipment request.

---

## 10. Sales Return — Customer Wants to Return Goods After Delivery

### 10.1 Create the Return

- The employee creates a Sales Return from the original transaction.
- The employee selects the products and quantities the customer wants to return.
- The system must not assume that all items from the original Delivery Note are being returned.
- Sales Return defines the products and quantities requested for return.

### 10.2 Choose the Return Collection Method

Sales Return should include a selection for how the returned goods will be collected.

If Bosta is selected:
- Create a Customer Return Pickup request through Bosta API.
- Use `type = 25`.
- Send the customer, address, and other data required by Bosta.
- Save the pickup operation ID, tracking number, AWB, and label in Sales Return or in a linked record/fields.
- Apply the same Loading, Error Dialog, Unknown, Lookup/Retry, and duplicate-prevention principles used for outbound shipment creation.

### 10.3 Returned COD

Add a field named **Returned COD** to Sales Return:

- The employee enters zero or a positive amount.
- The value is displayed to the employee as zero or a positive amount.
- When constructing the Bosta API request, the backend converts the value to a negative amount.
- Example: entering 500 EGP sends -500 to Bosta.
- Entering 0 sends 0.
- The employee must not enter a negative value manually.

Validate that Returned COD does not exceed the amount due to the customer under the business policy, taking into account discounts and amounts already refunded or settled.

**Important:** Creating a Customer Return Pickup request or sending Returned COD to Bosta does not mean the accounting refund has been completed. Keep these separate:
- Return Pickup Status.
- Warehouse Receipt / Inspection Status.
- Refund / Payment Status.

### 10.4 Physical Receipt at the Warehouse

- Do not return goods to sellable stock merely because a Sales Return was created or Bosta collected the return.
- Once goods physically arrive, confirm the quantities received.
- Inspect the goods and classify them as sellable or damaged.
- Record inventory movements using the appropriate ERPNext documents and configuration, such as Return Delivery Note / Sales Return, as applicable.
- Damaged goods must be moved to a damaged warehouse and must not be returned to available-for-sale stock.
- Prevent returned quantities from exceeding delivered quantities, taking previous returns into account.

---

## 11. Returned to Sender — Customer Did Not Receive the Shipment

This scenario is different from Customer Return Pickup.

### 11.1 When Bosta Reports Returned

If the original shipment was not delivered to the customer and Bosta later reports it as Returned:

1. Receive the Webhook or detect the status during synchronization.
2. Update Shipping Status on the original Delivery Note.
3. Automatically create a Draft Sales Return linked to the original transaction.
4. Notify an employee to review the return.
5. The employee reviews the products, quantities, and target warehouse.
6. After confirming physical receipt and inspecting the goods, the employee submits the return using the appropriate process.

### 11.2 Important Controls

- Do not create duplicate Draft Sales Returns for the same Delivery Note if a Webhook is repeated or synchronization runs again.
- Maintain a clear link from the Draft Sales Return to the original Delivery Note.
- Do not submit the Sales Return automatically just because Bosta reported Returned.
- Distinguish:
  - Returned to Sender: logistical status at Bosta.
  - Returned to Warehouse: confirmation of actual physical receipt.
- Do not return stock to available inventory before receipt and inspection.
- The accounting and tax treatment of an unclaimed shipment return must follow the company's ERPNext configuration and accounting policy.

### 11.3 Bosta Type

A Return to Sender shipment that the customer never received is not a new Customer Return Pickup. Do not create a new `type = 25` request merely because the original shipment was returned. Type 25 is intended for requesting collection of a customer return after delivery.

---

## 12. Bosta API Types Identified in the Supplied Documentation

The API documentation screenshot reviewed in the discussion shows the Create Delivery endpoint:

`POST http://app.bosta.co/api/v2/deliveries?apiVersion=1`

It shows API Key authentication in a header named `Authorization`.

It also lists the following values for the `type` field:

| Type | Meaning | COD note shown in the documentation |
|---:|---|---|
| 10 | Deliver | COD must be positive |
| 15 | Cash Collection | Detailed rules must be verified |
| 30 | Exchange | Detailed rules must be verified |
| 25 | Customer Return Pickup | COD must be negative when a refund is issued to the customer |

This information comes from the API documentation screenshot supplied by the user. The latest official Bosta documentation must be checked before implementation. The screenshot alone is not sufficient evidence for all fields, endpoints, or status rules.

---

## 13. Show Shipping Status in the Sales Order List

Shipping status should be visible in the Sales Order list so employees can monitor orders quickly.

However, one Sales Order may be linked to multiple Delivery Notes. Therefore, the status of a single Delivery Note must not be copied as though it represents the entire Sales Order.

Example:

| Delivery Note | Shipping Status |
|---|---|
| DN-001 | Delivered |
| DN-002 | In Transit |
| DN-003 | Returned |

The Sales Order should display an aggregated summary, for example:
- Number of Delivered shipments.
- Number of In Transit shipments.
- Number of Returned shipments.
- Number of Failed shipments or shipments requiring action.

Keep these dimensions separate:
- Fulfillment Status.
- Shipping Status.
- Payment / COD Status.

For example, Delivered does not necessarily mean COD has been remitted or reconciled.

---

## 14. Edge Cases and Additional Controls

| Scenario | Required Handling |
|---|---|
| Submit is clicked more than once | Prevent duplicate Create operations |
| Bosta creates the shipment but the connection drops before the response arrives | Mark Unknown and perform Lookup before Retry |
| Bosta succeeds but ERPNext fails to complete Submit | Retain External ID and reconcile without duplicate Create |
| Shipment is created but PDF is not saved | Retry label retrieval without creating another shipment |
| Duplicate Webhook | Ignore duplicate processing |
| Old Webhook arrives after a newer event | Do not move status backwards |
| Fake or untrusted Webhook | Verify authentication / signature |
| Employee edits address after shipment creation | Update or Cancel + Recreate, depending on Bosta support |
| Bosta cancels shipment independently | Update status and notify employee |
| Customer disputes a Delivered status | Delivery dispute / manual review |
| Shipment is lost | Lost / Investigation workflow |
| Only part of a shipment is returned | Record actual quantities received |
| Goods arrive damaged | Move them to damaged warehouse |
| COD has not been remitted | Collection reconciliation |
| Bosta API is unavailable | Safe Retry with operation logging |
| Shipment is Delivered and a Sales Return is later created | Keep delivery and return cycles separate |
| Multiple Delivery Notes exist for one Sales Order | Each Delivery Note has its own shipment and status |

---

## 15. Integration Log

Every integration operation should be recorded, either in a dedicated DocType or through an appropriate logging mechanism.

Suggested fields:
- ERPNext DocType and Document Name.
- Operation Type: Create / Cancel / Refresh / Return Pickup / Label Fetch.
- Provider: Bosta.
- External Reference.
- Bosta Delivery ID.
- Request ID.
- Attempt Number.
- Started At / Completed At.
- Result: Success / Failed / Unknown.
- HTTP Status Code, if available.
- Response Code / Error Code.
- Error message.
- User who initiated the operation.
- Correlation ID.

Requirements:
- Never store API secrets or tokens in logs.
- Mask or minimize personal data that is not required.
- Retain sanitized Request / Response data according to the company's data-retention policy.

*Epic #413 decision (2026-10-31): the log is Frappe's native Integration Request plus Custom Fields, not a new doctype.*

---

## 16. COD and Financial Reconciliation

Do not confuse these values:

- **COD Amount:** amount Bosta is instructed to collect from the customer.
- **Collected COD:** amount Bosta confirms it collected.
- **Remitted COD:** amount Bosta actually transferred to the company.
- **COD Difference:** difference between expected, collected, and remitted amounts.
- **Shipping Cost:** shipping charge.
- **Actual Shipping Cost:** final cost according to Bosta invoice / settlement.
- **Returned COD:** amount requested to be refunded to the customer in a Customer Return Pickup.

A reconciliation phase should match Bosta reports against ERPNext. Delivery Status = Delivered must not be treated as proof that COD has been settled.

*Epic #413 decision (2026-10-31): record and reconcile on the Delivery Note only; nothing posts to the general ledger in this epic.*

---

## 17. Suggested Status Fields

### 17.1 Integration Status on Delivery Note

Suggested values:

- `Not Created`
- `Creating`
- `Creation Unknown`
- `Created`
- `Creation Failed`
- `Cancellation Pending`
- `Cancellation Unknown`
- `Cancelled`

This field represents integration state, not logistical shipment state.

### 17.2 Shipping Status

Store the status returned by Bosta, with either the original value or a clearly defined mapping. Do not invent Bosta status names or assume they match ERPNext statuses before reviewing the API.

### 17.3 Return Status

Keep these separate:
- Return Pickup Status.
- Return Receipt Status.
- Inspection Status.
- Refund Status.

---

## 18. UX Requirements

1. When Create is sent to Bosta, show a Loading Indicator and the message `Sending to Bosta...`.
2. Prevent repeated Submit / Create clicks while an operation is in progress.
3. For a confirmed failure, show a clear Dialog with Retry.
4. For an Unknown result, show a Dialog explaining that the result is uncertain, with a Check with Bosta / Refresh action.
5. Do not offer a blind Create retry while the previous attempt is Unknown.
6. Show a Loading Indicator during cancellation.
7. If cancellation succeeds, show a success message and continue with Delivery Note cancellation.
8. If cancellation fails, do not cancel Delivery Note; show a Dialog explaining the reason and provide Retry.
9. If Bosta cancellation succeeds but ERPNext cancellation fails, explain that the shipment is already cancelled and that only the local ERPNext operation needs to be retried.
10. AWB, Tracking Number, Shipping Status, and data returned by Bosta must be Read Only.
11. Show Refresh only when needed.
12. Display Last Sync Time and the latest error clearly without overwhelming employees with technical details.
13. User-facing errors should be understandable; technical details belong in the Integration Log.

---

## 19. Items to Verify Before Implementation

The following points are not yet confirmed decisions. Do not assume Bosta supports them without checking its documentation or testing the API:

1. Does Bosta support an Idempotency Key? If so, what is its format?
2. Can a Delivery be searched using an ERPNext External Reference?
3. What is the exact shipment status lookup endpoint and response?
4. What exact response does Bosta return when cancelling a shipment that does not exist? Does `Shipment Not Found` definitively mean the shipment is absent or cancelled?
5. Which shipment statuses allow cancellation?
6. What is the Webhook payload? Does it include a signature or verification mechanism? Is there an Event ID or sequence number?
7. What are the official Bosta shipment statuses and their ordering rules?
8. What exact endpoint and fields are required for `type = 25` Customer Return Pickup?
9. What are the detailed COD rules for types 15 and 30?
10. Can the Airway Bill PDF be fetched or fetched again after shipment creation?
11. Can address, COD, or shipment details be updated after creation?
12. Does Bosta return Actual Shipping Cost during creation or only through settlement reports?
13. How can Collected COD and Remitted COD be queried?
14. What are Bosta API rate limits, request limits, and retry policies?
15. Can API behavior or status rules differ by account or service type?

These items must be tested in an appropriate environment before final implementation approval.

---

## 20. Acceptance Criteria

The integration is ready for acceptance when the following scenarios pass.

### Create Shipment
- [ ] Selecting Bosta in Delivery Note displays the required shipping fields.
- [ ] Address and customer data come from the correct shipping-address source.
- [ ] Estimated Shipping Cost is calculated using the Shipping Rule.
- [ ] Submit sends Create to Bosta.
- [ ] `Sending to Bosta...` is displayed.
- [ ] Delivery Note cannot become Submitted without confirmed shipment creation.
- [ ] AWB and Tracking Number are saved in Read Only fields.
- [ ] Airway Bill PDF is saved in Attachments.
- [ ] A timeout does not create a duplicate shipment.
- [ ] Lookup resolves Create Unknown before another Create is allowed.

### Tracking
- [ ] Webhooks update Shipping Status.
- [ ] Duplicate Webhooks are not processed twice.
- [ ] Old Webhooks do not move status backwards.
- [ ] Daily Reconciliation runs at 11:59 PM.
- [ ] Manual Refresh works and its result is logged.
- [ ] Shipping status appears on Delivery Note.
- [ ] Aggregated Delivery Note statuses appear correctly in the Sales Order list.

### Cancellation
- [ ] Delivery Note is not cancelled before Bosta cancellation is handled.
- [ ] Bosta cancellation failure prevents Delivery Note cancellation.
- [ ] Retry sends another cancellation attempt.
- [ ] Already Cancelled is treated as success.
- [ ] Shipment Not Found is treated as success only when its documented meaning is clear.
- [ ] If Bosta succeeds and ERPNext fails, the local operation can be retried without repeating external cancellation.

### Customer Return
- [ ] Sales Return defines the returned products and quantities.
- [ ] Selecting Bosta creates a Customer Return Pickup using `type = 25`.
- [ ] Returned COD accepts zero or a positive value.
- [ ] The value is converted to a negative amount for Bosta when required, without requiring the employee to enter a negative value.
- [ ] Pickup creation failure / Unknown does not create duplicate pickup requests.
- [ ] Pickup Status is separate from Warehouse Receipt and Refund Status.

### Returned to Sender
- [ ] A Bosta Returned status creates only one Draft Sales Return.
- [ ] An employee Notification is sent.
- [ ] The return is not submitted automatically.
- [ ] Stock is not restored before physical receipt and inspection.
- [ ] Return to Sender does not create a new Customer Return Pickup.

### Audit and Reconciliation
- [ ] All Create / Cancel / Refresh / Webhook operations are logged.
- [ ] API secrets are not exposed in logs or user-facing messages.
- [ ] COD, collection, remittance, and shipping cost are kept separate.
- [ ] The relationship between Sales Order, Delivery Note, Bosta Delivery, and Sales Return can be traced.

---

## 21. Final Workflow Summary

The agreed design is:

**Sales Order → Delivery Note → Bosta Create Delivery → Tracking / Webhooks → Delivery or Return**

For a customer return after delivery:

**Sales Return → Bosta Customer Return Pickup (`type = 25`) → Physical Receipt → Inspection → Stock / Refund Processing**

For a shipment the customer did not receive:

**Bosta Returned → Automatic Draft Sales Return → Employee Notification → Physical Receipt & Inspection → Return Approval**

The goal is for employees to manage sales fulfillment, shipping, and returns through the standard ERPNext documents, while the backend integration handles Bosta communication, status tracking, and error recovery—without compromising inventory accuracy or allowing duplicate shipments and cancellation operations.
