import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

# Data Streaming API

The Data Streaming API pushes real-time market data using the [MQTT](https://mqtt.org/) protocol ([v3.1.1](http://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)) over TCP/IP or WebSocket. Use it to receive live quotes, snapshots, and tick data as they happen.

For on-demand HTTP queries, see [Data API](data-api.md).

:::tip SDK Users
The Webull SDK handles MQTT connection, authentication, and message parsing automatically. See the [real-time streaming example](getting-started.md#step-3-subscribe-to-real-time-quotes) in Getting Started. The steps below are for manual integration without the SDK.
:::

## Supported Data

| Market | Categories |
|--------|------------|
| United States | Stocks, ETFs, Futures, Crypto, Event Contracts |

| Data Type | Description |
|-----------|-------------|
| QUOTE | Real-time order book |
| SNAPSHOT | Market snapshot |
| TICK | Tick-by-tick transaction details |

## Step 1: Establish an MQTT Connection

### Connection Endpoints

| Environment | Protocol | Endpoint |
|-------------|----------|----------|
| Production | TCP/IP | `data-api.webull.com:1883` |
| Production | WebSocket | `wss://data-api.webull.com:8883/mqtt` |

### MQTT Client Libraries

- [Python](https://github.com/eclipse/paho.mqtt.python)
- [Java](https://github.com/eclipse-paho/paho.mqtt.java)
- [JavaScript](http://github.com/eclipse/paho.mqtt.javascript)
- [Golang](https://github.com/eclipse-paho/paho.mqtt.golang)
- [More languages](https://mqtt.org/software/)

### CONNECT Packet Fields

| Field | Value |
|-------|-------|
| ClientId | A unique `session_id` you create (also used for subscribe/unsubscribe calls) |
| User Name | Your `App Key` |
| Password | Any value |

:::caution Connection Rules
- Do not reuse the same `session_id` across multiple connections under one App Key. A new connection with the same `session_id` will disconnect the previous one.
- Each App Key supports a maximum of 5 concurrent connections. Exceeding this returns error code `105`.
- After disconnecting, the server retains connection state for about 1 minute. If you've reached 5 connections, wait 1 minute before reconnecting.
- The server pushes messages at a maximum rate of 3 times per second per connection.
  :::

### Connection Error Codes

| Code | Description |
|------|-------------|
| 0 | Connection accepted |
| 1 | Unacceptable protocol version |
| 2 | Invalid ClientId |
| 3 | App Key is empty |
| 7 | Connection lost |
| 16 | Heartbeat timeout |
| 100 | Unknown error |
| 101 | Internal error |
| 102 | Connection already authenticated |
| 103 | Authentication failed |
| 104 | Invalid App Key |
| 105 | Exceeds connection limit |

## Step 2: Subscribe to Market Data

After establishing the MQTT connection, use the HTTP API to manage subscriptions:

- [Subscribe](../reference/subscribe.api.mdx) — Start receiving real-time data for specified symbols
- [Unsubscribe](../reference/unsubscribe.api.mdx) — Stop receiving data for specified symbols

:::caution
If the connection is dropped due to network issues, previous subscriptions are not automatically restored. You must re-subscribe after reconnecting.
:::

## Step 3: Parse Incoming Messages

Each message pushed from the server contains:

- **Topic** — Identifies the data type
- **Payload** — The actual data, serialized using [Protocol Buffers](https://protobuf.dev/) or JSON

### Topic-to-Payload Mapping

#### Supports Stocks, Futures and Crypto.

| Data Type | Topic | Payload Format | Description |
|-----------|-------|----------------|-------------|
| QUOTE | `quote` | Protobuf | Real-time order book |
| SNAPSHOT | `snapshot` | Protobuf | Market snapshot |
| TICK | `tick` | Protobuf | Tick-by-tick details |
| NOTICE | `notice` | JSON | Server notifications |
| ECHO | `echo` | Null | Online check (heartbeat) |

#### Only Supports Event contract

| Data Type | Topic | Payload Format | Description |
|-----------|-------|----------------|-------------|
| QUOTE | `event-quote` | Protobuf | Event contract order book |
| SNAPSHOT | `event-snapshot` | Protobuf | Event contract snapshot |
| TICK | `event-tick` | Protobuf | Event contract tick |
| NOTICE | `notice` | JSON | Server notifications |
| ECHO | `echo` | Null | Online check (heartbeat) |

## Protobuf Message Definitions

### Basic (shared by all types)

```protobuf
message Basic {
    string symbol = 1;
    string instrument_id = 2;
    string timestamp = 3;
}
```

### Quote (Real-time Order Book)

```protobuf
message Quote {
    Basic basic = 1;
    repeated AskBid asks = 2;
    repeated AskBid bids = 3;
}

message AskBid {
    string price = 1;
    string size = 2;
    repeated Order order = 3;
    repeated Broker broker = 4;
}

message Order {
    string mpid = 1;
    string size = 2;
}

message Broker {
    string bid = 1;
    string name = 2;
}
```

### Snapshot (Market Snapshot)

```protobuf
message Snapshot {
    Basic basic = 1;
    string trade_time = 2;
    string price = 3;
    string open = 4;
    string high = 5;
    string low = 6;
    string pre_close = 7;
    string volume = 8;
    string change = 9;
    string change_ratio = 10;
    string ext_trade_time = 11;
    string ext_price = 12;
    string ext_high = 13;
    string ext_low = 14;
    string ext_volume = 15;
    string ext_change = 16;
    string ext_change_ratio = 17;
    string ovn_trade_time = 18;
    string ovn_price = 19;
    string ovn_high = 20;
    string ovn_low = 21;
    string ovn_volume = 22;
    string ovn_change = 23;
    string ovn_change_ratio = 24;
}
```

### Tick (Tick-by-Tick Detail)

```protobuf
message Tick {
    Basic basic = 1;
    string time = 2;
    string price = 3;
    string volume = 4;
    string side = 5;
}
```

### Event Quote

```protobuf
message EventQuote {
    Basic basic = 1;
    repeated EventAskBid yes_bids = 2;
    repeated EventAskBid no_bids = 3;
}

message EventAskBid {
    string price = 1;
    string size = 2;
}
```

### Event Snapshot

```protobuf
message EventSnapshot {
    Basic basic = 1;
    string price = 2;
    string volume = 3;
    string last_trade_time = 4;
    string open_interest = 5;
    string yes_ask = 6;
    string yes_bid = 7;
    string yes_ask_size = 8;
    string yes_bid_size = 9;
    string no_ask = 10;
    string no_bid = 11;
    string no_ask_size = 12;
    string no_bid_size = 13;
}
```

### Event Tick

```protobuf
message EventTick {
    Basic basic = 1;
    string yes_price = 2;
    string no_price = 3;
    string volume = 4;
    string side = 5;
    string trade_id = 6;
    string time = 7;
}
```

### Notification (JSON)

```json
{
  "type": "status",
  "rtt": 100,
  "drop": 0,
  "sent": 0
}
```

## What's Next

Once your MQTT connection is live and subscriptions are active, you'll receive real-time market data as it happens. For a complete working example using the SDK, check the [Market Data API Getting Started](getting-started.md) guide.

If you run into issues with connections or data delivery, see [Additional Resources](../resources.md) for support channels.

