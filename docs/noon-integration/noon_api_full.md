# Noon API Reference (AI-normalized)

Generated from the public Noon API documentation Markdown views. Missing values are represented as `null`, `[]`, or explicit notes; undocumented fields are not inferred.

## Endpoint index

| # | Category | Endpoint ID | Method | Path | Title |
|---:|---|---|---|---|---|
| 1 | Authentication | `auth-service-api-login` | POST | `/identity/public/v1/api/login` | APILogin |
| 2 | Authentication | `auth-service-whoami` | GET | `/identity/v1/whoami` | Whoami |
| 3 | OAuth | `o-auth-service-create-token` | POST | `/identity/oauth/v1/token/create` | CreateToken |
| 4 | OAuth | `o-auth-service-exchange-token` | POST | `/identity/oauth/v1/token/exchange` | ExchangeToken |
| 5 | API User | `api-user-service-create-credential` | POST | `/identity/apiuser/v1/credential/create` | CreateCredential |
| 6 | API User | `api-user-service-remove-credentials` | POST | `/identity/apiuser/v1/credential/remove` | RemoveCredentials |
| 7 | FBPI | `fbpi-service-add-shipment-courier-awbs` | POST | `/fbpi/v1/shipment/courier-awbs/add` | AddShipmentCourierAwbs |
| 8 | FBPI | `fbpi-service-cancel-shipment` | POST | `/fbpi/v1/shipment/cancel` | CancelShipment |
| 9 | FBPI | `fbpi-service-create-sandbox-order` | POST | `/fbpi/v1/sandbox-order/create` | CreateSandboxOrder |
| 10 | FBPI | `fbpi-service-create-shipment` | POST | `/fbpi/v1/shipment/create` | CreateShipment |
| 11 | FBPI | `fbpi-service-get-fbpi-order` | GET | `/fbpi/v1/fbpi-order/{fbpi_order_nr}/get` | GetFbpiOrder |
| 12 | FBPI | `fbpi-service-get-fbpi-order-customer-data` | GET | `/fbpi/v1/fbpi-order/{fbpi_order_nr}/customer-details/get` | GetFbpiOrderCustomerData |
| 13 | FBPI | `fbpi-service-get-noon-logistics-aw-bs` | POST | `/fbpi/v1/shipment/noon-logistics-awbs/get` | GetNoonLogisticsAWBs |
| 14 | FBPI | `fbpi-service-get-shipment` | POST | `/fbpi/v1/shipment/get` | GetShipment |
| 15 | FBPI | `fbpi-service-list-fbpi-orders` | POST | `/fbpi/v1/fbpi-orders/list` | ListFbpiOrders |
| 16 | FBPI | `fbpi-service-update-order` | POST | `/fbpi/v1/fbpi-order/update` | UpdateOrder |
| 17 | FBPI | `fbpi-service-upload-fbpi-order-invoice` | POST | `/fbpi/v1/fbpi-order/invoice/upload` | UploadFbpiOrderInvoice |
| 18 | FBPI | `fbpi-service-upload-shipment-invoice` | POST | `/fbpi/v1/shipment/invoice/upload` | UploadShipmentInvoice |
| 19 | FBPO | `fbpo-service-get-po` | GET | `/fbpo/v1/po/{po_nr}/get` | GetPurchaseOrder |
| 20 | Global Product & Pricing | `xborder-pricing-service-batch-get-transfer-price` | POST | `/xborder-pricing/v1/transfer-price/get` | BatchGetTransferPrice |
| 21 | Global Product & Pricing | `xborder-pricing-service-batch-upsert-product` | POST | `/xborder-pricing/v1/product/upsert` | BatchUpsertProduct |
| 22 | Global Product & Pricing | `xborder-pricing-service-batch-upsert-transfer-price` | POST | `/xborder-pricing/v1/transfer-price/upsert` | BatchUpsertTransferPrice |
| 23 | Pricing | `pricing-service-batch-get-pricing` | POST | `/pricing/v1/pricing/get` | BatchGetPricing |
| 24 | Pricing | `pricing-service-batch-upsert-pricing` | POST | `/pricing/v1/pricing/upsert` | BatchUpsertPricing |
| 25 | Stock | `stock-service-get-stock` | POST | `/stock/v1/stock-list` | GetStock |
| 26 | Stock | `stock-service-update-stock` | POST | `/stock/v1/stock-update` | UpdateStock |
| 27 | Reports | `impex-service-create-export` | POST | `/impex/v1/export/create` | CreateExport |
| 28 | Reports | `impex-service-get-export-category-list` | GET | `/impex/v1/export/category/list` | GetExportCategoryList |
| 29 | Reports | `impex-service-get-export-status` | POST | `/impex/v1/export/status` | GetExportStatus |
| 30 | Catalog | `catplat-service-batch-child-sku-rename` | — | `—` | BatchChildSkuRename |
| 31 | Catalog | `catplat-service-batch-parent-sku-rename` | POST | `/catplat/v1/sku/parent/batch-rename` | BatchParentSkuRename |
| 32 | Catalog | `catplat-service-batch-sku-map` | POST | `/catplat/v1/sku/batch-map` | BatchSkuMap |
| 33 | Catalog | `catplat-service-child-sku-delete` | POST | `/catplat/v1/sku/child/delete` | ChildSkuDelete |
| 34 | Catalog | `catplat-service-create-barcode-import` | POST | `/catplat/v1/import/barcode` | CreateBarcodeImport |
| 35 | Catalog | `catplat-service-generate-import-signed-url` | POST | `/catplat/v1/import/generate-import-signed-url` | GenerateImportSignedUrl |
| 36 | Catalog | `catplat-service-get-import-status` | GET | `/catplat/v1/import/status/{reference}` | GetImportStatus |
| 37 | Catalog | `catplat-service-parent-sku-delete` | POST | `/catplat/v1/sku/parent/delete` | ParentSkuDelete |
| 38 | Catalog | `catplat-service-sku-barcode-map` | POST | `/catplat/v1/barcode/map` | BarcodeSkuMap |
| 39 | Catalog | `catplat-service-sku-generate` | POST | `/catplat/v1/sku/generate` | SkuGenerate |
| 40 | Warehouse Platform | `warehouse-platform-service-list-warehouses` | POST | `/warehouse-platform/v1/warehouses/list` | ListWarehouses |
| 41 | Offer | `offer-service-get-product-offers` | GET | `/offer/v1/product/{partner_sku}` | GetProductOffers |
| 42 | Event Notifications | `event-notifications-service-create-https-destination` | POST | `/event-notifications/v1/destination/https-destination/create` | CreateHttpsDestination |
| 43 | Event Notifications | `event-notifications-service-get-destination` | GET | `/event-notifications/v1/destination/{destination_code}/get` | GetDestination |
| 44 | Event Notifications | `event-notifications-service-list-destinations` | GET | `/event-notifications/v1/destination/list` | ListDestinations |
| 45 | Event Notifications | `event-notifications-service-list-event-types` | GET | `/event-notifications/v1/event-type/list` | ListEventTypes |
| 46 | Event Notifications | `event-notifications-service-update-destination` | POST | `/event-notifications/v1/destination/https-destination/{destination_code}/update` | UpdateDestination |
| 47 | Content | `content-service-get-content` | POST | `/content/v1/product/content/get` | GetContent |
| 48 | Content | `content-service-list-categories` | POST | `/content/v1/categories/list` | ListCategories |
| 49 | Content | `content-service-list-category-attributes` | POST | `/content/v1/categories/attributes/list` | ListCategoryAttributes |
| 50 | Content | `content-service-upsert-product` | POST | `/content/v1/product/upsert` | UpsertProduct |
| 51 | Returns | `return-reference-service-list-return-references` | POST | `/returns/v1/return-references/list` | ListReturnReferences |

## Authentication / auth-service-api-login

### category

Authentication

### endpoint_id

auth-service-api-login

### title

APILogin

### method

POST

### path

/identity/public/v1/api/login

### purpose

Authenticates a service account using an API JWT token.
The token is decoded to identify the service account, and a session is created upon successful authentication.
Session cookies are set in the response for subsequent authenticated requests.
Optionally, a default project code can be provided to scope the session.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `token` (string, required): The API JWT token used to authenticate the service account.
  - `default_project_code` (string): Optional project code to set as the default project for the session. Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `token` (string, required): The API JWT token used to authenticate the service account.
  - `default_project_code` (string): Optional project code to set as the default project for the session. Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: "- Type: object."
  content_types: ""
  fields: ""
  raw: "- Type: object."
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/authentication/auth-service-api-login.md


## Authentication / auth-service-whoami

### category

Authentication

### endpoint_id

auth-service-whoami

### title

Whoami

### method

GET

### path

/identity/v1/whoami

### purpose

Returns the user code and username of the currently authenticated user based on the active session.

No parameters documented.

No body documented.




A successful response.

- `user_code` (string, required): Unique identifier for the authenticated user.
- `username` (string, required): Username of the authenticated user.


An unexpected error response.

