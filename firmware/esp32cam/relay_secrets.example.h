#pragma once
// Copy to relay_secrets.h locally (init_device.py does this for a new checkout).
// LAN-only by default. Enable only after configuring YOUR server and CA.
#define KVIDO_RELAY_ENABLED 0
#define KVIDO_PUBLIC_URL "https://rover.example.com/kvido/"
#define KVIDO_RELAY_URL "wss://rover.example.com/kvido/device-ws"
// Must equal your Python server's private data/relay.key when relay is enabled.
#define KVIDO_RELAY_TOKEN "REPLACE_WITH_YOUR_SERVER_RELAY_KEY"
// Paste the PEM of the trusted public root CA for YOUR server certificate.
// Do not disable TLS verification. This empty value is for disabled relay only.
#define KVIDO_RELAY_ROOT_CA ""
