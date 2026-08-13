# Channel Protocol Matrix

> **Rule enforced by V2:** Mock network, not protocol. Every implemented
> channel capability below is backed by an officially verified page.
> Capabilities that official documentation cannot prove are explicitly
> marked `UNSUPPORTED` / `PENDING_OFFICIAL_SPEC` — never invented from
> Legacy or third-party material.
>
> Verification date: **2026-08-13** (official docs re-checked during V2).

Legend: `Implemented` = adapter code exists and is covered by a protocol
contract test. `Simulated` = works through the recording transport.
`Real network` = whether `REAL_CHANNEL_NETWORK` can enable live delivery
(credentials still required).

---

## 1. Feishu (飞书)

### 1.1 Inbound message receive

| Item | Value |
| --- | --- |
| Capability | `DM_INBOUND` / `GROUP_INBOUND` (message receive event) |
| Official docs | 接收消息 - 服务端 API - 飞书开放平台 |
| Official URL | https://open.feishu.cn/document/server-docs/im-v1/message/events/receive |
| Event type | `im.message.receive_v1` |
| Fields used | `header.token`, `header.event_type`, `event.sender.sender_id.open_id`, `event.message.message_id`, `event.message.chat_id`, `event.message.chat_type` (`p2p`/`group`), `event.message.message_type`, `event.message.content` (JSON string) |
| Idempotency field | **`message_id`** (official docs recommend dedup by message id on repeated push; `event_id` is NOT used) |
| Headers | none (token inside body) |
| Implemented | Yes — `app/adapters/feishu.py::FeishuAdapter.handle_http/build_inbound` |
| Test fixture | `tests/test_v2_protocol.py::test_ac26_feishu_official_shaped_inbound` (official-shaped payload incl. `schema: 2.0`, `header`, `event`) |
| Simulated | Yes — events arrive via POST body, no outbound connection needed |
| Real network | Disabled (inbound never dials out; only callback URL wiring is external) |

### 1.2 URL verification / challenge

| Item | Value |
| --- | --- |
| Capability | `WEBHOOK_VERIFICATION` |
| Official URL | https://open.feishu.cn/document/server-docs/im-v1/message/events/receive (事件订阅配置) |
| Protocol | POST `{"type":"url_verification","challenge":"...","token":"..."}` → reply `{"challenge":"..."}`; token must match configured Verification Token |
| Implemented | Yes — `FeishuAdapter.handle_http` returns `InboundRequest(challenge=...)` |
| Test | `test_ac26_feishu_url_verification_challenge`, `test_ac26_feishu_url_verification_token_mismatch_rejected` |
| Real network | n/a |

### 1.3 Encrypted event body (Encrypt Key)

| Item | Value |
| --- | --- |
| Capability | `WEBHOOK_VERIFICATION` (encrypted mode) |
| Official URL | https://open.feishu.cn/document/server-docs/im-v1/message/events/receive (事件订阅配置, Encrypt Key) |
| Fields used | `{ "encrypt": "<base64>" }` |
| Algorithm | AES-256-CBC, key = `encrypt_key`, IV = first 16 bytes of the ciphertext (current official config mode), PKCS7 padding; plaintext contains the original event JSON |
| Implemented | Yes — `FeishuAdapter._decrypt` / `FeishuAdapter.encrypt_for_test` |
| Test | `test_ac26_feishu_encrypted_event_roundtrip` (encrypt → webhook → build_inbound) |
| Real network | n/a |

### 1.4 Outbound DM message

| Item | Value |
| --- | --- |
| Capability | `DM_OUTBOUND` |
| Official docs | 发送消息 - 服务端 API - 飞书开放平台 |
| Official URL | https://open.feishu.cn/document/server-docs/im-v1/message/create |
| Endpoint | `POST https://open.feishu.cn/open-apis/im/v1/messages` |
| Query | `receive_id_type=open_id` |
| Headers | `Authorization: Bearer <tenant_access_token>`, `Content-Type: application/json; charset=utf-8` |
| Body | `{ "receive_id": "<open_id>", "msg_type": "text", "content": "{\"text\":\"...\"}", "uuid": "<idempotent uuid>" }` |
| Token API | `POST /open-apis/auth/v3/tenant_access_token/internal` (official tenant_access_token acquisition) |
| Implemented | Yes — `app/adapters/outbound.py::FeishuOutboundClient` |
| Test | `test_ac27_feishu_dm_outbound_contract` (asserts URL, query, headers, body) |
| Simulated | Yes — RecordingTransport captures request; `uuid` field used for dedupe |
| Real network | Enabled only via `REAL_CHANNEL_NETWORK=true` + credentials |

