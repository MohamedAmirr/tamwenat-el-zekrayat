# Shopify integration: manual test checklist (production)

For the business owner. Plain-language, step by step, in the Desk UI of the production site. Do the sections in order: each one depends on the one before it. Tick the box when a step passes. When something does not match the "Expected result", stop, do not repeat the step many times, and send me what the "If it fails, send me" line asks for.

This checklist was written from the code. Labels, statuses and messages are quoted from it. Where the code does not tell us exactly what you will see, the step says "check on the first run": write down what you actually see and send it to me. Nobody has run these steps yet.

The dashboard part of section 10 is covered in more detail by [shopify-reconciliation-smoke-test-checklist.md](../marketplace-integrations/shopify-reconciliation-smoke-test-checklist.md). Run that checklist for the dashboard instead of repeating it here.

## Before you start

### Warnings (read first)

- Several steps change LIVE Shopify data: product prices (section 5), stock quantities and stock tracking (section 6), and orders (sections 7 to 9: you will place, edit, refund and cancel real orders). Those steps are marked **LIVE SHOPIFY CHANGE**.
- Use ONE test product (a single variant, with a SKU) for everything. Do not map or push any other product.
- Use a low-priced, refundable test order. Sections 8 and 9 need about five test orders in total. Refund or cancel every one of them in Shopify when you are done.
- Never use the buttons "Sync All", "Push selected", "Sync Inventory" (list menu) or "Reconcile Shopify Inventory" while your account has other mappings besides the test one: they push every selected or every enabled mapping to your live store. This checklist only uses the per-mapping buttons.
- Do not put passwords, Client Secrets or tokens in anything you send me. Send screenshots of the error text only.

### What must be ready