The `Status` type defines a logical error model that is suitable for
different programming environments, including REST APIs and RPC APIs. It is
used by [gRPC](https://github.com/grpc). Each `Status` message contains
three pieces of data: error code, error message, and error details.
You can find out more about this error model and how to work with it in the
[API Design Guide](https://cloud.google.com/apis/design/errors).
- `status_id` (integer (int32))
- `status_code` (string)
- `message` (string)
- `details` (object[])




```python



import json
import time
import uuid

import jwt
import requests

BASE_URL = "https://noon-api-gateway.noon.partners"
USER_AGENT = "NoonApiClient/1.0"

with open("noon_credentials_sensitive.json", "r", encoding="utf-8") as file:
    credentials = json.load(file)

def create_jwt():
    return jwt.encode(
        {
            "sub": credentials["key_id"],
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
        },
        credentials["private_key"],
        algorithm="RS256",
    )

def get_authenticated_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    response = session.post(
        f"{BASE_URL}/identity/public/v1/api/login",
        json={
            "token": create_jwt(),
            "default_project_code": credentials["project_code"],
        },
    )
    if response.status_code != 200:
        raise Exception(f"Login failed with HTTP {response.status_code}: {response.text}")

    return session

session = get_authenticated_session()

headers = {
  'Accept': 'application/json'
}

res = session.get("https://noon-api-gateway.noon.partners/identity/v1/whoami", headers=headers)
```


```go
// Authentiction code is added below, to know more about authentication
// visit: https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests

package main

import (
  "bytes"
  "encoding/json"
  "fmt"
  "io"
  "net/http"
  "net/http/cookiejar"
  "os"
  "time"
  // go get github.com/google/uuid github.com/golang-jwt/jwt/v5
  "github.com/golang-jwt/jwt/v5"
  "github.com/google/uuid"
)
const BASE_URL = "https://noon-api-gateway.noon.partners"
const USER_AGENT = "NoonApiClient/1.0"

type Credentials struct {
  PrivateKey  string `json:"private_key"`
  KeyID       string `json:"key_id"`
  ProjectCode string `json:"project_code"`
}

var credentials Credentials

func check(err error) {
  if err != nil {
    fmt.Fprintln(os.Stderr, err)
    os.Exit(1)
  }
}

func read_credentials() Credentials {
  file, err := os.ReadFile("noon_credentials_sensitive.json")
  check(err)
  var credentials Credentials
  check(json.Unmarshal(file, &credentials))

  return credentials
}

func create_jwt() string {
  private_key, err := jwt.ParseRSAPrivateKeyFromPEM([]byte(credentials.PrivateKey))
  check(err)

  token := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
    "sub": credentials.KeyID,
    "iat": time.Now().Unix(),
    "jti": uuid.NewString(),
  })
  signed_token, err := token.SignedString(private_key)
  check(err)

  return signed_token
}
func get_authenticated_session() *http.Client {
  jar, err := cookiejar.New(nil)
  check(err)

  client := &http.Client{Jar: jar}

  body, _ := json.Marshal(map[string]string{
    "token":                create_jwt(),
    "default_project_code": credentials.ProjectCode,
  })

  req, err := http.NewRequest(
    "POST",
    BASE_URL+"/identity/public/v1/api/login",
    bytes.NewReader(body),
  )
  check(err)

  req.Header.Set("User-Agent", USER_AGENT)
  req.Header.Set("Content-Type", "application/json")
  resp, err := client.Do(req)
  check(err)
  defer resp.Body.Close()

  response_body, _ := io.ReadAll(resp.Body)

  if resp.StatusCode != 200 {
    fmt.Fprintf(os.Stderr, "Login failed with HTTP %d: %s\n", resp.StatusCode, response_body)
    os.Exit(1)
  }

  return client
}

func main() {
  credentials = read_credentials()
  client := get_authenticated_session()

  url := "https://noon-api-gateway.noon.partners/identity/v1/whoami"
  method := "GET"

  req, err := http.NewRequest(method, url, nil)
  if err != nil {
    fmt.Println(err)
    return
  }
  req.Header.Add("Accept", "application/json")

  res, err := client.Do(req)
  if err != nil {
    fmt.Println(err)
    return
  }
  defer res.Body.Close()

  body, err := io.ReadAll(res.Body)
  if err != nil {
    fmt.Println(err)
    return
  }
  fmt.Println(string(body))
}
```


```java
// Authentiction code is added below, to know more about authentication
// visit: https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests
import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.*;
import java.io.File;
import java.security.KeyFactory;
import java.security.interfaces.RSAPrivateKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.time.Instant;
import java.util.*;
public class Main {
    private static RSAPrivateKey readPrivateKey(String pem) throws Exception {
        String stripped = pem
                .replace("-----BEGIN PRIVATE KEY-----", "")
                .replace("-----END PRIVATE KEY-----", "")
                .replaceAll("\\s", "");
        byte[] encoded = Base64.getDecoder().decode(stripped);
        return (RSAPrivateKey) KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(encoded));
    }
    static OkHttpClient getAuthenticatedClient() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> credentials = mapper.readValue(new File("noon_credentials_sensitive.json"), Map.class);

        String privateKeyPem = (String) credentials.get("private_key");
        String keyId = (String) credentials.get("key_id");
        String projectCode = (String) credentials.get("project_code");

        RSAPrivateKey privateKey = readPrivateKey(privateKeyPem);
        long now = Instant.now().getEpochSecond();
        String jti = UUID.randomUUID().toString();
        String token = JWT.create()
                .withSubject(keyId)
                .withIssuedAt(java.util.Date.from(Instant.ofEpochSecond(now)))
                .withJWTId(jti)
                .sign(Algorithm.RSA256(null, privateKey));
        OkHttpClient client = new OkHttpClient.Builder()
                .cookieJar(new CookieJar() {
                    private List<Cookie> cookies = new ArrayList<>();
                    @Override
                    public void saveFromResponse(HttpUrl url, List<Cookie> cookies) {
                        this.cookies = cookies;
                    }
                    @Override
                    public List<Cookie> loadForRequest(HttpUrl url) {
                        return cookies != null ? cookies : new ArrayList<>();
                    }
                })
                .build();
        Map<String, Object> loginPayload = new HashMap<>();
        loginPayload.put("token", token);
        loginPayload.put("default_project_code", projectCode);
        RequestBody loginBody = RequestBody.create(
                mapper.writeValueAsString(loginPayload),
                MediaType.parse("application/json")
        );

        Request loginRequest = new Request.Builder()
                .url("https://noon-api-gateway.noon.partners/identity/public/v1/api/login")
                .post(loginBody)
                .header("User-Agent", "REPLACE_WITH_YOUR_USER_AGENT")
                .build();
        try (Response loginResponse = client.newCall(loginRequest).execute()) {
            if (!loginResponse.isSuccessful()) {
                throw new RuntimeException(
                        "Login failed: " + loginResponse.code() + " " + loginResponse.body().string()
                );
            }
        }
        return client;
    }
    public static void main(String[] args) throws Exception {
        OkHttpClient client = getAuthenticatedClient();
        MediaType mediaType = MediaType.parse("text/plain");
        RequestBody body = RequestBody.create(mediaType, "");
        Request request = new Request.Builder()
                .url("https://noon-api-gateway.noon.partners/identity/v1/whoami")
                .get()
                .addHeader("Accept", "application/json")
                .build();
        Response response = client.newCall(request).execute();
    }
}
```


```javascript
// Authentiction code is added below, to know more about authentication
// visit: https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests

import fs from 'fs';
import crypto from 'crypto'

const credentials = JSON.parse(
  fs.readFileSync('noon_credentials_sensitive.json', 'utf8'),
);

const BASE_URL = 'https://noon-api-gateway.noon.partners';
const USER_AGENT = 'NoonApiClient/1.0';
function base64url(value) {
  return Buffer.from(value).toString('base64url');
}

function create_jwt() {
  const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));

  const payload = base64url(JSON.stringify({
    sub: credentials.key_id,
    iat: Math.floor(Date.now() / 1000),
    jti: crypto.randomUUID(),
  }));

  const signing_input = `${header}.${payload}`;

  const signature = crypto.sign(
    'RSA-SHA256',
    Buffer.from(signing_input),
    credentials.private_key,
  );
  return `${signing_input}.${base64url(signature)}`;
}

function get_cookie_header(response) {
  const set_cookie_headers =
    response.headers.getSetCookie?.() ||
    [response.headers.get('set-cookie')].filter(Boolean);

  return set_cookie_headers
    .map((cookie) => cookie.split(';')[0])
    .join('; ');
}
async function get_authenticated_session() {
  const response = await fetch(`${BASE_URL}/identity/public/v1/api/login`, {
    method: 'POST',
    headers: {
      'User-Agent': USER_AGENT,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      token: create_jwt(),
      default_project_code: credentials.project_code,
    }),
  });

  if (response.status !== 200) {
    throw new Error(`Login failed with HTTP ${response.status}: ${await response.text()}`);
  }
  const cookie_header = get_cookie_header(response);

  return {
    get: (path) => fetch(`${BASE_URL}${path}`, {
      headers: { 'User-Agent': USER_AGENT, Cookie: cookie_header },
    }),
    post: (path, body) => fetch(`${BASE_URL}${path}`, {
      method: 'POST',


ExchangeToken | noon API Documentation (https://noon-docs.noonpartners.dev/docs/api-reference/oauth/o-auth-service-exchange-token)
citeturn12view0 [wordlim: 200] Crawled: last week; Content type: text/html; Source: click({"ref_id":"turn3view2","id":3}); Total lines: 69
cite0†Skip to main content L1: 
  * cite1†Authentication L3: 
  * cite2†OAuth L5: 
    * OAuth

      * cite2†Create​Token L9:       * cite3†Exchange​Token L10:   * cite4†API User L11: 
  * cite5†Fulfillment By Partner Integration (FBPI) L13: 
  * cite6†Fulfilled by Purchase Order (FBPO) L15: 
  * cite7†Global Product & Pricing L17: 
  * cite8†Pricing L19: 
  * cite9†Stock L21: 
  * cite10†Reports L23: 
  * cite11†Catalog L25: 
  * cite12†Warehouse Platform L27: 
  * cite13†Offer L29: 
  * cite14†Event Notifications L31: 
  * cite15†Content L33: 
  * cite16†Returns L35: 
Copy for LLMcite17†View as Markdown L37: # ExchangeToken

    POST

    ## /v1/token/exchange


Type  | Requests  | Time limit (seconds)
--- | --- | ---
Rate  | 1500  | 60
Burst  | 1500  | 60

Completes the OAuth exchange by executing the associated workflow. Provide the JWT access token obtained from CreateToken. The workflow is triggered on the seller's behalf, and the response includes the execution status, the seller's project code, a unique OAuth request ID for tracking, and the workflow result containing the protected resource (e.g. issued credentials). Refer to the integration guide for your specific workflow to see the expected result schema.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: null
fields: ""
raw: null
```

### request_schema

```yaml
documented: false
fields: ""
raw: null
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object],[object Object]"
  raw: |
    - `user_code` (string, required): Unique identifier for the authenticated user.
    - `username` (string, required): Username of the authenticated user.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/authentication/auth-service-whoami.md


## OAuth / o-auth-service-create-token

### category

OAuth

### endpoint_id

o-auth-service-create-token

### title

CreateToken

### method

POST

### path

/identity/oauth/v1/token/create

### purpose

Exchanges an authorization code for an OAuth access token.
Provide the authorization code received from the OAuth consent flow along with your client credentials (client_id and client_secret).
If your OAuth client is configured to require PKCE, the authorization flow must have started with a code_challenge and you must send the matching code_verifier here, or the exchange is rejected.
On success, returns a JWT access token, its expiry duration, granted scopes, and the seller's authorized project code.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie,Authorization"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `grant_type` (string, required): The OAuth grant type. Use 'authorization_code' for standard OAuth code exchange.
  - `code` (string, required): The authorization code received from the OAuth consent redirect.
  - `client_id` (string, required): Your OAuth client ID issued during app registration.
  - `client_secret` (string, required): Your OAuth client secret issued during app registration.
  - `code_verifier` (string): PKCE code verifier (RFC 7636): the high-entropy random string, 43-128 characters long,
  whose SHA-256 hash was sent as code_challenge when the authorization flow started
  (only S256 method is supported as a code_challenge method).
  Required whenever your OAuth client is configured to require PKCE. Clients not
  configured that way may still adopt PKCE per flow, in which case this is required for
  any flow that sent a code_challenge. Omit this field for flows that did not send a code_challenge.
  Use a fresh verifier for every authorization flow and never reuse one. Nullable.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `grant_type` (string, required): The OAuth grant type. Use 'authorization_code' for standard OAuth code exchange.
  - `code` (string, required): The authorization code received from the OAuth consent redirect.
  - `client_id` (string, required): Your OAuth client ID issued during app registration.
  - `client_secret` (string, required): Your OAuth client secret issued during app registration.
  - `code_verifier` (string): PKCE code verifier (RFC 7636): the high-entropy random string, 43-128 characters long,
  whose SHA-256 hash was sent as code_challenge when the authorization flow started
  (only S256 method is supported as a code_challenge method).
  Required whenever your OAuth client is configured to require PKCE. Clients not
  configured that way may still adopt PKCE per flow, in which case this is required for
  any flow that sent a code_challenge. Omit this field for flows that did not send a code_challenge.
  Use a fresh verifier for every authorization flow and never reuse one. Nullable.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `access_token` (string, required): The JWT token to use for token exchange.
    - `token_type` (string enum, required): The type of token issued (Bearer). Allowed values: `TOKEN_TYPE_UNSPECIFIED`, `TOKEN_TYPE_BEARER`. Default: `TOKEN_TYPE_UNSPECIFIED`.
    - `expires_in` (string, required): Duration until the token expires.
    - `scopes` (string[], required): List of scopes granted to the token.
    - `project_code` (string, required): The seller's authorized project code associated with this token.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/oauth/o-auth-service-create-token.md


## OAuth / o-auth-service-exchange-token

### category

OAuth

### endpoint_id

o-auth-service-exchange-token

### title

ExchangeToken

### method

POST

### path

/identity/oauth/v1/token/exchange

### purpose

Completes the OAuth exchange by executing the associated workflow.
Provide the JWT access token obtained from CreateToken.
The workflow is triggered on the seller's behalf, and the response includes the execution status, the seller's project code, a unique OAuth request ID for tracking, and the workflow result containing the protected resource (e.g. issued credentials). Refer to the integration guide for your specific workflow to see the expected result schema.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `access_token` (string, required): The JWT token obtained from the CreateToken response.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `access_token` (string, required): The JWT token obtained from the CreateToken response.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
      - `status_id` (integer (int32))
      - `status_code` (string)
      - `message` (string)
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
      - `status_id` (integer (int32))
      - `status_code` (string)
      - `message` (string)
      - `details` (object[])
    - `project_code` (string, required): The seller's project code associated with this OAuth exchange.
    - `oauth_request_id` (string, required): Unique identifier for this OAuth exchange request, used for tracking.
    - `result` (object): Workflow result containing the protected resource (e.g. issued credentials). The schema
    varies by the workflow configured for the client configured on the OAuth client — refer to the
    relevant integration guide for expected fields. Struct is used here
    because each workflow returns a different resource type; optional because
    result is only present when the workflow completes successfully and synchronously.
    The top-level value is always a JSON object. Nullable.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/oauth/o-auth-service-exchange-token.md


## API User / api-user-service-create-credential

### category

API User

### endpoint_id

api-user-service-create-credential

### title

CreateCredential

### method

POST

### path

/identity/apiuser/v1/credential/create

### purpose

Generates a new APIJWT credential (RSA key pair) for an API service account.
The returned private key is only available in this response — it is not stored server-side.
Optionally restrict the credential to specific IP addresses and set an expiry.
Caller must provide at least one of user_code or channel_identifier. Empty strings are invalid. If both are provided, user_code wins.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `user_code` (string): The API user's user_code. When set, it must be non-empty. If omitted,
  channel_identifier must be provided and will be used to resolve the
  user_code server-side. Nullable.
  - `whitelisted_ips` (string[], required): Optional list of IP addresses to restrict this credential to (max 10 IPs).
  - `expires_at` (string): Optional expiry in ISO 8601 format (e.g. "2026-12-31T23:59:59+00:00"). Nullable.
  - `channel_identifier` (string): The service account's channel identifier (username). When set, it must be
  non-empty. Used to resolve the user_code when the caller does not have it.
  Ignored if user_code is set. Nullable.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `user_code` (string): The API user's user_code. When set, it must be non-empty. If omitted,
  channel_identifier must be provided and will be used to resolve the
  user_code server-side. Nullable.
  - `whitelisted_ips` (string[], required): Optional list of IP addresses to restrict this credential to (max 10 IPs).
  - `expires_at` (string): Optional expiry in ISO 8601 format (e.g. "2026-12-31T23:59:59+00:00"). Nullable.
  - `channel_identifier` (string): The service account's channel identifier (username). When set, it must be
  non-empty. Used to resolve the user_code when the caller does not have it.
  Ignored if user_code is set. Nullable.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `key_id` (string, required): Unique key identifier (used as the JWT 'sub' claim when authenticating).
      - `private_key` (string, required): PEM-encoded RSA private key.
      - `channel_identifier` (string, required): The service account's email/username.
      - `project_code` (string, required): The project code this credential is scoped to.
      - `type` (string, required): Channel type (always "apijwt").
      - `issued_at` (string, required): ISO 8601 timestamp of when the credential was issued.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `credential` (object, required): The generated credential with private key material.
      - `key_id` (string, required): Unique key identifier (used as the JWT 'sub' claim when authenticating).
      - `private_key` (string, required): PEM-encoded RSA private key.
      - `channel_identifier` (string, required): The service account's email/username.
      - `project_code` (string, required): The project code this credential is scoped to.
      - `type` (string, required): Channel type (always "apijwt").
      - `issued_at` (string, required): ISO 8601 timestamp of when the credential was issued.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/apiuser/api-user-service-create-credential.md


## API User / api-user-service-remove-credentials

### category

API User

### endpoint_id

api-user-service-remove-credentials

### title

RemoveCredentials

### method

POST

### path

/identity/apiuser/v1/credential/remove

### purpose

Deactivates APIJWT credentials for an API service account.
If a key_id is provided, only that specific credential is removed.
If no key_id is provided, all active credentials for the user are removed.
Caller must provide at least one of user_code or channel_identifier. Empty strings are invalid. If both are provided, user_code wins.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `user_code` (string): The API user's user_code. When set, it must be non-empty. If omitted,
  channel_identifier must be provided and will be used to resolve the
  user_code server-side. Nullable.
  - `key_id` (string): Optional: the specific key_id to deactivate. If omitted, all active credentials are removed. Nullable.
  - `channel_identifier` (string): The service account's channel identifier (username). When set, it must be
  non-empty. Used to resolve the user_code when the caller does not have it.
  Ignored if user_code is set. Nullable.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `user_code` (string): The API user's user_code. When set, it must be non-empty. If omitted,
  channel_identifier must be provided and will be used to resolve the
  user_code server-side. Nullable.
  - `key_id` (string): Optional: the specific key_id to deactivate. If omitted, all active credentials are removed. Nullable.
  - `channel_identifier` (string): The service account's channel identifier (username). When set, it must be
  non-empty. Used to resolve the user_code when the caller does not have it.
  Ignored if user_code is set. Nullable.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object]"
  raw: |
    - `success` (boolean, required): Whether the operation completed successfully.
    - `credentials_removed` (integer (int32), required): Number of credentials that were deactivated.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/apiuser/api-user-service-remove-credentials.md


## FBPI / fbpi-service-add-shipment-courier-awbs

### category

FBPI

### endpoint_id

fbpi-service-add-shipment-courier-awbs

### title

AddShipmentCourierAwbs

### method

POST

### path

/fbpi/v1/shipment/courier-awbs/add

### purpose

Allows adding the courier and AWB number in case warehouse operations encounter scanning issues with the current one.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): The shipment identifier to update.
  - `awbs` (object[], required): courier and AWB details to be added.
    - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
    - `awb_nr` (string, required): AWB number — either noon-generated or your own.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): The shipment identifier to update.
  - `awbs` (object[], required): courier and AWB details to be added.
    - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
    - `awb_nr` (string, required): AWB number — either noon-generated or your own.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-add-shipment-courier-awbs.md


## FBPI / fbpi-service-cancel-shipment

### category

FBPI

### endpoint_id

fbpi-service-cancel-shipment

### title

CancelShipment

### method

POST

### path

/fbpi/v1/shipment/cancel

### purpose

Cancels an existing shipment.
All items included in that shipment will also be canceled

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): Shipment number to be canceled.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): Shipment number to be canceled.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-cancel-shipment.md


## FBPI / fbpi-service-create-sandbox-order

### category

FBPI

### endpoint_id

fbpi-service-create-sandbox-order

### title

CreateSandboxOrder

### method

POST

### path

/fbpi/v1/sandbox-order/create

### purpose

Creates a test order in the sandbox environment so you can safely test FBPI APIs end-to-end without affecting real data or fulfillment.
The sandbox order is created with the same shape as a real marketplace order and triggers the standard order-creation webhook to your configured endpoint.

Once created, you can call any FBPI API against this order to fetch its details, update item statuses, create shipments, cancel shipments, and explore the full integration flow.

Sandbox orders never result in real fulfillment, and any identifiers returned (such as AWBs) are for testing purposes only and cannot be tracked.
Sandbox orders are automatically deleted after a fixed retention period.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code where the sandbox order will be placed.
  Must be a warehouse you have already configured to receive order webhooks.
  - `idempotency_key` (string, required)
  - `items` (object[], required): The items to include in the sandbox order.
  Each entry creates one item on the order with the specified status.
    - `status` (string enum, required): The marketplace status to assign to this sandbox item (confirmed or cancelled). Allowed values: `MP_ITEM_STATUS_UNSPECIFIED`, `MP_ITEM_STATUS_CONFIRMED`, `MP_ITEM_STATUS_CANCELLED`. Default: `MP_ITEM_STATUS_UNSPECIFIED`.
    - `partner_sku` (string): Optional partner SKU for sandbox orders. If omitted or empty, the server assigns a dummy value.
  No validation is performed and it does not need to match a real SKU. Nullable.
  - `country_code` (string): Nullable.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code where the sandbox order will be placed.
  Must be a warehouse you have already configured to receive order webhooks.
  - `idempotency_key` (string, required)
  - `items` (object[], required): The items to include in the sandbox order.
  Each entry creates one item on the order with the specified status.
    - `status` (string enum, required): The marketplace status to assign to this sandbox item (confirmed or cancelled). Allowed values: `MP_ITEM_STATUS_UNSPECIFIED`, `MP_ITEM_STATUS_CONFIRMED`, `MP_ITEM_STATUS_CANCELLED`. Default: `MP_ITEM_STATUS_UNSPECIFIED`.
    - `partner_sku` (string): Optional partner SKU for sandbox orders. If omitted or empty, the server assigns a dummy value.
  No validation is performed and it does not need to match a real SKU. Nullable.
  - `country_code` (string): Nullable.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object]"
  raw: "- `fbpi_order_nr` (string, required)"
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-create-sandbox-order.md


## FBPI / fbpi-service-create-shipment

### category

FBPI

### endpoint_id

fbpi-service-create-shipment

### title

CreateShipment

### method

POST

### path

/fbpi/v1/shipment/create

### purpose

Used to create a shipment for an order.
Each shipment must have a unique integration_shipment_nr and a valid AWB (Air Waybill) number.
You can either use a Noon-generated AWB or your own courier’s AWB.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code handling the order.
  - `integration_shipment_nr` (string, required): Unique identifier for the shipment, No specific format enforced.
  - `fbpi_order_nr` (string, required): The FBPI order number linked to this shipment.
  - `awbs` (object[], required): Array containing courier and AWB number details.
    - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
    - `awb_nr` (string, required): AWB number — either noon-generated or your own.
  - `items` (object[], required): Array of items included in this shipment.
    - `mp_item_nr` (string, required): Marketplace item number to be included in this shipment.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code handling the order.
  - `integration_shipment_nr` (string, required): Unique identifier for the shipment, No specific format enforced.
  - `fbpi_order_nr` (string, required): The FBPI order number linked to this shipment.
  - `awbs` (object[], required): Array containing courier and AWB number details.
    - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
    - `awb_nr` (string, required): AWB number — either noon-generated or your own.
  - `items` (object[], required): Array of items included in this shipment.
    - `mp_item_nr` (string, required): Marketplace item number to be included in this shipment.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-create-shipment.md


## FBPI / fbpi-service-get-fbpi-order

### category

FBPI

### endpoint_id

fbpi-service-get-fbpi-order

### title

GetFbpiOrder

### method

GET

### path

/fbpi/v1/fbpi-order/{fbpi_order_nr}/get

### purpose

A webhook will be sent to your system containing the order details whenever an order is created from the marketplace (Noon or Namshi).
You can then use the fbpi_order_nr from the webhook to call this endpoint and retrieve full order details.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  ### Path Parameters
  - `fbpi_order_nr` (string, required): The order number used in the request.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  ### Path Parameters
  - `fbpi_order_nr` (string, required): The order number used in the request.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `mp_item_nr` (string, required): Marketplace item number.
      - `partner_sku` (string, required): Partner SKU for that item.
      - `mp_status` (string enum, required): Marketplace status of the item (confirmed or cancelled). Allowed values: `MP_ITEM_STATUS_UNSPECIFIED`, `MP_ITEM_STATUS_CONFIRMED`, `MP_ITEM_STATUS_CANCELLED`. Default: `MP_ITEM_STATUS_UNSPECIFIED`.
      - `integration_status` (string enum, required): Integration status of the item (acknowledged, out of stock, or shipped). Allowed values: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`, `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`, `INTEGRATION_ITEM_STATUS_OUT_OF_STOCK`, `INTEGRATION_ITEM_STATUS_SHIPPED`. Default: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`.
      - `delivered_invoice_price` (number (double), required): The invoiced price of the delivered item.
      - `cancellation_reason_code` (string): Reason code explaining why the item was cancelled, if applicable. Nullable.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `fbpi_order_nr` (string, required): The order number passed in the request.
    - `mp_code` (string, required): Marketplace source: 'noon' or 'namshi_v2'.
    - `mp_order_nr` (string, required): The marketplace order number retrieved from the Order.
    - `mp_country_code` (string, required): Marketplace country code (e.g., ae, sa, eg).
    - `customer_country_code` (string, required): The country from which the customer placed the order.
    - `merchant_code` (string, required): The merchant identifier, prefixed with STR.
    - `currency_code` (string, required): The customer’s local currency.
    - `warehouse_code` (string, required): Integration warehouse code where the order will be fulfilled.
    - `items` (object[], required): Array of items included in the order.
      - `mp_item_nr` (string, required): Marketplace item number.
      - `partner_sku` (string, required): Partner SKU for that item.
      - `mp_status` (string enum, required): Marketplace status of the item (confirmed or cancelled). Allowed values: `MP_ITEM_STATUS_UNSPECIFIED`, `MP_ITEM_STATUS_CONFIRMED`, `MP_ITEM_STATUS_CANCELLED`. Default: `MP_ITEM_STATUS_UNSPECIFIED`.
      - `integration_status` (string enum, required): Integration status of the item (acknowledged, out of stock, or shipped). Allowed values: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`, `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`, `INTEGRATION_ITEM_STATUS_OUT_OF_STOCK`, `INTEGRATION_ITEM_STATUS_SHIPPED`. Default: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`.
      - `delivered_invoice_price` (number (double), required): The invoiced price of the delivered item.
      - `cancellation_reason_code` (string): Reason code explaining why the item was cancelled, if applicable. Nullable.
    - `order_created_at` (string, required): Timestamp of when the order was created.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-get-fbpi-order.md


## FBPI / fbpi-service-get-fbpi-order-customer-data

### category

FBPI

### endpoint_id

fbpi-service-get-fbpi-order-customer-data

### title

GetFbpiOrderCustomerData

### method

GET

### path

/fbpi/v1/fbpi-order/{fbpi_order_nr}/customer-details/get

### purpose

Provides customer details for a given FBPI order, including their name, city, and administrative division.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `fbpi_order_nr` (string, required): The FBPI order number you received via webhook.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `fbpi_order_nr` (string, required): The FBPI order number you received via webhook.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `first_name` (string, required): Customer first name in latin characters.
    - `last_name` (string, required): Customer last name in latin characters.
    - `city` (string, required): Customer city in latin characters.
    - `administrative_division` (string, required): Customer administrative division in latin characters.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-get-fbpi-order-customer-data.md


## FBPI / fbpi-service-get-noon-logistics-aw-bs

### category

FBPI

### endpoint_id

fbpi-service-get-noon-logistics-aw-bs

### title

GetNoonLogisticsAWBs

### method

POST

### path

/fbpi/v1/shipment/noon-logistics-awbs/get

### purpose

Generates Noon AWB numbers to be used when creating shipments.
You can generate a single AWB per shipment or request up to 1000 AWBs in bulk and store them for future use.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `country_code` (string, required): Operating warehouse country code (e.g., ae, sa, eg).
  - `qty` (integer (int64), required): Number of AWBs to generate. Maximum 1000 per request.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `country_code` (string, required): Operating warehouse country code (e.g., ae, sa, eg).
  - `qty` (integer (int64), required): Number of AWBs to generate. Maximum 1000 per request.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object]"
  raw: "- `awb_nrs` (string[], required): The generated noon AWB numbers, one per requested quantity."
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-get-noon-logistics-aw-bs.md


## FBPI / fbpi-service-get-shipment

### category

FBPI

### endpoint_id

fbpi-service-get-shipment

### title

GetShipment

### method

POST

### path

/fbpi/v1/shipment/get

### purpose

Retrieves shipment details for a specific order and shipment number.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): The shipment number mapped to that order.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): Integration warehouse code.
  - `integration_shipment_nr` (string, required): The shipment number mapped to that order.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
      - `awb_nr` (string, required): AWB number — either noon-generated or your own.
      - `mp_item_nr` (string, required): Marketplace item number.
      - `partner_sku` (string, required): Partner SKU for that item.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `fbpi_order_nr` (string, required): The order number associated with the shipment.
    - `awbs` (object[], required): AWB details used for this shipment.
      - `courier` (string, required): Courier name — 'noon' or your own logistics provider.
      - `awb_nr` (string, required): AWB number — either noon-generated or your own.
    - `items` (object[], required): Items included in this shipment.
      - `mp_item_nr` (string, required): Marketplace item number.
      - `partner_sku` (string, required): Partner SKU for that item.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-get-shipment.md


## FBPI / fbpi-service-list-fbpi-orders

### category

FBPI

### endpoint_id

fbpi-service-list-fbpi-orders

### title

ListFbpiOrders

### method

POST

### path

/fbpi/v1/fbpi-orders/list

### purpose

Retrieves a paginated list of FBPI orders for a specific warehouse with optional filtering by order creation date range.
Use the created_after and created_before filters to limit results to orders created within a specific UTC time window. If no date filters are provided, all available orders for the specified warehouse will be returned. Results are returned in pages of up to 50 orders.
To retrieve additional pages,include the next_token query parameter returned in the previous response. When paginating, you must reuse the same filters applied in the initial request. The next_token controls pagination only and does not modify the result set.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "Query Parameters"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  ### Query Parameters
  - `next_token` (string, required): used for pagination. When requesting pages after the
  first, provide the token returned by your previous request, and do not change the filters applied
  in the first page when sending the token.
  
  The filters to apply when listing orders (warehouse and optional date range).
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code whose orders should be listed.
  - `created_after` (string): created_after / created_before: UTC timestamp in ISO-8601 format(YYYY-MM-DDTHH:MM:SSZ). Use
  these to limit the returned orders to those created within the inclusive
  date range. If omitted, no date filtering is applied. Nullable.
  - `created_before` (string): Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  ### Query Parameters
  - `next_token` (string, required): used for pagination. When requesting pages after the
  first, provide the token returned by your previous request, and do not change the filters applied
  in the first page when sending the token.
  
  The filters to apply when listing orders (warehouse and optional date range).
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code whose orders should be listed.
  - `created_after` (string): created_after / created_before: UTC timestamp in ISO-8601 format(YYYY-MM-DDTHH:MM:SSZ). Use
  these to limit the returned orders to those created within the inclusive
  date range. If omitted, no date filtering is applied. Nullable.
  - `created_before` (string): Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `fbpi_order_nr` (string, required): The order number passed in the request.
      - `mp_code` (string, required): Marketplace source: 'noon' or 'namshi_v2'.
      - `mp_order_nr` (string, required): The marketplace order number retrieved from the Order.
      - `mp_country_code` (string, required): Marketplace country code (e.g., ae, sa, eg).
      - `customer_country_code` (string, required): The country from which the customer placed the order.
      - `merchant_code` (string, required): The merchant identifier, prefixed with STR.
      - `currency_code` (string, required): The customer’s local currency.
      - `warehouse_code` (string, required): Integration warehouse code where the order will be fulfilled.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `next_token` (string, required)
    - `orders` (object[], required)
      - `fbpi_order_nr` (string, required): The order number passed in the request.
      - `mp_code` (string, required): Marketplace source: 'noon' or 'namshi_v2'.
      - `mp_order_nr` (string, required): The marketplace order number retrieved from the Order.
      - `mp_country_code` (string, required): Marketplace country code (e.g., ae, sa, eg).
      - `customer_country_code` (string, required): The country from which the customer placed the order.
      - `merchant_code` (string, required): The merchant identifier, prefixed with STR.
      - `currency_code` (string, required): The customer’s local currency.
      - `warehouse_code` (string, required): Integration warehouse code where the order will be fulfilled.
      - `items` (object[], required): Array of items included in the order.
        - `mp_item_nr` (string, required): Marketplace item number.
        - `partner_sku` (string, required): Partner SKU for that item.
        - `mp_status` (string enum, required): Marketplace status of the item (confirmed or cancelled). Allowed values: `MP_ITEM_STATUS_UNSPECIFIED`, `MP_ITEM_STATUS_CONFIRMED`, `MP_ITEM_STATUS_CANCELLED`. Default: `MP_ITEM_STATUS_UNSPECIFIED`.
        - `integration_status` (string enum, required): Integration status of the item (acknowledged, out of stock, or shipped). Allowed values: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`, `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`, `INTEGRATION_ITEM_STATUS_OUT_OF_STOCK`, `INTEGRATION_ITEM_STATUS_SHIPPED`. Default: `INTEGRATION_ITEM_STATUS_UNSPECIFIED`.
        - `delivered_invoice_price` (number (double), required): The invoiced price of the delivered item.
        - `cancellation_reason_code` (string): Reason code explaining why the item was cancelled, if applicable. Nullable.
      - `order_created_at` (string, required): Timestamp of when the order was created.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-list-fbpi-orders.md


## FBPI / fbpi-service-update-order

### category

FBPI

### endpoint_id

fbpi-service-update-order

### title

UpdateOrder

### method

POST

### path

/fbpi/v1/fbpi-order/update

### purpose

If the warehouse stock does not match the received order, this API allows you to mark one or more items as unavailable before shipment creation.
The unavailable items will be canceled on Noon’s side, and the rest will proceed to fulfillment.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `fbpi_order_nr` (string, required): The FBPI order number to update.
  - `items` (object[], required): Array of items to cancel before shipment creation.
    - `mp_item_nr` (string, required): Marketplace item number to exclude from fulfillment.
    - `status` (string enum, required): Status to mark item as out of stock. Allowed values: `UPDATE_ORDER_REQUEST_ITEM_STATUS_UNSPECIFIED`, `UPDATE_ORDER_REQUEST_ITEM_STATUS_OUT_OF_STOCK`. Default: `UPDATE_ORDER_REQUEST_ITEM_STATUS_UNSPECIFIED`.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `fbpi_order_nr` (string, required): The FBPI order number to update.
  - `items` (object[], required): Array of items to cancel before shipment creation.
    - `mp_item_nr` (string, required): Marketplace item number to exclude from fulfillment.
    - `status` (string enum, required): Status to mark item as out of stock. Allowed values: `UPDATE_ORDER_REQUEST_ITEM_STATUS_UNSPECIFIED`, `UPDATE_ORDER_REQUEST_ITEM_STATUS_OUT_OF_STOCK`. Default: `UPDATE_ORDER_REQUEST_ITEM_STATUS_UNSPECIFIED`.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-update-order.md


## FBPI / fbpi-service-upload-fbpi-order-invoice

### category

FBPI

### endpoint_id

fbpi-service-upload-fbpi-order-invoice

### title

UploadFbpiOrderInvoice

### method

POST

### path

/fbpi/v1/fbpi-order/invoice/upload

### purpose

Uploads a PDF invoice covering an entire FBPI order.
Use this when you issue one invoice for the whole order. If you invoice per shipment, use UploadShipmentInvoice instead.
Uploading again for the same order supersedes the previous invoice.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `fbpi_order_nr` (string, required): The FBPI order number the invoice covers.
  - `invoice` (string (byte), required): The invoice document. Supported format: PDF only (`application/pdf`).
  Sent as a base64-encoded string over HTTP/JSON; provide the raw payload only,
  with no data URL prefix such as `data:application/pdf;base64,`.
  Maximum decoded file size: 5 MiB. Requests exceeding this limit or using a
  non-PDF payload will be rejected.
  - `invoice_nr` (string, required): The invoice number as printed on the document.
  - `issued_at` (string, required): The date the invoice was issued, in ISO-8601 date format (YYYY-MM-DD).
  - `idempotency_key` (string, required): Unique key making retries safe. Repeating a call with the same key returns the
  original result instead of storing a second invoice. Max 64 characters.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `fbpi_order_nr` (string, required): The FBPI order number the invoice covers.
  - `invoice` (string (byte), required): The invoice document. Supported format: PDF only (`application/pdf`).
  Sent as a base64-encoded string over HTTP/JSON; provide the raw payload only,
  with no data URL prefix such as `data:application/pdf;base64,`.
  Maximum decoded file size: 5 MiB. Requests exceeding this limit or using a
  non-PDF payload will be rejected.
  - `invoice_nr` (string, required): The invoice number as printed on the document.
  - `issued_at` (string, required): The date the invoice was issued, in ISO-8601 date format (YYYY-MM-DD).
  - `idempotency_key` (string, required): Unique key making retries safe. Repeating a call with the same key returns the
  original result instead of storing a second invoice. Max 64 characters.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-upload-fbpi-order-invoice.md


## FBPI / fbpi-service-upload-shipment-invoice

### category

FBPI

### endpoint_id

fbpi-service-upload-shipment-invoice

### title

UploadShipmentInvoice

### method

POST

### path

/fbpi/v1/shipment/invoice/upload

### purpose

Uploads a PDF invoice covering a single shipment.
Use this when you issue one invoice per shipment. If you invoice at order level, use UploadFbpiOrderInvoice instead.
Uploading again for the same shipment supersedes the previous invoice.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code that handled the shipment.
  - `integration_shipment_nr` (string, required): The shipment number the invoice covers.
  - `invoice` (string (byte), required): The invoice document. Supported format: PDF only (`application/pdf`).
  Sent as a base64-encoded string over HTTP/JSON; provide the raw payload only,
  with no data URL prefix such as `data:application/pdf;base64,`.
  Maximum decoded file size: 5 MiB. Requests exceeding this limit or using a
  non-PDF payload will be rejected.
  - `invoice_nr` (string, required): The invoice number as printed on the document.
  - `issued_at` (string, required): The date the invoice was issued, in ISO-8601 date format (YYYY-MM-DD).
  - `idempotency_key` (string, required): Unique key making retries safe. Repeating a call with the same key returns the
  original result instead of storing a second invoice. Max 64 characters.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `warehouse_code` (string, required): The integration warehouse code that handled the shipment.
  - `integration_shipment_nr` (string, required): The shipment number the invoice covers.
  - `invoice` (string (byte), required): The invoice document. Supported format: PDF only (`application/pdf`).
  Sent as a base64-encoded string over HTTP/JSON; provide the raw payload only,
  with no data URL prefix such as `data:application/pdf;base64,`.
  Maximum decoded file size: 5 MiB. Requests exceeding this limit or using a
  non-PDF payload will be rejected.
  - `invoice_nr` (string, required): The invoice number as printed on the document.
  - `issued_at` (string, required): The date the invoice was issued, in ISO-8601 date format (YYYY-MM-DD).
  - `idempotency_key` (string, required): Unique key making retries safe. Repeating a call with the same key returns the
  original result instead of storing a second invoice. Max 64 characters.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpi/fbpi-service-upload-shipment-invoice.md


## FBPO / fbpo-service-get-po

### category

FBPO

### endpoint_id

fbpo-service-get-po

### title

GetPurchaseOrder

### method

GET

### path

/fbpo/v1/po/{po_nr}/get

### purpose

Retrieves the details of a specific purchase order by its number.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `po_nr` (string, required): The pucharse order number used in the request.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `po_nr` (string, required): The pucharse order number used in the request.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `year` (integer (int32), required): Year of the date. Must be from 1 to 9999, or 0 to specify a date without
    a year.
      - `month` (integer (int32), required): Month of a year. Must be from 1 to 12, or 0 to specify a year without a
    month and day.
      - `day` (integer (int32), required): Day of a month. Must be from 1 to 31 and valid for the year and month, or 0
    to specify a year by itself or a year and month where the day isn't
    significant.
      - `partner_sku` (string, required): Partner SKU for that item.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `po_nr` (string, required): The pucharse order number passed in the request.
    - `po_currency` (string, required): The currency of that purchase order.
    - `merchant_code` (string, required): The merchant identifier, prefixed with STR.
    - `warehouse_code` (string, required): Partner warehouse code representing the warehouse responsible for fulfilling the purchase order.
    - `po_status` (string enum, required): The status of the purchase order, stating whether it is "Pending" or "Released". Allowed values: `PO_STATUS_UNSPECIFIED`, `PO_STATUS_PENDING`, `PO_STATUS_RELEASED`. Default: `PO_STATUS_UNSPECIFIED`.
    - `po_release_date` (object, required): The release date of that purchase order.
      - `year` (integer (int32), required): Year of the date. Must be from 1 to 9999, or 0 to specify a date without
    a year.
      - `month` (integer (int32), required): Month of a year. Must be from 1 to 12, or 0 to specify a year without a
    month and day.
      - `day` (integer (int32), required): Day of a month. Must be from 1 to 31 and valid for the year and month, or 0
    to specify a year by itself or a year and month where the day isn't
    significant.
    - `po_lines` (object[], required): Array of purchase order lines included in that purchase order.
      - `partner_sku` (string, required): Partner SKU for that item.
      - `qty` (integer (int32), required): Quantity needed for that item.
      - `unit_cost` (number (double), required): Unit cost of that item.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/fbpo/fbpo-service-get-po.md


## Global Product & Pricing / xborder-pricing-service-batch-get-transfer-price

### category

Global Product & Pricing

### endpoint_id

xborder-pricing-service-batch-get-transfer-price

### title

BatchGetTransferPrice

### method

POST

### path

/xborder-pricing/v1/transfer-price/get

### purpose

Get the transfer price for a product

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required): Array of transfer price lookup items. Max 1000 items per request.
    - `partner_sku` (string, required): Partner SKU for that item.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required): Array of transfer price lookup items. Max 1000 items per request.
    - `partner_sku` (string, required): Partner SKU for that item.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required): Array of items for which transfer price info was asked. Max 1000 items per response (matches request cap).
      - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
      - `transfer_price_usd` (number (double)): Transfer price of the partner SKU in USD currency. Nullable.
      - `msrp_usd` (number (double)): MSRP in USD currency of the partner SKU. Nullable.
      - `is_active` (boolean): The pricing status of the item (Active or Inactive). Nullable.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/xborder-pricing/xborder-pricing-service-batch-get-transfer-price.md