### 1.5 Outbound group message

| Item | Value |
| --- | --- |
| Capability | `GROUP_OUTBOUND` |
| Official URL | https://open.feishu.cn/document/server-docs/im-v1/message/create |
| Endpoint | `POST https://open.feishu.cn/open-apis/im/v1/messages` |
| Query | `receive_id_type=chat_id` |
| Body | `{ "receive_id": "<chat_id>", ... }` (same send API, different receive_id_type) |
| Implemented | Yes — `FeishuOutboundClient` |
| Test | `test_ac27_feishu_group_outbound_contract` |
| Simulated | Yes |
| Real network | Disabled by default |

---

## 2. WeCom (企业微信)

> The official text-message callback format (below) contains **no chat id
> for group conversations** (`ToUserName`, `FromUserName`, `MsgId`,
> `AgentID` only). Therefore group **inbound** cannot be implemented from
> official docs and is honestly disabled.

### 2.1 Callback configuration & URL verification (GET echostr)

| Item | Value |
| --- | --- |
| Capability | `WEBHOOK_VERIFICATION` |
| Official docs | 接收消息与事件 - 概述(接入参数:URL / Token / EncodingAESKey) |
| Official URL | https://developer.work.weixin.qq.com/document/path/90930 |
| Fields used | `msg_signature`, `timestamp`, `nonce`, `echostr` (GET), `Encrypt` (POST body) |
| Signature | `sha1(sort([token, timestamp, nonce, encrypted]))` (official algorithm) |
| Retry | official: 5 s / 3 retries per event (webhook idempotent via `MsgId`) |
| Implemented | Yes — `app/adapters/wecom.py::WeComAdapter.handle_http` (GET challenge returns decrypted echostr) |
| Test | `test_ac28_wecom_signature_verification`, `test_ac28_wecom_url_verification_get` |
| Real network | n/a |

### 2.2 Text message callback (DM inbound)

| Item | Value |
| --- | --- |
| Capability | `DM_INBOUND` (user → app text message) |
| Official docs | 接收消息与事件 - 文本消息 |
| Official URL | https://developer.work.weixin.qq.com/document/path/90239 |
| Fields used | `ToUserName`, `FromUserName`, `CreateTime`, `MsgType`, `Content`, `MsgId`, `AgentID` |
| Idempotency field | `MsgId` |
| Format | XML body inside `<Encrypt>` (AES-256-CBC, PKCS7) |
| Implemented | Yes — `WeComAdapter.build_inbound` (XML parse, `wecom:<MsgId>` idempotency key) |
| Test | `test_ac28_wecom_encrypted_message_callback_parse` |
| Real network | Disabled by default |

### 2.3 Group inbound

| Item | Value |
| --- | --- |
| Capability | `GROUP_INBOUND` |
| Official docs | **No official page found proving a group message callback that includes a group chat id in the message payload** (90239 text format has no chat id; official group message events require the user to be in a chat that notifies the app, and the reachable event schema for such group messages was not verifiable during V2) |
| Status | **UNSUPPORTED / PENDING_OFFICIAL_SPEC** |
| Implemented | No — capability intentionally NOT in `WeComAdapter.capabilities` |
| Note | `WeComAdapter.group_inbound_support_note()` returns the honest limitation; business `OPERATOR` conversations still work via simulation/`wecom:` group conversation ids bound at registration |
| Real network | Disabled |

### 2.4 access_token acquisition

