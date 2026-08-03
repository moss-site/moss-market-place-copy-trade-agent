# moss market place copy trade agent

A Hyperliquid copy-trading service for Moss Agents, with a local CLI and Skill instructions for agent clients such as OpenClaw and Codex.

The service reads position and fill changes from a selected Moss Agent and mirrors them to a user's Hyperliquid account using a startup baseline and subsequent position deltas. It supports isolated multi-instance deployments, mandatory risk confirmation, stop-loss and take-profit controls, balance alerts, operational dashboards, automatic restarts, and managed updates.

> [!WARNING]
> This project can execute real trades. Start on testnet whenever possible, and verify the network, main account, Agent Wallet, follow ratio, stop-loss, take-profit, and slippage settings before starting the service. No strategy can guarantee a profit.

## Features

- Monitors Moss Agent fills and position changes and performs proportional Hyperliquid delta alignment
- Supports both standard EOA Agent Wallets and contract-agent executors
- Isolates configuration, SQLite data, logs, and PID files for each wallet instance
- Requires explicit confirmation of the follow ratio, stop-loss, and take-profit before startup
- Refreshes the Hyperliquid supported-coin universe dynamically
- Clamps reductions at zero to prevent an oversized close from opening a reverse position
- Provides trade history, statistics, balance snapshots, alerts, and an Agent dashboard
- Supports watchdog services through macOS `launchd` and Linux user `systemd`
- Supports update checks, backup-based upgrades, and rollback
- Optionally reports trade results to Agent Protocol

## How It Works

1. At startup, the service reads the selected Moss Agent's current positions and stores them as a local baseline.
2. When a later fill or position change arrives, it calculates the delta relative to that baseline.
3. It scales the change using `follow_ratio` and submits an order through the Hyperliquid Agent Wallet.
4. Direction clamping prevents a reduction from crossing zero and opening a position in the opposite direction.
5. Trades, baselines, balance snapshots, alerts, and report states are stored in an instance-specific SQLite database.

`allowed_coins` is retained only for compatibility with older configurations. The effective trading universe is determined by the Hyperliquid universe cache refreshed by the service.

## Requirements

- Python 3.10 or later
- macOS or Linux
- Network access to the Moss and Hyperliquid APIs
- An Agent Wallet for order signing, or an executor already authorized by a contract-agent

Install the dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Display the CLI help:

```bash
.venv/bin/python cli.py
```

## Quick Start: Standard Agent Wallet

### 1. Generate a wallet and instance configuration

```bash
.venv/bin/python cli.py config wallet-generate
```

The command generates a wallet from `config_default.json` and creates the following instance layout:

```text
~/.hyperliquid-copy-trade/<last-6-wallet-characters>/
|-- config_<last-6-wallet-characters>.json
|-- logs/
|-- follow_agent.db
`-- service.pid
```

The terminal output includes the Agent Wallet address, authorization page, and configuration path. The private key is stored in the configuration file, whose permissions are set to `0600` where supported.

> [!IMPORTANT]
> The current `config_default.json` is a mainnet template. Use `config_default.testnet.json` for testnet. Always inspect `hl_api_url` and `hl_authorize_url` before using real funds.

All subsequent commands must identify an instance configuration explicitly. The examples below use an environment variable for convenience:

```bash
export FOLLOW_CONFIG="$HOME/.hyperliquid-copy-trade/<last-6>/config_<last-6>.json"
```

You can also pass the path directly with `--config`:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config show
```

Do not run the service with `config.json` or `config_default.json`. The CLI accepts only instance files named `config_<6-hex-characters>.json`.

### 2. Complete Hyperliquid authorization

Open the authorization URL printed by `wallet-generate` with the main wallet, then complete the Agent and Builder authorization steps for the generated Agent Wallet. Funds and positions remain associated with the main account; the Agent Wallet signs orders on its behalf.