## Global Product & Pricing / xborder-pricing-service-batch-upsert-product

### category

Global Product & Pricing

### endpoint_id

xborder-pricing-service-batch-upsert-product

### title

BatchUpsertProduct

### method

POST

### path

/xborder-pricing/v1/product/upsert

### purpose

Create a new product or update an existing one

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required): Array of items needed to be created or updated.
    - `partner_sku` (string, required): Partner SKU for that item.
    - `dimensions_cm` (object): Dimensions (length, width, height) of that item in centimeters. Nullable.
      - `length` (number (double), required): Length of the item in centimeters.
      - `width` (number (double), required): Width of the item in centimeters.
      - `height` (number (double), required): Height of the item in centimeters.
    - `vm_weight_cm` (number (double)): Volumetric weight of that item in centimeters. Nullable.
    - `actual_weight_kg` (number (double)): Actual weight of that item in kilograms. Nullable.
    - `hs_code` (string): Harmonized system (HS) code for that item. Nullable.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required): Array of items needed to be created or updated.
    - `partner_sku` (string, required): Partner SKU for that item.
    - `dimensions_cm` (object): Dimensions (length, width, height) of that item in centimeters. Nullable.
      - `length` (number (double), required): Length of the item in centimeters.
      - `width` (number (double), required): Width of the item in centimeters.
      - `height` (number (double), required): Height of the item in centimeters.
    - `vm_weight_cm` (number (double)): Volumetric weight of that item in centimeters. Nullable.
    - `actual_weight_kg` (number (double)): Actual weight of that item in kilograms. Nullable.
    - `hs_code` (string): Harmonized system (HS) code for that item. Nullable.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): Status indicating whether the update executed successfully or encountered an error.
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required): Array of items created or updated in the request.
      - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): Status indicating whether the update executed successfully or encountered an error.
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/xborder-pricing/xborder-pricing-service-batch-upsert-product.md


