# Noon API reference snapshot

`noon_api_full.md` and `noon_api_full.yml` are a 2026-09-29 AI-normalized
snapshot of the public [Noon API documentation index](https://noon-docs.noonpartners.dev/llms.txt).
They are reference material, not an official OpenAPI specification. The Markdown
file is copied unchanged from the supplied snapshot. The YAML file is copied
with its 51 empty or null `source_url` values restored from the matching
endpoint sections of the Markdown file; the remaining supplied content is
unchanged.

The snapshot has extraction gaps. Many structured `fields` and `rate_limit`
values are the literal string `[object Object]`; those strings are not usable
schemas or quotas. The `BatchChildSkuRename` entry has no method or path and
explicitly marks its request and response schemas unavailable. The API login
entry does not expose a usable response schema. Consult each endpoint's
`source_url` and the official guides before implementing it, and leave any
unavailable value marked as unknown rather than filling it by inference.

For issue #143's authentication flow, also consult the official
[Quick Start](https://noon-docs.noonpartners.dev/docs/getting-started/quick-start),
[Authenticating Your Requests](https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests),
[Getting Your Credentials](https://noon-docs.noonpartners.dev/docs/authentication/getting-credentials),
[Errors](https://noon-docs.noonpartners.dev/docs/overview/errors),
[Rate Limits & Usage](https://noon-docs.noonpartners.dev/docs/overview/rate-limiting),
and [Static Sandbox](https://noon-docs.noonpartners.dev/docs/overview/sandbox)
guides. These guides are online sources, not copied into this snapshot.