Set the main account and verify authorization:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config set main_address 0xYOUR_MAIN_ADDRESS
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config check-auth
```

### 3. Select a Moss Agent

Mainnet Agent list: <https://moss.site/agent?mode=realtime>

Testnet Agent list: <https://alpha.moss.site/agent?mode=realtime>

Set the selected Agent ID and register the follower:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config set moss_source.agent_id agt_xxx
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" moss register
```

### 4. Confirm risk parameters

The service will not start until all risk parameters have been explicitly confirmed. This example uses a 50% follow ratio, 20% stop-loss, and 30% take-profit:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config confirm-risk \
  --follow-ratio 50% \
  --stop-loss 20 \
  --take-profit 30
```

- Follow ratio range: `0%` to `100%`
- Stop-loss range: `0%` to `100%`; use `0` to disable it
- Take-profit range: `0%` to `300%`; use `0` to disable it
- Stop-loss and take-profit are based on margin PnL percentage, not the underlying asset's price change
- Checks run on a polling interval rather than every market tick, so fast markets can exceed the configured threshold

After changing `moss_source.agent_id`, `follow_ratio`, `stop_loss_pct`, or `take_profit_pct`, run `config confirm-risk` again.

### 5. Start and inspect the service

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service start
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service status
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" dashboard
```

## Contract-agent Executor Mode

If an executor has already been authorized through a contract-agent or Agent Protocol contract, create an instance without generating a new wallet:

```bash
.venv/bin/python cli.py config init-contract-agent \
  --executor 0xEXECUTOR_ADDRESS \
  --main 0xAGENT_CONTRACT_OR_MASTER_ACCOUNT \
  --moss-agent agt_xxx \
  --network mainnet \
  --follow-ratio 50% \
  --stop-loss 20 \
  --take-profit 30
```

This command creates an instance configuration but deliberately leaves `private_key` empty. Enter the executor's private key only in the local configuration file. Never send it through chat, an issue, or a log.

Before starting, verify that:

- The address derived from the executor private key matches `wallet_address`
- The main account has authorized the executor through contract-agent or CoreWriter `addApiWallet`
- Hyperliquid `userRole` or `extraAgents` confirms that the authorization has propagated
- `main_address`, the network, the Moss Agent, and all risk parameters are correct

After entering the private key, run:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config check-auth
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service start
```

## Common Commands

| Action | Command |
| --- | --- |
| Show configuration | `python cli.py --config "$FOLLOW_CONFIG" config show` |
| Change a setting | `python cli.py --config "$FOLLOW_CONFIG" config set <key> <value>` |
| Check authorization | `python cli.py --config "$FOLLOW_CONFIG" config check-auth` |
| Start the service | `python cli.py --config "$FOLLOW_CONFIG" service start` |
| Show service status | `python cli.py --config "$FOLLOW_CONFIG" service status` |
| Stop the service | `python cli.py --config "$FOLLOW_CONFIG" service stop` |
| Pause and close positions | `python cli.py --config "$FOLLOW_CONFIG" service pause` |
| Resume and rebuild baseline | `python cli.py --config "$FOLLOW_CONFIG" service resume` |
| Switch Agent | `python cli.py --config "$FOLLOW_CONFIG" service switch` |
| Show the full dashboard | `python cli.py --config "$FOLLOW_CONFIG" dashboard` |
| Show recent trades | `python cli.py --config "$FOLLOW_CONFIG" trades --limit 20` |
| Show statistics | `python cli.py --config "$FOLLOW_CONFIG" stats` |
| Show balance snapshots | `python cli.py --config "$FOLLOW_CONFIG" balance --limit 20` |
| Show unread alerts | `python cli.py --config "$FOLLOW_CONFIG" alerts list --unread` |
| Show the baseline | `python cli.py --config "$FOLLOW_CONFIG" baseline show` |
| Reset the baseline | `python cli.py --config "$FOLLOW_CONFIG" baseline reset` |

`service stop` stops the process without closing positions. `service pause` attempts to close every position, stops the service, and clears the baseline. It is a high-impact operation; verify the account and network before running it.

## Watchdog

Install an instance-specific watchdog that performs a health check every 60 seconds:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog install --interval 60
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog status
```