## Global Product & Pricing / xborder-pricing-service-batch-upsert-transfer-price

### category

Global Product & Pricing

### endpoint_id

xborder-pricing-service-batch-upsert-transfer-price

### title

BatchUpsertTransferPrice

### method

POST

### path

/xborder-pricing/v1/transfer-price/upsert

### purpose

Set the transfer price for a product

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required): Array of transfer prices needed to be set or updated.
    - `partner_sku` (string, required): Partner SKU for that item.
    - `transfer_price_usd` (number (double)): Transfer price to be set or updated in USD currency for that item. Nullable.
    - `msrp_usd` (number (double)): MSRP needed to be set or updated for that item. Nullable.
    - `is_active` (boolean): The pricing status of the item (Active or Inactive). Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required): Array of transfer prices needed to be set or updated.
    - `partner_sku` (string, required): Partner SKU for that item.
    - `transfer_price_usd` (number (double)): Transfer price to be set or updated in USD currency for that item. Nullable.
    - `msrp_usd` (number (double)): MSRP needed to be set or updated for that item. Nullable.
    - `is_active` (boolean): The pricing status of the item (Active or Inactive). Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required): Array of items for which pricing will be set.
      - `partner_sku` (string, required): Partner SKU for that item.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/xborder-pricing/xborder-pricing-service-batch-upsert-transfer-price.md


