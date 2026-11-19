> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Environments

> The host for each environment, and how keys work across them.

Every example on this site reads the host from `NOVIG_HOST`. Set it once:

```bash theme={"dark"}
export NOVIG_HOST=https://api.paper.novig.com
```

| Environment | Host | Money | Location check |
| - | - | - | - |
| Paper | `https://api.paper.novig.com` | Play | No |
| Production | `https://api.novig.com` | Real | Yes |

Paper is our play-money exchange. It runs the same API as Production, without the location check.

In both environments, you create a key on Profile.

In Paper, use [Profile](https://paper.novig.com/Profile/ProfileScreen) on paper.novig.com. In Production, use [Profile](https://novig.com/Profile/ProfileScreen) on novig.com.

The websocket uses the same host as REST. On Paper, that's `wss://api.paper.novig.com`.

## Set up a Paper account

Paper has its own web app at [https://paper.novig.com](https://paper.novig.com). Every account starts with \$10,000 in play money, so you can trade without risk.

Paper skips the identity check. It fills in a simulated profile for you, so you never enter your real details.

**1. Sign up**

Open [paper.novig.com](https://paper.novig.com) and sign up.

**2. Set up your account**

Press **Fill in KYC Data, Accept all Terms, and Start with \$10,000**.

**3. Create a key**

Open [Profile](https://paper.novig.com/Profile/ProfileScreen), then press **Settings** and **Novig API**.

Production runs a real identity check and a location check, and trades real money.

## Each environment has its own keys

A key works only in the environment where you created it.
Production rejects a Paper key with `api key not found`.
Store each environment's keypair in its own secret.

## Switch environments

1. Point `NOVIG_HOST` at the new host.
2. Create a keypair in that environment.
3. Re-run the [Quickstart](https://docs.novig.com/api/quickstart.md) to test your signature.

## Connect over AWS PrivateLink

High-volume traders on Production can reach a dedicated trading gateway over AWS PrivateLink.
Your orders then travel inside AWS to a gateway that serves only you, not across the public internet.

To qualify, your account needs at least \$100,000,000 in monthly order-book volume. RFQ volume doesn't count.
We count volume as on the [Throttling](https://docs.novig.com/api/throttling.md#tiers) page. Spots are limited, and we grant each one at our discretion.

The gateway runs in AWS `us-east-1`. You create an interface VPC endpoint in your own AWS account to reach it.

To request access, email [developers@novig.com](mailto:developers@novig.com) with your AWS account ID.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.