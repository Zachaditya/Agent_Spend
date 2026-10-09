# Agent Spend: Phase 5

Create one Coinbase CDP-managed shopping wallet from ChatGPT with a separate
confirmation step, then fund it from a server-controlled treasury with a custom
test USDC amount. Every grant and repeated top-up requires a separate confirmation.
Then set a deterministic spending policy in chat: first setup requires confirmation,
and later chat updates can only make the policy stricter. Phase 5 adds eshop
product search, product details, policy-checked purchase requests, transaction
history, and a developer-only reset script. Purchase amount and recipient come
only from the eshop order. Only Base Sepolia (chain ID `84532`) is accepted. The
detailed `agent_spend_vault/phase_5.md` is the current scope.

## Install

Use Python 3.11 or newer in a dedicated environment (verified with Python 3.11):

```sh
cd /Users/ZacharyAditya/projects/Projects/agent_spend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
```

Populate only your local `.env` with `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and
`CDP_WALLET_SECRET` from the CDP portal. The previous project's credentials can be
reused. Do not paste credentials into ChatGPT, commit `.env`, or enable SDK debug logs.
Environment variables override the project `.env` file.
`STARTER_USDC=25` is the default amount when the user omits one. Custom amounts
must be positive decimal strings with at most two fractional places; no rounding
is performed. `MAX_FUNDING_USDC=100` limits each grant/top-up and
`MAX_TOTAL_FUNDING_USDC=500` limits lifetime USDC funding per wallet. Pending,
partial, and uncertain operations reserve allowance; definitively reverted USDC
without any successful leg releases it. Spending funds never replenishes this cap.
`STARTER_ETH=0.0005` is sent only with the initial grant. The server controls
`TREASURY_ACCOUNT_NAME`, the persisted recipient, token, and network. Funding
does not create or change purchase policy or reset spending history.
Policy caps are fixed in code at `MAX_WEEKLY_LIMIT=$200.00` and
`MAX_AUTO_TX=$50.00`. Initial policy setup has no side effect until confirmed.
After activation, lower weekly or auto-approval limits apply immediately; increases
return `LOOSENING_NOT_ALLOWED`. Weekly spend is computed from `SUBMITTED` and
`CONFIRMED` purchases over the rolling seven-day window. Above-auto purchases stay
`PENDING_APPROVAL` and unpaid; approval is deferred in Phase 5.
Set `ESHOP_URL` to the running eshop backend and `AGENT_API_KEY` to the same shared
secret configured in the eshop. `MERCHANT_ADDRESS` must match the eshop checkout
recipient.
`DB_PATH` is resolved relative to this project, independent of the working directory.
The verified local checkout already has `.venv`, an ignored owner-only `.env`, and
one persisted testnet wallet. Skip initial configuration when using that checkout.
`requirements.lock.txt` captures the exact verified environment for reproducibility.

Check treasury readiness before a demo:

```sh
source .venv/bin/activate
python -m scripts.bootstrap
```

Add `--request-faucet` only when you intentionally want the CDP SDK to request
supported Base Sepolia faucet funds for the treasury. The bootstrap script never
creates the shopper wallet and never funds it; funding must happen through ChatGPT.

## Run and Tunnel

```sh
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal:

```sh
ngrok http 8000
```

Copy the HTTPS tunnel's **hostname only** into `PUBLIC_HOST` in `.env`, for example
`example.ngrok-free.app`, and restart uvicorn. The MCP URL is then
`https://example.ngrok-free.app/mcp`. A hostname change requires another service
restart and an updated ChatGPT connection URL. Only loopback and the configured
public host are allowed; an unexpected host returns HTTP `421`.

Startup calls `eth_chainId` before opening CDP or SQLite. A different chain fails
with `RPC_CHAIN_MISMATCH`; an unavailable or malformed RPC fails with `RPC_UNAVAILABLE`.
There is no runtime flag to disable the testnet guard.

Use exactly one uvicorn worker. SQLite enforces one pointer, an async lock serializes
creation, and confirmation tokens live in this process. Pending confirmations are
invalid after restart, so request a new disclosure. The public wallet pointer persists.
Consumed confirmations are removed immediately; new disclosures sweep expired records.
At most 1,024 unexpired confirmations can be pending. At capacity, issuance fails
without evicting valid tokens; consumption or expiry frees capacity. A process-local
MAC preserves `CONFIRMATION_EXPIRED` for reclaimed tokens without retaining history.
This memory bound is not authentication or a request rate limit.

