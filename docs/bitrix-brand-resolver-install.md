# Governed brand resolution: Bitrix server installation

This extension supplies **custom** REST methods `panda.brand.preview` (read only)
and `panda.brand.resolve` (write). They are not standard Bitrix API methods.
Panda uses them through its existing activation service and ToolGateway. A
brand-bearing product cannot be written if the extension is absent or rejects
its request. There is no fallback to writing a brand name into an element link.

## Install after reviewing the PR

Requirements: Bitrix `rest` and `iblock` modules, PHP `mbstring` and `intl`,
MySQL/MariaDB with connection-level `GET_LOCK` support, and a REST identity with
the catalog scope. All application nodes must use the same primary database.
The resolver deliberately refuses a dictionary exceeding 10,000 elements rather
than interpreting an incomplete list as absence.

1. Back up the existing `local/php_interface/init.php`. Copy the reviewed
   `integrations/bitrix/server/panda_brand_rest.php` into
   `<document-root>/local/php_interface/panda_brand_rest.php`. Do not expose it
   as a separate HTTP endpoint or edit Bitrix core files.
2. Add the following to the existing initialization file, preserving its contents.
   Replace `PANDA_REST_USER_ID` with the integer ID of the authorized service user;
   it is not a webhook secret. Configure target IDs from the actual installed
   schema. These example IDs are the user's current Panda installation.

   ```php
   require_once __DIR__ . '/panda_brand_rest.php';
   PandaBrandRest::configure([
       'allowed_user_ids' => [PANDA_REST_USER_ID],
       'brand_iblock_id' => 12,
       'catalog_iblock_id' => 14,
       'brand_property_id' => 100,
   ]);
   AddEventHandler('rest', 'OnRestServiceBuildDescription', [PandaBrandRest::class, 'methods']);
   ```

3. The identity must have full read visibility of the brand dictionary and rights
   to add its elements. Hidden elements fail closed so they cannot be duplicated.
   Each call verifies that the configured property is `BRAND`, type `E`, on the
   configured catalog and links to the configured brand iblock. No client input
   selects a different dictionary. Give the integration only its necessary scopes.
4. First call `panda.brand.preview` with `{"name":"TCL"}` through the configured
   webhook. This performs no writes. Expect `existing` with a positive element ID
   or `create` with null ID. Errors such as missing module, permissions, wrong
   linkage, ambiguity or dictionary limit must be resolved before live approval.

Never put the webhook URL/token in source control, logs, a PR or a chat message.

## Approved write and recovery contract

Preview response:

```json
{"name":"TCL","code":"tcl","iblock_id":12,"brand_id":null,"action":"create"}
```

The approved frozen plan is passed to `panda.brand.resolve` together with an
independent brand idempotency key. The response contains these fields plus
`created` and the verified numeric `brand_id`. The Python caller independently
reads it back before the product receives its numeric `property100` value.

Name matching uses Unicode NFKC, trimmed/collapsed whitespace and case-insensitive
exact comparison; CODE is checked as another exact identity. Inactive elements
are included. Multiple matching IDs stop the operation. No predefined brand list
or model-specific brand IDs exist.

The server locks the entire dictionary, repeats its lookup, then calls
`CIBlockElement::Add` and verifies the persisted row. The canonical durable brand
identity is `XML_ID = panda-brand-v1:<sha256(normalized name)>`; it is independent
of the product or request. A retry after losing the response reuses the committed
brand. A renamed canonical identity stops instead of silently creating a twin.
An approved create plan can reuse a concurrently created brand with the same
name/code. An existing plan cannot silently switch IDs. Manual/third-party writers
do not participate in this lock; avoid competing writes during acceptance and
retain the post-create ambiguity check.

The brand and product are separate commits. A product failure does not delete a
successfully created brand; retry reuses it. Partial results must distinguish
brand creation from product creation. Do not report the task complete merely
because the brand exists. Brand `ACTIVE=Y` is part of this creation behavior;
the product remains inactive under the existing controlled-create contract.

## Verification

Run `php -l integrations/bitrix/server/panda_brand_rest.php` and
`php tests/bitrix_brand_rest_test.php`. The latter uses Bitrix and database doubles
and checks denial, schema mismatch, complete visibility, exact match, ambiguity,
lock contention/release, create/readback, lost-response replay and renamed identity.
It does not prove production Bitrix compatibility or actual MySQL concurrency.
The PHP-WASM test environment uses an ASCII-only Normalizer shim when intl is
absent; production requires the real intl extension.

Live acceptance remains a separate, explicit user-approved operation: show a
preview including the missing brand, approve, create brand, verify ID, create the
inactive product, verify its BRAND link, then repeat without duplicate creation.
Do not fabricate a test brand on the production site or claim live success based
on the mocked tests. Resolve any existing product-schema prerequisite errors
before accepting a write; brand creation must not precede a predictable product
validation failure.

Official references:

- https://dev.1c-bitrix.ru/api_d7/bitrix/iblock/rest/index.php
- https://dev.1c-bitrix.ru/api_help/iblock/classes/ciblockelement/add.php
- https://apidocs.bitrix24.com/settings/cloud-and-on-premise/on-premise/custom-methods.html
- https://dev.1c-bitrix.ru/api_help/iblock/classes/ciblock/getpermission.php