## Pricing / pricing-service-batch-get-pricing

### category

Pricing

### endpoint_id

pricing-service-batch-get-pricing

### title

BatchGetPricing

### method

POST

### path

/pricing/v1/pricing/get

### purpose

Returns pricing data for the given partner SKUs

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): The unique identifier for the item in the partner's system.
    - `country_code` (string, required)
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): The unique identifier for the item in the partner's system.
    - `country_code` (string, required)
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required)
      - `country_code` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `partner_sku` (string, required)
      - `country_code` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
      - `price` (number (double)): Nullable.
      - `msrp` (number (double)): Nullable.
      - `is_active` (boolean): Nullable.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/pricing/pricing-service-batch-get-pricing.md


## Pricing / pricing-service-batch-upsert-pricing

### category

Pricing

### endpoint_id

pricing-service-batch-upsert-pricing

### title

BatchUpsertPricing

### method

POST

### path

/pricing/v1/pricing/upsert

### purpose

Creates or updates pricing data for the given partner SKUs

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): The unique identifier for the item in the partner's system.
    - `country_code` (string, required)
    - `price` (number (double)): The price of the item to be updated. Nullable.
    - `msrp` (number (double)): Nullable.
    - `is_active` (boolean): Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): The unique identifier for the item in the partner's system.
    - `country_code` (string, required)
    - `price` (number (double)): The price of the item to be updated. Nullable.
    - `msrp` (number (double)): Nullable.
    - `is_active` (boolean): Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required)
      - `country_code` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `partner_sku` (string, required)
      - `country_code` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/pricing/pricing-service-batch-upsert-pricing.md


## Stock / stock-service-get-stock

### category

Stock

### endpoint_id

stock-service-get-stock

### title

GetStock

### method

POST

### path

/stock/v1/stock-list

### purpose

