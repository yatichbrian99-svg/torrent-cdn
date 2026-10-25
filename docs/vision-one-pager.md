# TorrentCDN — Vision One-Pager

**Source:** the owner's one-page statement of intent, written 2026-10-25. Recorded here with **wording unchanged**, for the same reason as `docs/vision.md`: it is the canonical short statement of what TorrentCDN is for, and the longer north star should be read against a fixed text rather than a paraphrase.

Where the two differ, this page is the **outside-facing** summary and `docs/vision.md` is the **inside-facing** long plan. Neither overrides the other; if a claim here is not yet true, the fix is to build it or to soften the claim, not to edit this text.

---

## The idea

**A borderless peer-to-peer network for compute and delivery.** Anyone with an idle GPU or a spare connection can earn from it, from any country. Any developer can use it, from any country, for less.

Two products on one network:

1. **TCDN-AI: inference and compute on idle GPUs.** An OpenAI-compatible API for open models, plus speech-to-text, embeddings, image generation and transcoding. Pay as you go, in your own currency.
2. **TCDN Pull: a model and file delivery network.** Fast, integrity-checked, peer-to-peer distribution of large files such as AI model weights. Works with Ollama and vLLM, and without any inference at all.

## The problem

- AI tokens are too expensive for most of the world's developers and vibe coders, and many cannot even pay for US-based services without an international card.
- Billions of dollars of GPU capacity sit idle in gaming PCs, labs, cafes and former mining rigs, earning nothing.
- Moving multi-gigabyte model files out of centralized clouds is slow and costs real money in egress fees.

## Why we win

- **Peer-to-peer delivery is our native strength.** We started as a CDN. Cold starts in seconds and near-zero egress cost are our home turf.
- **Two-sided flywheel:** more hosts earning lowers prices, lower prices bring more developers, and more developers bring more earnings for hosts.
- **Follow-the-sun capacity:** GPUs idle at night in one timezone while demand peaks in another.
- **Local payments, global reach:** M-Pesa and mobile money where cards fail, cards everywhere else, one wallet of credits worldwide.
- **Trust built in:** signed receipts, spot-check verification with graduated penalties, honest privacy tiers, and a confidential tier on attested hardware for regulated customers.
- **Regions as a feature:** customers choose where their data runs (EU, North America, Africa, Asia).

## Who it serves

- **Vibe coders and indie developers** who need cheap tokens and rate limits that don't get in the way, from Cursor, Cline, Continue or OpenCode.
- **Media and streaming companies** that need captions, translation and transcoding on the same network that delivers their video.
- **AI teams** that ship large models and want faster, cheaper distribution.
- **GPU owners** who want a dependable side income.

## How it spreads

- Be a one-line provider in the tools developers already use.
- Open-source the worker agent so a global community of hosts can form.
- Publish live public numbers: price, tokens per second, cold-start time and uptime by region, all measured by us.
- Launch with a single headline demo: load a large model in seconds, peer to peer.

## The path

| Stage | Focus |
|---|---|
| **Now (0-3 months)** | OpenAI-compatible endpoint, a few reliable GPUs, a daily pass paid by M-Pesa or card, 20 to 50 early developers, first media customer from the Enveu relationship |
| **Next (3-9 months)** | Open to outside hosts, transcription and embeddings, TCDN Pull for model delivery, regional pools, first NGO or media contracts |
| **Then (year 2)** | More payment rails and payout countries, spot and reserved capacity, confidential tier on real hardware, open-source worker agent |
| **Long term** | The default low-cost compute and delivery network for developers everywhere |

## Principles

- Honest claims only: what we say works has been tested, and anything simulated is labelled.
- Customers first: build what paying users ask for.
- Self-funded and lean: simple infrastructure, cheap to run, profitable by design.
- Privacy by default: no prompts stored, and clear labels for which tier protects what.