Other watchdog operations:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog check
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog disable
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog enable
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service watchdog uninstall
```

The watchdog uses `launchd` on macOS and user `systemd` on Linux. It respects the instance's `desired_state`, so an intentionally stopped or paused service is not restarted automatically.

## Updates and Rollback

Check the installed and available versions:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" update status
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" update check
```

Review the version and changelog before applying an update:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" update apply --yes
```

The update process creates a backup and may stop a running service. After an update, the service remains stopped and its risk parameters must be confirmed again before startup. Inspect the rollback candidate first, then add `--yes` to restore it:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" update rollback
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" update rollback --yes
```

## Key Configuration

| Field | Description | Default |
| --- | --- | --- |
| `main_address` | Hyperliquid main account that owns the funds and positions | Empty |
| `wallet_address` | Agent Wallet or executor used to sign orders | Empty |
| `moss_source.agent_id` | Moss Agent ID to follow | Empty |
| `follow_ratio` | Follow ratio; `1.0` means 100% | `1.0` |
| `slippage_percent` | Allowed slippage percentage for IOC limit orders | `1.5` |
| `alignment_loss_pct` | Position-alignment loss threshold | `3.0` |
| `stop_loss_pct` | Margin loss percentage that triggers stop-loss; `0` disables it | `0` |
| `take_profit_pct` | Margin profit percentage that triggers take-profit; `0` disables it | `0` |
| `low_balance_threshold_usd` | Withdrawable-balance alert threshold | `10.0` |
| `hyperliquid_auth.mode` | `standard` or `contract_agent` | `standard` |
| `agent_protocol_report.enabled` | Enables Agent Protocol reporting | `false` |

The explicit network templates are:

- `config_default.mainnet.json`
- `config_default.testnet.json`

## Logs and Troubleshooting

The default instance directory is `~/.hyperliquid-copy-trade/<last-6>/`. Common files include:

- `logs/service.log`: application events
- `logs/stdout.log`: background-process standard output
- `logs/stderr.log`: background-process error output
- `follow_agent.db`: baselines, trades, balance snapshots, and alerts
- `service_state.json`: desired service state and watchdog state
- `service.pid`: background service PID

Useful diagnostic commands:

```bash
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" service status
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" config check-auth
.venv/bin/python cli.py --config "$FOLLOW_CONFIG" dashboard
tail -n 100 ~/.hyperliquid-copy-trade/<last-6>/logs/stderr.log
```

If startup fails, first verify the configured network, confirm that the private key matches `wallet_address`, check Agent and Builder authorization, validate `moss_source.agent_id`, and confirm that the machine can reach the Moss and Hyperliquid APIs.

## Project Structure

```text
.
|-- cli.py                         # CLI entry point
|-- follow_service/                # Trading, storage, alerts, and watchdog code
|-- SKILL.md                       # Agent interaction flow and safety constraints
|-- config_default.json            # Current default configuration template
|-- config_default.mainnet.json    # Mainnet template
|-- config_default.testnet.json    # Testnet template
|-- VERSION.json                   # Update version and package URL
`-- requirements.txt               # Python dependencies
```

## Security

- Never commit, screenshot, paste, or send `private_key` through chat
- Never add instance configurations from `~/.hyperliquid-copy-trade/` to version control
- Use a dedicated Agent Wallet key with only the permissions needed for copy trading
- Deposit funds into the main wallet's Hyperliquid account, not directly into the Agent Wallet
- Validate authorization, opening, closing, pausing, and resuming with a small amount before enabling mainnet trading
- `service pause` submits closing orders; network congestion, slippage, or insufficient liquidity can prevent complete execution

For the complete agent conversation rules, authorization gates, and operational workflow, see [`SKILL.md`](SKILL.md).