Given a list of warehouse and partner_sku pairs return the quantity for each respective pair

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required)
    - `warehouse_code` (string, required)
    - `partner_sku` (string, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `items` (object[], required)
    - `warehouse_code` (string, required)
    - `partner_sku` (string, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `warehouse_code` (string, required)
      - `partner_sku` (string, required)
      - `status` (object, required): Status indicating whether the workflow executed successfully or encountered an error.
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
      - `qty` (integer (int32), required)
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `warehouse_code` (string, required)
      - `partner_sku` (string, required)
      - `status` (object, required): Status indicating whether the workflow executed successfully or encountered an error.
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
      - `qty` (integer (int32), required)
      - `processing_time` (string): Nullable.
      - `stock_updated_at` (string (date-time)): Nullable.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/stock/stock-service-get-stock.md


## Stock / stock-service-update-stock

### category

Stock

### endpoint_id

stock-service-update-stock

### title

UpdateStock

### method

POST

### path

/stock/v1/stock-update

### purpose

Update the stock in the warehouse

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `warehouse_code` (string, required)
    - `partner_sku` (string, required)
    - `qty` (integer (int32)): Nullable.
    - `processing_time` (string): Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `warehouse_code` (string, required)
    - `partner_sku` (string, required)
    - `qty` (integer (int32)): Nullable.
    - `processing_time` (string): Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `warehouse_code` (string, required)
      - `partner_sku` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `warehouse_code` (string, required)
      - `partner_sku` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/stock/stock-service-update-stock.md


## Reports / impex-service-create-export

### category

Reports

### endpoint_id

impex-service-create-export

### title

CreateExport

### method

POST

### path

/impex/v1/export/create

### purpose

Create an export request

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `export_category_code` (string, required)
  - `params` (object, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `export_category_code` (string, required)
  - `params` (object, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object]"
  raw: "- `export_code` (string, required)"
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/impex/impex-service-create-export.md


## Reports / impex-service-get-export-category-list

### category

Reports

### endpoint_id

impex-service-get-export-category-list

### title

GetExportCategoryList

### method

GET

### path

/impex/v1/export/category/list

### purpose

Get export category list

No parameters documented.

No body documented.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: null
fields: ""
raw: null
```

### request_schema

```yaml
documented: false
fields: ""
raw: null
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `export_category_code` (string, required)
      - `params` (object, required)
  content_types: ""
  fields: "[object Object],[object Object],[object Object]"
  raw: |
    - `export_categories` (object[], required)
      - `export_category_code` (string, required)
      - `params` (object, required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/impex/impex-service-get-export-category-list.md


## Reports / impex-service-get-export-status

### category

Reports

### endpoint_id

impex-service-get-export-status

### title

GetExportStatus

### method

POST

### path

/impex/v1/export/status

### purpose

Get export details, status and download link

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `export_code` (string, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `export_code` (string, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `export_code` (string, required)
    - `export_category_code` (string, required)
    - `export_status` (string, required)
    - `params` (string, required)
    - `project_code` (string, required)
    - `created_by` (string, required)
    - `download_url` (string): Nullable.
    - `created_at` (string, required)
    - `updated_at` (string, required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/impex/impex-service-get-export-status.md


## Catalog / catplat-service-batch-child-sku-rename

### category

Catalog

### endpoint_id

catplat-service-batch-child-sku-rename

### title

BatchChildSkuRename

### method

`null`

### path

`null`

### purpose

This API allows partners to rename parent partner SKUs.

### authentication

```yaml
documented: false
details: null
```

### rate_limit

`null`

### headers

```yaml
documented: false
details: null
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
details: null
```

### request_schema

```yaml
documented: false
fields: ""
raw: null
```

### responses

`[]`

### examples

`[]`

### errors

`[]`

### notes

```yaml
- "Public Markdown representation could not be retrieved; endpoint metadata is taken from llms.txt."
- "Request/response schemas are not exposed in the retrieved source."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-batch-child-sku-rename.md


## Catalog / catplat-service-batch-parent-sku-rename

### category

Catalog

### endpoint_id

catplat-service-batch-parent-sku-rename

### title

BatchParentSkuRename

### method

POST

### path

/catplat/v1/sku/parent/batch-rename

### purpose

This API allows partners to rename partner SKUs.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item; it is used
  to organize product variations under a single listing.
    - `rename_to` (string, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item; it is used
  to organize product variations under a single listing.
    - `rename_to` (string, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item; it is used
    to organize product variations under a single listing.
      - `rename_to` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item; it is used
    to organize product variations under a single listing.
      - `rename_to` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-batch-parent-sku-rename.md


## Catalog / catplat-service-batch-sku-map

### category

Catalog

### endpoint_id

catplat-service-batch-sku-map

### title

BatchSkuMap

### method

POST

### path

/catplat/v1/sku/batch-map

### purpose

This API allows partners to map their partner_skus to nsku_children.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
    - `nsku_child` (string, required): NSKU is the unique identifier generated and managed by noon itself for gated brands and #del# to delete mapping.
    - `force_map` (boolean): When set to true, it overrides the existing mapping between the partner_sku and nsku_child. Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
    - `nsku_child` (string, required): NSKU is the unique identifier generated and managed by noon itself for gated brands and #del# to delete mapping.
    - `force_map` (boolean): When set to true, it overrides the existing mapping between the partner_sku and nsku_child. Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
      - `nsku_child` (string, required): NSKU is the unique identifier generated and managed by noon itself for gated brands.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
      - `nsku_child` (string, required): NSKU is the unique identifier generated and managed by noon itself for gated brands.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-batch-sku-map.md


## Catalog / catplat-service-child-sku-delete

### category

Catalog

### endpoint_id

catplat-service-child-sku-delete

### title

ChildSkuDelete

### method

POST

### path

/catplat/v1/sku/child/delete

### purpose

This API allows partners to delete Partner SKUs/Child ZSKUs.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
    - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
    - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
      - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
      - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-child-sku-delete.md


## Catalog / catplat-service-create-barcode-import

### category

Catalog

### endpoint_id

catplat-service-create-barcode-import

### title

CreateBarcodeImport

### method

POST

### path

/catplat/v1/import/barcode

### purpose

Create barcode import using a File, BigQuery table, or inline items. Requirements: - Must provide exactly one source: file_url, bq_table, or items.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `file_url` (string, required): HTTP/HTTPS or GCS path for file imports (Supports CSV, TSV, XLSX, JSON, Parquet).
  - `bq_table` (string, required): BigQuery table reference for the data.
  - `items` (object, required): List of barcode items inserted inline.
    - `data` (object[], required): List of barcode mapping items provided inline in the request. Expected maximum: 10,000 items.
      - `partner_sku` (string, required): A unique user-defined product code/stock keeping unit (SKU) which already exists in the catalog.
      - `barcode` (string, required): The barcode, UPC, or ISBN number for the product.
      - `force_sync` (boolean, required): Bool value to force sync mappings and override any existing mapping between same "barcode" and partner_sku.
  - `config` (object, required): Configuration for the import behavior.
    - `is_notification_required` (boolean, required): Set the value to trigger a WHIP notification upon completion.
  Its optional default value is false.
    - `force_sync` (boolean, required): Set the value to forcefully synchronize/overwrite existing mappings for the overall request.
  Its optional default value is false.
  - `client_reference` (string, required): A unique user-defined client_reference. max_chars=100
  Must be unique.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `file_url` (string, required): HTTP/HTTPS or GCS path for file imports (Supports CSV, TSV, XLSX, JSON, Parquet).
  - `bq_table` (string, required): BigQuery table reference for the data.
  - `items` (object, required): List of barcode items inserted inline.
    - `data` (object[], required): List of barcode mapping items provided inline in the request. Expected maximum: 10,000 items.
      - `partner_sku` (string, required): A unique user-defined product code/stock keeping unit (SKU) which already exists in the catalog.
      - `barcode` (string, required): The barcode, UPC, or ISBN number for the product.
      - `force_sync` (boolean, required): Bool value to force sync mappings and override any existing mapping between same "barcode" and partner_sku.
  - `config` (object, required): Configuration for the import behavior.
    - `is_notification_required` (boolean, required): Set the value to trigger a WHIP notification upon completion.
  Its optional default value is false.
    - `force_sync` (boolean, required): Set the value to forcefully synchronize/overwrite existing mappings for the overall request.
  Its optional default value is false.
  - `client_reference` (string, required): A unique user-defined client_reference. max_chars=100
  Must be unique.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: "[object Object],[object Object]"
  raw: |
    - `import_reference` (string, required): Unique reference ID for checking status/reports of this import operation.
    - `client_reference` (string, required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-create-barcode-import.md


## Catalog / catplat-service-generate-import-signed-url

### category

Catalog

### endpoint_id

catplat-service-generate-import-signed-url

### title

GenerateImportSignedUrl

### method

POST

### path

/catplat/v1/import/generate-import-signed-url

### purpose

Generate a Signed URL for direct file upload to a GCS bucket. Supported file types: csv, tsv, json, xlsx, xlsm, parquet.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `file_type` (string enum, required): The type of file to be uploaded. Allowed values: `FILE_TYPE_UNSPECIFIED`, `FILE_TYPE_CSV`, `FILE_TYPE_TSV`, `FILE_TYPE_JSON`, `FILE_TYPE_XLSX`, `FILE_TYPE_XLSM`, `FILE_TYPE_PARQUET`. Default: `FILE_TYPE_UNSPECIFIED`.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `file_type` (string enum, required): The type of file to be uploaded. Allowed values: `FILE_TYPE_UNSPECIFIED`, `FILE_TYPE_CSV`, `FILE_TYPE_TSV`, `FILE_TYPE_JSON`, `FILE_TYPE_XLSX`, `FILE_TYPE_XLSM`, `FILE_TYPE_PARQUET`. Default: `FILE_TYPE_UNSPECIFIED`.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    Currently populated because only POST-based signed uploads are supported.
    Contains standard GCS V4 policy fields such as 'key', 'policy', 'x-goog-signature', etc.
  content_types: ""
  fields: "[object Object],[object Object],[object Object]"
  raw: |
    - `url` (string, required): The generated signed URL for the upload.
    - `fields` (object map, required): Additional form fields required when uploading via a signed POST URL.
    Currently populated because only POST-based signed uploads are supported.
    Contains standard GCS V4 policy fields such as 'key', 'policy', 'x-goog-signature', etc.
    - `identifier` (string, required): Unique identifier for the file once uploaded.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-generate-import-signed-url.md


## Catalog / catplat-service-get-import-status

### category

Catalog

### endpoint_id

catplat-service-get-import-status

### title

GetImportStatus

### method

GET

### path

/catplat/v1/import/status/{reference}

### purpose

Retrieve the current status and processing report of an import. Provides metrics on success/failure counts, current progress, and completion timestamps.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  ### Path Parameters
  - `reference` (string, required): Can be either system-generated import_reference or user-defined client_reference.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  ### Path Parameters
  - `reference` (string, required): Can be either system-generated import_reference or user-defined client_reference.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `is_notification_required` (boolean, required): Set the value to trigger a WHIP notification upon completion.
    Its optional default value is false.
      - `force_sync` (boolean, required): Set the value to forcefully synchronize/overwrite existing mappings for the overall request.
    Its optional default value is false.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `import_type` (string, required): Type of import (e.g., "barcode", "SKU Backfill", "SKU Grouping").
    - `import_reference` (string, required): Unique reference ID of the barcode import batch.
    - `client_reference` (string, required): User-defined client reference.
    - `created_by` (string, required): The email or username of the user who triggered the import.
    - `config` (object, required): Configuration for the import behavior.
      - `is_notification_required` (boolean, required): Set the value to trigger a WHIP notification upon completion.
    Its optional default value is false.
      - `force_sync` (boolean, required): Set the value to forcefully synchronize/overwrite existing mappings for the overall request.
    Its optional default value is false.
    - `file_url` (string, required): Original storage path where the file was stored.
    - `bq_table` (string, required): BigQuery table reference.
    - `report` (object, required): Processing report dictionary covering success/failed counts and duration metrics.
    - `import_status` (string enum, required): Current operation status. Allowed values: `IMPORT_STATUS_UNSPECIFIED`, `IMPORT_STATUS_PENDING`, `IMPORT_STATUS_PROCESSING`, `IMPORT_STATUS_COMPLETED`, `IMPORT_STATUS_FAILED`, `IMPORT_STATUS_PARTIAL`. Default: `IMPORT_STATUS_UNSPECIFIED`.
    - `sub_status` (string, required): Sub status for detailed tracking.
    - `created_at` (string (date-time), required): Creation timestamp.
    - `updated_at` (string (date-time), required): Timestamp of the latest status/sub_status change.
    - `completed_at` (string (date-time), required): Timestamp pinpointing the end-time validation and completion.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-get-import-status.md


## Catalog / catplat-service-parent-sku-delete

### category

Catalog

### endpoint_id

catplat-service-parent-sku-delete

### title

ParentSkuDelete

### method

POST

### path

/catplat/v1/sku/parent/delete

### purpose

This API enables partners to delete Parent Partner SKUs/ZSKU Parents; performing this action triggers a cascading deletion of all associated Child SKUs.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item;
  it is used to organize product variations under a single listing.
    - `zsku_parent` (string, required): A unique indentifier created by Noon against the parent partner SKU.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `parent_partner_sku` (string, required): A Parent Partner SKU represents the base product and is not a sellable item;
  it is used to organize product variations under a single listing.
    - `zsku_parent` (string, required): A unique indentifier created by Noon against the parent partner SKU.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `parent_partner_sku` (string): A Parent Partner SKU represents the base product and is not a sellable item;
    it is used to organize product variations under a single listing. Nullable.
      - `zsku_parent` (string, required): A unique indentifier created by Noon against the parent partner SKU.
      - `variants` (object[], required)
        - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
        - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `parent_partner_sku` (string): A Parent Partner SKU represents the base product and is not a sellable item;
    it is used to organize product variations under a single listing. Nullable.
      - `zsku_parent` (string, required): A unique indentifier created by Noon against the parent partner SKU.
      - `variants` (object[], required)
        - `partner_sku` (string, required): Unique identifier for the variant SKU or sellable SKU.
        - `zsku_child` (string, required): A unique indentifier created by Noon against the partner SKU.
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-parent-sku-delete.md


## Catalog / catplat-service-sku-barcode-map

### category

Catalog

### endpoint_id

catplat-service-sku-barcode-map

### title

BarcodeSkuMap

### method

POST

### path

/catplat/v1/barcode/map

### purpose

This API connects an existing partner_sku and a barcode, which for instance, allow warehouse systems to know exactly what that barcode represents in stock The barcode is created if it doesn't exist in the system already

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `barcode` (string, required)
    - `partner_sku` (string, required)
    - `force_map` (boolean): Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `items` (object[], required)
    - `barcode` (string, required)
    - `partner_sku` (string, required)
    - `force_map` (boolean): Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `barcode` (string, required): The barcode, UPC, or ISBN number for the product.
      - `partner_sku` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required)
      - `barcode` (string, required): The barcode, UPC, or ISBN number for the product.
      - `partner_sku` (string, required)
      - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
        - `status_id` (integer (int32))
        - `status_code` (string)
        - `message` (string)
        - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-sku-barcode-map.md


## Catalog / catplat-service-sku-generate

### category

Catalog

### endpoint_id

catplat-service-sku-generate

### title

SkuGenerate

### method

POST

### path

/catplat/v1/sku/generate

### purpose

This API generates SKUs that can be used as Partner SKUs.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: "application/json"
content_type: "application/json"
fields: ""
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `quantity` (integer (int32), required): The number of SKUs to generate. The maximum allowed is 200.
  
  
```

### request_schema

```yaml
documented: false
fields: ""
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `quantity` (integer (int32), required): The number of SKUs to generate. The maximum allowed is 200.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
      - `status_id` (integer (int32))
      - `status_code` (string)
      - `message` (string)
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `skus` (string[], required): These skus can be used as a Partner sku or a Parent Partner sku.
    - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
      - `status_id` (integer (int32))
      - `status_code` (string)
      - `message` (string)
      - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/catalog/catplat-service-sku-generate.md


## Warehouse Platform / warehouse-platform-service-list-warehouses

### category

Warehouse Platform

### endpoint_id

warehouse-platform-service-list-warehouses

### title

ListWarehouses

### method

POST

### path

/warehouse-platform/v1/warehouses/list

### purpose

Returns the list of warehouses associated with the partner.
The list can be filtered by fulfillment system code, and supports pagination to manage large sets of warehouses effectively.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `next_token` (string): Pagination cursor for retrieving the next page of results.
  Leave empty to retrieve the first page. Nullable.
  - `filters` (object): Filters to apply when listing warehouses.
  When paginating, filters must remain identical between calls that share the same cursor. Nullable.
    - `fulfillment_system_code` (string): Filter warehouses by fulfillment system code.
  If not specified, all warehouses will be returned. Nullable.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `next_token` (string): Pagination cursor for retrieving the next page of results.
  Leave empty to retrieve the first page. Nullable.
  - `filters` (object): Filters to apply when listing warehouses.
  When paginating, filters must remain identical between calls that share the same cursor. Nullable.
    - `fulfillment_system_code` (string): Filter warehouses by fulfillment system code.
  If not specified, all warehouses will be returned. Nullable.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    Empty string indicates that there are no subsequent pages. Nullable.
    Contains at most 500 warehouses.
      - `warehouse_code` (string, required): The unique code of the warehouse.
      - `display_name` (string, required): The display name of the warehouse.
      - `fulfillment_system_code` (string, required): The fulfillment system code of the warehouse.
      - `is_active` (boolean, required): Whether the warehouse is active and can be used for fulfillment.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `next_token` (string): Pagination cursor for retrieving the next page of results.
    Empty string indicates that there are no subsequent pages. Nullable.
    - `warehouses` (object[], required): The list of warehouses for this page.
    Contains at most 500 warehouses.
      - `warehouse_code` (string, required): The unique code of the warehouse.
      - `display_name` (string, required): The display name of the warehouse.
      - `fulfillment_system_code` (string, required): The fulfillment system code of the warehouse.
      - `is_active` (boolean, required): Whether the warehouse is active and can be used for fulfillment.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/warehouse_platform/warehouse-platform-service-list-warehouses.md


## Offer / offer-service-get-product-offers

### category

Offer

### endpoint_id

offer-service-get-product-offers

### title

GetProductOffers

### method

GET

### path

/offer/v1/product/{partner_sku}

### purpose

Retrieves offer details for a given partner SKU across all countries.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `partner_sku` (string, required): Your internal unique identifier for the product.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `partner_sku` (string, required): Your internal unique identifier for the product.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `offer_code` (string, required): Unique offer identifier.
      - `country_code` (string, required): The country in which the offer is available (e.g., ae, sa, eg).
      - `business_model` (string, required): The business model under which the offer is available (e.g., noon, supermall).
      - `price` (object): Current price of the offer. Nullable.
        - `amount` (number (double), required): Price amount.
        - `currency` (string, required): Currency code (e.g., AED, SAR, EGP).
      - `is_active` (boolean, required): Whether the product offer is active for this business model and country.
      - `active_net_stock` (integer (int32), required): Available net stock for the offer.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `partner_sku` (string, required): Partner provided SKU identifier.
    - `sku` (string, required): Noon SKU identifier.
    - `title` (string, required): Product title.
    - `brand` (string, required): Product brand name.
    - `offers` (object[], required): List of offers across different countries.
      - `offer_code` (string, required): Unique offer identifier.
      - `country_code` (string, required): The country in which the offer is available (e.g., ae, sa, eg).
      - `business_model` (string, required): The business model under which the offer is available (e.g., noon, supermall).
      - `price` (object): Current price of the offer. Nullable.
        - `amount` (number (double), required): Price amount.
        - `currency` (string, required): Currency code (e.g., AED, SAR, EGP).
      - `is_active` (boolean, required): Whether the product offer is active for this business model and country.
      - `active_net_stock` (integer (int32), required): Available net stock for the offer.
      - `live_status` (boolean, required): Whether the offer is currently live on the storefront.
      - `offer_issues` (object[], required): List of issues due to which the offer is not live.
        - `reason` (string, required): The issue reason.
        - `subreason` (string, required): The issue sub-reason.
        - `description` (string, required): Description of the issue.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/offer/offer-service-get-product-offers.md


## Event Notifications / event-notifications-service-create-https-destination

### category

Event Notifications

### endpoint_id

event-notifications-service-create-https-destination

### title

CreateHttpsDestination

### method

POST

### path

/event-notifications/v1/destination/https-destination/create

### purpose

Creates a new HTTPS webhook destination

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie,Authorization"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `destination_name` (string, required): A name to identify this destination. This is for your reference and has no impact on the functionality.
  - `url` (string, required): The URL of your server where you want to receive event notifications.
  - `credentials` (object, required): Key-value pairs that will be sent as HTTP headers on each webhook
  request to your server. Use these to authenticate or identify incoming
  requests on your end. At least one credential entry is required.
    - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
  on every webhook request. Provide one entry per header you want included;
  the property name is the header name and the value is the header value.
  Example payload:
    "values": {
      "Authorization": "Bearer xxx",
      "x-api-key": "secret-123"
    }
  On read responses, values are redacted (e.g. "******").
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `destination_name` (string, required): A name to identify this destination. This is for your reference and has no impact on the functionality.
  - `url` (string, required): The URL of your server where you want to receive event notifications.
  - `credentials` (object, required): Key-value pairs that will be sent as HTTP headers on each webhook
  request to your server. Use these to authenticate or identify incoming
  requests on your end. At least one credential entry is required.
    - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
  on every webhook request. Provide one entry per header you want included;
  the property name is the header name and the value is the header value.
  Example payload:
    "values": {
      "Authorization": "Bearer xxx",
      "x-api-key": "secret-123"
    }
  On read responses, values are redacted (e.g. "******").
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: "Use this code for any API calls that need to reference this destination."
  content_types: ""
  fields: "[object Object]"
  raw: |
    - `destination_code` (string, required): A unique code that identifies this destination.
    Use this code for any API calls that need to reference this destination.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications/event-notifications-service-create-https-destination.md


## Event Notifications / event-notifications-service-get-destination

### category

Event Notifications

### endpoint_id

event-notifications-service-get-destination

### title

GetDestination

### method

GET

### path

/event-notifications/v1/destination/{destination_code}/get

### purpose

Get a single destination

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie,Authorization"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: false
content_type: "Path Parameters"
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `destination_code` (string, required): A unique code that identifies this destination.
  Use this code for any API calls that need to reference this destination.
  
  No body documented.
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `destination_code` (string, required): A unique code that identifies this destination.
  Use this code for any API calls that need to reference this destination.
  
  No body documented.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `destination_code` (string, required): A unique code that identifies this destination.
    Use this code for any API calls that need to reference this destination.
      - `destination_name` (string, required): A name to identify this destination. For your reference only, no functional impact.
      - `is_active` (boolean, required): Whether this destination is active. Inactive destinations will not receive event notifications.
      - `url` (string, required): The URL of your server where you want to receive event notifications.
      - `credentials` (object, required): Credential keys configured for this destination. Values are redacted.
        - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
    on every webhook request. Provide one entry per header you want included;
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `destination` (object, required): The requested destination.
      - `destination_code` (string, required): A unique code that identifies this destination.
    Use this code for any API calls that need to reference this destination.
      - `destination_name` (string, required): A name to identify this destination. For your reference only, no functional impact.
      - `is_active` (boolean, required): Whether this destination is active. Inactive destinations will not receive event notifications.
      - `url` (string, required): The URL of your server where you want to receive event notifications.
      - `credentials` (object, required): Credential keys configured for this destination. Values are redacted.
        - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
    on every webhook request. Provide one entry per header you want included;
    the property name is the header name and the value is the header value.
    Example payload:
      "values": {
        "Authorization": "Bearer xxx",
        "x-api-key": "secret-123"
      }
    On read responses, values are redacted (e.g. "******").
      - `event_types` (string[], required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications/event-notifications-service-get-destination.md


## Event Notifications / event-notifications-service-list-destinations

### category

Event Notifications

### endpoint_id

event-notifications-service-list-destinations

### title

ListDestinations

### method

GET

### path

/event-notifications/v1/destination/list

### purpose

Gets all destinations for a given project

No parameters documented.

No body documented.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie,Authorization"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: null
fields: ""
raw: null
```

### request_schema

```yaml
documented: false
fields: ""
raw: null
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `destination_code` (string, required): A unique code that identifies this destination.
    Use this code for any API calls that need to reference this destination.
      - `destination_name` (string, required): A name to identify this destination. For your reference only, no functional impact.
      - `is_active` (boolean, required): Whether this destination is active. Inactive destinations will not receive event notifications.
      - `url` (string, required): The URL of your server where you want to receive event notifications.
      - `credentials` (object, required): Credential keys configured for this destination. Values are redacted.
        - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
    on every webhook request. Provide one entry per header you want included;
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `destinations` (object[], required): All destinations configured for this partner. Maximum of 5 destinations.
      - `destination_code` (string, required): A unique code that identifies this destination.
    Use this code for any API calls that need to reference this destination.
      - `destination_name` (string, required): A name to identify this destination. For your reference only, no functional impact.
      - `is_active` (boolean, required): Whether this destination is active. Inactive destinations will not receive event notifications.
      - `url` (string, required): The URL of your server where you want to receive event notifications.
      - `credentials` (object, required): Credential keys configured for this destination. Values are redacted.
        - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
    on every webhook request. Provide one entry per header you want included;
    the property name is the header name and the value is the header value.
    Example payload:
      "values": {
        "Authorization": "Bearer xxx",
        "x-api-key": "secret-123"
      }
    On read responses, values are redacted (e.g. "******").
      - `event_types` (string[], required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications/event-notifications-service-list-destinations.md


## Event Notifications / event-notifications-service-list-event-types

### category

Event Notifications

### endpoint_id

event-notifications-service-list-event-types

### title

ListEventTypes

### method

GET

### path

/event-notifications/v1/event-type/list

### purpose

Returns all available event types that can be subscribed to

No parameters documented.

No body documented.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: null
fields: ""
raw: null
```

### request_schema

```yaml
documented: false
fields: ""
raw: null
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    return format is a list of strings in the format "NAMESPACE::EVENT_NAME", e.g. "FBPI::ORDER_SYNC".
    Event types payload format and details are also available in our  event notification documentation.
  content_types: ""
  fields: "[object Object]"
  raw: |
    - `event_types` (string[], required): All available event types that can be subscribed to.
    return format is a list of strings in the format "NAMESPACE::EVENT_NAME", e.g. "FBPI::ORDER_SYNC".
    Event types payload format and details are also available in our  event notification documentation.
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "No body documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications/event-notifications-service-list-event-types.md


## Event Notifications / event-notifications-service-update-destination

### category

Event Notifications

### endpoint_id

event-notifications-service-update-destination

### title

UpdateDestination

### method

POST

### path

/event-notifications/v1/destination/https-destination/{destination_code}/update

### purpose

Updates an existing destination

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie,Authorization"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "Path Parameters"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `destination_code` (string, required): A unique code that identifies this destination.
  Use this code for any API calls that need to reference this destination.
  ### application/json
  - `destination_name` (string): A name to identify this destination. For your reference only, no functional impact. Nullable.
  - `is_active` (boolean): Whether this destination is active. Inactive destinations will not receive event notifications. Nullable.
  - `url` (string): The URL of your server where you want to receive event notifications. Nullable.
  - `credentials` (object): Key-value pairs that will be sent as HTTP headers on each webhook request
  to your server. Use these to authenticate or identify incoming requests on
  your end.
  Behavior:
    - Omitted: existing credentials are left unchanged.
    - Present with values: replaces all existing credentials.
    - Present with an empty map: removes all existing credentials. Nullable.
    - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
  on every webhook request. Provide one entry per header you want included;
  the property name is the header name and the value is the header value.
  Example payload:
    "values": {
      "Authorization": "Bearer xxx",
      "x-api-key": "secret-123"
    }
  On read responses, values are redacted (e.g. "******").
  - `event_types` (object[], required): Event type subscriptions to modify for this destination.
  Each entry upserts a single subscription based on its is_active flag:
    - is_active=true:  subscribes the destination to this event type
                       (creates the subscription if it does not exist).
    - is_active=false: unsubscribes the destination from this event type.
  
  Event types not included in the list are left unchanged. To leave all
  subscriptions as-is, omit the field or pass an empty list.
  Note: setting the destination-level is_active to false does not remove
  subscriptions. They are preserved, but no events will be delivered
  while the destination is inactive.
  Example: [
    {event_type: "FBPI::ORDER_SYNC", is_active: true},
    {event_type: "FBPO::PO_SYNC",    is_active: false}
  ]
    - `event_type` (string, required): The unique name of the event type, in the format "NAMESPACE::EVENT_NAME", e.g. "FBPI::ORDER_SYNC".
    - `is_active` (boolean, required): Whether to activate or deactivate this event type for the destination.
  If true, the event type will be activated. If false, it will be deactivated.
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  ### Path Parameters
  - `destination_code` (string, required): A unique code that identifies this destination.
  Use this code for any API calls that need to reference this destination.
  ### application/json
  - `destination_name` (string): A name to identify this destination. For your reference only, no functional impact. Nullable.
  - `is_active` (boolean): Whether this destination is active. Inactive destinations will not receive event notifications. Nullable.
  - `url` (string): The URL of your server where you want to receive event notifications. Nullable.
  - `credentials` (object): Key-value pairs that will be sent as HTTP headers on each webhook request
  to your server. Use these to authenticate or identify incoming requests on
  your end.
  Behavior:
    - Omitted: existing credentials are left unchanged.
    - Present with values: replaces all existing credentials.
    - Present with an empty map: removes all existing credentials. Nullable.
    - `values` (object map, required): A map of HTTP header name to header value. Each entry is sent as a header
  on every webhook request. Provide one entry per header you want included;
  the property name is the header name and the value is the header value.
  Example payload:
    "values": {
      "Authorization": "Bearer xxx",
      "x-api-key": "secret-123"
    }
  On read responses, values are redacted (e.g. "******").
  - `event_types` (object[], required): Event type subscriptions to modify for this destination.
  Each entry upserts a single subscription based on its is_active flag:
    - is_active=true:  subscribes the destination to this event type
                       (creates the subscription if it does not exist).
    - is_active=false: unsubscribes the destination from this event type.
  
  Event types not included in the list are left unchanged. To leave all
  subscriptions as-is, omit the field or pass an empty list.
  Note: setting the destination-level is_active to false does not remove
  subscriptions. They are preserved, but no events will be delivered
  while the destination is inactive.
  Example: [
    {event_type: "FBPI::ORDER_SYNC", is_active: true},
    {event_type: "FBPO::PO_SYNC",    is_active: false}
  ]
    - `event_type` (string, required): The unique name of the event type, in the format "NAMESPACE::EVENT_NAME", e.g. "FBPI::ORDER_SYNC".
    - `is_active` (boolean, required): Whether to activate or deactivate this event type for the destination.
  If true, the event type will be activated. If false, it will be deactivated.
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: null
  content_types: ""
  fields: ""
  raw: ""
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications/event-notifications-service-update-destination.md


## Content / content-service-get-content

### category

Content

### endpoint_id

content-service-get-content

### title

GetContent

### method

POST

### path

/content/v1/product/content/get

### purpose

Retrieves content information for specified products.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `sku_parent` (string, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `sku_parent` (string, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `url` (string, required)
      - `sort` (integer (int64), required)
      - `visibility` (string enum, required): Allowed values: `VISIBILITY_STATUS_UNSPECIFIED`, `VISIBILITY_STATUS_VISIBLE`, `VISIBILITY_STATUS_HIDDEN`. Default: `VISIBILITY_STATUS_UNSPECIFIED`.
      - `review_status` (string enum, required): Allowed values: `REVIEW_STATUS_UNSPECIFIED`, `REVIEW_STATUS_VALID`, `REVIEW_STATUS_PENDING`, `REVIEW_STATUS_INVALID`. Default: `REVIEW_STATUS_UNSPECIFIED`.
      - `issues` (object[], required)
        - `code` (string, required)
        - `message` (string, required)
      - `language` (string enum, required): Allowed values: `LANGUAGE_UNSPECIFIED`, `LANGUAGE_EN`, `LANGUAGE_AR`. Default: `LANGUAGE_UNSPECIFIED`.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `sku_parent` (string, required)
    - `attributes` (object map, required)
    - `images` (object[], required)
      - `url` (string, required)
      - `sort` (integer (int64), required)
      - `visibility` (string enum, required): Allowed values: `VISIBILITY_STATUS_UNSPECIFIED`, `VISIBILITY_STATUS_VISIBLE`, `VISIBILITY_STATUS_HIDDEN`. Default: `VISIBILITY_STATUS_UNSPECIFIED`.
      - `review_status` (string enum, required): Allowed values: `REVIEW_STATUS_UNSPECIFIED`, `REVIEW_STATUS_VALID`, `REVIEW_STATUS_PENDING`, `REVIEW_STATUS_INVALID`. Default: `REVIEW_STATUS_UNSPECIFIED`.
      - `issues` (object[], required)
        - `code` (string, required)
        - `message` (string, required)
    - `statuses` (object[], required)
      - `language` (string enum, required): Allowed values: `LANGUAGE_UNSPECIFIED`, `LANGUAGE_EN`, `LANGUAGE_AR`. Default: `LANGUAGE_UNSPECIFIED`.
      - `content` (object, required)
        - `completeness` (string, required)
        - `missing_attributes` (string[], required)
        - `invalid_attributes` (string[], required)
      - `qc` (object, required)
        - `status` (string enum, required): Allowed values: `QC_STATUS_UNSPECIFIED`, `QC_STATUS_APPROVED`, `QC_STATUS_REJECTED`, `QC_STATUS_PENDING`, `QC_STATUS_NOT_ELIGIBLE`. Default: `QC_STATUS_UNSPECIFIED`.
        - `rejection_reasons` (string[], required)
        - `comment` (string, required)
      - `overall_status` (string enum, required): Allowed values: `OVERALL_STATUS_UNSPECIFIED`, `OVERALL_STATUS_ACTIVE`, `OVERALL_STATUS_INACTIVE`. Default: `OVERALL_STATUS_UNSPECIFIED`.
      - `errors` (object[], required)
        - `code` (string, required)
        - `message` (string, required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/content/content-service-get-content.md


## Content / content-service-list-categories

### category

Content

### endpoint_id

content-service-list-categories

### title

ListCategories

### method

POST

### path

/content/v1/categories/list

### purpose

Lists all available product categories in the catalog.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: "application/json"
content_type: "application/json"
fields: ""
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - Type: object.
  
  
```

### request_schema

```yaml
documented: false
fields: ""
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - Type: object.
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    These codes represent the full hierarchy path for a specific product type
    (e.g., family-type-subtype).
  content_types: ""
  fields: "[object Object]"
  raw: |
    - `categories` (string[], required): A list of unique category codes.
    These codes represent the full hierarchy path for a specific product type
    (e.g., family-type-subtype).
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/content/content-service-list-categories.md


## Content / content-service-list-category-attributes

### category

Content

### endpoint_id

content-service-list-category-attributes

### title

ListCategoryAttributes

### method

POST

### path

/content/v1/categories/attributes/list

### purpose

Retrieves the attributes for a specified product category.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `category_code` (string, required)
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  
  ### application/json
  - `category_code` (string, required)
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `attribute_code` (string, required)
      - `is_mandatory` (boolean, required)
      - `is_facet` (boolean, required)
      - `attribute_type` (string enum, required): Allowed values: `ATTRIBUTE_TYPE_UNSPECIFIED`, `ATTRIBUTE_TYPE_BOOL`, `ATTRIBUTE_TYPE_DATETIME`, `ATTRIBUTE_TYPE_FILE`, `ATTRIBUTE_TYPE_JSONDICT`, `ATTRIBUTE_TYPE_METRIC`, `ATTRIBUTE_TYPE_NUMERIC`, `ATTRIBUTE_TYPE_SELECT`, `ATTRIBUTE_TYPE_TEXT`. Default: `ATTRIBUTE_TYPE_UNSPECIFIED`.
      - `is_localizable` (boolean, required)
      - `is_multivalued` (boolean, required)
      - `max_values` (integer (int32)): Nullable.
      - `min_characters` (integer (int32)): Nullable.
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `attributes` (object[], required)
      - `attribute_code` (string, required)
      - `is_mandatory` (boolean, required)
      - `is_facet` (boolean, required)
      - `attribute_type` (string enum, required): Allowed values: `ATTRIBUTE_TYPE_UNSPECIFIED`, `ATTRIBUTE_TYPE_BOOL`, `ATTRIBUTE_TYPE_DATETIME`, `ATTRIBUTE_TYPE_FILE`, `ATTRIBUTE_TYPE_JSONDICT`, `ATTRIBUTE_TYPE_METRIC`, `ATTRIBUTE_TYPE_NUMERIC`, `ATTRIBUTE_TYPE_SELECT`, `ATTRIBUTE_TYPE_TEXT`. Default: `ATTRIBUTE_TYPE_UNSPECIFIED`.
      - `is_localizable` (boolean, required)
      - `is_multivalued` (boolean, required)
      - `max_values` (integer (int32)): Nullable.
      - `min_characters` (integer (int32)): Nullable.
      - `max_characters` (integer (int32)): Nullable.
      - `is_html_allowed` (boolean): Nullable.
      - `allowed_html_tags` (string[], required)
      - `number_min` (number (double)): Nullable.
      - `number_max` (number (double)): Nullable.
      - `is_negative_allowed` (boolean): Nullable.
      - `date_min` (string (date-time)): The earliest allowed date (ISO 8601 format) for datetime attributes. Nullable.
      - `date_max` (string (date-time)): The latest allowed date (ISO 8601 format) for datetime attributes. Nullable.
      - `min_size_in_kilobytes` (number (double)): Nullable.
      - `max_size_in_kilobytes` (number (double)): Nullable.
      - `allowed_mimetypes` (string[], required)
      - `additional_validation_regex` (string): Nullable.
      - `attribute_options` (string[], required)
      - `attribute_metric_units` (string[], required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/content/content-service-list-category-attributes.md


## Content / content-service-upsert-product

### category

Content

### endpoint_id

content-service-upsert-product

### title

UpsertProduct

### method

POST

### path

/content/v1/product/upsert

### purpose

Creates or updates a product in the catalog.

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `skus` (object[], required)
    - `partner_sku` (string, required): Your internal unique identifier for this specific variation. It has to be unique across all your products.
    - `size` (string): The specific size value for this SKU (e.g., Large, 42, S). It should reflect the actual label size. Nullable.
  - `brand` (string, required)
  - `category` (string, required)
  - `images` (object[], required)
    - `url` (string, required)
    - `sort` (integer (int64), required)
  - `attributes` (object map, required)
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `skus` (object[], required)
    - `partner_sku` (string, required): Your internal unique identifier for this specific variation. It has to be unique across all your products.
    - `size` (string): The specific size value for this SKU (e.g., Large, 42, S). It should reflect the actual label size. Nullable.
  - `brand` (string, required)
  - `category` (string, required)
  - `images` (object[], required)
    - `url` (string, required)
    - `sort` (integer (int64), required)
  - `attributes` (object map, required)
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `sku` (string, required)
      - `partner_sku` (string, required)
      - `psku_code` (string, required)
      - `size` (string): Nullable.
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `sku_parent` (string, required)
    - `variants` (object[], required)
      - `sku` (string, required)
      - `partner_sku` (string, required)
      - `psku_code` (string, required)
      - `size` (string): Nullable.
    - `status` (object, required): The `Status` type defines a logical error model that is suitable for
    different programming environments, including REST APIs and RPC APIs. It is
    used by [gRPC](https://github.com/grpc). Each `Status` message contains
    three pieces of data: error code, error message, and error details.
    You can find out more about this error model and how to work with it in the
    [API Design Guide](https://cloud.google.com/apis/design/errors).
      - `status_id` (integer (int32))
      - `status_code` (string)
      - `message` (string)
      - `details` (object[])
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
- "Nullable request fields are preserved in request_schema.fields."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/content/content-service-upsert-product.md


## Returns / return-reference-service-list-return-references

### category

Returns

### endpoint_id

return-reference-service-list-return-references

### title

ListReturnReferences

### method

POST

### path

/returns/v1/return-references/list

### purpose

List return items and their barcode references matching a barcode for the given merchants

### authentication

```yaml
documented: false
details: "The endpoint page does not expose a dedicated authentication field."
```

### rate_limit

```yaml
rate: "[object Object]"
burst: "[object Object]"
```

### headers

```yaml
documented: false
observed_in_examples: "Content-Type,Accept,User-Agent,Cookie"
```

### path_parameters

`[]`

### query_parameters

`[]`

### request_body

```yaml
documented: true
content_type: "application/json"
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `barcode` (string, required): Barcode to look up return references for.
  - `merchant_codes` (string[], required): Optional. Merchant codes to scope the results to (user must have access to). If omitted, results
  are scoped to the default_project_code, (max 200 items).
  
  
```

### request_schema

```yaml
documented: true
fields: "[object Object],[object Object]"
raw: |
  ## Request
  
  No parameters documented.
  ### application/json
  - `barcode` (string, required): Barcode to look up return references for.
  - `merchant_codes` (string[], required): Optional. Merchant codes to scope the results to (user must have access to). If omitted, results
  are scoped to the default_project_code, (max 200 items).
  
  
```

### responses

```yaml
- status: "200"
  description: "A successful response."
  content_types: ""
  fields: ""
  raw: "A successful response."
- status: "application/json"
  description: |
    - `mp_code` (string, required): Marketplace code the item belongs to.
      - `purchase_item_nr` (string, required): Unique identifier of the purchased item being returned.
      - `partner_sku` (string, required): Partner's SKU for the item.
      - `merchant_code` (string, required): Code of the merchant that owns the item.
      - `references` (object[], required): Barcode references attached to this item.
        - `barcode` (string, required): Barcode value of the reference.
        - `barcode_type` (string, required): Type of the barcode (awb_nr/rms_barcode).
      - `order_nr` (string, required)
  content_types: ""
  fields: "[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object]"
  raw: |
    - `items` (object[], required): Return items matching the requested barcode (max 200 items).
      - `mp_code` (string, required): Marketplace code the item belongs to.
      - `purchase_item_nr` (string, required): Unique identifier of the purchased item being returned.
      - `partner_sku` (string, required): Partner's SKU for the item.
      - `merchant_code` (string, required): Code of the merchant that owns the item.
      - `references` (object[], required): Barcode references attached to this item.
        - `barcode` (string, required): Barcode value of the reference.
        - `barcode_type` (string, required): Type of the barcode (awb_nr/rms_barcode).
      - `order_nr` (string, required)
- status: "default"
  description: "An unexpected error response."
  content_types: ""
  fields: ""
  raw: "An unexpected error response."
```

### examples

`[]`

### errors

```yaml
- status: "default"
  description: "An unexpected error response."
  fields: ""
  raw: "An unexpected error response."
```

### notes

```yaml
- "No parameters documented."
```

### source_url

https://noon-docs.noonpartners.dev/docs/api-reference/returns/return-reference-service-list-return-references.md