| Item | Value |
| --- | --- |
| Capability | internal (auth) |
| Official docs | 获取access_token |
| Official URL | https://developer.work.weixin.qq.com/document/path/91039 |
| Endpoint | `GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=<corpid>&corpsecret=<secret>` |
| Result | `{ "access_token": "...", "expires_in": 7200 }` (cached) |
| Implemented | Yes — `WeComOutboundClient._get_access_token` (cached; offline no-op) |
| Test | `test_ac28_wecom_gettoken_cached` (asserts no network call offline) |

### 2.5 Outbound DM (application message)

| Item | Value |
| --- | --- |
| Capability | `DM_OUTBOUND` |
| Official docs | 发送应用消息 |
| Official URL | https://developer.work.weixin.qq.com/document/path/90236 |
| Endpoint | `POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=<token>` |
| Body | `{ "touser": "<userid>", "msgtype": "text", "agentid": "<agentid>", "text": { "content": "..." }, "safe": 0 }` |
| Implemented | Yes — `app/adapters/outbound.py::WeComOutboundClient` |
| Test | `test_ac28_wecom_outbound_dm_contract` |
| Simulated | Yes |
| Real network | Disabled by default |

### 2.6 Outbound group (appchat push)

| Item | Value |
| --- | --- |
| Capability | `GROUP_OUTBOUND` |
| Official docs | 应用推送消息(群聊会话) |
| Official URL | https://developer.work.weixin.qq.com/document/path/90248 |
| Endpoint | `POST https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token=<token>` |
| Body | `{ "chatid": "<chatid>", "msgtype": "text", "text": { "content": "..." } }` |
| Implemented | Yes — `WeComOutboundClient` (target kind CONVERSATION → `appchat/send`) |
| Test | `test_ac28_wecom_outbound_group_contract` |
| Simulated | Yes |
| Real network | Disabled by default |

---

## 3. Capability summary

| Capability | Feishu | WeCom |
| --- | --- | --- |
| `DM_INBOUND` | ✅ implemented | ✅ implemented (text message callback) |
| `GROUP_INBOUND` | ✅ implemented (`chat_type=group`) | ⛔ `PENDING_OFFICIAL_SPEC` |
| `DM_OUTBOUND` | ✅ implemented (`receive_id_type=open_id`) | ✅ implemented (`message/send`) |
| `GROUP_OUTBOUND` | ✅ implemented (`receive_id_type=chat_id`) | ✅ implemented (`appchat/send`) |
| `WEBHOOK_VERIFICATION` | ✅ implemented (token / challenge / encrypt) | ✅ implemented (sha1 signature / echostr / AES) |
| `MESSAGE_REPLY` | not required (send-only) | not required (send-only) |
| `CARD_ACTION` | not in V2 scope | not in V2 scope |

**Transport switching:** `REAL_CHANNEL_NETWORK=false` (default) uses the
recording `HttpTransport`; `true` swaps in `RealHttpTransport`. Swapping in
real credentials later requires **config + transport only** — no redesign of
identity / conversation / ticket / notification / workflow.

---

## 4. Official docs referenced (evidence)

| # | Channel | Doc title | URL |
| --- | --- | --- | --- |
| 1 | Feishu | 接收消息 - 服务端 API | https://open.feishu.cn/document/server-docs/im-v1/message/events/receive |
| 2 | Feishu | 发送消息 - 服务端 API | https://open.feishu.cn/document/server-docs/im-v1/message/create |
| 3 | WeCom | 接收消息与事件 - 概述(回调配置) | https://developer.work.weixin.qq.com/document/path/90930 |
| 4 | WeCom | 接收消息与事件 - 文本消息 | https://developer.work.weixin.qq.com/document/path/90239 |
| 5 | WeCom | 获取access_token | https://developer.work.weixin.qq.com/document/path/91039 |
| 6 | WeCom | 发送应用消息 | https://developer.work.weixin.qq.com/document/path/90236 |
| 7 | WeCom | 应用推送消息(群聊会话) | https://developer.work.weixin.qq.com/document/path/90248 |

- WeCom group **inbound** and any capability not in the table above:
  `UNSUPPORTED / PENDING_OFFICIAL_SPEC`.
- Legacy `hmac-sha256(timestamp:nonce)` (reference `wecom_bridge_server.py`)
  is **NOT** used anywhere — it is not an official protocol.