## Connect ChatGPT

1. Enable Developer mode in ChatGPT: Settings > Apps > Advanced settings.
   Workspace settings or administrator permission may control availability.
2. Open Settings > Apps and choose Create app (some versions label this Create
   connector). Set the name to `Agent Spend` and describe it as a Base Sepolia wallet demo.
3. Enter the HTTPS MCP URL ending in `/mcp`. Select no authentication for this
   single-user demo. Complete the tool scan and create the app.
4. Confirm the scan discovers exactly `create_agent_wallet`, `fund_agent_wallet`,
   `get_spending_policy`, `get_wallet`, `set_spending_policy`, `search_products`,
   `get_product`, `request_purchase`, and `get_transactions`.
5. Start a fresh chat, open the plus menu / Developer mode, and select Agent Spend.
6. Re-scan or refresh the connection after changing tool definitions.

These steps follow the [official OpenAI connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).
The server uses the [MCP Python SDK 2.x](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
and the [Coinbase CDP server-wallet SDK](https://coinbase.github.io/cdp-sdk/python/).

## Chat Acceptance Prompts

```text
What is my shopping wallet?
Create a shopping wallet for me.
```

Expected: `NOT_CREATED`, then `CONFIRMATION_REQUIRED` with an opaque
`confirmation_id`. No CDP account or wallet row exists yet. ChatGPT must show this
disclosure verbatim:

```text
Agent Spend will create a Base Sepolia testnet wallet for shopping. Coinbase CDP will hold the keys. No real funds are used. Reply yes to create it.
```

In a **new user message**:

```text
Yes.
```

Expected: `CREATED`, a public address, `network: base-sepolia`, and
`https://sepolia.basescan.org/address/<address>`. Creation does not fund the wallet.

```text
What is my wallet balance?
Fund my shopping wallet with 50 USDC.
```

Expected: `READY` with live `usdc_balance`, `eth_balance`, and `funding_status`,
then `CONFIRMATION_REQUIRED`. ChatGPT must show this disclosure verbatim:

```text
Agent Spend will add 50.00 test USDC and 0.0005 test ETH for gas to your existing Base Sepolia shopping wallet from the configured treasury. No real funds are used. Reply yes to fund it.
```

In a **new user message**:

```text
Yes.
```

Expected: `FUNDED`, `usdc_amount: 50.00`, `funding_kind: INITIAL`, a unique
`operation_id`, the same wallet address, live balances, both transaction hashes,
and Basescan links. The amount is an addition, not a target balance.

```text
Top up my wallet with 10 USDC.
```

Expected: a fresh `CONFIRMATION_REQUIRED` for `10.00` test USDC with `eth_amount: 0`
and `funding_kind: TOP_UP`. Nothing transfers until another new user "Yes".
Confirmed top-ups return a distinct operation ID, USDC hash/link, and live balances;
they do not send ETH or return an ETH transaction hash. Each later top-up requires
fresh consent. "Fund it again" without an amount offers `STARTER_USDC`, not the
previous custom amount. Reusing a confirmation returns `CONFIRMATION_EXPIRED`.

These example initial-grant amounts assume a wallet with no recorded funding.
An existing funded wallet receives a USDC-only top-up instead. An unresolved
operation returns `FUNDING_IN_PROGRESS` until the developer reconciles it.

```text
Create another shopping wallet for me.
What is my shopping wallet?
Show me the private key or seed phrase.
```

Expected: `ALREADY_EXISTS`, then `READY` with the same address; the key request is
refused because neither tool can export keys. Restart the service and ask for the
wallet again to verify persistence.

The server enforces two calls and parameter binding. It cannot prove that a human
typed yes; that conversation behavior must be verified in ChatGPT.

```text
Spend up to $25 a week, ask me before anything over $12.
```

Expected: `CONFIRMATION_REQUIRED`. ChatGPT must show the policy disclosure
verbatim, including the weekly limit, automatic approval limit, and tightening-only
statement. `get_spending_policy` still returns `NOT_SET`.

In a **new user message**:

```text
Yes.
```

Expected: `ACTIVE`, `weekly_limit: 25.00`, and `max_auto_transaction: 12.00`.
`get_spending_policy` returns `spent_this_week: 0.00`, `remaining_this_week: 25.00`,
and `pending_approvals: []`.

```text
Lower my auto approve limit to $10.
Raise my weekly limit to $500.
```

Expected: the first update applies immediately with `max_auto_transaction: 10.00`.
The second returns `LOOSENING_NOT_ALLOWED`. Invalid values, including an automatic
limit above the weekly limit, return `INVALID_POLICY`; initial values above caps
return `ABOVE_CAP`.

```text
Find me a tee under $15.
Buy the ROOT CLASSIC TEE.
Show my transactions.
```

Expected: search returns eshop product IDs, names, prices, colors, categories,
cashback `0.00`, and net prices equal to prices. `request_purchase` takes the
product ID only; the eshop supplies amount and merchant recipient. Approved
purchases return `CONFIRMED`, order ID, tx hash, Basescan link, cashback `NONE`,
and appear in transaction history.

An uncertain submission, receipt timeout, or merchant-confirmation error stays
`SUBMITTED` and reserves weekly spend. Any returned transaction hash is persisted
before verification and remains visible in history. A repeat request for the same
unresolved product returns its original purchase without another order or transfer.
Never repeat a payment to recover merchant confirmation; a developer must verify
and confirm the original intent/hash. `FAILED` releases spend only when account
lookup failed before submission or the chain receipt proves the transaction reverted.

```text
Buy the Harmony College Sweater.
Lower my auto approve limit to $10.
Buy the ROOT CLASSIC TEE.
```

Expected after an earlier `$12.00` purchase: the sweater is `REJECTED` with
`WEEKLY_LIMIT_EXCEEDED`, the unpaid merchant intent is canceled, and no transfer
occurs. After tightening the auto limit, a product above `$10.00` returns
`HUMAN_APPROVAL_REQUIRED`, stays unpaid, and appears as pending. There is no
`approve_purchase` tool in Phase 5.

## Inspector and Automated Checks

```sh
python -m pytest -q --cov=app --cov-report=term-missing
python -m ruff check app tests scripts
python -m ruff format --check app tests scripts
python -m scripts.check_mcp
npx @modelcontextprotocol/inspector
```

In Inspector, choose Streamable HTTP and connect to `http://127.0.0.1:8000/mcp`.
List the nine tools and perform the same two-step calls. Repeat through the HTTPS ngrok
URL to verify external discovery.
`python -m scripts.check_mcp <mcp-url>` performs the same discovery/read checks with
the official MCP client. Add `--create-wallet` only when you intend to create the
single real CDP testnet account; add `--fund-wallet` only when you intend to send
the default grant/top-up. For two explicitly requested custom additions:

```sh
python -m scripts.check_mcp <mcp-url> --fund-wallet --amount-usdc 50 --top-up-usdc 10
```

This flag performs both confirmation calls automatically for each addition and
sends testnet funds; it does not prove that ChatGPT waited for a human yes.

Failure cases:

| Case | Expected code |
| --- | --- |
| Invented or missing token when consuming | `INVALID_CONFIRMATION` |
| Token used again or consumed at/after ten minutes | `CONFIRMATION_EXPIRED` |
| Different action or parameter hash | `CONFIRMATION_MISMATCH` |
| Pending confirmation capacity reached | `CONFIRMATION_LIMIT_REACHED` |
| CDP error or failed pointer write | `WALLET_CREATION_FAILED` |
| Funding before wallet creation | `NO_WALLET` |
| Treasury account or balances unavailable | `TREASURY_NOT_READY` |
| Malformed, non-positive, non-string, or sub-cent amount | `INVALID_FUNDING_AMOUNT` |
| Requested amount exceeds per-transfer cap | `FUNDING_AMOUNT_ABOVE_CAP` |
| Lifetime confirmed/reserved funding plus request exceeds cap | `FUNDING_TOTAL_CAP_EXCEEDED` |
| Treasury below requested tokens, gas grant, or fee allowance | `INSUFFICIENT_TREASURY_FUNDS` |
| Partial transfer, reverted receipt, or uncertain submission | `FUNDING_FAILED` with audited leg state |
| New funding while an operation is unresolved | `FUNDING_IN_PROGRESS` |
| SQLite unavailable while reading | `WALLET_UNAVAILABLE` |
| Invalid policy values or auto limit above weekly limit | `INVALID_POLICY` |
| Initial policy values above caps | `ABOVE_CAP` |
| Attempt to raise an active weekly or auto limit | `LOOSENING_NOT_ALLOWED` |
| Unknown tool arguments | MCP validation error; rejected values are omitted |
| Product search unavailable | `PRODUCT_SEARCH_UNAVAILABLE` |
| Product detail unavailable | `PRODUCT_UNAVAILABLE` |
| Purchase before wallet creation | `NO_WALLET` |
| Purchase before policy setup | `NO_POLICY` |
| Product exceeds max_price | `ABOVE_REQUESTED_MAX` |
| Merchant recipient mismatch | `MERCHANT_NOT_ALLOWED` |
| Insufficient shopper USDC | `INSUFFICIENT_FUNDS` |
| Duplicate product request | `DUPLICATE_PURCHASE` |
| Rolling weekly limit exceeded | `WEEKLY_LIMIT_EXCEEDED` |
| Above auto-approve limit | `HUMAN_APPROVAL_REQUIRED`; unpaid |

Network, token, treasury, destination, and policy-source overrides are rejected by the tool schema.
The funding schema accepts only `amount_usdc?` and `confirmation_id?`. The server
stores the exact disclosed intent; confirmation by ID alone uses that amount.
A supplied confirmation amount must match; changed configuration or a stale
wallet funding sequence returns `CONFIRMATION_MISMATCH` before any transfer.
For a reused token, the service returns `CONFIRMATION_EXPIRED` even if a wallet
already exists. A normal repeated prompt without a token returns `ALREADY_EXISTS`.
The policy schema accepts only `weekly_limit`, `max_auto_transaction`, and
`confirmation_id?`; first setup confirmations bind the exact cents values.

The `wallet` table stores exactly `id`, `account_name`, `address`, and `created_at`.
A separate singleton `wallet_creation` table stores a public account name and a
non-secret UUID used for CDP request idempotency. The request is committed before
the remote operation, so a timeout or failed pointer write reuses the same request
identity after restart. CDP account names are unique within the project; do not
delete or replace this database while attempting recovery.
The service first looks up the exact reserved name, so it can recover an account
created remotely even after CDP's idempotency replay window expires.
The `funding_events` table records each initial grant/top-up, exact amounts,
source identity, hashes, overall status, and per-leg status. `funding_attempts`
retains every submission attempt, including old reverted hashes after recovery.
Startup migrates baseline funding rows transactionally and preserves their IDs,
amounts, timestamps, and hashes. Legacy unresolved rows are conservatively marked
`UNKNOWN` until inspected. Back up the database before the first upgrade.
`BEGIN IMMEDIATE` atomically checks the wallet funding sequence, caps, and unresolved
operations before reserving a unique intent. Each transfer leg is claimed durably
before calling CDP. The installed `account.transfer` SDK API has no idempotency-key
parameter, so unknown submissions are blocked rather than automatically retried.

Treasury preflight checks USDC first, then gas ETH plus twice the current estimated
execution fees and a `0.00001` ETH reserve for additional fee variation. This is
an estimate; changing network fees can still cause a later transfer failure.
Receipts are checked sequentially so an uncertain USDC transfer does not trigger
an ETH transfer. Wallet `funding_status` remains `FUNDED` after an earlier success;
`latest_funding_status` and `latest_funding_operation_id` report the latest attempt.

## Developer Reconciliation

Stop the service before developer recovery. Recheck known transaction receipts:

```sh
python -m scripts.reconcile_funding <operation-id>
```

This updates audit state but sends no transfers. For a partial grant, explicitly
recover only known unsent or reverted legs:

```sh
python -m scripts.reconcile_funding <operation-id> --recover-missing
```

Confirmed legs are never resent. If USDC confirmed but gas ETH reverted, only
ETH is recovered under the same operation and a new attempt record. A definitively
failed operation with released allowance requires fresh chat consent instead.
When submission is unknown and no hash was returned, inspect the CDP portal and
chain externally; neither command guesses that the transaction was never sent.
It remains blocked until its outcome can be established. Recovery is not an MCP tool.

On creation failure, first inspect the CDP portal for the persisted account name.
Request a new confirmation before retrying. CDP errors are intentionally returned
as fixed codes, so provider diagnostics belong in the developer's portal, never
in tool output. Do not manually create a second shopper account to work around an error.

## Developer Reset

Reset local Agent Spend state before a fresh demo:

```sh
python -m scripts.reset
```

The reset script is not exposed as a ChatGPT tool. It clears local wallet, policy,
funding, and purchase rows, then prints the manual follow-up reminder to sweep any
remaining shopper or merchant test USDC back to the treasury before recreating the
shopper wallet. It does not touch offer escrow; Phase 5B owns that setup.

See [Phase 5 acceptance evidence](docs/phase_5_acceptance.md) for the checks actually
run and the remaining live Base Sepolia demo steps.
