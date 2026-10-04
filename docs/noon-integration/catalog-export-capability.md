# Noon catalog export capability (#170)

## Verified capability

The documented read-only export-category endpoint,
`GET /impex/v1/export/category/list`, returned the following catalog export
category for a connected Noon account:

```text
noon_catalog_catalogexport
```

Its returned parameter contract was:

| Parameter | Required |
| --- | --- |
| `country` | Yes |
| `noon_status` | Yes |
| `brand_code` | No |
| `family_code` | No |

This replaces #168's earlier assumption that Noon has no bulk catalog source.
The category is a viable source for a future Noon "Needs Mapping" queue.

## Boundary for implementation

This verification did **not** create an export or download one. The category
listing alone does not establish the export payload format, the permitted
values for its parameters, or pagination/size behavior.

Before #168 implements Noon catalog import, use a sandbox or test Noon
connection to explicitly scope and verify a small export. Do not create an
export or make any write/change against the live Noon store.

## Reference

- `docs/noon-integration/noon_api_full.md` — Reports export endpoint snapshot.
- https://noon-docs.noonpartners.dev/docs/api-reference/impex/impex-service-get-export-category-list.md