1. The Shopify custom app is installed on your store (created in Shopify's Dev Dashboard; this is a manual step on Shopify's side, tracked as #100). You need its Client ID and Client Secret (keep them private).
2. The app has these access scopes (from `docs/shopify/README.md`): `read_locations`, `read_products`, `write_products`, `read_inventory`, `write_inventory`, `read_orders`. Without `write_products` the price push fails; without `write_inventory` the stock push fails.
3. The site is currently on a Frappe Cloud trial. Shopify can only send orders to us while the site is reachable on the internet and not suspended. If the trial stops, nothing from Shopify arrives. Also note the site must be running its background workers and scheduler (Frappe Cloud does this by default); several steps below wait for scheduled jobs.
4. The webhook address Shopify calls is the site's own public URL plus `/api/method/marketplace_integrations.api.shopify_webhooks.webhook_event` (built from the configured site URL in `integrations/shopify/webhooks.py`, `get_webhook_callback_url()`). You do not type it anywhere: the "Sync Webhook Subscriptions" button registers it. It must be the address of the production site, not a preview or another site.
5. Log in as your own user with the **System Manager** role (not Administrator). Every screen in this checklist is limited to System Manager. If you see "Not permitted" anywhere, tell me which screen.
6. Optional check that the scheduled jobs exist: search "Scheduled Job Type" in the search bar, open the list, and look for rows whose method contains `sweep_changed_bins`, `retry_errored_webhook_deliveries` and `sweep_due_sales_order_syncs`. If any is missing, tell me before you go on (they are created by a site migrate, not by installing code).
7. In ERPNext, have ready: a Warehouse (a leaf warehouse, not a group) for Shopify stock, a Company, a Selling Price List whose currency equals the Shopify store currency, and one test Item for your test product (see step 4.1).

### How to read the Error Log

1. Type "Error Log" in the search bar and open the list.
2. The newest entries are at the top. Open the newest entry that was created when the step failed (check the time).
3. Copy the title and the full text of the error field (the long text with "Traceback"). Check on the first run which field name holds it.
4. Send me both. If there is no new Error Log entry, say so. Many messages in this checklist appear only on screen or on a record, not in the Error Log.

### Where the other things you will look at are

- **Marketplace Sync Log**: search bar, "Marketplace Sync Log". Each Sales Order sync attempt and each failed order import writes one row (fields Sync Type, Marketplace Account, Marketplace Order, Status, Trigger Source, Attempted At, Completed At, Request Data, Response Data, Traceback). Rows are deleted after 90 days.
- **Shopify Webhook Delivery**: search bar, "Shopify Webhook Delivery". One row per message Shopify sent us (fields Connection, Shop Domain, Topic, Webhook ID, Order GID, Status, Attempt Count, Received At, Processed At, Error). Status is one of Received, Processed, Error.
- The workspace **Tamwenat Marketplaces** (apps screen) has shortcuts to almost everything.

### Result table

Copy this table into your own notes and fill it in as you go (one row per step you ran; add rows as needed).

| Step | Date | Pass / Fail | Notes (what you saw, order number, time) |
| --- | --- | --- | --- |
| 1.1 | | | |
| 1.2 | | | |
| 1.3 | | | |
| ... | | | |

## 1. Shopify Connection (#101, #102)

- [ ] **Step 1.1: Create the connection**
  1. Open the workspace **Tamwenat Marketplaces** and click the shortcut **Shopify Connection** (or search "Shopify Connection"). Click **Add Shopify Connection** (new).
  2. Fill in **Connection Name** (any name you like), **Shop Domain** (only the host name, for example `your-store.myshopify.com`, no `https://`, no slash), **Client ID**, **Client Secret**. Leave **Previous Client Secret** empty. Tick **Enabled**.
  3. Click **Save**.
  - Expected result: it saves. The record gets an automatic name that starts with `SHOPCONN-`. Further down, the section **Webhook Subscriptions** shows **Registration Status** "Not Checked" (check on the first run) and the other fields empty. A button **Sync Webhook Subscriptions** appears at the top.
  - If it fails, send me: a screenshot of the red message and the Shop Domain value you typed (not the secret).

- [ ] **Step 1.2: The Shop Domain safety check**
  1. On the same record, change **Shop Domain** to `https://your-store.myshopify.com` (with the https part). Click **Save**.
  - Expected result: it refuses to save and shows: "Shop Domain must be a bare hostname containing only letters, digits, dots, and hyphens (no scheme, path, port, userinfo, or query/fragment)."
  2. Put the correct bare host name back and **Save** again.
  - Expected result: it saves.
  - If it fails, send me: the exact message you saw (or the fact that it saved with the `https://` value).

- [ ] **Step 1.3: There is no "Test Connection" button**
  1. Look at the top of the saved record.
  - Expected result: there is NO "Test Connection" button on a Shopify Connection (that button exists only for Noon). The only action button is **Sync Webhook Subscriptions**. The first real check that your Client ID and Secret work is when you save the Marketplace Account in section 2 (it reads the shop and its Locations from Shopify).
  - If it fails, send me: a screenshot if you do see a "Test Connection" button.

## 2. Marketplace Account bound to a Shopify Location (#103)

- [ ] **Step 2.1: Create the account**
  1. Workspace **Tamwenat Marketplaces**, shortcut **Marketplace Account**, then **Add Marketplace Account**.
  2. Set **Account Name** (any name), **Platform** = Shopify, **Connection** = the Shopify Connection from section 1. Do not type the **External Account ID**: for Shopify it is read-only and is filled in automatically.
  3. Fill **Warehouse** (a leaf Warehouse), **ERPNext Company**, **ERPNext Selling Price List**. Leave **ERPNext Customer** alone (read-only, filled automatically). Leave **Enabled** ticked.
  - Do not save yet.
  - Expected result: in the section **Shopify Inventory Location**, a button **Load Shopify Locations** is visible at the top (it needs the Connection to be filled in).

- [ ] **Step 2.2: Load the Shopify Locations**
  1. Click **Load Shopify Locations**.
  - Expected result: a short wait ("Loading active Shopify Locations..."), then a dialog **Select Shopify Location** with a drop-down **Active Location** listing your store's active locations as "Name (gid://shopify/Location/...)". If it says "No active Locations were returned by Shopify." your store has no active location; tell me.
  2. Choose the location where the test product's stock lives and click **Use Location**.
  - Expected result: **Shopify Location GID** and **Shopify Location Name** are filled.
  - If it fails, send me: the red message (a wrong Client ID or Secret shows "Failed to authenticate with Shopify: ..."; a missing scope shows a Shopify "Access denied" text), and the newest Error Log entry.

- [ ] **Step 2.3: Save**
  1. Click **Save**.
  - Expected result: it saves. **External Account ID** now shows a value starting `gid://shopify/Shop/`. **Shopify Location Name** shows the location's name. Buttons **Reconcile Shopify Inventory** and a **View** group (Orders, Product Mappings, Sync Log) appear.
  - If it fails, send me: the red message and a screenshot of the form (hide nothing secret; there is none on this form).

- [ ] **Step 2.4: A wrong Location is refused**
  1. Change **Shopify Location GID** to `gid://shopify/Location/1`. Click **Save**.
  - Expected result: it refuses with "Shopify Location gid://shopify/Location/1 is not an active Location on SHOPCONN-xxxxx." (with your connection's name).
  2. Press the browser's reload, or click **Load Shopify Locations** again and re-select the right location, and **Save**.
  - Expected result: it saves again.
  - If it fails, send me: the exact message.

- [ ] **Step 2.5: A group Warehouse is refused**
  1. Set **Warehouse** to a group warehouse (for example "All Warehouses" if it is offered). Click **Save**.
  - Expected result: "Marketplace Account Warehouse must be a leaf Warehouse, not a group Warehouse." If the group warehouse is not offered in the drop-down at all, that is also fine; write "not offered".
  2. Put the right leaf Warehouse back and **Save**.
  - If it fails, send me: the exact message.

- [ ] **Step 2.6: A second account for the same store is refused**
  1. Create another Marketplace Account with Platform Shopify and the same Connection (do not save the first one over). Click **Save**.
  - Expected result: "A Marketplace Account for platform Shopify and external account gid://shopify/Shop/... already exists (...)." Close this second form without saving (do not keep it).
  - If it fails, send me: the message, and whether a second account got created (then delete it).

- [ ] **Step 2.7: Disabled connection**
  1. Open the Shopify Connection, untick **Enabled**, **Save**.
  2. Open the Marketplace Account, click **Load Shopify Locations**.
  - Expected result: a red message "Shopify Connection SHOPCONN-xxxxx is disabled." (your connection's name). Shopify is not contacted.
  3. Open the Shopify Connection again, tick **Enabled**, **Save**.
  4. Back on the account, click **Load Shopify Locations** once more.
  - Expected result: the dialog from step 2.2 opens again (just close it).
  - If it fails, send me: the exact message. Make sure **Enabled** is ticked again before going on.

## 3. Webhooks (#104)

- [ ] **Step 3.1: Register the webhooks**
  1. Open the Shopify Connection. Click **Sync Webhook Subscriptions**.
  2. A confirmation box appears: "This will delete any of the 7 tracked webhook topics' subscriptions found registered at a URL other than this site's own callback URL, then re-create whatever is missing at this site's callback URL. If another environment (e.g. a different dev tunnel, or another site) shares this same Shopify app, its subscriptions can be deleted by this action. Continue?" If another site or a test environment uses this same Shopify app, stop and ask me. Otherwise confirm.
  - Expected result: a green message "All Shopify webhook subscriptions are registered." After the form reloads, the section **Webhook Subscriptions** shows **Registration Status** = Complete, **Missing Topics** empty, **Last Checked On** = now.
  - The seven topics are: orders/create, orders/updated, orders/cancelled, orders/paid, refunds/create, app/uninstalled, app/scopes_update.
  - If it fails, send me: the message. An orange message "Shopify webhook subscriptions are incomplete - see Missing Topics." means **Registration Status** = Incomplete (send me the **Missing Topics** text). A red message "Shopify webhook subscription sync had errors: ..." means Error (send me the text and **Last Webhook Error**). A red "Failed to sync Shopify webhook subscriptions: ..." also writes an Error Log entry called "Shopify webhook subscription sync failed" (send me that).

- [ ] **Step 3.2: Pressing it again is harmless**
  1. Click **Sync Webhook Subscriptions** again and confirm.
  - Expected result: the same green message, **Registration Status** still Complete, **Last Checked On** updated.
  - If it fails, send me: the message and the field values.

- [ ] **Step 3.3: Look at the delivery list (it may still be empty)**
  1. Open **Shopify Webhook Delivery** (workspace shortcut or search).
  - Expected result: the list opens. It may be empty now; rows appear when Shopify sends something (from section 7). Do not create rows by hand.
  - How to read a row later: **Topic** is written like ORDERS_CREATE; **Status** Received (arrived, not finished), Processed (done) or Error (failed, see **Error**); **Attempt Count** is how many times we tried (up to 5); **Order GID** is Shopify's order id.
  - If it fails, send me: the exact message if you cannot open the list.

- [ ] **Step 3.4: Know what a duplicate, a forged message and a retry look like (read only)**
  Nothing to do here. You cannot and should not fake requests. This is what you will be able to see from the screens:
  - **Duplicate**: Shopify sometimes sends the same message twice. You will still see only ONE row per **Webhook ID**. If the first attempt already says Processed, the second is silently ignored. If the first attempt was in Error, the second one processes the same row again and **Attempt Count** goes up.
  - **Bad signature**: a message whose signature does not match is refused with "Invalid Shopify webhook." and NO row is created in Shopify Webhook Delivery and no Error Log entry is written. So the only sign is on Shopify's side (its webhook delivery report), not in ERPNext. A missing row for an order you know exists is the symptom; tell me.
  - **Hourly retry**: every hour the system retries rows with Status Error (and rows stuck on Received for more than 15 minutes). **Attempt Count** goes up by one each time. After 5 attempts it gives up, and the row stays Error; it then appears in **Marketplace Needs Attention** as "Webhook Retries Exhausted".
  - [ ] I have read this.

## 4. Products and variant mapping (#105, #168)

- [ ] **Step 4.1: Prepare the test product in ERPNext**
  1. In Shopify, pick your ONE test product (one variant, with a SKU). Note its price and SKU.
  2. In ERPNext, create an Item for it (or use an existing one). It must be enabled, a stock item (the **Maintain Stock** option ticked), not a template with variants. Give it an **Item Price** in the same Selling Price List you set on the account, in the same currency as the Shopify store, with a rate that is different from the Shopify price by a small amount you can recognise (for example +1).
  - Expected result: the Item and its Item Price are saved.
  - If it fails, send me: the message.

- [ ] **Step 4.2: Run the catalog sweep from "Marketplace Products"**
  1. Open the page **Marketplace Products** (workspace shortcut). In the field **Marketplace Account** pick your Shopify account.
  2. Click **Run Catalog Sweep** (top right).
  - Expected result: the status line shows "Queuing catalog sweep...", then "Catalog sweep running: N processed so far.", then "Catalog sweep complete: N variant(s) need a mapping." (or "...no unmapped variants found."). A table appears with the columns **Variant GID**, **SKU**, **Shopify Product** and an editable **Item** column. Your test variant is in it. (The sweep only reads Shopify; nothing is changed there.)
  - If it fails, send me: the status line text, and the newest Error Log entry.

- [ ] **Step 4.3: Map the test variant to the Item**
  1. In the row of the test variant, click the **Item** cell and choose your test Item. Confirm if the grid asks you to.
  - Expected result: a green toast "Mapping saved: gid://shopify/ProductVariant/... → ITEM-NAME". The row disappears from the list (mapped products drop out).
  - If it fails, send me: the toast or red message.

- [ ] **Step 4.4: Check the saved mapping**
  1. Open **Marketplace Product Mapping** (workspace shortcut) and open the new row.
  - Expected result: **Marketplace Account** = your account; **Item** = your Item; **External Reference** and **External Variant ID** = the same `gid://shopify/ProductVariant/...` value; **External Product ID**, **External SKU** (and **External Barcode** if the product has one) filled; **Enabled** ticked; **Sync Status** = Not Synced. Buttons **Push to Shopify** and **Sync Shopify Inventory** are at the top.
  - If it fails, send me: a screenshot of the mapping and the Error Log entry if there is one.

- [ ] **Step 4.5: The exception categories on the "Shopify Catalog Mapping" page**
  1. Open the page **Marketplace Shopify Catalog Mapping** (workspace shortcut; its title is **Shopify Catalog Mapping**). Pick the account and click **Run Catalog Sweep**.
  - Expected result: "Catalog sweep complete: N exception(s) found." (or "no exceptions found."). The table has the columns **Exception**, **Variant GID**, **SKU**, **Barcode**, **Shopify Product**, **Item**, **Actions**. The test variant is no longer listed. Every other variant of your store shows as **Unmapped**. A mapped row would show an **Open mapping** link.
  2. (Optional, **LIVE SHOPIFY CHANGE**, deletes a Shopify product) To see **Deleted**: create a second throwaway product in Shopify, run the sweep, map it to the same or another Item here, then delete that throwaway product in Shopify and run the sweep again.
  - Expected result: that variant now shows in the **Exception** column as **Deleted**. Afterwards open its mapping and delete the mapping.
  3. (Optional) To see **Variant-less**: in Marketplace Product Mapping create a mapping with your account, your Item and **External Reference** `TEST-NOT-A-GID`, save, run the sweep.
  - Expected result: that row shows as **Variant-less**. If Save refuses the value, send me the message and skip the rest. Afterwards delete this mapping.
  - If it fails, send me: the status line, a screenshot of the table, and the Error Log entry. If the status line says "N row(s) failed to refresh, see Error Log.", send me the Error Log entries too.

- [ ] **Step 4.6: "Marketplace Product Management" (read only)**
  1. Open the page **Marketplace Product Management**. It has two blocks: **ERPNext Items** (buttons **Export Items**, **Import Items**) and **Marketplace Mappings** (buttons **Export Mappings**, **Import Mappings**).
  2. Click only **Export Mappings**. Do NOT use any Import button or **Apply reviewed changes** in this test.
  - Expected result: you get an .xlsx file (it may run as a background job first: "Background job queued..."; check on the first run). Open it: your test mapping is in it.
  - If it fails, send me: the message shown on the page.

## 5. Price publishing (#106)

**LIVE SHOPIFY CHANGE.** This changes the price of your real test product in Shopify. Only the price is sent (never title, SKU or barcode).

- [ ] **Step 5.1: Push the price**
  1. Open the test mapping from step 4.4. Click **Push to Shopify**.
  - Expected result: "Queuing Shopify price push...", then a box titled **Pushing to Shopify** that counts up and ends with "Push complete: 1 succeeded, 0 failed, 0 skipped." and a table (Mapping, Status, Error) with Status **Synced**. Close the box. The mapping reloads with **Sync Status** = Synced, **Last Synced On** filled, **Last Error** empty.
  - If it fails, send me: the box's text (Status and Error columns), the mapping's **Last Error**, and the newest Error Log entry. Typical messages: "Price List X's currency (A) does not match the Shopify shop's currency (B).", "Item X has no current Item Price in Price List Y.", or "Shopify did not accept the price update." (the last usually means the `write_products` scope is missing).

- [ ] **Step 5.2: Check in Shopify**
  1. In Shopify admin open the test product.
  - Expected result: the variant's price equals your ERPNext Item Price rate. Title, SKU and barcode are unchanged.
  - If it fails, send me: the ERPNext rate, the Shopify price and the mapping's **Sync Status**.

- [ ] **Step 5.3: Prices are only pushed when you press the button**
  1. Change the Item Price rate in ERPNext by a small amount. Wait 5 minutes. Look at the product in Shopify.
  - Expected result: Shopify's price does NOT change by itself (there is no automatic price sync). Press **Push to Shopify** again to bring it into line, and check Shopify shows the new price.
  - If it fails, send me: what Shopify shows and when it changed.

- [ ] **Step 5.4: Where price errors show**
  1. Open the **Marketplace Product Mapping** list. If a mapping had an error it shows a red **Error** indicator, and the button **Sync Errors** filters the list to those.
  - Expected result: your test mapping shows **Synced** (green). (If you want to see an error, tell me first; it needs a deliberate setup change.)
  - If it fails, send me: a screenshot of the list.

## 6. Inventory sync (#107)

ERPNext is the master for stock. The quantity ERPNext holds in the account's Warehouse is pushed to Shopify as the "On hand" quantity at the bound Location (whole units, never below 0). Nothing from Shopify ever changes ERPNext stock.

**LIVE SHOPIFY CHANGE.** The first sync of a variant may switch on stock tracking for it in Shopify and activate it at your Location. Do this only for your ONE test variant.

- [ ] **Step 6.1: Put stock in ERPNext**
  1. In ERPNext create a **Stock Entry** of type Material Receipt for the test Item, 7 units, target Warehouse = the account's Warehouse. **Submit** it.
  - Expected result: the Item's stock in that Warehouse is 7 (check Stock Balance or the Item's dashboard).
  - If it fails, send me: the message.

- [ ] **Step 6.2: Push stock by hand once**
  1. Open the test mapping and click **Sync Shopify Inventory**.
  - Expected result: "Queuing Shopify inventory sync...", then a box titled **Syncing Shopify inventory** ending with "Sync complete: 1 succeeded, 0 failed, 0 skipped." The mapping reloads; in the section **Shopify Inventory Sync**: **Inventory Sync Status** = Synced, **Inventory Last Synced On** filled, **Inventory Last Error** empty.
  - In Shopify the test product shows **On hand** = 7 at your Location.
  - If it fails, send me: the box text, **Inventory Last Error**, the Error Log entry. Typical messages: "Marketplace Account X requires a Warehouse for Shopify inventory sync.", "Marketplace Account X's Warehouse Y belongs to Company A, not the account's configured Company B.", "Shopify Location ... is no longer an active Location on Connection ...", "Item is not a stock Item.", "Item is a template (has variants).".

- [ ] **Step 6.3: Stock increase reaches Shopify by itself**
  1. In ERPNext submit another Material Receipt of 3 units (total 10) into the same Warehouse. Do nothing else.
  2. Wait up to 5 minutes (a background check runs every 2 minutes, and then the push runs). Reload the mapping.
  - Expected result: **Inventory Last Synced On** is newer; Shopify shows **On hand** = 10.
  - If it fails, send me: the time of the Stock Entry, the mapping's three inventory fields, and the current Shopify quantity. Also check that the scheduled jobs from "What must be ready" item 6 exist.

- [ ] **Step 6.4: Stock decrease reaches Shopify**
  1. Submit a Stock Entry of type Material Issue of 4 units from that Warehouse (total 6). Wait up to 5 minutes.
  - Expected result: Shopify **On hand** = 6; the mapping shows a newer **Inventory Last Synced On**.
  - If it fails, send me: the same as step 6.3.

- [ ] **Step 6.5: Where errors show**
  1. Open the Marketplace Product Mapping list. A mapping with an inventory error shows a red **Inventory Error** indicator, and the button **Inventory Errors** filters to them. The error text is in the mapping's **Inventory Last Error**. The **Marketplace Channel Status** report counts both price and inventory errors under **Product Sync Errors** (section 10).
  - Expected result: your test mapping shows no error.
  - If it fails, send me: a screenshot and the **Inventory Last Error** text.

- [ ] **Step 6.6: Know the rules (read only)**
  - The system also re-pushes everything once a day.
  - **Mark each Shopify order as fulfilled in Shopify, AND post the stock-out in ERPNext** (a submitted Delivery Note, or a Sales Invoice with Update Stock ticked). Importing an order creates a Sales Order but never reduces stock by itself. If the stock-out is not posted in ERPNext, the next sync puts the old quantity back in Shopify.
  - [ ] I have read this.

## 7. Order import (#108, #109, #126)

**LIVE SHOPIFY CHANGE.** You place a real order now. Name it "Order O1" in your notes.

- [ ] **Step 7.1: Place Order O1**
  1. Place an order for 2 units of the test product (storefront checkout, or create it in Shopify admin), with your real name, email, phone and a full delivery address, and make sure it is paid (the order must show as paid; only PAID or PARTIALLY_PAID orders get a Sales Order).
  - Expected result: Shopify shows the order as paid.
  - If it fails, send me: the Shopify order number and what Shopify says.

- [ ] **Step 7.2: The webhook arrived**
  1. Wait 1 to 2 minutes. Open **Shopify Webhook Delivery** and press the list's refresh.
  - Expected result: new rows for your connection, for example topics ORDERS_CREATE, ORDERS_PAID and possibly ORDERS_UPDATED, with **Status** = Processed, **Attempt Count** = 1, **Order GID** `gid://shopify/Order/...`, **Received At** and **Processed At** filled, **Error** empty. (One row per **Webhook ID**, even if Shopify sent twice.)
  - If it fails, send me: for each row Topic, Status, Attempt Count and the Error text; or "no rows at all" (then the webhook address or the trial site is the first suspect). A Processed row whose **Error** field says "Shopify order ... could not be refetched (not found or outside the read_orders window)." means the order could not be read (older than 60 days or deleted).

- [ ] **Step 7.3: The connection's health fields**
  1. Open the Shopify Connection. Look at the section **Webhook Delivery Health**.
  - Expected result: **Last Delivery At**, **Last Delivery Topic** and **Last Successful Processing At** are filled; **Last Webhook Error** is empty.
  - If it fails, send me: the four values.

- [ ] **Step 7.4: The Marketplace Order exists with the customer and address**
  1. Open the list **Marketplace Order** (workspace shortcut **Shopify Orders**). Open Order O1 (its **Order Code** is the Shopify order name/number).
  - Expected result, tab **Overview**: **Platform** = Shopify, **Marketplace Account** = your account, **External Order ID** = the order's gid, **Order Code** = the Shopify order name, **External Financial Status** and **External Fulfillment Status** filled from Shopify (the top of the form shows two indicators "Financial: ..." and "Fulfillment: ..."). Section **Customer and Delivery**: **Customer First Name**, **Customer Last Name**, **Customer Email**, **Customer Phone Number**, **Customer Formatted Address**, **Customer City** match what you typed at checkout; **Billing Address** and **Shipping Address** hold the raw address data.
  - Tab **Payment & Promotions**: **Sub Total**, **Discount**, **Order Total**, **Total Taxes**, **Shipping Total**, **Shop Currency**, **Presentment Currency** match Shopify.
  - Tab **Items & Price Comparison**: the line with quantity 2, and in the section **Live Mapping and Price Comparison** a line like "gid://shopify/ProductVariant/...: ITEM-NAME" (it says "Unmapped" if the variant has no mapping, and "No Shopify variant" for a custom line).
  - If it fails, send me: screenshots of the three tabs and which values differ from Shopify. Blank customer or address fields can mean the app lacks permission to read customer details; send me which are blank.

- [ ] **Step 7.5: "Refresh from Shopify" on one order**
  1. On Order O1 click **Refresh from Shopify**.
  - Expected result: "Refreshing from Shopify...", the form reloads, nothing duplicates (still one Marketplace Order for this order) and the values stay correct. This button only re-reads the order; it never creates a Sales Order.
  - If it fails, send me: the red message and the Error Log entry.

- [ ] **Step 7.6: Import by date range**
  1. On the **Marketplace Order** list click **Retrieve from Shopify**. In the dialog **Retrieve Shopify Orders** pick **Marketplace Account**, **Start Date** (yesterday) and **End Date** (today). Click **Retrieve**.
  - Expected result: a message "Shopify order import queued: ..." then a box **Retrieving Shopify orders** ending with "Retrieval complete: N succeeded, 0 failed, 0 skipped." with a table (Order, Status, Error). No second copy of Order O1 appears in the list. A range over 60 days is refused with "Shopify order imports are limited to 60 days." (try it only if you want to see that message), and a Start Date later than the End Date is refused with "Start Date must be before or equal to End Date.". Importing by date range never creates Sales Orders either.
  - If it fails, send me: the box text (Status and Error columns) and the Error Log entry. Failed orders also get a row in Marketplace Sync Log (Sync Type "Order Import", Status Error) and in the report **Shopify Order Import Failures**.

- [ ] **Step 7.7: Only if some delivery row shows Error: watch the hourly retry**
  1. Note the row's **Attempt Count**. Wait for the next full hour plus 5 minutes and reload the row.
  - Expected result: **Attempt Count** is one higher and Status is Processed if the cause is gone, or still Error with a newer **Error**. After 5 attempts it stops.
  - If it fails, send me: the row's Topic, Status, Attempt Count, Error, and the Error Log entries titled "Shopify webhook processing failed (...)" or "Shopify webhook retry sweep: row failed".
  - [ ] Not needed (no Error rows) / [ ] Done.

## 8. Sales Order creation and automatic sync (#110, #130, #132, #133)

How it works: when an order webhook has been imported, the system waits a **debounce** of **45 seconds** (every new message for the same order pushes the wait out again), and a check that runs **every minute** picks it up. So expect the Sales Order roughly **1 to 2 minutes** after the last message for that order.

- [ ] **Step 8.1: The automatic sync log row**
  1. About 2 minutes after step 7.1, open **Marketplace Sync Log**.
  - Expected result: a row with **Sync Type** = Sales Order Sync, **Marketplace Account** = your account, **Marketplace Order** = Order O1, **Status** = Success, **Trigger Source** = Shopify Webhook.
  - If it fails, send me: all rows for O1 (Status, Trigger Source, **Traceback** and **Response Data**). A Status Invalid row is an expected blocker (the text is in **Response Data**); Status Error also has a Traceback. Also send the newest Error Log entry titled "Marketplace Sales Order synchronization".

- [ ] **Step 8.2: The "ERPNext Sync" tab**
  1. Open Order O1, tab **ERPNext Sync**.
  - Expected result: **ERPNext Sales Order** is a link, **ERPNext Sync Status** = Synced, **Last ERPNext Sync On** is filled, **Last Sync Error** is empty. In the list the order shows a green **Synced** indicator. The four possible statuses are: Synced (green), Not Synced (grey), Modified After Sync (orange), Sync Error (red).
  - If it fails, send me: the status, **Last Sync Error** and a screenshot. Typical Sync Error / blocker messages: "No enabled Shopify variant mapping exists for line ...", "Shopify order ... is not financially eligible for synchronization (...)", "Marketplace Account ... needs an ERPNext Company before synchronizing orders.", "Marketplace Account ... needs an ERPNext Selling Price List before synchronizing orders.", "ERPNext Selling Price List ... must be enabled and marked for selling."

- [ ] **Step 8.3: The Sales Order itself**
  1. Click the **ERPNext Sales Order** link.
  - Expected result: the Sales Order is already **Submitted** (the system submits it right away; it is not left as a draft). Customer is an ERPNext Customer created from the buyer's email, with name and phone (an order without email uses a generic Customer named after the account). The item is your test Item, quantity 2, and the **Rate** equals the Shopify price per unit AFTER discounts and BEFORE tax (compare with the Marketplace Order's Items tab, **Transaction Amount** divided by quantity), not your ERPNext Item Price. Shipping and taxes are not copied as lines. The **Warehouse** is the account's Warehouse. Delivery date = the order's processed date. In the buyer's Customer you find an Address titled "Shopify Marketplace Address-..." set as the primary address, built from the shipping address (billing if there is no shipping). The Sales Order has a read-only link to the Marketplace Order (field **Marketplace Order**; check on the first run where it sits on the form).
  - If it fails, send me: screenshots of the Sales Order (items and totals) and the Marketplace Order's Items tab.

- [ ] **Step 8.4: Later price changes do not change the Sales Order**
  1. Change the test Item's Item Price in ERPNext. Reopen the Sales Order and the Marketplace Order.
  - Expected result: the Sales Order's rate is unchanged and the order still shows **Synced**.
  - If it fails, send me: the rate before and after, and the sync status.

- [ ] **Step 8.5: Editing the Shopify order updates the Sales Order**
  **LIVE SHOPIFY CHANGE** on Order O1 (still unfulfilled and not delivered).
  1. In Shopify edit Order O1 and reduce the quantity from 2 to 1, and confirm the edit (Shopify may ask you to refund the difference; do so).
  2. Wait 2 minutes. Open Order O1 and its Sales Order.
  - Expected result: new Shopify Webhook Delivery rows (for example ORDERS_UPDATED, maybe REFUNDS_CREATE) with Status Processed; Order O1's item shows the new quantity; a new **Marketplace Sync Log** row (Sales Order Sync, Success, Trigger Source Shopify Webhook); the **same** Sales Order now has quantity 1 and the lower amount (no second Sales Order); **ERPNext Sync Status** = Synced. For a short moment before the sync runs, the status can read **Modified After Sync**; that is normal.
  - If it fails, send me: the Sync Log rows (Status, Response Data), the Order's **Last Sync Error**, and the Sales Order quantity. If the edit triggered a refund you may also get the notification from step 9.3.

- [ ] **Step 8.6: Manual button (only if you meet it)**
  1. If an order shows **Not Synced**, **Modified After Sync** or **Sync Error**, its form shows a button **Send to ERPNext** (when there is no Sales Order yet) or **Update ERPNext Order** (when there is one and the status is not Synced). Click it.
  - Expected result: "Synchronizing with ERPNext...", the form reloads. If the order cannot be synced, a red box titled **ERPNext Sync Blocked** gives the reason. The Marketplace Sync Log gets a row with **Trigger Source** = Manual. In the Marketplace Sync Log list, selecting Error rows and clicking **Retry Selected Errors** does the same as a re-sync.
  - If it fails, send me: the box text.
  - [ ] Not needed / [ ] Done.

## 9. Cancel and refund cases (#136, #178, #179, #180, #183)

**LIVE SHOPIFY CHANGE** in every step: you place and then cancel or refund real test orders. After each test, refund/cancel the order in Shopify if the test did not already do it, and cancel any Delivery Note you made so your test stock is right again.

- [ ] **Step 9.1: Cancel before delivery (Order A)**
  1. Place a new paid test order "Order A" (1 unit). Wait until its Sales Order exists (about 2 minutes) and shows **Synced**.
  2. In Shopify cancel Order A (with refund). Do NOT create a Delivery Note in ERPNext.
  3. Wait 2 minutes. Open Order A and its Sales Order.
  - Expected result: new Webhook Delivery rows (for example ORDERS_CANCELLED) with Status Processed; Order A shows **Cancelled At** filled (and a cancelled state in the order's Cancellation section); the Sales Order is **Cancelled** automatically; a Marketplace Sync Log row with Status Success; the order's **ERPNext Sync Status** = Synced (check on the first run).
  - If it fails, send me: the Sync Log rows (Status, Response Data), **Last Sync Error**, and the Sales Order's status.

- [ ] **Step 9.2: Partial refund before delivery (Order B)**
  1. Place a paid test order "Order B" with 2 units. Wait for its Sales Order (**Synced**).
  2. In Shopify refund 1 of the 2 units (leave the order open). If Shopify offers to restock the item, do not restock: ERPNext stock is the master and would overwrite it anyway.
  3. Wait 2 minutes.
  - Expected result: a Webhook Delivery row REFUNDS_CREATE, Processed; Order B's **Refunds** field (tab Payment & Promotions) is filled and its financial status becomes PARTIALLY_REFUNDED; the same Sales Order now has quantity 1; **ERPNext Sync Status** = Synced. In the Marketplace Order list the button **Show Refunded Orders** shows Order B.
  - If it fails, send me: the same items as step 9.1.

- [ ] **Step 9.3: The refund notification (#136)**
  1. After step 9.2 look at the bell (notifications) at the top of Desk, and your email.
  - Expected result: a notification "Refund detected on Marketplace Order <order name>" with the text "Refund evidence was recorded for order <order name>." and a link **View Marketplace Order**. It is addressed to every user with the System Manager role. Where it appears (bell only, or also email) depends on the site's settings; check on the first run and tell me.
  - If it fails, send me: whether nothing arrived anywhere, and the time of the refund.

- [ ] **Step 9.4: Refund after delivery (Order C)**
  1. Place a paid test order "Order C" (1 unit). Wait for the **Synced** Sales Order.
  2. In ERPNext open its Sales Order, click **Create > Delivery Note**, and **Submit** the Delivery Note (this reduces your test stock).
  3. In Shopify refund Order C. Wait 2 minutes, then open Order C.
  - Expected result: **ERPNext Sync Status** = Modified After Sync (orange) and **Last Sync Error** says "Marketplace return or refund evidence needs a compensating ERP transaction." Nothing is changed or reversed in ERPNext. The order appears in the report **Marketplace Needs Attention** (section 10). The system does NOT create the return for you.
  - If it fails, send me: the sync status, **Last Sync Error**, and the Sync Log rows (Status and Response Data).

- [ ] **Step 9.5: Cancel after delivery (Order D)**
  1. Place a paid test order "Order D" (1 unit), wait for the **Synced** Sales Order, create and **Submit** a Delivery Note for it.
  2. In Shopify cancel Order D (with refund). Wait 2 minutes.
  - Expected result: **ERPNext Sync Status** = Modified After Sync. **Last Sync Error** is one of: "Marketplace return or refund evidence needs a compensating ERP transaction." (if a refund was recorded) or "The cancelled marketplace order is already fully delivered in ERPNext; a return or other compensating transaction is needed.". The Sales Order stays untouched.
  - If it fails, send me: the exact **Last Sync Error** and the Sync Log rows.
  - Optional variant for a billed order: use a submitted Sales Invoice (without delivery) instead of the Delivery Note. Expect Modified After Sync with a message such as "A submitted Sales Order line is already billed in ERPNext." or "ERPNext delivery or billing execution blocks an automatic submitted-order update."

- [ ] **Step 9.6: Close a Sales Order in ERPNext: Shopify cannot be cancelled from ERPNext yet (#183)**
  1. Use Order O1 (its Sales Order is Submitted, with nothing delivered). Open the Sales Order and use the **Status** menu to **Close** it (exact menu location: check on the first run).
  - Expected result: ERPNext closes the Sales Order as usual. Then a box titled **Cancel on Shopify** (orange) appears with: "Cancel the order in Shopify; ERPNext cannot cancel Shopify orders yet." and a bullet "Shopify order cancellation from ERPNext is not available yet.". There is no "Cancel on Shopify" button to press, because that action is not built. Nothing is sent to Shopify.
  - If it fails, send me: a screenshot of what appeared (or "no box").

- [ ] **Step 9.7: What the system says about a Closed order**
  1. Wait up to about 1 hour (a check runs hourly) and open Order O1.
  - Expected result: **ERPNext Sync Status** = Modified After Sync, **Last Sync Error** begins "The Sales Order is Closed in ERPNext but the marketplace still expects fulfillment; reopen it or review the order." (check on the first run; wording may differ if something else changed on the order). It also shows in **Marketplace Needs Attention**.
  - If it fails, send me: the status, **Last Sync Error** and the time you closed the Sales Order.

- [ ] **Step 9.8: Cancel a Sales Order in ERPNext**
  1. On Order O1's Sales Order use **Status > Re-open** (check on the first run), then on Order O1 click **Update ERPNext Order**. Expected: **Synced** again. Then **Cancel** the Sales Order (the normal Cancel button) and confirm.
  - Expected result: after the cancel, the same box **Cancel on Shopify** appears with the same two lines as in step 9.6. About an hour later Order O1 shows Modified After Sync with a message starting "The linked ERPNext Sales Order is no longer an active submitted document." followed by "Cancel the order in Shopify; ERPNext cannot cancel Shopify orders yet."
  2. Now cancel Order O1 in Shopify (with refund).
  - Expected result: after about 2 minutes the Order shows **Cancelled At**; when the system next checks it, the status returns to Synced ("The cancelled marketplace order is already cancelled or closed in ERPNext."), which can take up to an hour.
  - If it fails, send me: the boxes you saw, **Last Sync Error** (before and after) and the Sync Log rows.

- [ ] **Step 9.9: Clean up**
  1. Refund/cancel any of orders A, B, C, D, O1 still open in Shopify. Cancel the Delivery Notes you submitted. Make sure the test Item's ERPNext stock is what you want (a Material Receipt/Issue fixes it); wait for the stock sync.
  - Expected result: nothing open on your Shopify test product.

## 10. Operations views (#139, #167, #169)

For the full dashboard check, run [shopify-reconciliation-smoke-test-checklist.md](../marketplace-integrations/shopify-reconciliation-smoke-test-checklist.md) instead of repeating it here. This section only checks that the views show what you just did.

- [ ] **Step 10.1: The workspace**
  1. Open the apps screen and click **Tamwenat Marketplaces**.
  - Expected result: shortcuts including **Marketplace Channel Status**, **Marketplace Account**, **Marketplace Needs Attention**, under Shopify **Shopify Connection**, **Marketplace Shopify Catalog Mapping**, **Shopify Orders**, and also **All Marketplace Orders**, **Marketplace Product Mapping**, **Marketplace Products**, **Marketplace Product Management**, **Marketplace Sync Log**, **Shopify Webhook Delivery**, **Shopify Reconciliation Summary**, **Shopify Order Import Failures**. Click each one once: each opens without "Not permitted". (A separate workspace "Shopify Reconciliation" also exists.)
  - If it fails, send me: which shortcut is missing or fails and the message.

- [ ] **Step 10.2: Marketplace Channel Status**
  1. Open the report **Marketplace Channel Status**.
  - Expected result: one row per account with the columns **Account**, **Platform**, **Enabled**, **Channel Status** (OK green, Warning orange, Error red, Disabled grey), **Orders Needing Attention**, **Product Sync Errors**, **Import Errors**, **Fulfillment Errors**, **Last Checked**. Your Shopify account shows Error if orders C or D (or O1) still need attention, otherwise OK (a **Registration Status** other than Complete, or a **Last Webhook Error** on the connection, also turns it Warning/Error). Clicking a non-zero count opens the matching list or report.
  - If it fails, send me: a screenshot of the row and the connection's **Registration Status** and **Last Webhook Error**.

- [ ] **Step 10.3: Marketplace Needs Attention**
  1. Open the report **Marketplace Needs Attention**. Its filters are **Platform**, **Account** and **Problem**.
  - Expected result: columns **Problem**, **Platform**, **Account**, **Reference Type**, **Reference**, **What Happened**, **Detected**, **Next Action**. Orders C and D (and O1 if it was left in a Modified After Sync state) appear with Problem "Modified After Sync" and the message you saw in section 9. After you fix an order (for example with **Update ERPNext Order**), it disappears. Problem types you can meet: Sync Error, Modified After Sync, Unmapped Items, Order Import Error, Webhook Retries Exhausted.
  - If it fails, send me: a screenshot and the expected-but-missing order.

- [ ] **Step 10.4: Shopify Reconciliation Summary**
  1. Open the report **Shopify Reconciliation Summary**. Set **From Date** and **To Date** to today.
  - Expected result: columns **Metric**, **Value**, **Detail**, ten metrics; each says "(current)" or "(in selected range)". The number of orders received in range matches your test orders; the refunded amount reflects your refunds.
  - If it fails, send me: a screenshot and which number disagrees with what you did.

- [ ] **Step 10.5: Shopify Order Import Failures**
  1. Open the report **Shopify Order Import Failures**.
  - Expected result: columns **Log**, **Marketplace Account**, **Trigger Source**, **Attempted At**, **Request Data**, **Error**. It is empty if no import failed during your tests.
  - If it fails, send me: the rows (Trigger Source, Error).

- [ ] **Step 10.6: List shortcuts**
  1. On the **Marketplace Order** list, click **Needs Attention** (opens that report) and **Show Refunded Orders** (filters to Shopify orders that are REFUNDED or PARTIALLY_REFUNDED; Order B and C should be there).
  - If it fails, send me: a screenshot.

## Not built, do not test

- **Cancel a Shopify order from ERPNext (#182).** Closing or cancelling a Sales Order only shows the explanatory box from step 9.6; it never cancels anything in Shopify (the only built "cancel on the platform" path is for Talabat).
- **Shopify Payments transactions and payouts (#111).** Not built.
- **Shipping data for carrier integrations (#156).** Not built.
- **Creating the Shopify custom app (#100).** A manual step on Shopify's side, done before this checklist.
- Do not uninstall the app from Shopify as a test: the "app/uninstalled" message disables the Shopify Connection.

## Known gaps (verified in code)

- No "Test Connection" button on Shopify Connection (`shopify_connection.js` only adds "Sync Webhook Subscriptions").
- A webhook with a bad signature leaves no trace in ERPNext (`api/shopify_webhooks.py`: it throws "Invalid Shopify webhook." before any row is created).
- Manual import (Retrieve from Shopify) and "Refresh from Shopify" never create a Sales Order; only a webhook, the scheduled order check for already-linked orders, or the manual "Send to ERPNext" / "Update ERPNext Order" button do.
- Prices are pushed only by the "Push to Shopify" button (or the list actions); nothing pushes a price automatically.
- There is no fulfillment sync to Shopify; you mark orders fulfilled in Shopify yourself (`docs/shopify/README.md`, Inventory section).
- Delivered, billed, refunded-after-delivery and returned cases are only flagged ("Modified After Sync"); no return or credit note is created automatically.
- Shopify orders older than 60 days cannot be imported (the date range limit and Shopify's `read_orders` window).
- `docs/shopify/README.md` ("Controlled Sales Order handoff (#110)") still says the Sales Order is created as a Draft; the code now submits a fresh Sales Order immediately (`services/sales_order_sync.py`, #131).
